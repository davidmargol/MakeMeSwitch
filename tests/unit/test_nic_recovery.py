from makemeswitch.core import interfaces as netif
from makemeswitch.core.engine import Engine
import pytest
from makemeswitch import nic_names
from types import SimpleNamespace
import subprocess
import sys
import os
import time


def test_naming_lock_serializes_processes(tmp_path):
    marker = tmp_path / 'entered'
    code = 'from makemeswitch.nic_names import naming_lock; from pathlib import Path; import sys\nwith naming_lock(): Path(sys.argv[1]).write_text("entered")'
    with nic_names.naming_lock():
        proc = subprocess.Popen([sys.executable, '-c', code, str(marker)])
        try:
            time.sleep(.3)
            assert not marker.exists()
        finally:
            pass
    assert proc.wait(timeout=10) == 0
    assert marker.read_text() == 'entered'


def test_boot_unit_ordering():
    from pathlib import Path
    source = (Path(__file__).parents[2] / 'install.sh').read_text()
    unit = source.split('cat > /etc/systemd/system/makemeswitch-nicnames.service <<UNIT')[1].split('\nUNIT')[0]
    assert 'Before=makemeswitch.service' in unit
    assert 'Before=openvswitch' not in unit
    assert 'Requires=openvswitch-switch.service' in unit
    assert 'WantedBy=multi-user.target' in unit
    assert 'PathChanged=/sys/class/net' not in source



def test_duplicate_mac_rejected():
    ports = [{'name': f'Ethernet{i}', 'linux_if': f'enp{i}s0', 'present': True, 'link': {'mac': '02:00:00:00:00:01'}} for i in [1, 2]]
    with pytest.raises(ValueError, match='Duplicate MAC'):
        nic_names.desired_mapping(ports, set())


@pytest.mark.parametrize('fail_at', [1, 2, 3, 4, None])
def test_permutation_rollback(monkeypatch, fail_at):
    names = {'Ethernet1': 'aa', 'Ethernet2': 'bb'}
    original = names.copy()
    count = 0
    monkeypatch.setattr(nic_names, '_live_name_for_mac', lambda mac: next((n for n, m in names.items() if m == mac), None))
    monkeypatch.setattr(nic_names, '_live_name_for_mac_of', names.get)
    monkeypatch.setattr(nic_names, '_addresses_of', lambda n: [])
    monkeypatch.setattr(nic_names, '_run', lambda args: SimpleNamespace(returncode=0, stdout='[{"flags":["UP"]}]', stderr=''))
    def rename(a, b):
        nonlocal count
        count += 1
        if count == fail_at or b in names:
            return SimpleNamespace(returncode=1, stderr='injected failure', stdout='')
        names[b] = names.pop(a)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(nic_names, '_set_name', rename)
    if fail_at:
        with pytest.raises(RuntimeError):
            nic_names.rename_live({'aa': 'Ethernet2', 'bb': 'Ethernet1'})
        assert names == original
    else:
        nic_names.rename_live({'aa': 'Ethernet2', 'bb': 'Ethernet1'})
        assert names == {'Ethernet2': 'aa', 'Ethernet1': 'bb'}


def test_sync_never_changes_network(monkeypatch, store, fake_ovs):
    eng = Engine(settings={'db_path': store.path, 'bridge': 'mms0'}, store=store, ovs=fake_ovs)
    monkeypatch.setattr(netif, 'physical_links', lambda **kw: [netif.LinkInfo('enp1s2', mac='02:00:00:00:00:01', identity='pci:0000:01:02.0', kind='pci')])
    monkeypatch.setattr(netif, 'protected_interfaces', lambda: ['eth0'])
    def forbidden(*args):
        pytest.fail('read/discovery path attempted kernel rename')
    monkeypatch.setattr(nic_names, 'rename_live', forbidden)
    monkeypatch.setattr(nic_names, 'write_rules', forbidden)
    eng.sync_ports()


@pytest.mark.parametrize('count', [256, 512])
def test_large_port_counts(count, monkeypatch, store, fake_ovs):
    links = [netif.LinkInfo(f'enp{i}s0', mac=f'02:00:00:{i>>8:02x}:{i&255:02x}:01', identity=f'pci:0000:{i//32:02x}:{i%32:02x}.0', kind='pci') for i in range(count)]
    monkeypatch.setattr(netif, 'physical_links', lambda **kw: links)
    monkeypatch.setattr(netif, 'protected_interfaces', lambda: ['eth0'])
    eng = Engine(settings={'db_path': store.path, 'bridge': 'mms0'}, store=store, ovs=fake_ovs)
    rows = eng.sync_ports()
    assert len(rows) == count
    assert rows[-1]['name'] == f'Ethernet{count}'


