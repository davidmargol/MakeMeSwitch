"""Run ONLY via sudo unshare --net --mount --pid --fork --mount-proc.
Real kernel + private OVS DB/daemon. No host NIC/udev or OVS socket writes.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
if not os.environ.get('MMS_TEST_INSTALLED'):
    sys.path.insert(0, str(ROOT))


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def worker():
    from makemeswitch.core.engine import Engine
    from makemeswitch.nic_names import apply_engine
    engine = Engine()
    apply_engine(engine)
    engine.boot(apply=True)
    rows = engine.port_table()
    assert all(r['linux_if'] == r['name'] and r['present'] for r in rows)
    assert set(engine.ovs.list_ports()) == {r['name'] for r in rows}
    for row in rows:
        link = json.loads(run('ip', '-j', 'link', 'show', row['name']))[0]
        assert 'UP' in link['flags']
    print(f'BOOT VERIFIED: {len(rows)} ports named, attached and admin-UP')


def main():
    if '--worker' in sys.argv:
        worker()
        return
    # Fresh netns must contain ONLY loopback before we do anything.
    assert [x['ifname'] for x in json.loads(run('ip', '-j', 'link'))] == ['lo']
    run('mount', '--make-rprivate', '/')
    run('mount', '-t', 'sysfs', 'sysfs', '/sys')
    with tempfile.TemporaryDirectory(prefix='mms-nic-kernel-') as temp:
        base = Path(temp)
        os.environ['MMS_DB'] = str(base / 'state.db')
        os.environ['MMS_NAMING_LOCK'] = str(base / 'naming.lock')
        os.environ['MMS_CONFIG'] = str(base / 'config.yaml')
        os.environ['OVS_RUNDIR'] = temp
        (base / 'config.yaml').write_text('bridge: mms-test\nattach_new_nics: true\n')
        bindir = base / 'bin'
        bindir.mkdir()
        wrapper = bindir / 'ovs-vsctl'
        wrapper.write_text(f'#!/bin/sh\nexec /usr/bin/ovs-vsctl --db=unix:{temp}/db.sock "$@"\n')
        wrapper.chmod(0o755)
        os.environ['PATH'] = str(bindir) + ':/usr/sbin:' + os.environ['PATH']
        run('/usr/bin/ovsdb-tool', 'create', str(base / 'ovs.db'), '/usr/share/openvswitch/vswitch.ovsschema')
        procs = []
        handles = []
        try:
            for name, cmd in [
                ('db', ['/usr/sbin/ovsdb-server', str(base / 'ovs.db'), f'--remote=punix:{temp}/db.sock', f'--unixctl={temp}/db.ctl', f'--pidfile={temp}/db.pid']),
                ('switch', ['/usr/sbin/ovs-vswitchd', f'unix:{temp}/db.sock', f'--unixctl={temp}/switch.ctl', f'--pidfile={temp}/switch.pid']),
            ]:
                log = open(base / f'{name}.log', 'w+')
                handles.append(log)
                procs.append(subprocess.Popen(cmd, stdout=log, stderr=log))
                if name == 'db':
                    for _ in range(100):
                        if (base / 'db.sock').exists():
                            break
                        if procs[-1].poll() is not None:
                            raise RuntimeError('OVS DB exited')
                        time.sleep(.05)
                    run('ovs-vsctl', '--no-wait', 'init')
            run('ovs-vsctl', '--timeout=10', 'add-br', 'mms-test')
            run('ip', 'link', 'add', 'eth0', 'type', 'dummy')
            run('ip', 'link', 'set', 'eth0', 'address', '02:ff:00:00:00:01')
            run('ip', 'addr', 'add', '192.0.2.1/24', 'dev', 'eth0')
            run('ip', 'link', 'set', 'eth0', 'up')
            mgmt_before = json.loads(run('ip', '-j', 'addr', 'show', 'eth0'))
            previous = 0
            for total in [6, 8, 11]:
                for i in range(previous + 1, total + 1):
                    name = f'mmstmp{i}' if total == 8 else f'enp{i}s0'
                    run('ip', 'link', 'add', name, 'type', 'dummy')
                    run('ip', 'link', 'set', name, 'address', f'02:00:00:00:00:{i:02x}')
                for repeat in range(2):
                    print(run(sys.executable, __file__, '--worker'), end='')
                    assert json.loads(run('ip', '-j', 'addr', 'show', 'eth0')) == mgmt_before
                previous = total
            # Real permutation and injected second-phase failure; restore admin UP.
            from makemeswitch import nic_names
            original = nic_names._set_name
            count = 0
            def fail_fourth(a, b):
                nonlocal count
                count += 1
                if count == 4:
                    return subprocess.CompletedProcess([], 1, '', 'injected kernel-harness failure')
                return original(a, b)
            for n in ['Ethernet1', 'Ethernet2']:
                run('ovs-vsctl', 'del-port', 'mms-test', n)
            run('ip', 'addr', 'add', '198.51.100.1/24', 'dev', 'Ethernet1')
            nic_names._set_name = fail_fourth
            try:
                nic_names.rename_live({'02:00:00:00:00:01': 'Ethernet2', '02:00:00:00:00:02': 'Ethernet1'})
                raise AssertionError('expected injected failure')
            except RuntimeError as exc:
                assert 'rollback: complete' in str(exc), str(exc)
            finally:
                nic_names._set_name = original
            assert nic_names._live_name_for_mac('02:00:00:00:00:01') == 'Ethernet1'
            assert '198.51.100.1/24' in nic_names._addresses_of('Ethernet1')
            assert 'UP' in json.loads(run('ip', '-j', 'link', 'show', 'Ethernet1'))[0]['flags']
            assert not any(p.name.startswith('mmstmp') for p in Path('/sys/class/net').iterdir())
            print('KERNEL ROLLBACK VERIFIED: names, IPv4, admin UP; management unchanged')
        except Exception:
            for log in handles:
                log.flush(); log.seek(0)
                print(log.read(), file=sys.stderr)
            raise
        finally:
            for proc in reversed(procs):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
            for log in handles:
                log.close()


if __name__ == '__main__':
    main()
