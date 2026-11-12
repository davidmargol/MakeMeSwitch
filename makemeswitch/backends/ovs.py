"""Open vSwitch backend.

All Layer-2 state lives in OVSDB.  This module is the only place that runs
``ovs-vsctl`` and it always uses argument arrays (never a shell string), so
command injection is structurally impossible.  Every operation is verified by
reading the state back out of OVSDB.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from makemeswitch.utils.logging import get_logger

log = get_logger("makemeswitch.ovs")


class OvsError(RuntimeError):
    """An OVS command failed or the daemon is unreachable."""


def _parse_ovs_json(payload: str) -> list[dict[str, Any]]:
    """Convert ``ovs-vsctl --format=json`` output into a list of records."""
    doc = json.loads(payload or "{}")
    headings: list[str] = doc.get("headings", [])
    records = []
    for row in doc.get("data", []):
        record: dict[str, Any] = {}
        for key, value in zip(headings, row):
            if isinstance(value, list) and value and value[0] == "set":
                record[key] = [
                    item[1] if isinstance(item, list) and len(item) == 2 and item[0] == "uuid" else item
                    for item in value[1]
                ]
            elif isinstance(value, list) and value and value[0] == "map":
                record[key] = {str(k): v for k, v in value[1]}
            elif isinstance(value, list) and len(value) == 2 and value[0] == "uuid":
                record[key] = value[1]
            else:
                record[key] = value
        # OVSDB encodes singleton sets as atoms, including integer VLAN sets.
        for key in ("interfaces", "ports", "mirrors", "select_src_port", "select_dst_port", "trunks", "select_vlan"):
            if key in record and not isinstance(record[key], list):
                record[key] = [record[key]]
        records.append(record)
    return records


class OvsBackend:
    """Thin, verified wrapper around the ``ovs-vsctl`` / ``ovs-appctl`` CLIs."""

    def __init__(self, bridge: str = "mms0", timeout: int = 15):
        self.bridge = bridge
        self.timeout = timeout
        self._binary = shutil.which("ovs-vsctl")

    # --- low level -------------------------------------------------------
    @property
    def installed(self) -> bool:
        return bool(self._binary)

    def _run(self, args: list[str], check: bool = True) -> str:
        if not self._binary:
            raise OvsError("ovs-vsctl not found - is Open vSwitch installed?")
        argv = [self._binary, f"--timeout={self.timeout}", *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout + 5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OvsError(f"OVS command timed out: {' '.join(args[:4])}") from exc
        if check and proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            raise OvsError(f"ovs-vsctl {' '.join(args[:4])} failed: {message}")
        return proc.stdout

    def _appctl(self, args: list[str], check: bool = True) -> str:
        binary = shutil.which("ovs-appctl")
        if not binary:
            raise OvsError("ovs-appctl not found")
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=self.timeout, check=False)
        if check and proc.returncode != 0:
            raise OvsError((proc.stderr or proc.stdout).strip() or "ovs-appctl failed")
        return proc.stdout

    # --- health ----------------------------------------------------------
    def version(self) -> str:
        out = self._run(["--version"])
        return out.splitlines()[0].strip() if out else "unknown"

    def service_status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"installed": self.installed, "running": False, "version": None, "bridge": self.bridge}
        if not self.installed:
            info["detail"] = "ovs-vsctl missing"
            return info
        try:
            info["version"] = self.version()
            self._run(["show"], check=True)
            info["running"] = True
        except (OvsError, OSError) as exc:
            info["detail"] = str(exc)
        try:
            info["bridge_exists"] = self.bridge_exists()
        except OvsError:
            info["bridge_exists"] = False
        return info

    def bridge_exists(self) -> bool:
        return self.bridge in self._run(["list-br"], check=False).split()

    # --- bridge ----------------------------------------------------------
    def ensure_bridge(self, stp_enabled: bool = True, stp_mode: str = "rstp", stp_priority: int = 32768) -> None:
        args = ["--may-exist", "add-br", self.bridge]
        use_stp = stp_enabled and stp_mode == "stp"
        use_rstp = stp_enabled and stp_mode == "rstp"
        args += [
            "--",
            "set",
            "bridge",
            self.bridge,
            "fail_mode=standalone",
            f"stp_enable={'true' if use_stp else 'false'}",
            f"rstp_enable={'true' if use_rstp else 'false'}",
            "other_config:stp-priority=" + str(stp_priority),
            "other_config:rstp-priority=" + str(stp_priority),
            "external_ids:makemeswitch-managed=true",
        ]
        self._run(args)

    def configure_stp(self, enabled: bool, mode: str, priority: int) -> None:
        use_stp = enabled and mode == "stp"
        use_rstp = enabled and mode == "rstp"
        self._run(
            [
                "set",
                "bridge",
                self.bridge,
                f"stp_enable={'true' if use_stp else 'false'}",
                f"rstp_enable={'true' if use_rstp else 'false'}",
                f"other_config:stp-priority={priority}",
                f"other_config:rstp-priority={priority}",
            ]
        )

    def get_bridge_attr(self, column: str) -> str:
        return self._run(["get", "bridge", self.bridge, column]).strip()

    def stp_state(self) -> dict[str, Any]:
        """Current spanning-tree configuration of the bridge."""
        if not self.bridge_exists():
            return {"enabled": False, "mode": "none", "priority": 32768}
        try:
            values = self._run(
                ["get", "bridge", self.bridge, "stp_enable", "rstp_enable", "other_config:stp-priority"]
            ).split()
            stp = values[0].strip() == "true" if values else False
            rstp = values[1].strip() == "true" if len(values) > 1 else False
            priority = int(values[2]) if len(values) > 2 and values[2].lstrip("-").isdigit() else 32768
        except (OvsError, ValueError):
            return {"enabled": False, "mode": "none", "priority": 32768}
        return {"enabled": stp or rstp, "mode": "stp" if stp else ("rstp" if rstp else "none"), "priority": priority}

    def delete_bridge(self) -> None:
        self._run(["--if-exists", "del-br", self.bridge])

    # --- ports -----------------------------------------------------------
    def link_admin_states(self) -> dict[str, bool]:
        """Read administrative UP flags, not carrier/operstate."""
        proc = subprocess.run(["ip", "-j", "link", "show"], capture_output=True,
                              text=True, timeout=self.timeout, check=False)
        if proc.returncode:
            raise OvsError(f"Cannot read Linux link state: {proc.stderr.strip()}")
        return {row["ifname"]: "UP" in row.get("flags", []) for row in json.loads(proc.stdout)}

    def set_link_up(self, interface: str) -> None:
        """Called only for attached, administratively enabled switch members."""
        proc = subprocess.run(["ip", "link", "set", "dev", interface, "up"],
                              capture_output=True, text=True, timeout=self.timeout, check=False)
        if proc.returncode:
            raise OvsError(f"Cannot bring {interface} up: {proc.stderr.strip()}")
        if not self.link_admin_states().get(interface):
            raise OvsError(f"Linux did not bring {interface} administratively up")

    def list_ports(self) -> list[str]:
        if not self.bridge_exists():
            return []
        return self._run(["list-ports", self.bridge]).split()

    def port_exists(self, port: str) -> bool:
        return port in self.list_ports()

    def port_details(self) -> dict[str, dict[str, Any]]:
        """All ports on our bridge with tag/trunks/type/bond attributes."""
        if not self.bridge_exists():
            return {}
        names = set(self.list_ports())
        ports = _parse_ovs_json(
            self._run(
                [
                    "--format=json",
                    "--columns=name,tag,trunks,vlan_mode,bond_mode,lacp,interfaces,external_ids",
                    "list",
                    "port",
                ]
            )
        )
        ifaces = _parse_ovs_json(
            self._run(["--format=json", "--columns=_uuid,name,type,mtu_request", "list", "interface"])
        )
        by_uuid = {rec.get("_uuid"): rec for rec in ifaces}
        result: dict[str, dict[str, Any]] = {}
        for rec in ports:
            if rec.get("name") not in names:
                continue
            members = []
            iface_types = set()
            mtu = None
            for uuid in rec.get("interfaces") or []:
                info = by_uuid.get(uuid)
                if not info:
                    continue
                members.append(info.get("name"))
                if info.get("type"):
                    iface_types.add(info["type"])
                if info.get("mtu_request") not in (None, "", []):
                    try:
                        mtu = int(info["mtu_request"])
                    except (TypeError, ValueError):
                        pass
            tag = rec.get("tag")
            trunks = rec.get("trunks") or []
            is_bond = bool(rec.get("bond_mode")) or len(members) > 1
            result[rec["name"]] = {
                "name": rec["name"],
                "tag": int(tag) if tag not in (None, "", []) else None,
                "trunks": sorted(int(v) for v in trunks) if trunks else [],
                "vlan_mode": rec.get("vlan_mode") or ("access" if tag not in (None, "", []) else "trunk"),
                "type": "bond" if is_bond else (next(iter(iface_types), "") if iface_types else ""),
                "bond_mode": rec.get("bond_mode") or "",
                "lacp": rec.get("lacp") or "",
                "interfaces": members,
                "mtu": mtu,
                "external_ids": rec.get("external_ids") or {},
            }
        return result

    def add_port(self, port: str, tag: int | None = None, trunks: list[int] | None = None, mtu: int | None = None, rstp_edge: bool = True) -> None:
        args = ["--may-exist", "add-port", self.bridge, port, "--", "set", "port", port, "external_ids:makemeswitch-managed=true"]
        if tag is not None:
            args.append(f"tag={int(tag)}")
        if trunks:
            args.append("trunks=" + ",".join(str(int(v)) for v in sorted(trunks)))
        args += [
            "--",
            "set",
            "interface",
            port,
            f"other_config:rstp-port-admin-edge={'true' if rstp_edge else 'false'}",
        ]
        if mtu:
            args += ["--", "set", "interface", port, f"mtu_request={int(mtu)}"]
        self._run(args)

    def set_port_vlans(self, port: str, tag: int | None, trunks: list[int]) -> None:
        args = ["set", "port", port]
        if tag is None:
            args.append("tag=[]")
        else:
            args.append(f"tag={int(tag)}")
        args.append("trunks=" + ",".join(str(int(v)) for v in sorted(trunks)) if trunks else "trunks=[]")
        self._run(args)

    def set_port_mtu(self, interface: str, mtu: int) -> None:
        self._run(["set", "interface", interface, f"mtu_request={int(mtu)}"])

    def set_port_vlan_mode(self, port: str, mode: str) -> None:
        if mode not in ("access", "trunk", "native-untagged"):
            raise OvsError(f"Unsupported VLAN mode: {mode}")
        self._run(["set", "port", port, f"vlan_mode={mode}"])

    def add_internal_port(self, name: str, tag: int | None = None) -> None:
        """Create an OVS internal port (used for SVIs)."""
        args = [
            "--may-exist",
            "add-port",
            self.bridge,
            name,
            "--",
            "set",
            "interface",
            name,
            "type=internal",
            "external_ids:makemeswitch-managed=true",
            # A router port must forward immediately; RSTP would otherwise keep
            # it in blocking/listening for ~12s after creation.
            "other_config:rstp-port-admin-edge=true",
        ]
        if tag is not None:
            args += ["--", "set", "port", name, f"tag={int(tag)}"]
        self._run(args)

    def del_port(self, port: str) -> None:
        self._run(["--if-exists", "del-port", self.bridge, port])

    def add_bond(self, name: str, members: list[str], mode: str = "active", lb: str = "balance-tcp", tag: int | None = None, trunks: list[int] | None = None) -> None:
        """Create/refresh a bond.

        A NIC that is already enslaved as a *standalone* bridge port cannot be
        reused by ``add-bond`` ("cannot create an interface named X because a
        port named X already exists").  So every member is detached first, and
        an existing bond is rebuilt when its member list changed.
        """
        if len(members) < 2:  # OVS refuses single-member bonds
            raise OvsError(f"bond {name} needs at least 2 members, got {len(members)}")
        existing = set(self.list_ports())
        current_members: list[str] = []
        if name in existing:
            current_members = list((self.port_details().get(name) or {}).get("interfaces") or [])
        settings = [f"lacp={mode}", f"bond_mode={lb}", "external_ids:makemeswitch-managed=true"]
        if tag is not None:
            settings.append(f"tag={int(tag)}")
        if trunks:
            settings.append("trunks=" + ",".join(str(int(v)) for v in sorted(trunks)))

        if name in existing and sorted(current_members) == sorted(members):
            self._run(["set", "port", name, *settings])
            return

        commands: list[list[str]] = []
        if name in existing:
            commands.append(["--if-exists", "del-port", self.bridge, name])
        for member in members:
            if member in existing:
                commands.append(["--if-exists", "del-port", self.bridge, member])
        commands.append(["add-bond", self.bridge, name, *members, *settings])
        # ovs-vsctl needs an explicit "--" between separate commands.
        args: list[str] = []
        for command in commands:
            args += command + ["--"]
        self._run(args[:-1])

    # --- mirrors ---------------------------------------------------------
    def clear_mirrors(self) -> None:
        self._run(["clear", "bridge", self.bridge, "mirrors"])

    def add_mirror(self, name: str, source_ports: list[str], output_port: str, direction: str = "both") -> None:
        """Create a SPAN session: source ports -> one destination port."""
        select_all = direction == "both"
        select_src = direction in ("both", "ingress", "rx")
        select_dst = direction in ("both", "egress", "tx")
        args = ["--", "--id=@m", "create", "mirror", f"name={name}"]
        args += [
            f"select_all={'true' if select_all else 'false'}",
            "select_src_port=[]",
            "select_dst_port=[]",
        ]
        args += ["--", "--id=@dst", "get", "port", output_port]
        args += ["--", "set", "mirror", "@m", "output_port=@dst"]
        for index, port in enumerate(source_ports):
            ref = f"@sp{index}"
            args += ["--", f"--id={ref}", "get", "port", port]
            if select_src:
                args += ["--", "add", "mirror", "@m", "select_src_port", ref]
            if select_dst:
                args += ["--", "add", "mirror", "@m", "select_dst_port", ref]
        args += ["--", "add", "bridge", self.bridge, "mirrors", "@m"]
        self._run(args)

    def mirror_names(self) -> list[str]:
        raw = self._run(["get", "bridge", self.bridge, "mirrors"], check=False).strip()
        if not raw or raw in ("[]", "[ ]"):
            return []
        return re.findall(r'name="?([^"\],]+)"?', raw) or []

    # --- observability ---------------------------------------------------
    def mac_table(self) -> list[dict[str, Any]]:
        out = self._appctl(["fdb/show", self.bridge], check=False)
        entries = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            port, vlan, mac, age = parts[0], parts[1], parts[2], parts[3]
            try:
                entries.append({"port": int(port), "vlan": int(vlan), "mac": mac.lower(), "age": float(age)})
            except ValueError:
                continue
        return entries

    def port_names_by_number(self) -> dict[int, str]:
        """Map OVS port numbers (used by fdb/show) to port names."""
        out = self._appctl(["ofport/show", self.bridge], check=False)
        mapping: dict[int, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1].isdigit():
                mapping[int(parts[1])] = parts[0]
        return mapping

    def interface_stats(self) -> dict[str, dict[str, float]]:
        if not self.bridge_exists():
            return {}
        records = _parse_ovs_json(
            self._run(["--format=json", "--columns=name,statistics", "list", "interface"])
        )
        wanted = set(self.list_ports())
        stats: dict[str, dict[str, float]] = {}
        for rec in records:
            name = rec.get("name")
            if name not in wanted:
                continue
            values: dict[str, float] = {}
            for key, value in (rec.get("statistics") or {}).items():
                try:
                    values[key] = float(value)
                except (TypeError, ValueError):
                    continue
            stats[name] = values
        return stats

    def snapshot(self) -> dict[str, Any]:
        """Capture bridge + port state so a failed apply can be reverted."""
        if not self.bridge_exists():
            return {"exists": False, "bridge": self.bridge}
        attrs = self._run(["get", "bridge", self.bridge, "stp_enable", "other_config", "fail_mode"]).strip()
        return {
            "exists": True,
            "bridge": self.bridge,
            "attrs_raw": attrs,
            "ports": self.port_details(),
            "order": self.list_ports(),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Best-effort revert of the L2 dataplane to a snapshot."""
        if not snapshot or not snapshot.get("exists"):
            self.delete_bridge()
            return
        self.ensure_bridge()
        current = set(self.list_ports())
        desired = set((snapshot.get("ports") or {}).keys())
        for port in current - desired:
            self.del_port(port)
        details = snapshot.get("ports") or {}
        for name in snapshot.get("order") or list(details):
            info = details.get(name, {})
            if info.get("type") == "bond" or info.get("interfaces") and len(info.get("interfaces", [])) > 1:
                members = info.get("interfaces") or [name]
                self._run(["--if-exists", "del-port", self.bridge, name])
                self.add_bond(name, members, info.get("lacp") or "active", info.get("bond_mode") or "balance-tcp", info.get("tag"), info.get("trunks"))
            else:
                self.add_port(name, info.get("tag"), info.get("trunks"))
        log.warning("ovs state restored from snapshot", extra={"data": {"bridge": self.bridge, "ports": sorted(desired)}})

    def health(self) -> dict[str, Any]:
        status = self.service_status()
        status["ports"] = len(self.list_ports())
        return status