def test_startup_configuration_follows_identity(monkeypatch, store, fake_ovs):
    links = [netif.LinkInfo('enp2s0', mac='02:00:00:00:00:02', identity='pci:0000:02:00.0', kind='pci')]
    monkeypatch.setattr(netif, 'physical_links', lambda **kw: links)
    monkeypatch.setattr(netif, 'protected_interfaces', lambda: ['eth0'])
    eng = Engine(settings={'db_path': store.path, 'bridge': 'mms0'}, store=store, ovs=fake_ovs)
    eng.sync_ports()
    eng.config.interfaces['Ethernet1'].description = 'saved-uplink'
    eng.save_startup('test')
    links.insert(0, netif.LinkInfo('enp1s0', mac='02:00:00:00:00:01', identity='pci:0000:01:00.0', kind='pci'))
    eng.sync_ports()
    saved = store.load_config('startup')
    assert saved.interfaces['Ethernet2'].description == 'saved-uplink'


def test_nested_pci_and_parked_eight(tmp_path, monkeypatch, store, fake_ovs):
    root = tmp_path / 'net'
    root.mkdir()
    for i in range(8):
        name = f'Ethernet{i+1}' if i < 6 else f'mmstmp{i}'
        dev = tmp_path / 'devices/pci0000:00/0000:00:1e.0' / f'0000:01:{i:02x}.0/virtio0'
        dev.mkdir(parents=True)
        link = root / name
        link.mkdir()
        (link / 'device').symlink_to(dev)
        (link / 'address').write_text(f'02:00:00:00:00:{i:02x}')
    monkeypatch.setattr(netif, 'SYS_NET', root)
    monkeypatch.setattr(netif, 'protected_interfaces', lambda: ['eth0'])
    links = netif.physical_links()
    assert len(links) == 8
    assert len({l.identity for l in links}) == 8
    eng = Engine(settings={'db_path': store.path, 'bridge': 'mms0'}, store=store, ovs=fake_ovs)
    assert len(eng.sync_ports()) == 8


def test_leaf_pci_identity(tmp_path, monkeypatch):
    dev = tmp_path / 'devices/pci0000:00/0000:00:1e.0/0000:01:02.0/virtio7'
    dev.mkdir(parents=True)
    root = tmp_path / 'net/enp1s2'
    root.mkdir(parents=True)
    (root / 'device').symlink_to(dev)
    monkeypatch.setattr(netif, 'SYS_NET', root.parent)
    assert netif.interface_identity('enp1s2')[0] == 'pci:0000:01:02.0'


def test_stale_bridge_identity_alias_is_pruned(store, fake_ovs, monkeypatch):
    """0.1.4 keyed several NICs by their shared PCI bridge; the leftover row must go."""
    from makemeswitch.core import interfaces as netif
    from makemeswitch.core.engine import Engine
    from makemeswitch.core.interfaces import LinkInfo

    settings = {
        "bridge": "mms0",
        "db_path": str(store.path),
        "log_dir": None,
        "allow_virtual_ports": False,
        "attach_new_nics": True,
        "stp_mode": "rstp",
        "stp_priority": 32768,
    }
    engine = Engine(settings=settings, store=store, ovs=fake_ovs)

    # Ghost row from the broken build: bridge identity, MAC of a live NIC.
    store.register_port("pci:0000:00:1e.0", "Ethernet7", "mmstmp7", "pci", mac="bc:24:11:7f:d4:4a")

    links = [
        LinkInfo(name="Ethernet1", mac="bc:24:11:b2:d0:36", identity="pci:0000:00:13.0", kind="pci"),
        LinkInfo(name="Ethernet2", mac="bc:24:11:7f:d4:4a", identity="pci:0000:01:02.0", kind="pci"),
    ]
    monkeypatch.setattr(netif, "protected_interfaces", lambda extra=None: ["eth0"])
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: list(links))

    rows = engine.sync_ports()
    identities = {r["identity"] for r in rows}
    assert "pci:0000:00:1e.0" not in identities
    assert "pci:0000:01:02.0" in identities
    assert len([r for r in rows if r["present"]]) == 2
    assert all(r["present"] for r in rows)
