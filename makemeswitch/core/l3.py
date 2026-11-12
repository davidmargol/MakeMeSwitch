"""Layer-3 dataplane: SVIs on OVS internal ports and static routes.

An SVI is an OVS ``internal`` port placed in its VLAN (``tag``) with a Linux IP
address on top.  The host kernel then routes between VLANs.  Static routes are
installed in the main table.  FRRouting is optional and only needed for
dynamic protocols; nothing here pretends to speak OSPF/BGP.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any, Callable

from makemeswitch.core.models import SwitchConfig
from makemeswitch.utils.logging import get_logger

log = get_logger("makemeswitch.l3")


class L3Error(RuntimeError):
    pass


class L3Manager:
    def __init__(self, bridge: str, run: Callable[..., subprocess.CompletedProcess] | None = None):
        self.bridge = bridge
        self._run = run or self._default_run
        self._applied_svis: set[int] = set()
        self._applied_routes: set[str] = set()
        self._forwarding: bool | None = None
        self._forward_chain = False

    @staticmethod
    def _default_run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
        if check and proc.returncode != 0:
            raise L3Error((proc.stderr or proc.stdout).strip() or f"command failed: {' '.join(args)}")
        return proc

    @staticmethod
    def signature(cfg: SwitchConfig) -> tuple[Any, ...]:
        return (
            cfg.ip_routing,
            tuple(sorted((s.vlan, s.ip, s.admin_up) for s in cfg.svis.values())),
            tuple(sorted((r.prefix, r.nexthop, r.interface) for r in cfg.routes)),
        )

    def apply(self, cfg: SwitchConfig, ovs) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        desired_svis = {
            s.vlan: s for s in cfg.svis.values() if s.ip and s.admin_up and cfg.ip_routing and cfg.vlans.get(s.vlan)
        }
        # Remove SVIs that no longer exist.
        for vid in sorted(self._applied_svis - set(desired_svis)):
            name = f"vlan{vid}"
            self._run(["ip", "addr", "flush", "dev", name], check=False)
            ovs.del_port(name)
            self._applied_svis.discard(vid)
            ops.append({"action": "del-svi", "target": f"Vlan{vid}", "detail": "internal port removed"})
        # Create/refresh SVIs.
        for vid, svi in sorted(desired_svis.items()):
            name = f"vlan{vid}"
            ovs.add_internal_port(name, tag=vid)
            self._run(["ip", "link", "set", name, "up"])
            self._run(["ip", "addr", "replace", str(svi.ip), "dev", name])
            if vid not in self._applied_svis:
                ops.append({"action": "add-svi", "target": f"Vlan{vid}", "detail": str(svi.ip)})
            else:
                ops.append({"action": "update-svi", "target": f"Vlan{vid}", "detail": str(svi.ip)})
            self._applied_svis.add(vid)
        # Forwarding sysctl - only touched when L3 is (or was) configured.
        if cfg.ip_routing or self._forwarding is not None:
            desired_forward = bool(cfg.ip_routing and desired_svis)
            if desired_forward != self._forwarding:
                self._run(
                    ["sysctl", "-q", "-w", f"net.ipv4.ip_forward={1 if desired_forward else 0}"],
                    check=desired_forward,
                )
                self._forwarding = desired_forward
        # Static routes.
        desired_routes = {r.prefix: r for r in cfg.routes} if cfg.ip_routing else {}
        for prefix in sorted(self._applied_routes - set(desired_routes)):
            self._run(["ip", "route", "del", prefix], check=False)
            self._applied_routes.discard(prefix)
            ops.append({"action": "del-route", "target": prefix, "detail": "removed"})
        for prefix, route in sorted(desired_routes.items()):
            args = ["ip", "route", "replace", prefix]
            if route.nexthop:
                args += ["via", route.nexthop]
            if route.interface:
                args += ["dev", route.interface]
            try:
                self._run(args)
                if prefix not in self._applied_routes:
                    ops.append({"action": "add-route", "target": prefix, "detail": route.nexthop or route.interface or ""})
                self._applied_routes.add(prefix)
            except L3Error as exc:
                raise L3Error(f"static route {prefix} failed: {exc}") from exc
        names = [f"vlan{vid}" for vid in sorted(desired_svis)]
        self._sync_forward_rules(names)
        return ops

    def _sync_forward_rules(self, names: list[str]) -> None:
        """Permit forwarding only between switch SVI interfaces.

        Hosts running Docker (and many hardened installs) set the FORWARD
        policy to DROP.  Inter-VLAN routing traverses FORWARD, so the switch
        owns a dedicated ``OSW-FORWARD`` chain that matches nothing but its own
        SVI interfaces.  Existing rules for other interfaces are untouched.
        """
        if not names:
            self._teardown_forward_rules()
            return
        if not shutil.which("iptables"):
            log.warning("iptables unavailable; inter-VLAN routing may be blocked by the host firewall")
            return

        def ipt(*args: str) -> bool:
            return subprocess.run(
                ["iptables", *args], capture_output=True, text=True, timeout=10, check=False
            ).returncode == 0

        if ipt("-C", "FORWARD", "-j", "OSW-FORWARD") is False:
            ipt("-N", "OSW-FORWARD")
            ipt("-I", "FORWARD", "1", "-j", "OSW-FORWARD")
        ipt("-F", "OSW-FORWARD")
        for name in names:
            ipt("-A", "OSW-FORWARD", "-i", name, "-j", "ACCEPT")
            ipt("-A", "OSW-FORWARD", "-o", name, "-j", "ACCEPT")
        ipt("-A", "OSW-FORWARD", "-j", "RETURN")
        self._forward_chain = True

    def _teardown_forward_rules(self) -> None:
        if not shutil.which("iptables"):
            return

        def ipt(*args: str) -> bool:
            return subprocess.run(
                ["iptables", *args], capture_output=True, text=True, timeout=10, check=False
            ).returncode == 0

        while ipt("-D", "FORWARD", "-j", "OSW-FORWARD"):
            pass
        ipt("-F", "OSW-FORWARD")
        ipt("-X", "OSW-FORWARD")
        self._forward_chain = False

    def flush(self, ovs) -> None:
        for vid in sorted(self._applied_svis):
            name = f"vlan{vid}"
            self._run(["ip", "addr", "flush", "dev", name], check=False)
            ovs.del_port(name)
        self._applied_svis.clear()
        for prefix in sorted(self._applied_routes):
            self._run(["ip", "route", "del", prefix], check=False)
        self._applied_routes.clear()
        self._teardown_forward_rules()
