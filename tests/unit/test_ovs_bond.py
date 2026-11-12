"""Building a bond must not trip over ports that already exist in the bridge."""
import pytest

from makemeswitch.backends.ovs import OvsBackend, OvsError


@pytest.fixture()
def backend(monkeypatch):
    ovs = OvsBackend(bridge="mms0")
    calls: list[list[str]] = []
    monkeypatch.setattr(ovs, "_run", lambda args, check=True: calls.append(list(args)) or "")
    ovs.calls = calls  # type: ignore[attr-defined]
    return ovs


def test_bond_detaches_standalone_ports_first(backend, monkeypatch):
    monkeypatch.setattr(backend, "list_ports", lambda: ["Ethernet2", "Ethernet3", "Ethernet4"])
    monkeypatch.setattr(backend, "port_details", lambda: {"Ethernet4": {"interfaces": ["Ethernet4"]}})

    backend.add_bond("bond1", ["Ethernet2", "Ethernet3"])

    assert backend.calls == [
        [
            "--if-exists",
            "del-port",
            "mms0",
            "Ethernet2",
            "--",
            "--if-exists",
            "del-port",
            "mms0",
            "Ethernet3",
            "--",
            "add-bond",
            "mms0",
            "bond1",
            "Ethernet2",
            "Ethernet3",
            "lacp=active",
            "bond_mode=balance-tcp",
            "external_ids:makemeswitch-managed=true",
        ]
    ]


def test_existing_bond_with_same_members_is_only_reconfigured(backend, monkeypatch):
    monkeypatch.setattr(backend, "list_ports", lambda: ["bond1"])
    monkeypatch.setattr(backend, "port_details", lambda: {"bond1": {"interfaces": ["Ethernet2", "Ethernet3"]}})

    backend.add_bond("bond1", ["Ethernet3", "Ethernet2"], tag=10)

    assert backend.calls == [
        ["set", "port", "bond1", "lacp=active", "bond_mode=balance-tcp",
         "external_ids:makemeswitch-managed=true", "tag=10"]
    ]


def test_changed_member_list_rebuilds_the_bond(backend, monkeypatch):
    monkeypatch.setattr(backend, "list_ports", lambda: ["bond1", "Ethernet4"])
    monkeypatch.setattr(backend, "port_details", lambda: {"bond1": {"interfaces": ["Ethernet2", "Ethernet3"]}})

    backend.add_bond("bond1", ["Ethernet2", "Ethernet4"])

    args = backend.calls[0]
    assert args[:5] == ["--if-exists", "del-port", "mms0", "bond1", "--"]
    assert args.count("--") == 2  # one separator after the bond, one after the member
    assert "add-bond" in args
    assert "Ethernet4" in args


def test_single_member_bond_is_refused_before_calling_ovs(backend):
    with pytest.raises(OvsError):
        backend.add_bond("bond1", ["Ethernet2"])
    assert backend.calls == []
