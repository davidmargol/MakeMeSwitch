"""Configuration model and validation tests."""
import pytest

from makemeswitch.core.models import (
    ConfigError,
    InterfaceConfig,
    SwitchConfig,
    Vlan,
    format_vlan_list,
    normalize_interface_name,
    parse_vlan_list,
)


def test_normalize_interface_names():
    assert normalize_interface_name("ethernet 1/1") == "Ethernet1"
    assert normalize_interface_name("Eth1/1") == "Ethernet1"
    assert normalize_interface_name("Ethernet1") == "Ethernet1"
    assert normalize_interface_name("eth3") == "Ethernet3"
    assert normalize_interface_name("2/24") == "Ethernet88"
    assert normalize_interface_name("port-channel3") == "Port-Channel3"
    assert normalize_interface_name("vlan10") == "Vlan10"


def test_legacy_keys_are_reindexed():
    cfg = SwitchConfig()
    cfg.interfaces["Ethernet1/2"] = InterfaceConfig(name="Ethernet1/2", linux_if="eth9", attached=True)
    assert cfg.validate() == []
    assert list(cfg.interfaces) == ["Ethernet2"]
    assert cfg.interfaces["Ethernet2"].linux_if == "eth9"


def test_vlan_list_roundtrip():
    assert parse_vlan_list("10,20,30-32") == [10, 20, 30, 31, 32]
    assert format_vlan_list([1, 2, 3, 10, 20]) == "1-3,10,20"
    with pytest.raises(ConfigError):
        parse_vlan_list("4095")
    with pytest.raises(ConfigError):
        parse_vlan_list("30-20")


def test_vlan_validation_and_duplicate_names():
    cfg = SwitchConfig()
    cfg.add_vlan(10, "A")
    cfg.add_vlan(20, "A")
    errors = cfg.validate()
    assert any("unique" in error for error in errors)


def test_delete_vlan_in_use_is_refused():
    cfg = SwitchConfig()
    cfg.add_vlan(10, "SERVERS")
    cfg.interfaces["Ethernet1"] = InterfaceConfig(name="Ethernet1", linux_if="eth9", attached=True, access_vlan=10)
    with pytest.raises(ConfigError):
        cfg.delete_vlan(10)
    with pytest.raises(ConfigError):
        cfg.delete_vlan(1)


def test_trunk_native_vlan_must_not_be_tagged():
    cfg = SwitchConfig()
    cfg.add_vlan(10)
    cfg.add_vlan(20)
    cfg.interfaces["Ethernet3"] = InterfaceConfig(
        name="Ethernet3", linux_if="eth11", attached=True, mode="trunk", trunk_allowed=[10, 20], native_vlan=10
    )
    errors = cfg.validate()
    assert any("native VLAN" in error for error in errors)


def test_invalid_mtu_and_hostname():
    with pytest.raises(ConfigError):
        InterfaceConfig(name="Ethernet1", mtu=100)
    with pytest.raises(ConfigError):
        SwitchConfig().set_hostname("-bad-")


def test_attached_port_requires_nic():
    cfg = SwitchConfig()
    cfg.interfaces["Ethernet1"] = InterfaceConfig(name="Ethernet1", attached=True, linux_if=None)
    assert any("no physical NIC" in error for error in cfg.validate())


def test_protected_management_interface_is_rejected():
    cfg = SwitchConfig()
    cfg.protected_interfaces = ["eth0"]
    cfg.interfaces["Ethernet1"] = InterfaceConfig(name="Ethernet1", linux_if="eth0", attached=True)
    assert any("protected management" in error for error in cfg.validate())


def test_serialisation_roundtrip():
    cfg = SwitchConfig()
    cfg.set_hostname("LAB-SW01")
    cfg.add_vlan(10, "SERVERS")
    cfg.interfaces["Ethernet1"] = InterfaceConfig(name="Ethernet1", linux_if="eth9", attached=True, access_vlan=10)
    clone = SwitchConfig.from_dict(cfg.to_dict())
    assert clone.to_dict() == cfg.to_dict()
    assert clone.validate() == []
