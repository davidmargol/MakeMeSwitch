"""Regression tests for explicit all/none trunk intent."""
import pytest

from makemeswitch.core.models import InterfaceConfig, SwitchConfig, format_vlan_list, parse_vlan_list


def test_all_and_none_roundtrip():
    assert parse_vlan_list('all') == list(range(1, 4095))
    assert parse_vlan_list('none') == []
    assert format_vlan_list(list(range(1, 4095))) == 'all'
    assert format_vlan_list([]) == 'none'
    for allowed in ([], list(range(1, 4095))):
        iface = InterfaceConfig(name='Ethernet1', mode='trunk', trunk_allowed=allowed)
        assert InterfaceConfig.from_dict(iface.to_dict()).trunk_allowed == allowed


def test_all_is_ovs_empty_trunks_not_configured_vlan_snapshot(engine, fake_ovs):
    cfg = engine.config.copy()
    cfg.interfaces['Ethernet3'].trunk_allowed = list(range(1, 4095))
    assert cfg.validate() == []
    engine.apply(cfg, confirm=True)
    assert fake_ovs.port_details()['eth11']['trunks'] == []
    assert fake_ovs.port_details()['eth11']['tag'] is None
    assert engine.plan(cfg) == []


def test_all_with_native_vlan_keeps_native_trunk_mode(engine, fake_ovs):
    cfg = engine.config.copy()
    iface = cfg.interfaces['Ethernet3']
    iface.trunk_allowed = list(range(1, 4095))
    iface.native_vlan = 10
    engine.apply(cfg, confirm=True)
    assert fake_ovs.port_details()['eth11']['vlan_mode'] == 'native-untagged'
    assert fake_ovs.port_details()['eth11']['trunks'] == []
    assert engine.plan(cfg) == []


def test_none_detaches_instead_of_accidentally_allowing_all(engine, fake_ovs):
    cfg = engine.config.copy()
    cfg.interfaces['Ethernet3'].trunk_allowed = []
    engine.apply(cfg, confirm=True)
    assert 'eth11' not in fake_ovs.port_details()
    assert engine.config.interfaces['Ethernet3'].attached
    cfg.interfaces['Ethernet3'].trunk_allowed = [10]
    engine.apply(cfg, confirm=True)
    assert fake_ovs.port_details()['eth11']['trunks'] == [10]
