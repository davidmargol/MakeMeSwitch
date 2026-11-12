"""Configuration engine: the single source of truth for CLI and web panel.

Rules enforced here:

* every change is validated before it touches the kernel;
* OVS is changed only through declarative reconciliation (idempotent);
* a failed apply is rolled back to the pre-change OVS snapshot;
* enslaving a NIC into the bridge always requires explicit confirmation;
* the running config and OVS are compared by ``diff()`` for drift detection.
"""
from __future__ import annotations

import os
import platform
import re
import socket
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from makemeswitch import PROJECT_NAME, __version__
from makemeswitch.backends.frr import FrrBackend
from makemeswitch.backends.ovs import OvsBackend, OvsError
from makemeswitch.core import interfaces as netif
from makemeswitch.core import portnames
from makemeswitch.core.l3 import L3Manager
from makemeswitch.core.models import ConfigError, InterfaceConfig, SwitchConfig, format_vlan_list
from makemeswitch.core.store import Store
from makemeswitch.utils.logging import get_logger

log = get_logger("makemeswitch.engine")

DEFAULT_SETTINGS: dict[str, Any] = {
    "bridge": "mms0",
    "db_path": "/var/lib/makemeswitch/makemeswitch.db",
    "state_dir": "/var/lib/makemeswitch",
    "log_dir": "/var/log/makemeswitch",
    "allow_virtual_ports": False,
    "attach_new_nics": True,
    "stp_mode": "rstp",
    "stp_priority": 32768,
    "lldp_enabled": False,
    "api": {"bind": "127.0.0.1", "port": 8000, "https": False, "session_ttl_seconds": 28800},
    "web": {"enabled": True},
}


class NeedsConfirmation(RuntimeError):
    """Raised when an operation would enslave a physical NIC."""

    def __init__(self, message: str, preview: dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.preview = preview


@dataclass
class ApplyResult:
    changed: bool
    ops: list[dict[str, Any]]
    config: SwitchConfig
    rollback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "ops": self.ops, "rollback": self.rollback, "config": self.config.to_dict()}


