"""Documented post-install defaults and the MAC identifier exposed to the GUI."""
import os

from makemeswitch.core.engine import Engine
from makemeswitch.core.security import SecurityService


def test_bootstrap_admin_uses_documented_default(store, monkeypatch):
    monkeypatch.setenv("MMS_ADMIN_PASSWORD", "MakeMeSwitch")
    security = SecurityService(store)

    created = security.ensure_bootstrap_admin()

    assert created == ("admin", "MakeMeSwitch")
    assert security.authenticate("admin", "MakeMeSwitch") is not None
    assert security.authenticate("admin", "wrong-password") is None


def test_reset_to_default_password_works_after_install(store):
    security = SecurityService(store)
    security.set_credentials("admin", "some-old-password", "admin", create=True)

    security.set_credentials("admin", "MakeMeSwitch", "admin")

    assert security.authenticate("admin", "MakeMeSwitch") is not None
    assert security.authenticate("admin", "some-old-password") is None


def test_port_table_exposes_mac_for_gui(engine: Engine):
    engine.ports = {
        "Ethernet1": {
            "name": "Ethernet1",
            "linux_if": "ens19",
            "identity": "pci:0000:03:00.0",
            "kind": "pci",
            "present": True,
            "mac": "aa:bb:cc:dd:ee:ff",
            "link": {},
        },
        # older rows had no dedicated column: fall back to the identity value
        "Ethernet2": {
            "name": "Ethernet2",
            "linux_if": "ens20",
            "identity": "mac:11:22:33:44:55:66",
            "kind": "usb",
            "present": True,
            "mac": "",
            "link": {},
        },
    }

    rows = {row["name"]: row for row in engine.port_table()}

    assert rows["Ethernet1"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert rows["Ethernet2"]["mac"] == "11:22:33:44:55:66"


def test_store_persists_mac(store):
    store.register_port("pci:0000:03:00.0", "Ethernet1", "ens19", "pci", mac="aa:bb:cc:dd:ee:ff")
    assert store.port_by_name("Ethernet1")["mac"] == "aa:bb:cc:dd:ee:ff"

    # re-registering without a MAC must not wipe the stored one
    store.register_port("pci:0000:03:00.0", "Ethernet1", "Ethernet1", "pci")
    assert store.port_by_name("Ethernet1")["mac"] == "aa:bb:cc:dd:ee:ff"
