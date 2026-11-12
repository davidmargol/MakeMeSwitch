"""Ports are numbered in physical order, and ranges work like IOS."""
import pytest

from makemeswitch.cli.shell import execute_line
from makemeswitch.cli.modes import Session
from makemeswitch.core import interfaces as netif
from makemeswitch.core.engine import Engine, _topology_key
from makemeswitch.core.interfaces import LinkInfo
from makemeswitch.core.models import InterfaceConfig, SwitchConfig


def _link(name: str, identity: str, mac: str, kind: str = "pci") -> LinkInfo:
    return LinkInfo(name=name, mac=mac, identity=identity, kind=kind)


# Same shape as the reporter's host: five NICs on PCI bus 0, one on bus 1.
BUS0 = [
    _link("ens19", "pci:0000:00:13.0", "bc:24:11:00:00:19"),
    _link("ens20", "pci:0000:00:14.0", "bc:24:11:00:00:20"),
    _link("ens21", "pci:0000:00:15.0", "bc:24:11:00:00:21"),
    _link("ens22", "pci:0000:00:16.0", "bc:24:11:00:00:22"),
    _link("ens23", "pci:0000:00:17.0", "bc:24:11:00:00:23"),
]
BUS1 = [_link("enp1s1", "pci:0000:01:00.0", "bc:24:11:00:01:01")]


def test_topology_key_sorts_pci_bus_then_slot():
    ordered = sorted(BUS1 + BUS0, key=_topology_key)
    assert [link.name for link in ordered] == ["ens19", "ens20", "ens21", "ens22", "ens23", "enp1s1"]


def test_usb_adapters_sort_after_onboard_nics():
    usb = _link("enx001122334455", "usb:1-1.2", "00:11:22:33:44:55", kind="usb")
    assert _topology_key(BUS1[0]) < _topology_key(usb)


@pytest.fixture()
def ordered_engine(store, fake_ovs, monkeypatch):
    settings = {
        "bridge": "mms0",
        "db_path": str(store.path),
        "log_dir": None,
        "allow_virtual_ports": False,
        "stp_mode": "rstp",
        "stp_priority": 32768,
    }
    engine = Engine(settings=settings, store=store, ovs=fake_ovs)
    # A legacy install: numbered by discovery order, so Ethernet1 is the NIC on
    # PCI bus 1 that the kernel enumerated first.
    store.register_port("pci:0000:01:00.0", "Ethernet1", "enp1s1", "pci", mac="bc:24:11:00:01:01")
    store.register_port("pci:0000:00:13.0", "Ethernet2", "ens19", "pci", mac="bc:24:11:00:00:19")
    store.register_port("pci:0000:00:14.0", "Ethernet3", "ens20", "pci", mac="bc:24:11:00:00:20")
    engine.config = SwitchConfig()
    engine.config.interfaces["Ethernet1"] = InterfaceConfig(
        name="Ethernet1", identity="pci:0000:01:00.0", linux_if="enp1s1", attached=True, description="uplink"
    )
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: [*BUS1, *BUS0])
    monkeypatch.setattr(netif, "protected_interfaces", lambda: {"eth0"})
    return engine


def test_renumber_puts_ports_in_physical_order(ordered_engine):
    ordered_engine.sync_ports()
    by_name = {row["name"]: row for row in ordered_engine.port_table()}

    assert by_name["Ethernet1"]["linux_if"] == "ens19"
    assert by_name["Ethernet5"]["linux_if"] == "ens23"
    assert by_name["Ethernet6"]["linux_if"] == "enp1s1"


def test_renumber_keeps_configuration_with_the_nic(ordered_engine):
    ordered_engine.sync_ports()

    # the description belonged to enp1s1, which is now Ethernet6
    assert "Ethernet1" not in ordered_engine.config.interfaces or not ordered_engine.config.interfaces["Ethernet1"].description
    assert ordered_engine.config.interfaces["Ethernet6"].description == "uplink"
    assert ordered_engine.config.interfaces["Ethernet6"].linux_if == "enp1s1"


def test_interface_range_applies_to_every_member(engine):
    session = Session(engine=engine, interactive=False)
    session.last_error = None
    for line in ("enable", "conf t", "int ra eth1-2", "description range-test", "exit"):
        execute_line(session, line)

    assert session.last_error is None
    assert engine.config.interfaces["Ethernet1"].description == "range-test"
    assert engine.config.interfaces["Ethernet2"].description == "range-test"
    assert engine.config.interfaces["Ethernet3"].description != "range-test"


def test_interface_range_accepts_lists_and_prefixed_forms(engine):
    for spec, expected in (
        ("eth1,eth3", ["Ethernet1", "Ethernet3"]),
        ("ethernet 1 - 2", ["Ethernet1", "Ethernet2"]),
        ("eth1-eth2", ["Ethernet1", "Ethernet2"]),
    ):
        session = Session(engine=engine, interactive=False)
        for line in ("enable", "conf t", f"int ra {spec}"):
            execute_line(session, line)
        assert session.last_error is None, spec
        assert session.range_members == expected, spec


def test_interface_command_works_inside_interface_mode(engine):
    session = Session(engine=engine, interactive=False)
    for line in ("enable", "conf t", "int eth1", "int eth 2"):
        execute_line(session, line)

    assert session.last_error is None
    assert session.mode == "if"
    assert session.interface == "Ethernet2"


def test_renumber_follows_nic_count_changes(ordered_engine, monkeypatch):
    """A VM that gains adapters must gain ports, in physical order."""
    ordered_engine.sync_ports()
    assert len(ordered_engine.port_table()) == 6

    extra = [_link("ens24", "pci:0000:00:18.0", "bc:24:11:00:00:24"),
             _link("ens25", "pci:0000:00:19.0", "bc:24:11:00:00:25")]
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: [*BUS1, *BUS0, *extra])
    ordered_engine.sync_ports()

    names = {row["linux_if"]: row["name"] for row in ordered_engine.port_table()}
    assert names["ens19"] == "Ethernet1"
    assert names["ens24"] == "Ethernet6"
    assert names["ens25"] == "Ethernet7"
    assert names["enp1s1"] == "Ethernet8"

    # remove one again: the rest is renumbered, nothing is lost
    monkeypatch.setattr(netif, "physical_links", lambda include_virtual=False, exclude=None: [*BUS0, *extra])
    ordered_engine.sync_ports()
    names = {row["linux_if"]: row["name"] for row in ordered_engine.port_table() if row["present"]}
    assert names["ens19"] == "Ethernet1"
    assert names["ens24"] == "Ethernet6"
    assert names["ens25"] == "Ethernet7"
    # the unplugged enp1s1 keeps a reserved (inactive) port name
    absent = [row["name"] for row in ordered_engine.port_table() if not row["present"]]
    assert absent == ["Ethernet8"]


def test_bridge_interfaces_are_pruned_from_the_port_table(ordered_engine):
    """Rows registered for ovs-system/mms0 by older builds must disappear."""
    ordered_engine.store.register_port(
        "mac:26:c2:34:85:cd:32", "Ethernet77", "ovs-system", "virtual", mac="26:c2:34:85:cd:32"
    )
    ordered_engine.store.register_port(
        "mac:11:22:33:44:55:66", "Ethernet4096", "mms0", "virtual", mac="11:22:33:44:55:66"
    )
    ordered_engine.sync_ports()
    table = ordered_engine.port_table()
    assert all(row["linux_if"] not in ("ovs-system", "mms0") for row in table)
    assert "Ethernet77" not in {row["name"] for row in table}
    assert "Ethernet4096" not in {row["name"] for row in table}
    assert len(table) == 6
