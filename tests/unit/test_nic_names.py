import pytest

from makemeswitch.nic_names import canonical, desired_mapping, port_number, simple_name, sort_key


def port(name="Ethernet1", linux="ens19", mac="02:00:00:00:00:01"):
    return dict(
        name=name,
        linux_if=linux,
        present=True,
        protected=False,
        identity="pci:0000:00:19.0",
        link={"mac": mac},
    )


def test_simple_names_and_legacy_spellings():
    assert simple_name(3) == "Ethernet3"
    assert port_number("Ethernet10") == 10
    assert port_number("Ethernet1/2") == 2
    assert canonical("Ethernet1/1") == "Ethernet1"
    assert canonical("eth 2") == "Ethernet2"
    assert canonical("2/3") == "Ethernet67"
    assert canonical("Vlan10") is None


def test_numeric_sorting():
    assert sorted(["Ethernet10", "Ethernet2", "Ethernet1"], key=sort_key) == ["Ethernet1", "Ethernet2", "Ethernet10"]


def test_mapping_skips_management_and_absent():
    mapping = desired_mapping([port(), port("Ethernet2", "eth0", "02:00:00:00:00:02")], {"eth0"})
    assert mapping == {"02:00:00:00:00:01": "Ethernet1"}
    absent = port("Ethernet3")
    absent["present"] = False
    assert desired_mapping([absent], set()) == {}


def test_mapping_rejects_bad_mac_and_duplicates():
    with pytest.raises(ValueError):
        desired_mapping([port(mac="nope")], set())
    with pytest.raises(ValueError):
        desired_mapping([port(), port(mac="02:00:00:00:00:09")], set())
