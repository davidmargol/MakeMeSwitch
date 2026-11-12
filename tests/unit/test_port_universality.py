"""Any number of NICs, any mix of PCIe/USB/virtio, numbered 1..N without gaps."""
import pytest

from makemeswitch.core import interfaces as netif
from makemeswitch.core.engine import Engine
from makemeswitch.core.interfaces import LinkInfo


def _nic(name, mac, identity=None):
    return LinkInfo(name=name, mac=mac, identity=identity, kind="pci" if identity else "virtual",
                    operstate="up", carrier=True)


BUS0 = [_nic("enp0s19", "bc:24:11:b2:d0:36", "pci:0000:00:13.0")]
BUS1 = [
    _nic("enp1s1", "bc:24:11:ca:e9:28", "pci:0000:01:01.0"),
    _nic("enp1s2", "bc:24:11:87:e4:03", "pci:0000:01:02.0"),
    _nic("enp1s3", "bc:24:11:43:13:e8", "pci:0000:01:03.0"),
]


@pytest.fixture()
def engine(store, fake_ovs, monkeypatch):
    settings = {
        "bridge": "mms0",
        "db_path": str(store.path),
        "log_dir": None,
        "allow_virtual_ports": False,
        "attach_new_nics": True,
        "stp_mode": "rstp",
        "stp_priority": 32768,
    }
    eng = Engine(settings=settings, store=store, ovs=fake_ovs)
    monkeypatch.setattr(netif, "protected_interfaces", lambda extra=None: ["eth0"])
    monkeypatch.setattr(netif, "default_route_interfaces", lambda: {"eth0"})
    return eng, monkeypatch


def test_eight_nics_numbered_without_gaps(engine):
    eng, monkeypatch = engine
    links = [*BUS0, *BUS1, _nic("enp2s1", "bc:24:11:00:00:01", "pci:0000:02:01.0"),
             _nic("enp2s2", "bc:24:11:00:00:02", "pci:0000:02:02.0")]
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: list(links))
    rows = eng.sync_ports()
    assert [r["name"] for r in rows] == [f"Ethernet{i}" for i in range(1, 7)]
    assert all(r["present"] for r in rows)


def test_sixty_four_nics(engine):
    eng, monkeypatch = engine
    links = [_nic(f"enp{i}s0", f"bc:24:11:01:{i:02x}:01", f"pci:0000:{i:02x}:00.0") for i in range(1, 65)]
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: list(links))
    rows = eng.sync_ports()
    assert len(rows) == 64
    assert rows[0]["name"] == "Ethernet1"
    assert rows[-1]["name"] == "Ethernet64"


def test_configuration_travels_with_the_nic(engine):
    """Renumbering must not move a description/VLAN to another physical port."""
    eng, monkeypatch = engine
    links = [*BUS0, *BUS1]
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: list(links))
    eng.sync_ports()
    target = next(r["name"] for r in eng.port_table() if r["linux_if"] == "enp1s2")
    eng.config.interfaces[target].description = "uplink-to-core"
    eng.config.interfaces[target].access_vlan = 10

    # A NIC with a lower PCI address shows up: numbering shifts.
    extra = [_nic("enp0s5", "bc:24:11:aa:bb:cc", "pci:0000:00:05.0")]
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: [*extra, *links])
    eng.sync_ports()

    moved = next(r["name"] for r in eng.port_table() if r["linux_if"] == "enp1s2")
    assert moved == "Ethernet4"
    assert eng.config.interfaces[moved].description == "uplink-to-core"
    assert eng.config.interfaces[moved].access_vlan == 10
    # the new port took the first number
    assert eng.config.interfaces["Ethernet1"].identity == "pci:0000:00:05.0"
    assert eng.config.interfaces["Ethernet1"].attached is True


def test_new_nics_are_attached_automatically(engine):
    eng, monkeypatch = engine
    links = [*BUS0]
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: list(links))
    eng.sync_ports()
    assert eng.config.interfaces["Ethernet1"].attached is True

    links = [*BUS0, *BUS1]
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: list(links))
    eng.sync_ports()
    for name in ("Ethernet2", "Ethernet3", "Ethernet4"):
        assert eng.config.interfaces[name].attached is True
        assert eng.config.interfaces[name].admin_up is True


def test_attach_policy_can_be_disabled(engine):
    eng, monkeypatch = engine
    eng.settings["attach_new_nics"] = False
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: [*BUS0])
    eng.sync_ports()
    assert eng.config.interfaces["Ethernet1"].attached is False


def test_removing_a_nic_keeps_numbering_contiguous(engine):
    eng, monkeypatch = engine
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: [*BUS0, *BUS1])
    eng.sync_ports()
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: [*BUS0, BUS1[1], BUS1[2]])
    rows = eng.sync_ports()
    present = [r for r in rows if r["present"]]
    assert [r["name"] for r in present] == ["Ethernet1", "Ethernet2", "Ethernet3"]
    absent = [r for r in rows if not r["present"]]
    assert len(absent) == 1 and absent[0]["name"] == "Ethernet4"
