"""Real PTY Tab smoke with production run_cli and an in-memory OVS backend.

Run with the dev venv (pexpect required). No host networking or discovery.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ['MMS_NO_RENAME'] = '1'


def child(tmp):
    import conftest
    from makemeswitch.core.store import Store
    from makemeswitch.cli import shell
    engine = conftest.engine.__wrapped__(Store(Path(tmp) / 'state.db'), conftest.FakeOvs())
    engine.settings['cli'] = {'history_file': str(Path(tmp) / 'history')}
    recorded = []
    original = shell.execute_line

    def record(session, line):
        recorded.append(line)
        original(session, line)

    shell.execute_line = record
    shell.run_cli(engine=engine, auto_confirm=True)
    (Path(tmp) / 'commands.json').write_text(json.dumps(recorded))
    assert engine.config.interfaces['Ethernet1'].access_vlan == 20
    assert engine.config.interfaces['Ethernet2'].access_vlan == 20


def parent():
    import pexpect
    with tempfile.TemporaryDirectory(prefix='mms-tab-') as tmp:
        proc = pexpect.spawn(sys.executable, [str(Path(__file__).resolve()), '--child', tmp],
                             encoding='utf-8', timeout=15, dimensions=(30, 120),
                             env=dict(os.environ, TERM='xterm', PROMPT_TOOLKIT_NO_CPR='1'))
        proc.logfile = sys.stdout
        proc.expect('TEST-SW>')
        for command, prompt in [('enable', 'TEST-SW#'), ('configure terminal', 'TEST-SW(config)#'),
                                ('interface Ethernet1', 'TEST-SW(config-if)#')]:
            proc.sendline(command)
            proc.expect_exact(prompt)
        for mode in ('if', 'if-range'):
            prompt = f'TEST-SW(config-{mode})#'
            # Two literal Tab keypresses must list actual VLAN IDs.
            proc.send('switchport access vlan \t\t')
            proc.expect(r'1\s+10\s+20')
            proc.sendcontrol('c')
            proc.expect_exact('^C')
            proc.expect_exact(prompt)
            # A unique partial value is completed and executed, not just listed.
            proc.send('switchport access vlan 2\t')
            proc.sendline('')
            proc.expect_exact(('Ethernet1' if mode == 'if' else 'Ethernet2') + ' access VLAN 20.')
            proc.expect_exact(prompt)
            proc.send('switchport mode tr\t')
            proc.sendline('')
            proc.expect_exact(('Ethernet1' if mode == 'if' else 'Ethernet2') + ' is now a trunk switchport.')
            proc.expect_exact(prompt)
            if mode == 'if':
                proc.sendline('interface range Ethernet1-2')
                proc.expect_exact('TEST-SW(config-if-range)#')
        proc.sendcontrol('d')
        proc.expect(pexpect.EOF)
        proc.close()
        assert proc.exitstatus == 0, proc.exitstatus
        commands = json.loads((Path(tmp) / 'commands.json').read_text())
        assert commands.count('switchport access vlan 20') == 2, commands
        assert commands.count('switchport mode trunk') == 2, commands
        print('\nPTY TAB VERIFIED: config-if + config-if-range VLAN menus, VLAN prefix and mode completion')
        print('Accepted commands:', commands)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        child(sys.argv[2])
    else:
        parent()
