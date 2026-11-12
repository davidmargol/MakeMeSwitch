"""Shared fixtures: temporary store, fake OVS backend and an engine."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import os

# Tests must never rename the machine's real NICs.
os.environ.setdefault("MMS_NO_RENAME", "1")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from makemeswitch.core.engine import Engine
from makemeswitch.core.models import InterfaceConfig, SwitchConfig, Vlan
from makemeswitch.core.store import Store


class FakeOvs:
    """In-memory stand-in for :class:`OvsBackend` used by unit tests."""

    def __init__(self) -> None:
        self.installed = True
        self.bridges: dict[str, dict[str, dict[str, Any]]] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.fail_on: set[str] = set()
        self.stats: dict[str, dict[str, float]] = {}
        self._snapshots: list[dict[str, Any]] = []
        self.stp: dict[str, Any] = {"enabled": False, "mode": "none", "priority": 32768}
        self.mirrors_created: list[str] = []

    # -- helpers ----------------------------------------------------------
    def _bridge(self, name: str) -> dict[str, dict[str, Any]]:
        return self.bridges.setdefault(name or "mms0", {})

    def _record(self, action: str, *args) -> None:
        self.calls.append((action, args))
        if action in self.fail_on:
            raise RuntimeError(f"injected failure on {action}")

    # -- backend API ------------------------------------------------------
    def link_admin_states(self):
        return {}

    def set_link_up(self, interface):
        self._record("set_link_up", interface)

    def version(self) -> str:
        return "fake-ovs 1.0"

    def health(self) -> dict[str, Any]:
        return {"installed": True, "running": True, "version": "fake-ovs 1.0", "bridge": "mms0", "bridge_exists": bool(self.bridges), "ports": len(self.bridges.get("mms0", {}))}

    def service_status(self) -> dict[str, Any]:
        return self.health()

    def bridge_exists(self) -> bool:
        return "mms0" in self.bridges

    def ensure_bridge(self, stp_enabled: bool = True, stp_mode: str = "rstp", stp_priority: int = 32768) -> None:
        self._record("ensure_bridge", stp_enabled, stp_mode, stp_priority)
        self._bridge("mms0")
        self.stp = {
            "enabled": bool(stp_enabled) and stp_mode != "none",
            "mode": stp_mode if stp_enabled and stp_mode != "none" else "none",
            "priority": stp_priority,
        }

    def delete_bridge(self) -> None:
        self._record("delete_bridge")
        self.bridges.pop("mms0", None)

    def list_ports(self) -> list[str]:
        return sorted(self.bridges.get("mms0", {}))

    def port_exists(self, port: str) -> bool:
        return port in self.bridges.get("mms0", {})

    def port_details(self) -> dict[str, dict[str, Any]]:
        return {name: dict(info) for name, info in self.bridges.get("mms0", {}).items()}

    def add_port(self, port: str, tag=None, trunks=None, mtu=None, rstp_edge=True) -> None:
        self._record("add_port", port)
        self._bridge("mms0")[port] = {
            "name": port,
            "tag": tag,
            "trunks": sorted(trunks or []),
            "type": "",
            "bond_mode": "",
            "lacp": "",
            "interfaces": [port],
            "mtu": mtu,
            "external_ids": {"makemeswitch-managed": "true"},
        }

    def set_port_vlans(self, port: str, tag, trunks) -> None:
        self._record("set_port_vlans", port)
        info = self._bridge("mms0").setdefault(port, {"name": port, "interfaces": [port], "external_ids": {}})
        info["tag"] = tag
        info["trunks"] = sorted(trunks or [])

    def set_port_mtu(self, interface: str, mtu: int) -> None:
        self._record("set_port_mtu", interface, mtu)
        if interface in self._bridge("mms0"):
            self._bridge("mms0")[interface]["mtu"] = mtu

    def set_port_vlan_mode(self, port, mode):
        self._bridge("mms0")[port]["vlan_mode"] = mode

    def del_port(self, port: str) -> None:
        self._record("del_port", port)
        self._bridge("mms0").pop(port, None)

    def add_bond(self, name, members, mode="active", lb="balance-tcp", tag=None, trunks=None) -> None:
        self._record("add_bond", name, tuple(members))
        self._bridge("mms0")[name] = {
            "name": name,
            "tag": tag,
            "trunks": sorted(trunks or []),
            "type": "bond",
            "bond_mode": lb,
            "lacp": mode,
            "interfaces": list(members),
            "mtu": None,
            "external_ids": {"makemeswitch-managed": "true"},
        }
        for member in members:
            self._bridge("mms0").pop(member, None)

    def clear_mirrors(self) -> None:
        self._record("clear_mirrors")
        self.mirrors_created = []

    def mirror_names(self) -> list[str]:
        return list(self.mirrors_created)

    def add_mirror(self, name, sources, output, direction="both") -> None:
        self._record("add_mirror", name, tuple(sources), output)
        self.mirrors_created.append(name)

    def stp_state(self) -> dict[str, Any]:
        return dict(self.stp)

    def configure_stp(self, enabled, mode, priority) -> None:
        self._record("configure_stp", enabled, mode, priority)
        self.stp = {"enabled": bool(enabled) and mode != "none", "mode": mode if enabled and mode != "none" else "none", "priority": priority}

    def interface_stats(self) -> dict[str, dict[str, float]]:
        return {port: dict(self.stats.get(port, {})) for port in self.list_ports()}

    def mac_table(self) -> list[dict[str, Any]]:
        return []

    def port_names_by_number(self) -> dict[int, str]:
        return {}

    def snapshot(self) -> dict[str, Any]:
        snap = {"exists": self.bridge_exists(), "bridge": "mms0", "ports": self.port_details(), "order": self.list_ports()}
        self._snapshots.append(snap)
        return snap

    def restore(self, snapshot: dict[str, Any]) -> None:
        self._record("restore")
        if not snapshot.get("exists"):
            self.bridges.pop("mms0", None)
            return
        self.bridges["mms0"] = {name: dict(info) for name, info in (snapshot.get("ports") or {}).items()}


@pytest.fixture(autouse=True)
def isolated_naming_lock(tmp_path, monkeypatch):
    monkeypatch.setenv('MMS_NAMING_LOCK', str(tmp_path / 'naming.lock'))


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "makemeswitch.db")


@pytest.fixture()
def fake_ovs() -> FakeOvs:
    return FakeOvs()


@pytest.fixture()
def engine(store: Store, fake_ovs: FakeOvs) -> Engine:
    settings = {
        "bridge": "mms0",
        "db_path": str(store.path),
        "log_dir": None,
        "allow_virtual_ports": False,
        "stp_mode": "rstp",
        "stp_priority": 32768,
    }
    eng = Engine(settings=settings, store=store, ovs=fake_ovs)
    eng.sync_ports = lambda: []  # type: ignore[assignment]
    cfg = SwitchConfig()
    cfg.set_hostname("TEST-SW")
    cfg.vlans[10] = Vlan(10, "SERVERS")
    cfg.vlans[20] = Vlan(20, "CLIENTS")
    cfg.interfaces["Ethernet1"] = InterfaceConfig(name="Ethernet1", linux_if="eth9", attached=True, access_vlan=10)
    cfg.interfaces["Ethernet2"] = InterfaceConfig(name="Ethernet2", linux_if="eth10", attached=True, access_vlan=10)
    cfg.interfaces["Ethernet3"] = InterfaceConfig(name="Ethernet3", linux_if="eth11", attached=True, mode="trunk", trunk_allowed=[10, 20])
    eng.apply(cfg, user="fixture", confirm=True)
    eng.save_startup(user="fixture")
    return eng
