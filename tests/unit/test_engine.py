"""Engine tests: reconciliation, confirmation, idempotency and rollback."""
import pytest

from makemeswitch.core.engine import NeedsConfirmation
from makemeswitch.core.models import ConfigError, InterfaceConfig, SwitchConfig, Vlan


def test_plan_lists_new_ports(engine):
    cfg = engine.config.copy()
    ops = engine.plan(cfg)
    assert ops == []  # everything already reconciled by the fixture


def test_apply_is_idempotent(engine):
    result = engine.apply(engine.config.copy(), user="test", confirm=True)
    assert result.changed is False
    assert result.ops == []


def test_vlan_change_updates_ovs(engine, fake_ovs):
    cfg = engine.config.copy()
    cfg.interfaces["Ethernet1"].access_vlan = 20
    result = engine.apply(cfg, user="test", confirm=True)
    assert result.changed
    assert fake_ovs.port_details()["eth9"]["tag"] == 20


def test_shutdown_removes_port_from_forwarding(engine, fake_ovs):
    cfg = engine.config.copy()
    cfg.interfaces["Ethernet2"].admin_up = False
    engine.apply(cfg, user="test", confirm=True)
    assert "eth10" not in fake_ovs.port_details()


def test_trunk_configuration(engine, fake_ovs):
    details = fake_ovs.port_details()["eth11"]
    assert details["trunks"] == [10, 20]
    assert details["tag"] is None


def test_new_attachment_requires_confirmation(engine):
    cfg = engine.config.copy()
    cfg.interfaces["Ethernet4"] = InterfaceConfig(name="Ethernet4", linux_if="eth12", attached=True, access_vlan=10)
    with pytest.raises(NeedsConfirmation) as excinfo:
        engine.apply(cfg, user="test")
    assert "Ethernet4" in excinfo.value.message
    assert excinfo.value.preview["newly_attached"] == ["Ethernet4"]


def test_validation_blocks_undefined_vlan(engine):
    cfg = engine.config.copy()
    cfg.interfaces["Ethernet1"].access_vlan = 399
    with pytest.raises(ConfigError):
        engine.apply(cfg, user="test", confirm=True)


def test_failed_apply_rolls_back(engine, fake_ovs):
    before = engine.config.to_dict()
    fake_ovs.fail_on.add("add_port")
    cfg = engine.config.copy()
    cfg.interfaces["Ethernet1"].access_vlan = 20
    with pytest.raises(RuntimeError):
        engine.apply(cfg, user="test", confirm=True)
    assert engine.config.to_dict() == before
    assert "restore" in [call[0] for call in fake_ovs.calls]


def test_drift_detection(engine, fake_ovs):
    fake_ovs.bridges["mms0"]["eth9"]["tag"] = 99
    drift = engine.diff()
    assert any("tag drift" in issue["message"] for issue in drift)


def test_save_and_restore_startup(engine):
    cfg = engine.config.copy()
    cfg.set_hostname("CHANGED")
    engine.apply(cfg, user="test", confirm=True)
    assert engine.unsaved_changes()
    engine.save_startup("test")
    assert not engine.unsaved_changes()
    engine.load_startup("test")
    assert engine.config.hostname == "CHANGED"


def test_rollback_restores_history(engine):
    original = engine.config.hostname
    cfg = engine.config.copy()
    cfg.set_hostname("ROLLBACK-ME")
    engine.apply(cfg, user="test", confirm=True)
    engine.rollback(user="test")
    assert engine.config.hostname == original


def test_boot_discards_unsaved_changes(engine, fake_ovs):
    cfg = engine.config.copy()
    cfg.set_hostname("NEVER-SAVED")
    engine.apply(cfg, user="test", confirm=True)
    assert engine.config.hostname == "NEVER-SAVED"
    engine.boot(apply=True)
    assert engine.config.hostname == "TEST-SW"
    assert not engine.unsaved_changes()


def test_vlan_deletion_guard(engine):
    cfg = engine.config.copy()
    with pytest.raises(ConfigError):
        cfg.delete_vlan(10)
    errors = cfg.validate()
    assert errors == []
