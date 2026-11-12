"""Linux admin-state repairs, isolated from the host network."""
import pytest

from makemeswitch.core.models import InterfaceConfig


def test_apply_repairs_attached_admin_up_nic_even_without_ovs_drift(engine, fake_ovs):
    fake_ovs.link_states = {'eth9': False, 'eth10': True, 'eth11': True}
    fake_ovs.link_admin_states = lambda: dict(fake_ovs.link_states)
    fake_ovs.set_link_up = lambda name: fake_ovs.link_states.update({name: True})
    result = engine.apply(engine.config.copy(), confirm=True)
    assert result.changed
    assert fake_ovs.link_states['eth9'] is True
    assert any(op['action'] == 'link-up' and op['target'] == 'eth9' for op in result.ops)
    assert engine.plan(engine.config) == []


def test_no_link_up_for_unattached_or_shutdown(engine, fake_ovs):
    cfg = engine.config.copy()
    cfg.interfaces['Ethernet1'].admin_up = False
    cfg.interfaces['Ethernet2'].attached = False
    calls = []
    fake_ovs.link_admin_states = lambda: {'eth9': False, 'eth10': False, 'eth11': False}
    fake_ovs.set_link_up = calls.append
    engine.apply(cfg, confirm=True)
    assert calls == ['eth11']