def load_settings(path: str | None = None) -> dict[str, Any]:
    """Merge /etc/makemeswitch/makemeswitch.yaml over built-in defaults."""
    settings = dict(DEFAULT_SETTINGS)
    candidates = [path] if path else [
        os.environ.get("MMS_CONFIG"),
        "/etc/makemeswitch/makemeswitch.yaml",
        str(Path(__file__).resolve().parents[2] / "config" / "defaults.yaml"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                data = yaml.safe_load(Path(candidate).read_text()) or {}
            except (OSError, yaml.YAMLError):
                continue
            for key, value in data.items():
                if isinstance(value, dict) and isinstance(settings.get(key), dict):
                    settings[key] = {**settings[key], **value}
                else:
                    settings[key] = value
            break
    env_bridge = os.environ.get("MMS_BRIDGE")
    if env_bridge:
        settings["bridge"] = env_bridge
    env_db = os.environ.get("MMS_DB")
    if env_db:
        settings["db_path"] = env_db
    if os.environ.get("MMS_ALLOW_VIRTUAL", "").lower() in ("1", "true", "yes"):
        settings["allow_virtual_ports"] = True
    return settings


class Engine:
    def __init__(self, settings: dict[str, Any] | None = None, store: Store | None = None, ovs: OvsBackend | None = None):
        self.settings = settings or load_settings()
        self.store = store or Store(self.settings["db_path"])
        self.ovs = ovs or OvsBackend(self.settings.get("bridge", "mms0"))
        self.frr = FrrBackend()
        self.bridge = self.settings.get("bridge", "mms0")
        self.l3 = L3Manager(self.bridge)
        self._l3_sig: tuple | None = None
        self.allow_virtual = bool(self.settings.get("allow_virtual_ports", False))
        self.ports: dict[str, dict[str, Any]] = {}
        self._last_stats: dict[str, tuple[float, dict[str, float]]] = {}
        self._last_rates: dict[str, dict[str, float]] = {}
        self._running_ts: float | None = None
        self.config = self._load_running()
        self._configure_remote_syslog()
        self.startup = self.store.load_config("startup") or self.config.copy()

    # --- cross-process consistency ---------------------------------------
    def refresh(self) -> bool:
        """Reload the running config when another process (CLI) changed it."""
        meta = self.store.config_meta("running")
        if not meta:
            return False
        if self._running_ts is not None and abs(meta["updated_at"] - self._running_ts) < 1e-6:
            return False
        stored = self.store.load_config("running")
        if stored is None:
            return False
        self.config = stored
        self._running_ts = meta["updated_at"]
        self.startup = self.store.load_config("startup") or self.startup
        return True

    # --- boot ------------------------------------------------------------
    def _load_running(self) -> SwitchConfig:
        stored = self.store.load_config("running")
        if stored is None:
            cfg = SwitchConfig()
            cfg.stp.mode = self.settings.get("stp_mode", "rstp")
            cfg.stp.priority = int(self.settings.get("stp_priority", 32768))
            cfg.lldp_enabled = bool(self.settings.get("lldp_enabled", False))
            cfg.mgmt = {"web_enabled": bool(self.settings.get("web", {}).get("enabled", True))}
            self.store.save_config("running", cfg, "bootstrap")
            self.store.save_config("startup", cfg, "bootstrap")
            self.store.add_event("info", "engine", "Initial configuration created")
            meta = self.store.config_meta("running")
            self._running_ts = meta["updated_at"] if meta else None
            return cfg
        meta = self.store.config_meta("running")
        self._running_ts = meta["updated_at"] if meta else None
        return stored

    def bootstrap(self, apply: bool = True) -> None:
        """Register physical ports and (optionally) reconcile OVS at startup."""
        self.sync_ports()
        if apply:
            try:
                self.reconcile(self.config, raise_on_error=False)
            except Exception as exc:  # pragma: no cover - service must always start
                log.error("bootstrap reconcile failed", extra={"data": {"error": str(exc)}})
                self.store.add_event("error", "engine", f"Startup reconcile failed: {exc}")

    def boot(self, apply: bool = True) -> None:
        """Service/boot sequence: the startup config wins over unsaved changes.

        This is what makes ``write memory`` meaningful: a reboot discards the
        running configuration that was never saved.
        """
        startup = self.store.load_config("startup")
        if startup is not None:
            self.config = startup.copy()
            self._running_ts = None
            self.store.save_config("running", self.config, "boot")
            self.store.add_event("info", "engine", "Startup configuration loaded (unsaved changes discarded)")
        self.bootstrap(apply=apply)
        self.startup = self.config.copy()
        self.store.save_config("running", self.config, "boot")

    def sync_ports(self) -> list[dict[str, Any]]:
        from makemeswitch.nic_names import naming_lock
        with naming_lock():
            return self._sync_ports_locked()

    def _sync_ports_locked(self) -> list[dict[str, Any]]:
        """Rebuild the port map from the NICs that are present right now.

        Invariants, whatever the previous install left behind:

        * every detected Ethernet NIC is a port — PCIe, USB, virtio, 1 or 64 of
          them; the count is never fixed,
        * the management interface is not a port and is never touched,
        * present ports are numbered ``Ethernet1..N`` contiguously in physical
          order, so there are no gaps and no ``Ethernet4096`` leftovers,
        * configuration travels with the hardware identity, so renumbering
          never moves a VLAN to a different NIC,
        * a NIC that appears later is attached to the switch automatically when
          ``attach_new_nics`` is on (the installer default).
        """
        self._migrate_port_names()
        self._prune_internal_ports()
        protected = set(netif.protected_interfaces())
        links = [
            link
            for link in netif.physical_links(include_virtual=self.allow_virtual, exclude={self.bridge})
            if link.name not in protected
        ]
        links.sort(key=_topology_key)
        # 0. Rows whose identity is a stale alias of a live NIC (0.1.4 collapsed
        #    several NICs behind one PCI bridge onto a single identity) would
        #    otherwise linger as a "not present" ghost next to the real port.
        self._prune_stale_aliases(links)
        # 1. Match every present NIC to its row: identity first (PCI/USB path),
        #    then MAC.  The MAC fallback keeps a NIC's port name when the kernel
        #    only reports a MAC identity (virtio, some hypervisor NICs).
        for link in links:
            identity = _link_identity(link)
            row = self.store.port_by_identity(identity) or self.store.port_by_mac(link.mac)
            if row is None:
                name = self._allocate_port_name()
                if name is None:
                    continue
                self.store.register_port(identity, name, link.name, link.kind, note=link.description, mac=link.mac)
                self.store.add_event("info", "ports", f"Discovered {name} -> {link.name} ({identity})")
                continue
            if row["identity"] != identity:
                self.store.update_port_identity(row["identity"], identity, link.name, link.mac)
            else:
                self.store.register_port(identity, row["name"], link.name, link.kind, mac=link.mac)
        # 2. Compact the numbering: Ethernet1..N in physical order, always.
        self._renumber_ports(links, protected)
        # Re-read discovery before publishing. Kernel naming is an explicit
        # boot/install action, never a side effect of this read path.
        links = [
            link
            for link in netif.physical_links(include_virtual=self.allow_virtual, exclude={self.bridge})
            if link.name not in protected
        ]
        links.sort(key=_topology_key)
        # 3. Publish the map and create interface config for new ports.
        self.ports = {}
        seen: set[str] = set()
        for link in links:
            identity = _link_identity(link)
            record = self.store.port_by_identity(identity)
            if not record:
                continue
            seen.add(identity)
            name = record["name"]
            record["link"] = link.to_dict()
            record["present"] = True
            record["protected"] = False
            self.ports[name] = record
            cfg_iface = self.config.interfaces.get(name)
            if cfg_iface is None:
                attach = bool(self.settings.get("attach_new_nics", True))
                self.config.interfaces[name] = InterfaceConfig(
                    name=name,
                    identity=identity,
                    linux_if=link.name,
                    attached=attach,
                    admin_up=attach,
                )
                self.config.interfaces[name].description = ""
            else:
                cfg_iface.identity = identity
                cfg_iface.linux_if = link.name
        for row in self.store.all_ports():
            if row["identity"] in seen:
                continue
            self.store.mark_port_present(row["identity"], False)
            record = dict(row)
            record["present"] = False
            record["protected"] = False
            self.ports[row["name"]] = record
        self.store.save_config("running", self.config, "sync-ports")
        return self.port_table()

    def _prune_stale_aliases(self, links: list[Any]) -> None:
        """Delete rows whose stored identity no longer exists but whose MAC is
        live under a different identity.

        Builds up to 0.1.4 keyed NICs behind a PCI bridge by the bridge address,
        so one row could stand for several adapters. After the endpoint-identity
        fix those rows match nothing; the NIC is registered fresh, and the old
        row would stay in the table as a phantom "not present" port.
        """
        present = {_link_identity(link) for link in links}
        by_mac = {link.mac.lower(): _link_identity(link) for link in links if link.mac}
        for row in self.store.all_ports():
            identity = row.get("identity") or ""
            if identity in present:
                continue
            live = by_mac.get((row.get("mac") or "").lower())
            if not live or live == identity:
                continue
            self.config.interfaces.pop(row["name"], None)
            self.store.delete_port(identity)
            self.store.add_event(
                "warning",
                "ports",
                f"Removed stale port alias {row['name']} ({identity}); NIC is now {live}",
            )

    def _prune_internal_ports(self) -> None:
        """Drop rows that were registered for bridge/internal interfaces.

        Older builds could mistake ``ovs-system``/``mms0`` for NICs (they carry a
        MAC), which polluted the port table with VIRTUAL entries.
        """
        for row in self.store.all_ports():
            linux = row.get("linux_if") or ""
            identity = row.get("identity") or ""
            bogus = netif.is_internal_link(linux) or linux == self.bridge
            if not bogus and identity.startswith("mac:") and not netif.NIC_NAME_RE.match(linux):
                bogus = True
            if not bogus:
                continue
            self.config.interfaces.pop(row["name"], None)
            self.store.delete_port(identity)
            self.store.add_event("warning", "ports", f"Removed non-NIC interface from port table: {linux}")

    def _renumber_ports(self, links: list[Any], protected: set[str]) -> None:
        """Compact present ports to ``Ethernet1..N`` in physical order.

        Logical names are recomputed on sync; kernel names are applied only by
        the explicit boot action. Tables from older builds (gaps, ``Ethernet4096``,
        duplicates) heals itself.  Interface configuration follows the hardware
        identity, never the number.
        """
        present: list[tuple[str, Any]] = []
        for link in links:
            identity = _link_identity(link)
            if link.name in protected or not self.store.port_by_identity(identity):
                continue
            present.append((identity, link))
        if not present:
            return
        present.sort(key=lambda item: _topology_key(item[1]))
        desired: dict[str, str] = {}
        taken: set[str] = set()
        for index, (identity, _link) in enumerate(present, start=1):
            desired[identity] = portnames.simple_name(index)
            taken.add(portnames.simple_name(index))
        # Rows for NICs that are not present keep their name unless it clashes;
        # clashing ones are parked on the next free number.
        next_free = len(present) + 1
        for row in self.store.all_ports():
            if row["identity"] in desired:
                continue
            if row["name"] not in taken:
                taken.add(row["name"])
                continue
            while portnames.simple_name(next_free) in taken:
                next_free += 1
            desired[row["identity"]] = portnames.simple_name(next_free)
            taken.add(portnames.simple_name(next_free))
            next_free += 1
        rows = {row["identity"]: row for row in self.store.all_ports()}
        changes = {
            identity: new_name
            for identity, new_name in desired.items()
            if identity in rows and rows[identity]["name"] != new_name
        }
        if not changes:
            return
        old_names = {identity: rows[identity]["name"] for identity in changes}
        self.store.renumber_ports(changes)
        # Move both saved and running config simultaneously (also LAG members).
        remap = {old_names[i]: name for i, name in changes.items()}
        startup = self.store.load_config('startup')
        for cfg in [self.config, startup]:
            if cfg is None:
                continue
            moved = {name: cfg.interfaces.pop(name) for name in remap if name in cfg.interfaces}
            for old, iface in moved.items():
                iface.name = remap[old]
                cfg.interfaces[iface.name] = iface
            for pc in cfg.port_channels.values():
                pc.members = [remap.get(member, member) for member in pc.members]
        if startup is not None:
            self.store.save_config('startup', startup, 'port-identity-migration')
            self.startup = startup

        self.store.add_event(
            "info",
            "ports",
            "Renumbered ports to physical order: "
            + ", ".join(f"{old_names[i]}->{n}" for i, n in sorted(changes.items(), key=lambda kv: kv[1])),
        )



    def _allocate_port_name(self) -> str | None:
        used = {row["name"] for row in self.store.all_ports()}
        used.update(self.config.interfaces.keys())
        for number in range(1, portnames.MAX_PORT_NUMBER + 1):
            candidate = portnames.simple_name(number)
            if candidate not in used:
                return candidate
        return None

    def _migrate_port_names(self) -> None:
        """Rename legacy ``Ethernet1/N`` ports to ``Ethernet1``, ``Ethernet2`` …

        Identities are never touched, so a rename cannot move a port onto a
        different physical NIC.
        """
        if not portnames.needs_migration(self.config, self.store):
            return
        renamed = portnames.migrate_store(self.store)
        changed = portnames.migrate_config(self.config)
        if changed:
            self.store.save_config("running", self.config, "migrate-names")
            self.store.add_event(
                "info", "ports", f"Renamed {renamed} switch port(s) to simple names (Ethernet1, Ethernet2, …)"
            )

    # --- helpers ---------------------------------------------------------
    def port_table(self) -> list[dict[str, Any]]:
        rows = []
        for name, record in sorted(self.ports.items(), key=_port_sort_key):
            iface = self.config.interfaces.get(name)
            rows.append(
                {
                    "name": name,
                    "linux_if": record.get("linux_if"),
                    "identity": record.get("identity"),
                    "mac": record.get("mac") or (record.get("identity", "").split(":", 1)[1]
                                                 if str(record.get("identity", "")).startswith("mac:")
                                                 else ""),
                    "kind": record.get("kind"),
                    "present": bool(record.get("present", True)),
                    "protected": bool(record.get("protected")),
                    "description": iface.description if iface else "",
                    "attached": iface.attached if iface else False,
                    "admin_up": iface.admin_up if iface else True,
                    "mode": iface.mode if iface else "access",
                    "access_vlan": iface.access_vlan if iface else 1,
                    "trunk_allowed": iface.trunk_allowed if iface else [1],
                    "native_vlan": iface.native_vlan if iface else None,
                    "mtu": iface.mtu if iface else 1500,
                    "channel_group": iface.channel_group if iface else None,
                    "link": record.get("link"),
                }
            )
        return rows

    def resolve_linux_if(self, name: str) -> str | None:
        iface = self.config.interfaces.get(name)
        if iface and iface.linux_if:
            return iface.linux_if
        record = self.store.port_by_name(name)
        return record.get("linux_if") if record else None

    # --- desired state ---------------------------------------------------
    def _link_states(self) -> dict[str, bool]:
        """Administrative link flags; unknown on error so we never guess."""
        try:
            return self.ovs.link_admin_states()
        except OvsError as exc:
            log.warning("cannot read Linux link state", extra={"data": {"error": str(exc)}})
            return {}

    @staticmethod
    def _desired_members(desired: dict[str, dict[str, Any]]) -> list[str]:
        return sorted({member for want in desired.values() for member in want["interfaces"]})

    def _down_link_ops(self, desired: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        states = self._link_states()
        return [
            {"action": "link-up", "target": member, "detail": "set Linux administrative state up"}
            for member in self._desired_members(desired)
            if states.get(member) is False
        ]

    def _ensure_links_up(self, desired: dict[str, dict[str, Any]]) -> None:
        states = self._link_states()
        for member in self._desired_members(desired):
            if states.get(member) is False:
                self.ovs.set_link_up(member)

    def desired_ports(self, cfg: SwitchConfig) -> dict[str, dict[str, Any]]:
        desired: dict[str, dict[str, Any]] = {}
        channels: dict[int, str] = {}
        for pc in cfg.port_channels.values():
            tag, trunks = _vlan_params_for_channel(cfg, pc)
            members = []
            for member in pc.members:
                iface = cfg.interfaces.get(member)
                if not iface or not iface.attached or not iface.admin_up:
                    continue
                if iface.mode == "trunk" and not iface.trunk_allowed:
                    continue
                linux = self._linux_for(cfg, iface)
                if linux:
                    members.append(linux)
            if len(members) >= 2:
                desired[pc.name] = {
                    "port": pc.name,
                    "interfaces": members,
                    "type": "bond",
                    "tag": tag,
                    "trunks": trunks,
                    "lacp": pc.mode,
                    "bond_mode": pc.lb,
                    "mtu": None,
                    "logical": f"Port-Channel{pc.id}",
                    "vlan_mode": _vlan_mode(_channel_representative(cfg, pc)),
                }
                channels[pc.id] = pc.name
        for iface in cfg.interfaces.values():
            if not iface.attached or not iface.admin_up:
                continue
            # OVS empty trunks means ALL, never NONE. Block none by removing
            # forwarding membership, while preserving the attachment intent.
            if iface.mode == "trunk" and not iface.trunk_allowed:
                continue
            if iface.channel_group is not None and iface.channel_group in channels:
                continue
            linux = self._linux_for(cfg, iface)
            if not linux:
                continue
            tag, trunks = _vlan_params(cfg, iface)
            desired[linux] = {
                "port": linux,
                "interfaces": [linux],
                "type": "",
                "tag": tag,
                "trunks": trunks,
                "lacp": "",
                "bond_mode": "",
                "mtu": iface.mtu if iface.mtu != 1500 else None,
                "logical": iface.name,
                "vlan_mode": _vlan_mode(iface),
            }
        return desired

    @staticmethod
    def _linux_for(cfg: SwitchConfig, iface: InterfaceConfig) -> str | None:
        if iface.linux_if:
            return iface.linux_if
        return None

    def plan(self, cfg: SwitchConfig) -> list[dict[str, Any]]:
        desired = self.desired_ports(cfg)
        try:
            actual = self.ovs.port_details() if self.ovs.installed else {}
        except OvsError:
            actual = {}
        ops: list[dict[str, Any]] = []
        ops.extend(self._down_link_ops(desired))
        if not self.ovs.bridge_exists():
            ops.append({"action": "create-bridge", "target": self.bridge, "detail": "create OVS bridge"})
        for name, want in sorted(desired.items()):
            have = actual.get(name)
            if not have:
                ops.append(
                    {
                        "action": "add-bond" if want["type"] == "bond" else "add-port",
                        "target": name,
                        "detail": f"{want['logical']}: mode={want['vlan_mode']}"
                        + (f" vlan={want['tag']}" if want["tag"] else "")
                        + (f" trunks={format_vlan_list(want['trunks'])}" if want["trunks"] else ""),
                        "interfaces": want["interfaces"],
                    }
                )
                continue
            if have.get("tag") != want["tag"] or sorted(have.get("trunks") or []) != sorted(want["trunks"]):
                ops.append(
                    {
                        "action": "update-vlans",
                        "target": name,
                        "detail": f"tag {have.get('tag')} -> {want['tag']}, trunks {format_vlan_list(have.get('trunks') or [])} -> {format_vlan_list(want['trunks'])}",
                    }
                )
            if want["mtu"] and have.get("mtu") != want["mtu"]:
                ops.append({"action": "set-mtu", "target": name, "detail": f"mtu -> {want['mtu']}"})
            if have.get("vlan_mode", "access" if have.get("tag") is not None else "trunk") != want["vlan_mode"]:
                ops.append({"action": "update-vlan-mode", "target": name, "detail": want["vlan_mode"]})
        managed = {name for name, info in actual.items() if (info.get("external_ids") or {}).get("makemeswitch-managed") == "true"}
        bond_members = {
            member for want in desired.values() if want["type"] == "bond" for member in want["interfaces"]
        }
        for name in sorted(set(actual) - set(desired) - bond_members):
            if name in managed or name in {i.linux_if for i in cfg.interfaces.values() if i.linux_if}:
                ops.append({"action": "del-port", "target": name, "detail": "detach from bridge"})
        mirror_names = set(self.ovs.mirror_names()) if cfg.mirrors else set()
        if cfg.mirrors and mirror_names != {f"span{m.id}" for m in cfg.mirrors.values()}:
            ops.append({"action": "configure-mirrors", "target": self.bridge, "detail": f"{len(cfg.mirrors)} session(s)"})
        try:
            state = self.ovs.stp_state()
        except Exception:  # noqa: BLE001
            state = {}
        desired_stp = {
            "enabled": cfg.stp.enabled and cfg.stp.mode != "none",
            "mode": cfg.stp.mode if cfg.stp.mode != "none" else "none",
            "priority": cfg.stp.priority,
        }
        if state != desired_stp:
            ops.append(
                {
                    "action": "configure-stp",
                    "target": self.bridge,
                    "detail": f"enabled={desired_stp['enabled']} mode={desired_stp['mode']} priority={desired_stp['priority']}",
                }
            )
        l3_active = bool(cfg.svis or cfg.routes or cfg.ip_routing or self._l3_sig)
        if l3_active and self.l3.signature(cfg) != self._l3_sig:
            ops.append(
                {
                    "action": "configure-l3",
                    "target": self.bridge,
                    "detail": f"{len(cfg.svis)} SVI(s), {len(cfg.routes)} static route(s), routing={'on' if cfg.ip_routing else 'off'}",
                }
            )
        return ops

    # --- apply -----------------------------------------------------------
    def _configure_remote_syslog(self) -> None:
        """(Re)point the remote syslog sender at the current configuration."""
        try:
            from makemeswitch.core import logging_remote

            logging_remote.configure(getattr(self.config, "syslog", None), self.config.hostname)
        except Exception:  # noqa: BLE001 - never block startup on logging
            pass

    def apply(
        self,
        new_config: SwitchConfig,
        user: str = "cli",
        confirm: bool = False,
        dry_run: bool = False,
    ) -> ApplyResult:
        errors = new_config.validate()
        if errors:
            raise ConfigError("; ".join(errors))
        preview = self.plan(new_config)
        newly_attached = [
            iface.name
            for name, iface in new_config.interfaces.items()
            if iface.attached and (name not in self.config.interfaces or not self.config.interfaces[name].attached)
        ]
        if dry_run:
            return ApplyResult(changed=bool(preview), ops=preview, config=new_config)
        if newly_attached and not confirm:
            names = ", ".join(newly_attached)
            raise NeedsConfirmation(
                f"Attaching {names} will enslave the physical NIC(s) into OVS bridge {self.bridge}. "
                "Host networking on those NICs stops working until detached.",
                {"ops": preview, "newly_attached": newly_attached, "config": new_config.to_dict()},
            )
        if not preview:
            self.config = new_config
            self._configure_remote_syslog()
            self.store.save_config("running", new_config, user)
            return ApplyResult(changed=False, ops=[], config=new_config)
        snapshot = self.ovs.snapshot()
        try:
            self.reconcile(new_config)
        except Exception as exc:
            log.error("apply failed; rolling back", extra={"data": {"error": str(exc), "traceback": traceback.format_exc()}})
            try:
                self.ovs.restore(snapshot)
                self.store.add_event("error", "engine", f"Apply failed, rolled back OVS state: {type(exc).__name__}: {exc}")
            except Exception as restore_exc:  # pragma: no cover
                log.error("rollback failed", extra={"data": {"error": str(restore_exc)}})
                self.store.add_event("critical", "engine", f"Apply and rollback both failed: {restore_exc}")
            raise
        self.config = new_config
        self.startup = self.startup or new_config.copy()
        self.store.save_config("running", new_config, user)
        self.store.add_event("info", "config", f"Configuration applied by {user}: {len(preview)} change(s)")
        return ApplyResult(changed=True, ops=preview, config=new_config)

    def reconcile(self, cfg: SwitchConfig, raise_on_error: bool = True) -> list[dict[str, Any]]:
        """Make OVS match ``cfg`` (idempotent).

        Ports are created while RSTP is temporarily paused and RSTP is enabled
        afterwards.  Creating a port on a live RSTP bridge leaves it (and the
        local/SVI ports) blocked for the 15 s forwarding delay; adding it
        before RSTP starts means edge ports forward immediately.
        """
        desired = self.desired_ports(cfg)
        want_stp = cfg.stp.enabled and cfg.stp.mode != "none"
        actual = self.ovs.port_details()
        # Bring the bridge up with RSTP paused; enabled again at the end of the
        # apply so that freshly created ports (and their SVI/internal peers)
        # start in forwarding instead of waiting the 15 s STP forwarding delay.
        self.ovs.ensure_bridge(False, cfg.stp.mode, cfg.stp.priority)
        for name, want in sorted(desired.items()):
            if want["type"] == "bond":
                self.ovs.add_bond(want["port"], want["interfaces"], want["lacp"], want["bond_mode"], want["tag"], want["trunks"])
            else:
                self.ovs.add_port(name, want["tag"], want["trunks"], rstp_edge=True)
                if want["mtu"]:
                    self.ovs.set_port_mtu(name, int(want["mtu"]))
            have = self.ovs.port_details().get(want["port"])
            if have is None:
                raise OvsError(f"OVS did not accept port {want['port']}")
            if have.get("tag") != want["tag"] or sorted(have.get("trunks") or []) != sorted(want["trunks"]):
                self.ovs.set_port_vlans(want["port"], want["tag"], want["trunks"])
            self.ovs.set_port_vlan_mode(want["port"], want["vlan_mode"])
        configured_linux = {i.linux_if for i in cfg.interfaces.values() if i.linux_if}
        # Enslaving does not guarantee administrative UP, and an operator's
        # `ip link set down` must be corrected by the next apply.  Runs after
        # the ports exist so add-port side effects are covered too.
        self._ensure_links_up(desired)
        # Bond members live inside their bond port: they are not stale ports.
        bond_members = {
            member for want in desired.values() if want["type"] == "bond" for member in want["interfaces"]
        }
        for name in set(actual) - set(desired) - bond_members:
            info = actual[name]
            if (info.get("external_ids") or {}).get("makemeswitch-managed") == "true" or name in configured_linux:
                self.ovs.del_port(name)
        self.ovs.clear_mirrors()
        if cfg.mirrors:
            for mirror in cfg.mirrors.values():
                dest = cfg.interfaces.get(mirror.destination or "")
                dest_linux = dest.linux_if if dest else None
                if not dest_linux:
                    continue
                sources = [
                    cfg.interfaces[s].linux_if
                    for s in mirror.source
                    if cfg.interfaces.get(s) and cfg.interfaces[s].linux_if
                ]
                if sources:
                    try:
                        self.ovs.add_mirror(f"span{mirror.id}", [s for s in sources if s], dest_linux, mirror.direction)
                    except OvsError as exc:
                        self.store.add_event("warning", "mirror", f"Mirror {mirror.id} failed: {exc}")
        self.l3.apply(cfg, self.ovs)
        self._l3_sig = self.l3.signature(cfg)
        # RSTP last: every port (including SVI internal ports) already exists,
        # so edge ports forward immediately instead of waiting fwd-delay.
        self.ovs.configure_stp(want_stp, cfg.stp.mode, cfg.stp.priority)
        return self.plan(cfg)

    # --- configuration lifecycle -----------------------------------------
    def save_startup(self, user: str = "cli") -> None:
        self.store.save_config("startup", self.config, user)
        self.startup = self.config.copy()
        self.store.add_event("info", "config", f"Startup configuration saved by {user}")

    def load_startup(self, user: str = "cli") -> ApplyResult:
        if self.startup is None:
            raise ConfigError("no startup configuration present")
        result = self.apply(self.startup.copy(), user=user, confirm=True)
        return result

    def rollback(self, history_id: int | None = None, user: str = "cli") -> ApplyResult:
        entries = self.store.history(limit=20)
        if not entries:
            raise ConfigError("no configuration history available")
        if history_id is None:
            # skip the most recent entry (it is the current config)
            target_id = entries[1]["id"] if len(entries) > 1 else entries[0]["id"]
        else:
            target_id = history_id
        cfg = self.store.history_config(target_id)
        if cfg is None:
            raise ConfigError(f"history entry {target_id} not found")
        result = self.apply(cfg, user=user, confirm=True)
        self.store.add_event("warning", "config", f"Rollback to history entry {target_id} by {user}")
        return result

    def diff(self) -> list[dict[str, Any]]:
        """Compare the running config with live OVS state."""
        desired = self.desired_ports(self.config)
        try:
            actual = self.ovs.port_details()
        except OvsError as exc:
            return [{"severity": "error", "message": f"OVS unreachable: {exc}"}]
        problems = []
        for name, want in desired.items():
            have = actual.get(want["port"])
            if not have:
                problems.append({"severity": "warning", "port": want["logical"], "message": f"port {want['port']} missing from OVS"})
                continue
            if have.get("tag") != want["tag"]:
                problems.append({"severity": "warning", "port": want["logical"], "message": f"tag drift: expected {want['tag']}, found {have.get('tag')}"})
            if sorted(have.get("trunks") or []) != sorted(want["trunks"]):
                problems.append({"severity": "warning", "port": want["logical"], "message": "trunk VLAN list drift"})
        for name in set(actual) - set(desired):
            problems.append({"severity": "info", "port": name, "message": "port present in OVS but not in running config"})
        return problems

    def configs_equal(self) -> bool:
        return self.store.load_config("startup") is not None and self.config.to_dict() == (self.store.load_config("startup") or SwitchConfig()).to_dict()

    def unsaved_changes(self) -> bool:
        return self.config.to_dict() != (self.startup or SwitchConfig()).to_dict()

    # --- reporting -------------------------------------------------------
    def interface_snapshot(self) -> list[dict[str, Any]]:
        try:
            stats = self.ovs.interface_stats()
        except OvsError:
            stats = {}
        rows = []
        for port in self.port_table():
            link = port.get("link") or {}
            linux = port.get("linux_if") or ""
            live = netif.link_by_name(linux) if linux else None
            if live:
                link = live.to_dict()
            port_stats = stats.get(linux, {})
            iface = self.config.interfaces.get(port["name"])
            if iface and not iface.admin_up:
                status = "administratively down"
            elif not port["present"]:
                status = "not present"
            elif link.get("link_up"):
                status = "up"
            elif iface and iface.attached and live:
                status = "down"
            else:
                status = "not connected" if port["present"] else "not present"
            rows.append(
                {
                    **port,
                    "link": link,
                    "status": status,
                    "stats": port_stats,
                    "vlans": _member_vlans(self.config, port["name"]),
                }
            )
        return rows

    def traffic_rates(self) -> dict[str, dict[str, float]]:
        """Per-port byte/packet rates since the previous sample."""
        now = time.time()
        try:
            stats = self.ovs.interface_stats()
        except OvsError:
            return {}
        rates: dict[str, dict[str, float]] = {}
        for port, values in stats.items():
            previous = self._last_stats.get(port)
            if previous and (now - previous[0]) >= 4.0:
                # OVS refreshes interface counters roughly every few seconds; a
                # 4 s window keeps the rate meaningful instead of oscillating to 0.
                elapsed = max(now - previous[0], 1e-3)
                rates[port] = {
                    "rx_bps": max((values.get("rx_bytes", 0) - previous[1].get("rx_bytes", 0)) * 8 / elapsed, 0),
                    "tx_bps": max((values.get("tx_bytes", 0) - previous[1].get("tx_bytes", 0)) * 8 / elapsed, 0),
                    "rx_pps": max((values.get("rx_packets", 0) - previous[1].get("rx_packets", 0)) / elapsed, 0),
                    "tx_pps": max((values.get("tx_packets", 0) - previous[1].get("tx_packets", 0)) / elapsed, 0),
                }
            elif port in self._last_rates:
                # Too soon for a meaningful delta: reuse the last computed rate so
                # concurrent API/WebSocket readers do not see fake zeros.
                rates[port] = self._last_rates[port]
                continue
            else:
                rates[port] = {"rx_bps": 0, "tx_bps": 0, "rx_pps": 0, "tx_pps": 0}
            self._last_stats[port] = (now, values)
            self._last_rates[port] = rates[port]
        return rates

    def mac_table(self) -> list[dict[str, Any]]:
        try:
            entries = self.ovs.mac_table()
            names = self.ovs.port_names_by_number()
        except OvsError:
            return []
        l2_to_logical = {i.linux_if: i.name for i in self.config.interfaces.values() if i.linux_if}
        result = []
        for entry in entries:
            port_name = names.get(entry["port"], str(entry["port"]))
            vlan = self.config.vlans.get(entry["vlan"])
            result.append(
                {
                    "mac": entry["mac"],
                    "vlan": entry["vlan"],
                    "vlan_name": vlan.name if vlan else "",
                    "ovs_port": entry["port"],
                    "port": l2_to_logical.get(port_name, port_name),
                    "linux_if": port_name,
                    "age": entry["age"],
                    "type": "dynamic",
                }
            )
        return sorted(result, key=lambda e: (e["vlan"], e["mac"]))

    def system_info(self) -> dict[str, Any]:
        uptime = 0.0
        try:
            uptime = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pass
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
        meminfo = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                meminfo[key] = int(value.split()[0]) * 1024 if value.split() else 0
        except (OSError, ValueError):
            pass
        total_mem = meminfo.get("MemTotal", 0) or 1
        available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        temperature = None
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            raw = (zone / "temp").read_text().strip() if (zone / "temp").exists() else ""
            if raw.lstrip("-").isdigit():
                value = int(raw) / 1000.0
                if -20 < value < 150:
                    temperature = round(value, 1)
                    break
        os_name = platform.platform()
        try:
            os_release = dict(
                line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line
            )
            os_name = os_release.get("PRETTY_NAME", os_name).strip('"')
        except OSError:
            pass
        return {
            "hostname": socket.gethostname(),
            "config_hostname": self.config.hostname,
            "makemeswitch_version": __version__,
            "project": PROJECT_NAME,
            "platform": os_name,
            "kernel": platform.release(),
            "python": platform.python_version(),
            "uptime_seconds": uptime,
            "cpu_count": os.cpu_count() or 1,
            "load": [round(v, 2) for v in load],
            "cpu_percent": min(round(load[0] / max(os.cpu_count() or 1, 1) * 100, 1), 100.0),
            "memory_total": total_mem,
            "memory_available": available,
            "memory_percent": round((1 - available / total_mem) * 100, 1),
            "temperature_c": temperature,
            "bridge": self.bridge,
            "ovs": self.ovs.health(),
            "frr": self.frr.status(),
            "lldp": self.lldp_status(),
            "ports_total": len(self.ports),
            "ports_active": sum(1 for p in self.interface_snapshot() if p["status"] == "up"),
            "vlans": len(self.config.vlans),
            "unsaved_changes": self.unsaved_changes(),
        }

    def lldp_status(self) -> dict[str, Any]:
        import shutil

        if not shutil.which("lldpcli"):
            return {"installed": False, "enabled": self.config.lldp_enabled, "neighbors": [], "detail": "lldpd not installed"}
        import subprocess

        if not self.config.lldp_enabled:
            return {"installed": True, "enabled": False, "neighbors": [], "detail": "LLDP disabled in configuration"}
        try:
            proc = subprocess.run(["lldpcli", "show", "neighbors", "-f", "json"], capture_output=True, text=True, timeout=8, check=False)
            if proc.returncode != 0:
                return {"installed": True, "enabled": True, "neighbors": [], "detail": (proc.stderr or "lldpcli failed").strip()}
            import json

            data = json.loads(proc.stdout or "{}")
            neighbors = []
            for local_if, payload in (data.get("lldp") or {}).get("interface", {}).items():
                for entry in payload.get("neighbors", {}).values() if isinstance(payload.get("neighbors"), dict) else payload.get("neighbors", []):
                    neighbors.append(
                        {
                            "local_interface": local_if,
                            "chassis": entry.get("chassis", {}).get("name", {}).get("value") if isinstance(entry.get("chassis"), dict) else None,
                            "port": entry.get("port", {}).get("id", {}).get("value") if isinstance(entry.get("port"), dict) else None,
                            "ttl": entry.get("ttl", {}).get("ttl", None),
                        }
                    )
            return {"installed": True, "enabled": True, "neighbors": neighbors, "detail": ""}
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return {"installed": True, "enabled": True, "neighbors": [], "detail": str(exc)}

    def interface_detail(self, name: str) -> dict[str, Any]:
        for row in self.interface_snapshot():
            if row["name"].lower() == name.lower():
                return row
        raise ConfigError(f"interface {name} not found")


def _member_vlans(cfg: SwitchConfig, name: str) -> dict[str, Any]:
    iface = cfg.interfaces.get(name)
    if not iface or not iface.attached:
        return {"mode": "unassigned", "untagged": [], "tagged": [], "native": None, "trunk_all": False}
    if iface.mode == "access":
        return {
            "mode": "access",
            "untagged": [iface.access_vlan],
            "tagged": [],
            "native": iface.access_vlan,
            "trunk_all": False,
        }
    native = iface.native_vlan
    if iface.trunk_all:
        # An all-trunk carries every VLAN; listing 4094 ids would bloat the API,
        # so report the configured VLANs plus an explicit flag for the UI.
        tagged = [int(v) for v in cfg.vlans if int(v) != native]
        return {"mode": "trunk", "untagged": [native] if native else [], "tagged": tagged, "native": native, "trunk_all": True}
    tagged = [v for v in iface.trunk_allowed if v != native]
    return {"mode": "trunk", "untagged": [native] if native else [], "tagged": tagged, "native": native, "trunk_all": False}


def _vlan_params(cfg: SwitchConfig, iface: InterfaceConfig) -> tuple[int | None, list[int]]:
    if iface.mode == "access":
        return iface.access_vlan, []
    native = iface.native_vlan
    allowed = [] if iface.trunk_all else [v for v in iface.trunk_allowed if v != native]
    return native, allowed


def _vlan_mode(iface: InterfaceConfig | None) -> str:
    if iface is None or iface.mode == "access":
        return "access"
    return "native-untagged" if iface.native_vlan is not None else "trunk"


def _channel_representative(cfg: SwitchConfig, pc) -> InterfaceConfig | None:
    """Member whose L2 settings define the bond (prefer a forwarding one)."""
    members = [cfg.interfaces[m] for m in pc.members if m in cfg.interfaces]
    if not members:
        return None
    eligible = [i for i in members if i.mode != "trunk" or i.trunk_allowed]
    return (eligible or members)[0]


def _vlan_params_for_channel(cfg: SwitchConfig, pc) -> tuple[int | None, list[int]]:
    first = _channel_representative(cfg, pc)
    if first is None:
        return None, []
    return _vlan_params(cfg, first)


def _link_identity(link: Any) -> str:
    """Stable identity for a NIC: PCI/USB topology, else its MAC."""
    return getattr(link, "identity", None) or f"mac:{getattr(link, 'mac', '')}"


def _topology_key(link: Any) -> tuple:
    """Physical order of a NIC: PCI bus/slot first, then USB topology.

    Used so that ``Ethernet1`` is the first port on the motherboard, not
    whichever interface the kernel happened to enumerate first.
    """
    identity = getattr(link, "identity", None) or ""
    if identity.startswith("pci:"):
        numbers = [int(part, 16) for part in re.findall(r"[0-9a-fA-F]+", identity[4:])]
        while len(numbers) < 4:
            numbers.insert(0, 0)
        return (0, *numbers[:4])
    if identity.startswith("usb:"):
        path = [int(part) for part in re.findall(r"\d+", identity[4:])]
        return (1, *path)
    return (2, 0, 0, 0, getattr(link, "mac", "") or "")


def _port_sort_key(item: tuple[str, dict[str, Any]]):
    """Order Ethernet1, Ethernet2 … Ethernet10 numerically (not lexically)."""
    return portnames.sort_key(item[0])
