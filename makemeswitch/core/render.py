"""Render a SwitchConfig as Cisco-inspired configuration text."""
from __future__ import annotations

from makemeswitch import __version__
from makemeswitch.core.models import SwitchConfig, format_vlan_list


def render_config_section(cfg: SwitchConfig, heading: str) -> str | None:
    """Return one stanza (e.g. ``interface Ethernet1/1``) from the full config."""
    wanted = " ".join(heading.split()).lower()
    for block in render_running_config(cfg, include_defaults=True).split("\n!"):
        lines = [line for line in block.strip("\n").splitlines() if line.strip()]
        if lines and " ".join(lines[0].split()).lower() == wanted:
            return "\n".join(lines + ["!"])
    return None


def render_running_config(
    cfg: SwitchConfig,
    include_defaults: bool = False,
    users: list[dict] | None = None,
    show_secrets: bool = False,
) -> str:
    lines: list[str] = ["! MakeMeSwitch CLI configuration", f"! version {__version__}", "!"]
    lines.append(f"hostname {cfg.hostname}")
    if cfg.domain_name:
        lines.append(f"ip domain-name {cfg.domain_name}")
    lines.append("!")
    if cfg.banner.enabled:
        for kind in ("login", "motd"):
            text = getattr(cfg.banner, kind, "")
            if not text:
                continue
            lines.append(f"banner {kind} ^C")
            lines.extend(text.splitlines() or [""])
            lines.append("^C")
        lines.append("!")
    lines.append(f"spanning-tree mode {cfg.stp.mode}")
    if cfg.stp.mode != "none":
        lines.append(f"spanning-tree priority {cfg.stp.priority}")
    if cfg.stp.mode == "none" or not cfg.stp.enabled:
        lines.append("no spanning-tree enable")
    lines.append("lldp run" if cfg.lldp_enabled else "no lldp run")
    if cfg.ip_routing:
        lines.append("ip routing")
    lines.append("!")
    for vlan in sorted(cfg.vlans.values(), key=lambda v: v.vid):
        lines.append(f"vlan {vlan.vid}")
        if vlan.name:
            lines.append(f" name {vlan.name}")
        if vlan.description:
            lines.append(f" description {vlan.description}")
        lines.append("!")
    for pc in sorted(cfg.port_channels.values(), key=lambda p: p.id):
        lines.append(f"interface Port-Channel{pc.id}")
        lines.append(f" lacp mode {pc.mode}")
        lines.append(f" load-balance {pc.lb}")
        lines.append("!")
    for svi in sorted(cfg.svis.values(), key=lambda s: s.vlan):
        lines.append(f"interface Vlan{svi.vlan}")
        if svi.description:
            lines.append(f" description {svi.description}")
        if svi.ip:
            lines.append(f" ip address {svi.ip}")
        lines.append(" " + ("no shutdown" if svi.admin_up else "shutdown"))
        lines.append("!")
    for iface in sorted(cfg.interfaces.values(), key=lambda i: _if_sort(i.name)):
        if not include_defaults and not iface.attached and not iface.description and iface.mtu == 1500:
            continue
        lines.append(f"interface {iface.name}")
        if iface.description:
            lines.append(f" description {iface.description}")
        if not iface.attached:
            lines.append(" no switchport")
        else:
            lines.append(f" switchport mode {iface.mode}")
            if iface.mode == "access":
                lines.append(f" switchport access vlan {iface.access_vlan}")
            else:
                allowed = "all" if iface.trunk_all else format_vlan_list(iface.trunk_allowed)
                lines.append(f" switchport trunk allowed vlan {allowed}")
                if iface.native_vlan is not None:
                    lines.append(f" switchport trunk native vlan {iface.native_vlan}")
        if iface.channel_group is not None:
            lines.append(f" channel-group {iface.channel_group} mode {cfg.port_channels[iface.channel_group].mode}")
        if iface.mtu != 1500:
            lines.append(f" mtu {iface.mtu}")
        lines.append(" " + ("no shutdown" if iface.admin_up else "shutdown"))
        lines.append("!")
    for mirror in sorted(cfg.mirrors.values(), key=lambda m: m.id):
        lines.append(f"monitor session {mirror.id} source interface {', '.join(mirror.source)} {mirror.direction}")
        if mirror.destination:
            lines.append(f"monitor session {mirror.id} destination interface {mirror.destination}")
        lines.append("!")
    for route in cfg.routes:
        if route.nexthop:
            lines.append(f"ip route {route.prefix} {route.nexthop}")
        else:
            lines.append(f"ip route {route.prefix} interface {route.interface}")
    if cfg.routes:
        lines.append("!")
    if cfg.snmp.enabled:
        for community in cfg.snmp.communities:
            name = community.name if show_secrets else "********"
            sources = " ".join(community.sources)
            suffix = f" {sources}" if sources else ""
            lines.append(f"snmp-server community {name} ro{suffix}")
        if cfg.snmp.contact:
            lines.append(f"snmp-server contact {cfg.snmp.contact}")
        if cfg.snmp.location:
            lines.append(f"snmp-server location {cfg.snmp.location}")
        if cfg.snmp.sys_name:
            lines.append(f"snmp-server name {cfg.snmp.sys_name}")
        if getattr(cfg.snmp, "sys_object_id", ""):
            lines.append(f"snmp-server object-id {cfg.snmp.sys_object_id}")
        if getattr(cfg.snmp, "version", "v2c") == "v1":
            lines.append("snmp-server version v1")
        lines.append(f"snmp-server listen {cfg.snmp.bind} port {cfg.snmp.port}")
    else:
        lines.append("no snmp-server")
    lines.append("!")
    if cfg.syslog.enabled:
        lines.append(
            f"logging host {cfg.syslog.destination} transport {cfg.syslog.transport} "
            f"port {cfg.syslog.port} severity {cfg.syslog.severity} "
            f"facility {cfg.syslog.facility} format {cfg.syslog.fmt}"
        )
        if cfg.syslog.source:
            lines.append(f"logging source-interface {cfg.syslog.source}")
    else:
        lines.append("no logging remote")
    lines.append("!")
    for user in users or []:
        from makemeswitch.core.security import role_to_privilege

        privilege = role_to_privilege(user.get("role", "readonly"))
        # Hashes are never exported, even by ``show running-config``.  Import
        # accepts a real Cisco type 9 hash; it is redacted when displayed.
        marker = "secret 9 <redacted>" if str(user.get("password_hash", "")).startswith("$9$") else "secret <redacted>"
        lines.append(f"username {user['username']} privilege {privilege} {marker}")
    if users:
        lines.append("!")
    lines.append("end")
    return "\n".join(lines)


def render_interface_config(cfg: SwitchConfig, name: str) -> str:
    """One interface/SVI block from the running configuration."""
    text = render_running_config(cfg, include_defaults=True)
    wanted = name.lower()
    block: list[str] = []
    capturing = False
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "interface" and parts[1].lower() == wanted:
            capturing = True
        elif capturing and line == "!":
            block.append(line)
            break
        if capturing:
            block.append(line)
    return "\n".join(block) if block else f"! interface {name} is not configured"


def _if_sort(name: str):
    if "/" in name:
        head, _, tail = name.partition("/")
        return (0, int(tail) if tail.isdigit() else 0, head)
    return (1, 0, name)
