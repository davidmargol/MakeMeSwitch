"""All CLI commands.  Handlers mutate a copy of the running config and hand it
to the engine; nothing here talks to OVS directly.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import time
from typing import Callable

from rich import box
from rich.console import Console
from rich.table import Table

from makemeswitch import __version__
from makemeswitch.cli.modes import CONFIG_MODES, MODE_PROMPT, Session
from makemeswitch.cli.parser import CommandSpec
from makemeswitch.core.models import (
    BannerConfig,
    ConfigError,
    InterfaceConfig,
    MirrorSession,
    PortChannel,
    SnmpCommunity,
    StaticRoute,
    Svi,
    SyslogConfig,
    Vlan,
    format_vlan_list,
    normalize_interface_name,
    parse_vlan_list,
)
from makemeswitch.core import interfaces as netif
from makemeswitch.core.render import render_config_section, render_running_config

COMMANDS: list[CommandSpec] = []


def command(*path: str, modes=("priv",), help: str = "", trailing: bool = False, privilege: str = "operator"):
    def decorator(func: Callable):
        COMMANDS.append(
            CommandSpec(path=tuple(path), handler=func, modes=tuple(modes), help=help, trailing=trailing, privilege=privilege)
        )
        return func

    return decorator


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def _bytes(value: float | int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    parts.append(f"{hours:02d}:{minutes:02d}:{secs:02d}")
    return " ".join(parts)


def _table(title: str | None = None) -> Table:
    return Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=False, padding=(0, 1))


def _speed(value: int | None) -> str:
    """Render link speed the way IOS does: 10G, 1G, 100M."""
    if not value:
        return "auto"
    speed = int(value)
    if speed >= 1000:
        return f"{speed / 1000:.0f}G" if speed % 1000 == 0 else f"{speed / 1000:.1f}G"
    return f"{speed}M"


def _vlan_ports(session: Session, vid: int) -> tuple[list[str], list[str]]:
    untagged, tagged = [], []
    for iface in session.config.interfaces.values():
        if not iface.attached or not iface.admin_up:
            continue
        if iface.mode == "access" and iface.access_vlan == vid:
            untagged.append(iface.name)
        elif iface.mode == "trunk":
            if iface.native_vlan == vid:
                untagged.append(iface.name + "(native)")
            elif vid in iface.trunk_allowed:
                tagged.append(iface.name)
    return untagged, tagged


def _parse_options(argv: list[str], valid: set[str]) -> tuple[dict[str, list[str]], str | None]:
    options: dict[str, list[str]] = {}
    index = 0
    while index < len(argv):
        key = argv[index].lower()
        if key not in valid:
            return {}, f"unrecognized option {argv[index]!r}"
        values = []
        index += 1
        while index < len(argv) and argv[index].lower() not in valid:
            values.append(argv[index])
            index += 1
        options[key] = values
    return options, None


# --------------------------------------------------------------------------
# session / mode commands
# --------------------------------------------------------------------------
@command("enable", modes=("exec",), help="Enter privileged EXEC mode", privilege="readonly")
def cmd_enable(session: Session, argv: list[str]) -> None:
    session.mode = "priv"


@command("disable", modes=("priv",), help="Return to user EXEC mode", privilege="readonly")
def cmd_disable(session: Session, argv: list[str]) -> None:
    session.mode = "exec"


@command("exit", modes=("exec", "priv", "config", "if", "vlan", "pc", "svi", "ifrange"), help="Exit current mode", privilege="readonly")
def cmd_exit(session: Session, argv: list[str]) -> None:
    if session.mode == "exec":
        session.running = False
        return
    if session.mode == "priv":
        session.mode = "exec"
        return
    if session.mode == "config":
        session.mode = "priv"
        return
    session.mode = "config"
    session.interface = None
    session.vlan_id = None
    session.channel_id = None
    session.svi_vlan = None
    session.range_members = []


@command("end", modes=("config", "if", "vlan", "pc", "svi", "ifrange"), help="Return to privileged EXEC mode")
def cmd_end(session: Session, argv: list[str]) -> None:
    session.mode = "priv"
    session.interface = None
    session.vlan_id = None
    session.channel_id = None
    session.svi_vlan = None
    session.range_members = []


@command("configure", "terminal", modes=("priv",), help="Enter global configuration mode")
def cmd_configure(session: Session, argv: list[str]) -> None:
    if not session.can("operator"):
        session.error("authorization failed: configuration privilege required")
        return
    session.mode = "config"


@command("do", modes=("config", "if", "vlan", "pc", "svi"), help="Run a privileged EXEC command", trailing=True)
def cmd_do(session: Session, argv: list[str]) -> None:
    if not argv:
        session.error("incomplete command")
        return
    from makemeswitch.cli.shell import execute_line

    saved = session.mode
    session.mode = "priv"
    try:
        execute_line(session, " ".join(argv))
    finally:
        session.mode = saved


# --------------------------------------------------------------------------
# show commands
# --------------------------------------------------------------------------
@command("show", "terminal", "monitor", modes=("exec", "priv"), help="Session terminal monitoring status", privilege="readonly")
def cmd_show_terminal_monitor(session: Session, argv: list[str]) -> None:
    # Neither the interactive shell nor one-shot execution subscribes to logs.
    # Do not invent a session flag that claims streaming is enabled.
    session.info("Terminal monitoring: disabled for this session.")
    session.info("Live asynchronous log streaming is not supported by this CLI; use 'show logging' for stored events.")


@command("show", "version", modes=("exec", "priv"), help="Display version information", privilege="readonly")
def cmd_show_version(session: Session, argv: list[str]) -> None:
    info = session.engine.system_info()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Hostname", info["config_hostname"])
    table.add_row("MakeMeSwitch CLI", f"{__version__}")
    table.add_row("Platform", info["platform"])
    table.add_row("Kernel", info["kernel"])
    table.add_row("Uptime", _uptime(info["uptime_seconds"]))
    table.add_row("Switching engine", info["ovs"].get("version") or "not available")
    table.add_row("Bridge", f"{info['bridge']} ({'up' if info['ovs'].get('bridge_exists') else 'not created'})")
    table.add_row("FRRouting", info["frr"].get("version") or "not installed (optional)")
    table.add_row("LLDP", "installed" if info["lldp"].get("installed") else "not installed (optional)")
    session.show(table)


@command("show", "interfaces", "description", modes=("exec", "priv"), help="Port status and description", privilege="readonly")
def cmd_show_interfaces_description(session: Session, argv: list[str]) -> None:
    table = _table()
    table.add_column("Interface", no_wrap=True, min_width=10)
    table.add_column("Status", no_wrap=True, min_width=11)
    table.add_column("Protocol", no_wrap=True, min_width=8)
    table.add_column("Description", min_width=12)
    for row in session.engine.interface_snapshot():
        status = row["status"]
        style = "green" if status == "up" else ("yellow" if status in ("down", "not connected") else "red")
        protocol = "up" if status == "up" else "down"
        table.add_row(
            row["name"],
            f"[{style}]{status}[/{style}]",
            protocol,
            row.get("description") or "",
        )
    session.show(table)


@command("show", "interfaces", "status", modes=("exec", "priv"), help="Port status summary", privilege="readonly")
@command("show", "interfaces", modes=("exec", "priv"), help="All interfaces", privilege="readonly")
def cmd_show_interfaces(session: Session, argv: list[str]) -> None:
    table = _table()
    # A narrow terminal (80 cols over SSH) has no room for free text: dropping
    # the description keeps every other column at full width instead of
    # squeezing headers into single letters. Use `show interfaces description`.
    wide = session.console.width >= 100
    table.add_column("Port", no_wrap=True, min_width=10)
    table.add_column("Name", no_wrap=True, min_width=7)
    table.add_column("Status", no_wrap=True, min_width=11)
    table.add_column("Vlan", no_wrap=True, min_width=9)
    table.add_column("Duplex", no_wrap=True, min_width=6)
    table.add_column("Speed", no_wrap=True, min_width=5)
    table.add_column("Type", no_wrap=True, min_width=4)
    if wide:
        table.add_column("Description", min_width=12)
    for row in session.engine.interface_snapshot():
        link = row.get("link") or {}
        iface = session.config.interfaces.get(row["name"])
        if iface and not row["attached"]:
            vlan = "unassigned"
        elif iface and iface.mode == "trunk":
            vlan = "trunk " + format_vlan_list(iface.trunk_allowed)
        elif iface:
            vlan = str(iface.access_vlan)
        else:
            vlan = "-"
        status = row["status"]
        style = "green" if status == "up" else ("yellow" if status in ("down", "not connected") else "red")
        cells = [
            row["name"],
            row.get("linux_if") or "-",
            f"[{style}]{status}[/{style}]",
            vlan,
            link.get("duplex") or "auto",
            _speed(link.get("speed")),
            (row.get("kind") or "").upper() or "-",
        ]
        if wide:
            cells.append(row.get("description") or "")
        table.add_row(*cells)
    session.show(table)
    if not session.engine.ports:
        session.info("No switch ports detected. Attach a NIC with 'switchport attach' from interface configuration.")


@command("show", "interfaces", "mapping", modes=("exec", "priv"), help="Stable port-to-NIC mapping", privilege="readonly")
def cmd_show_mapping(session: Session, argv: list[str]) -> None:
    table = _table("Interface mapping")
    for column in ("Switch port", "Linux NIC", "Stable identity", "Present", "Protected", "Attached"):
        table.add_column(column)
    for row in session.engine.port_table():
        table.add_row(
            row["name"],
            row["linux_if"] or "-",
            row["identity"] or "-",
            "yes" if row["present"] else "[red]no[/red]",
            "[yellow]management[/yellow]" if row["protected"] else "no",
            "yes" if row["attached"] else "no",
        )
    session.show(table)


@command("show", "interfaces", "counters", modes=("exec", "priv"), help="RX/TX counters", privilege="readonly")
def cmd_show_counters(session: Session, argv: list[str]) -> None:
    table = _table("Interface counters")
    for column in ("Port", "RX bytes", "TX bytes", "RX pkts", "TX pkts", "RX errors", "TX errors", "RX dropped", "TX dropped"):
        table.add_column(column, justify="right")
    for row in session.engine.interface_snapshot():
        stats = row.get("stats") or {}
        table.add_row(
            row["name"],
            _bytes(stats.get("rx_bytes")),
            _bytes(stats.get("tx_bytes")),
            f"{int(stats.get('rx_packets', 0)):,}",
            f"{int(stats.get('tx_packets', 0)):,}",
            f"{int(stats.get('rx_errors', 0)):,}",
            f"{int(stats.get('tx_errors', 0)):,}",
            f"{int(stats.get('rx_dropped', 0)):,}",
            f"{int(stats.get('tx_dropped', 0)):,}",
        )
    session.show(table)


@command("show", "interfaces", "<interface>", modes=("exec", "priv"), help="Detailed interface information", privilege="readonly")
def cmd_show_interface_detail(session: Session, argv: list[str]) -> None:
    try:
        name = normalize_interface_name(argv[0])
        row = session.engine.interface_detail(name)
    except (ConfigError, IndexError) as exc:
        session.error(str(exc))
        return
    link = row.get("link") or {}
    stats = row.get("stats") or {}
    iface = session.config.interfaces.get(name)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Interface", name)
    table.add_row("Linux name", row.get("linux_if") or "-")
    table.add_row("Stable identity", row.get("identity") or "-")
    table.add_row("Description", row.get("description") or "-")
    table.add_row("Administrative state", "up" if (iface and iface.admin_up) else "down")
    table.add_row("Link status", row["status"])
    table.add_row("MAC address", link.get("mac") or "-")
    table.add_row("Speed", f"{link['speed']} Mb/s" if link.get("speed") else "unknown")
    table.add_row("Duplex", link.get("duplex") or "unknown")
    table.add_row("MTU", str(iface.mtu if iface else link.get("mtu") or "-"))
    table.add_row("Switchport mode", iface.mode if iface else "-")
    if iface and iface.mode == "trunk":
        table.add_row("Allowed VLANs", format_vlan_list(iface.trunk_allowed))
        table.add_row("Native VLAN", str(iface.native_vlan) if iface.native_vlan else "none")
    elif iface:
        table.add_row("Access VLAN", str(iface.access_vlan))
    table.add_row("Channel group", str(iface.channel_group) if iface and iface.channel_group else "-")
    table.add_row("Driver", link.get("driver") or "-")
    table.add_row("USB path", link.get("usb_path") or "-")
    table.add_row("RX packets", f"{int(stats.get('rx_packets', 0)):,}")
    table.add_row("TX packets", f"{int(stats.get('tx_packets', 0)):,}")
    table.add_row("RX / TX errors", f"{int(stats.get('rx_errors', 0))} / {int(stats.get('tx_errors', 0))}")
    session.show(table)


@command("show", "vlan", "brief", modes=("exec", "priv"), help="VLAN summary", privilege="readonly")
@command("show", "vlan", modes=("exec", "priv"), help="All VLANs", privilege="readonly")
def cmd_show_vlan(session: Session, argv: list[str]) -> None:
    table = _table("VLANs")
    for column in ("VLAN", "Name", "Status", "Untagged ports", "Tagged ports"):
        table.add_column(column)
    for vlan in sorted(session.config.vlans.values(), key=lambda v: v.vid):
        untagged, tagged = _vlan_ports(session, vlan.vid)
        table.add_row(str(vlan.vid), vlan.name, "active", ", ".join(untagged) or "-", ", ".join(tagged) or "-")
    session.show(table)


@command("show", "vlan", "<vlan>", modes=("exec", "priv"), help="Single VLAN details", privilege="readonly")
def cmd_show_vlan_id(session: Session, argv: list[str]) -> None:
    try:
        vid = int(argv[0])
    except (ValueError, IndexError):
        session.error("invalid VLAN id")
        return
    vlan = session.config.vlans.get(vid)
    if not vlan:
        session.error(f"VLAN {vid} does not exist")
        return
    untagged, tagged = _vlan_ports(session, vid)
    session.info(f"VLAN {vid}  name {vlan.name}  status active")
    session.info(f"  untagged: {', '.join(untagged) or '-'}")
    session.info(f"  tagged:   {', '.join(tagged) or '-'}")


@command("show", "mac", "address-table", modes=("exec", "priv"), help="MAC address table", trailing=True, privilege="readonly")
def cmd_show_mac(session: Session, argv: list[str]) -> None:
    options, error = _parse_options(argv, {"vlan", "interface", "address"})
    if error:
        session.error(error)
        return
    entries = session.engine.mac_table()
    vlan_filter = options.get("vlan", [None])[0] if options.get("vlan") else None
    if_filter = options.get("interface", [None])[0] if options.get("interface") else None
    address_filter = options.get("address", [None])[0] if options.get("address") else None
    if vlan_filter:
        entries = [e for e in entries if str(e["vlan"]) == vlan_filter]
    if if_filter:
        entries = [e for e in entries if e["port"].lower() == normalize_interface_name(if_filter).lower()]
    if address_filter:
        entries = [e for e in entries if e["mac"].lower() == address_filter.lower()]
    table = _table("MAC address table")
    for column in ("VLAN", "MAC address", "Type", "Port", "Age"):
        table.add_column(column)
    for entry in entries:
        table.add_row(str(entry["vlan"]), entry["mac"], entry["type"], entry["port"], f"{int(entry['age'])}s")
    session.show(table)
    session.info(f"Total entries: {len(entries)} (learned by Open vSwitch)")


@command("show", "ip", "interface", "brief", modes=("exec", "priv"), help="Layer-3 interface summary", privilege="readonly")
def cmd_show_ip_brief(session: Session, argv: list[str]) -> None:
    table = _table()
    for column in ("Interface", "IP address", "Admin", "Link"):
        table.add_column(column)
    for svi in sorted(session.config.svis.values(), key=lambda s: s.vlan):
        table.add_row(f"Vlan{svi.vlan}", svi.ip or "unassigned", "up" if svi.admin_up else "down", "up")
    for row in session.engine.interface_snapshot():
        iface = session.config.interfaces.get(row["name"])
        if iface and iface.mode == "trunk":
            table.add_row(row["name"], "unassigned", "up" if iface.admin_up else "down", row["status"])
    if not session.config.svis:
        session.info("No SVIs configured. Layer-3 routing is optional and requires FRRouting for dynamic protocols.")
    session.show(table)


@command("show", "system", "resources", modes=("exec", "priv"), help="CPU, memory and temperature", privilege="readonly")
def cmd_show_resources(session: Session, argv: list[str]) -> None:
    info = session.engine.system_info()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Uptime", _uptime(info["uptime_seconds"]))
    table.add_row("CPU", f"{info['cpu_percent']}% of {info['cpu_count']} core(s), load {info['load']}")
    table.add_row("Memory", f"{_bytes(info['memory_total'] - info['memory_available'])} / {_bytes(info['memory_total'])} ({info['memory_percent']}%)")
    table.add_row("Temperature", f"{info['temperature_c']} C" if info["temperature_c"] is not None else "not available")
    table.add_row("Ports", f"{info['ports_active']} active / {info['ports_total']} total")
    table.add_row("VLANs", str(info["vlans"]))
    session.show(table)


@command("show", "running-config", modes=("priv",), help="Running configuration", privilege="readonly")
def cmd_show_running(session: Session, argv: list[str]) -> None:
    users = session.engine.store.list_users(include_secrets=True)
    session.console.print(
        render_running_config(session.config, include_defaults=True, users=users, show_secrets=False),
        markup=False,
        highlight=False,
    )


@command("show", "running-config", "interface", "<interface>", modes=("priv",), help="Running configuration of one interface", privilege="readonly")
def cmd_show_running_interface(session: Session, argv: list[str]) -> None:
    try:
        name = normalize_interface_name(argv[0])
    except ConfigError as exc:
        session.error(exc.message)
        return
    section = render_config_section(session.config, f"interface {name}")
    if section is None:
        session.error(f"interface {name} is not in the running configuration")
        return
    session.console.print(section, markup=False, highlight=False)


@command("show", "startup-config", "interface", "<interface>", modes=("priv",), help="Startup configuration of one interface", privilege="readonly")
def cmd_show_startup_interface(session: Session, argv: list[str]) -> None:
    startup = session.engine.store.load_config("startup")
    if startup is None:
        session.error("no startup configuration present")
        return
    try:
        name = normalize_interface_name(argv[0])
    except ConfigError as exc:
        session.error(exc.message)
        return
    section = render_config_section(startup, f"interface {name}")
    if section is None:
        session.error(f"interface {name} is not in the startup configuration")
        return
    session.console.print(section, markup=False, highlight=False)


@command("show", "startup-config", modes=("priv",), help="Startup configuration", privilege="readonly")
def cmd_show_startup(session: Session, argv: list[str]) -> None:
    startup = session.engine.store.load_config("startup")
    if startup is None:
        session.error("no startup configuration present")
        return
    users = session.engine.store.list_users(include_secrets=True)
    session.console.print(
        render_running_config(startup, include_defaults=True, users=users, show_secrets=False),
        markup=False,
        highlight=False,
    )


@command("show", "configuration", "drift", modes=("priv",), help="Compare running config with OVS", privilege="readonly")
def cmd_show_drift(session: Session, argv: list[str]) -> None:
    issues = session.engine.diff()
    if not issues:
        session.info("No drift detected: Open vSwitch matches the running configuration.")
        return
    table = _table("Configuration drift")
    for column in ("Severity", "Port", "Detail"):
        table.add_column(column)
    for issue in issues:
        table.add_row(issue.get("severity", ""), issue.get("port", "-"), issue.get("message", ""))
    session.show(table)


@command("show", "logging", modes=("exec", "priv"), help="Recent system events", privilege="readonly")
def cmd_show_logging(session: Session, argv: list[str]) -> None:
    table = _table("System events")
    for column in ("Time", "Severity", "Source", "Message"):
        table.add_column(column)
    for event in session.engine.store.list_events(limit=25):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"]))
        table.add_row(stamp, event["severity"], event["source"], event["message"])
    session.show(table)


@command("show", "management", modes=("exec", "priv"), help="Management interface (never a switch port)", privilege="readonly")
def cmd_show_management(session: Session, argv: list[str]) -> None:
    """Show the NIC(s) that carry host management connectivity.

    Like the dedicated management port on a physical switch: visible, but not
    configurable as a switch port.
    """
    protected = [
        name
        for name in netif.protected_interfaces()
        if not name.startswith(("br-", "docker", "veth", "virbr", "lo"))
    ]
    addresses = netif.addresses_by_interface()
    if not protected:
        session.info("No management interface detected (no default route / IP address).")
        return
    table = _table()
    table.add_column("Management", no_wrap=True, min_width=10)
    table.add_column("Linux NIC", no_wrap=True, min_width=9)
    table.add_column("IPv4", no_wrap=True, min_width=15)
    table.add_column("Role", min_width=16)
    for index, name in enumerate(protected, 1):
        link = netif.link_by_name(name)
        roles = []
        if name in netif.default_route_interfaces():
            roles.append("default route")
        if addresses.get(name):
            roles.append("has IP")
        table.add_row(
            f"Mgmt{index}",
            name,
            ", ".join(addresses.get(name, [])) or "-",
            ", ".join(roles) or "protected",
        )
    session.show(table)
    session.info("The management interface is never enslaved into the switch and keeps its kernel name.")


@command("show", "lldp", "neighbors", modes=("exec", "priv"), help="LLDP neighbors", privilege="readonly")
def cmd_show_lldp(session: Session, argv: list[str]) -> None:
    status = session.engine.lldp_status()
    if not status["installed"]:
        session.error("LLDP is not available: lldpd is not installed on this host")
        return
    if not status["enabled"]:
        session.error("LLDP is disabled: enable it with 'lldp run' in configuration mode")
        return
    table = _table("LLDP neighbors")
    for column in ("Local interface", "Neighbor", "Neighbor port", "TTL"):
        table.add_column(column)
    for neighbor in status["neighbors"]:
        table.add_row(
            neighbor.get("local_interface") or "-",
            neighbor.get("chassis") or "unidentified",
            neighbor.get("port") or "unidentified",
            str(neighbor.get("ttl") or "-"),
        )
    session.show(table)
    if not status["neighbors"]:
        session.info("No LLDP neighbors reported.")


@command("show", "spanning-tree", modes=("exec", "priv"), help="Spanning-tree state", privilege="readonly")
def cmd_show_stp(session: Session, argv: list[str]) -> None:
    stp = session.config.stp
    session.info(f"Spanning-tree mode: {stp.mode}  enabled: {stp.enabled}  priority: {stp.priority}")
    try:
        output = subprocess.run(
            ["ovs-appctl", "rstp/show", session.engine.bridge], capture_output=True, text=True, timeout=10, check=False
        )
        if output.returncode == 0 and output.stdout.strip():
            session.console.print(output.stdout, markup=False, highlight=False)
            return
        session.info("RSTP is not running on the bridge (disabled or bridge missing).")
    except (OSError, subprocess.TimeoutExpired):
        session.error("unable to query OVS for spanning-tree state")


@command("show", "etherchannel", "summary", modes=("exec", "priv"), help="Port-channel / LACP state", privilege="readonly")
@command("show", "port-channel", "summary", modes=("exec", "priv"), help="Port-channel / LACP state", privilege="readonly")
def cmd_show_port_channel(session: Session, argv: list[str]) -> None:
    table = _table("Port-channels")
    for column in ("Group", "Port-channel", "Configured LACP mode", "Members", "OVS state"):
        table.add_column(column)
    details = {}
    available = True
    try:
        details = session.engine.ovs.port_details()
    except Exception:  # noqa: BLE001
        available = False
    for pc in sorted(session.config.port_channels.values(), key=lambda p: p.id):
        if not pc.members:
            state = "no members (not in OVS)"
        elif len(pc.members) < 2:
            state = "1 member - bond starts at 2"
        else:
            state = "present in OVS" if pc.name in details else "not in OVS"
        if not available:
            state = "unavailable"
        table.add_row(str(pc.id), pc.name, pc.mode, ", ".join(pc.members) or "-", state)
    session.show(table)
    if not session.config.port_channels:
        session.info("No port-channels configured.")


@command("show", "ip", "route", modes=("exec", "priv"), help="Routing table", privilege="readonly")
def cmd_show_routes(session: Session, argv: list[str]) -> None:
    table = _table("Static routes")
    for column in ("Prefix", "Next-hop", "Interface"):
        table.add_column(column)
    for route in session.config.routes:
        table.add_row(route.prefix, route.nexthop or "-", route.interface or "-")
    session.show(table)
    if not session.config.routes:
        session.info("No static routes configured.")


@command("ping", "<host>", modes=("exec", "priv"), help="Ping a host from the switch", privilege="readonly")
@command("ping", "<host>", "<count>", modes=("exec", "priv"), help="Ping with a packet count", privilege="readonly")
def cmd_ping(session: Session, argv: list[str]) -> None:
    host = argv[0]
    if not re.match(r"^[A-Za-z0-9_.:-]{1,253}$", host):
        session.error("invalid host")
        return
    count = argv[1] if len(argv) > 1 else "4"
    if not count.isdigit() or not 1 <= int(count) <= 20:
        session.error("count must be 1-20")
        return
    try:
        proc = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", host], capture_output=True, text=True, timeout=int(count) * 3 + 5, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        session.error(f"ping failed: {exc}")
        return
    session.console.print(proc.stdout or proc.stderr, markup=False, highlight=False)


# --------------------------------------------------------------------------
# privileged operations
# --------------------------------------------------------------------------
@command("write", modes=("priv", "config"), help="Save running config to startup", privilege="operator")
@command("write", "memory", modes=("priv", "config"), help="Save running config to startup", privilege="operator")
@command("copy", "running-config", "startup-config", modes=("priv",), help="Copy running to startup", privilege="operator")
def cmd_write_memory(session: Session, argv: list[str]) -> None:
    session.engine.save_startup(session.username)
    session.info("Configuration saved.")


@command("write", "erase", modes=("priv",), help="Reset startup configuration only (confirmation required)", privilege="admin")
def cmd_write_erase(session: Session, argv: list[str]) -> None:
    session.info("This resets startup configuration to defaults. Running configuration and forwarding remain unchanged.")
    if not session.ask_confirm("Erase startup configuration? [yes/no] "):
        session.info("Startup configuration erase aborted.")
        return
    from makemeswitch.core.models import SwitchConfig

    # Persist an empty/default startup rather than deleting its row: boot falls
    # back to running when no startup exists, which would undo the erase.
    startup = SwitchConfig()
    session.engine.store.save_config("startup", startup, session.username)
    session.engine.startup = startup
    session.engine.store.add_event("info", "config", f"Startup configuration erased by {session.username}")
    session.info("Startup configuration reset to defaults. Running configuration unchanged; defaults take effect on reload.")


@command("reload", modes=("priv",), help="Reload the startup configuration", privilege="admin")
def cmd_reload(session: Session, argv: list[str]) -> None:
    session.info("Reload discards the running configuration and applies the saved startup configuration.")
    if not session.ask_confirm("Proceed with reload? [confirm] "):
        session.info("Reload aborted.")
        return
    try:
        session.engine.load_startup(session.username)
        session.info("System configuration reloaded.")
    except Exception as exc:  # noqa: BLE001
        session.error(f"reload failed: {exc}")


@command("rollback", modes=("priv",), help="Roll back to a previous configuration", trailing=True, privilege="admin")
def cmd_rollback(session: Session, argv: list[str]) -> None:
    history_id = None
    if argv and argv[0].isdigit():
        history_id = int(argv[0])
    entries = session.engine.store.history(limit=10)
    if not entries:
        session.error("no configuration history available")
        return
    if history_id is None:
        session.info("Recent configurations:")
        for entry in entries:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))
            session.info(f"  #{entry['id']}  {stamp}  {entry['action']} by {entry['user']}")
        if not session.ask_confirm("Roll back to the previous configuration? [yes/no] "):
            session.info("Rollback aborted.")
            return
    try:
        session.engine.rollback(history_id, session.username)
        session.info("Rollback applied.")
    except Exception as exc:  # noqa: BLE001
        session.error(f"rollback failed: {exc}")


@command("clear", "counters", modes=("priv",), help="Reset interface counters", trailing=True, privilege="operator")
def cmd_clear_counters(session: Session, argv: list[str]) -> None:
    session.error("interface counters cannot be cleared: the OVS backend exposes cumulative counters only")


# --------------------------------------------------------------------------
# global configuration
# --------------------------------------------------------------------------
@command("hostname", "<name>", modes=("config",), help="Set the switch hostname")
def cmd_hostname(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        cfg.set_hostname(argv[0])
    except (ConfigError, IndexError) as exc:
        session.error(str(exc))
        return
    if session.commit(cfg):
        session.info(f"Hostname set to {cfg.hostname}")


@command("vlan", "<vlan>", modes=("config",), help="Create/enter a VLAN")
def cmd_vlan(session: Session, argv: list[str]) -> None:
    try:
        vid = int(argv[0])
    except (ValueError, IndexError):
        session.error("invalid VLAN id")
        return
    cfg = session.config.copy()
    try:
        cfg.add_vlan(vid)
    except ConfigError as exc:
        session.error(exc.message)
        return
    if not session.commit(cfg, quiet=True):
        return
    session.mode = "vlan"
    session.vlan_id = vid


@command("no", "vlan", "<vlan>", modes=("config",), help="Delete a VLAN")
def cmd_no_vlan(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        cfg.delete_vlan(int(argv[0]))
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    if session.commit(cfg):
        session.info(f"VLAN {argv[0]} deleted.")


MODES_WITH_IFACE = ("config", "if", "vlan", "pc", "svi")


@command("interface", "range", "<range>", modes=MODES_WITH_IFACE, trailing=True, help="Select several ports (eth1-4, eth1,3,5)")
def cmd_interface_range(session: Session, argv: list[str]) -> None:
    from makemeswitch.core.portnames import canonical, port_number, simple_name

    spec_tokens = session.last_command_tokens[2:]
    raw = "".join(spec_tokens).lower()
    if not raw:
        session.error("incomplete command")
        return
    names: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            left, _, right = part.partition("-")
            start = canonical(left)
            match = re.fullmatch(r"(?:ethernet|eth|et|e)?(\d{1,3})", right.strip())
            if start is None or not match:
                session.error(f"invalid range '{part}'")
                return
            first = port_number(start)
            last = int(match.group(1))
            if first is None or last < first:
                session.error(f"invalid range '{part}'")
                return
            names.extend(simple_name(number) for number in range(first, last + 1))
        else:
            name = canonical(part)
            if name is None:
                session.error(f"invalid interface '{part}'")
                return
            names.append(name)
    unique = list(dict.fromkeys(names))
    missing = [name for name in unique if name not in session.config.interfaces]
    if missing:
        session.error("unknown interface(s): " + ", ".join(missing) + " (see 'show interfaces mapping')")
        return
    session.mode = "ifrange"
    session.range_members = unique
    session.interface = unique[0]
    session.info(f"Range: {', '.join(unique)}")


@command("interface", "<interface>", modes=MODES_WITH_IFACE, help="Select a port (Ethernet1/1, eth1/1, Vlan10, port-channel1)")
@command("interface", "ethernet", "<interface>", modes=MODES_WITH_IFACE, help="Select an Ethernet port")
@command("interface", "port-channel", "<channel>", modes=MODES_WITH_IFACE, help="Select a port-channel")
@command("interface", "vlan", "<vlan>", modes=MODES_WITH_IFACE, help="Select an SVI")
def cmd_interface(session: Session, argv: list[str]) -> None:
    raw = [token.lower() for token in session.last_command_tokens[1:]]
    value = argv[0]
    cfg = session.config
    kind = "if"
    if any(token in ("port-channel", "po") for token in raw):
        kind = "pc"
    elif any(token in ("vlan", "vl") for token in raw):
        kind = "svi"
    else:
        try:
            normalized = normalize_interface_name(value)
        except ConfigError as exc:
            session.error(exc.message)
            return
        if normalized.lower().startswith("vlan"):
            kind, value = "svi", normalized
        elif normalized.lower().startswith("port-channel"):
            kind, value = "pc", normalized
    if kind == "if":
        try:
            name = normalize_interface_name(value)
        except ConfigError as exc:
            session.error(exc.message)
            return
        if name not in cfg.interfaces:
            session.error(f"interface {name} does not exist (see 'show interfaces mapping')")
            return
        session.mode = "if"
        session.interface = name
    elif kind == "pc":
        try:
            pc_id = int(re.sub(r"\D", "", value) or value)
        except ValueError:
            session.error("invalid port-channel id")
            return
        if not 1 <= pc_id <= 128:
            session.error("port-channel id must be 1-128")
            return
        if pc_id not in cfg.port_channels:
            # Cisco behaviour: entering the interface creates the port-channel.
            # It may stay empty; it only becomes a bond once 2+ members exist.
            cfg.port_channels[pc_id] = PortChannel(pc_id)
            session.info(f"Port-channel {pc_id} created (add members with 'channel-group {pc_id} mode active').")
        session.mode = "pc"
        session.channel_id = pc_id
    else:
        try:
            vid = int(re.sub(r"\D", "", value) or value)
        except ValueError:
            session.error("invalid VLAN id")
            return
        if vid not in cfg.vlans:
            session.error(f"VLAN {vid} does not exist")
            return
        session.mode = "svi"
        session.svi_vlan = vid


@command("no", "interface", "port-channel", "<channel>", modes=("config",), help="Delete a port-channel")
def cmd_no_interface_port_channel(session: Session, argv: list[str]) -> None:
    try:
        pc_id = int(re.sub(r"\D", "", argv[0]) or argv[0])
    except ValueError:
        session.error("invalid port-channel id")
        return
    cfg = session.config.copy()
    channel = cfg.port_channels.get(pc_id)
    if not channel:
        session.error(f"port-channel {pc_id} does not exist")
        return
    for member in list(channel.members):
        iface = cfg.interfaces.get(member)
        if iface and iface.channel_group == pc_id:
            iface.channel_group = None
    del cfg.port_channels[pc_id]
    if session.commit(cfg):
        session.info(f"Port-channel {pc_id} removed; its members are standalone switch ports again.")


@command("no", "interface", "ethernet", "<interface>", modes=("config",), help="Reset a switch port")
def cmd_no_interface(session: Session, argv: list[str]) -> None:
    try:
        name = normalize_interface_name(argv[0])
    except ConfigError as exc:
        session.error(exc.message)
        return
    cfg = session.config.copy()
    existing = cfg.interfaces.get(name)
    if not existing:
        session.error(f"interface {name} does not exist")
        return
    cfg.interfaces[name] = InterfaceConfig(name=name, identity=existing.identity, linux_if=existing.linux_if)
    if session.commit(cfg):
        session.info(f"Interface {name} reset to defaults.")


@command("spanning-tree", "mode", "<mode>", modes=("config",), help="stp | rstp | none")
def cmd_stp_mode(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    mode = argv[0].lower()
    if mode not in ("stp", "rstp", "none"):
        session.error("mode must be stp, rstp or none")
        return
    cfg.stp.mode = mode
    cfg.stp.enabled = mode != "none"
    if session.commit(cfg, quiet=True):
        session.info(f"Spanning-tree mode set to {mode}.")


@command("spanning-tree", "priority", "<priority>", modes=("config",), help="Bridge priority (multiple of 4096)")
def cmd_stp_priority(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        cfg.stp.priority = int(argv[0])
        cfg.stp.__post_init__()
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    if session.commit(cfg, quiet=True):
        session.info(f"Spanning-tree priority set to {cfg.stp.priority}.")


@command("spanning-tree", "enable", modes=("config",), help="Enable spanning-tree")
@command("no", "spanning-tree", "enable", modes=("config",), help="Disable spanning-tree")
def cmd_stp_toggle(session: Session, argv: list[str]) -> None:
    raw = session.last_command_tokens
    cfg = session.config.copy()
    cfg.stp.enabled = not ("no" in raw)
    if cfg.stp.mode == "none" and cfg.stp.enabled:
        cfg.stp.mode = "rstp"
    if session.commit(cfg, quiet=True):
        session.info(f"Spanning-tree {'enabled' if cfg.stp.enabled else 'disabled'}.")


@command("lldp", "run", modes=("config",), help="Enable LLDP")
@command("no", "lldp", "run", modes=("config",), help="Disable LLDP")
def cmd_lldp(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.lldp_enabled = "no" not in session.last_command_tokens
    if session.commit(cfg, quiet=True):
        status = session.engine.lldp_status()
        if cfg.lldp_enabled and not status["installed"]:
            session.info("LLDP enabled in configuration, but lldpd is not installed on this host.")
        else:
            session.info(f"LLDP {'enabled' if cfg.lldp_enabled else 'disabled'}.")


@command("ip", "routing", modes=("config",), help="Enable IPv4 routing/SVIs")
@command("no", "ip", "routing", modes=("config",), help="Disable IPv4 routing")
def cmd_ip_routing(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.ip_routing = "no" not in session.last_command_tokens
    if session.commit(cfg, quiet=True):
        session.info(f"IP routing {'enabled' if cfg.ip_routing else 'disabled'}.")


@command("ip", "route", "<prefix>", "<nexthop>", modes=("config",), help="Add a static route")
def cmd_ip_route(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        route = StaticRoute(argv[0], nexthop=argv[1])
    except (ConfigError, ValueError, IndexError) as exc:
        session.error(str(exc))
        return
    cfg.routes = [r for r in cfg.routes if r.prefix != route.prefix] + [route]
    if session.commit(cfg, quiet=True):
        session.info(f"Static route {route.prefix} via {route.nexthop} installed.")


@command("no", "ip", "route", "<prefix>", modes=("config",), help="Remove a static route")
def cmd_no_ip_route(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        prefix = str(ipaddress.ip_network(argv[0], strict=False))
    except (ValueError, IndexError):
        session.error("invalid prefix")
        return
    before = len(cfg.routes)
    cfg.routes = [r for r in cfg.routes if r.prefix != prefix]
    if len(cfg.routes) == before:
        session.error(f"route {prefix} not found")
        return
    if session.commit(cfg, quiet=True):
        session.info(f"Static route {prefix} removed.")


@command("monitor", "session", "<session>", "source", "interface", "<interface>", modes=("config",), help="SPAN source (both directions)")
@command("monitor", "session", "<session>", "source", "interface", "<interface>", "<direction>", modes=("config",), help="SPAN source direction")
def cmd_monitor_source(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        sid = int(argv[0])
        iface = normalize_interface_name(argv[1])
        direction = argv[2].lower() if len(argv) > 2 else "both"
        if direction not in ("both", "ingress", "egress", "rx", "tx"):
            raise ConfigError("direction must be both, ingress or egress")
        direction = {"rx": "ingress", "tx": "egress"}.get(direction, direction)
        if iface not in cfg.interfaces:
            raise ConfigError(f"interface {iface} does not exist")
        existing = cfg.mirrors.get(sid)
        if existing:
            if iface not in existing.source:
                existing.source.append(iface)
        else:
            cfg.mirrors[sid] = MirrorSession(sid, [iface], None, direction)
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    if session.commit(cfg, quiet=True):
        session.info(f"Mirror session {sid}: source {iface} ({direction}).")


@command("monitor", "session", "<session>", "destination", "interface", "<interface>", modes=("config",), help="SPAN destination")
def cmd_monitor_destination(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        sid = int(argv[0])
        iface = normalize_interface_name(argv[1])
        if iface not in cfg.interfaces:
            raise ConfigError(f"interface {iface} does not exist")
        mirror = cfg.mirrors.get(sid)
        if not mirror:
            raise ConfigError(f"create the source of session {sid} first")
        mirror.destination = iface
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    if session.commit(cfg, quiet=True):
        session.info(f"Mirror session {sid}: destination {iface}.")


@command("no", "monitor", "session", "<session>", modes=("config",), help="Delete a SPAN session")
def cmd_no_monitor(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        sid = int(argv[0])
    except (ValueError, IndexError):
        session.error("invalid session id")
        return
    if sid not in cfg.mirrors:
        session.error(f"monitor session {sid} does not exist")
        return
    del cfg.mirrors[sid]
    if session.commit(cfg, quiet=True):
        session.info(f"Mirror session {sid} removed.")


# --------------------------------------------------------------------------
# interface configuration
# --------------------------------------------------------------------------
def _iface_cfg(session: Session) -> tuple[object, str]:
    name = session.interface
    if not name or name not in session.config.interfaces:
        raise ConfigError("no interface selected")
    return session.config.copy(), name


@command("description", modes=("if", "svi", "vlan", "pc"), help="Set the description", trailing=True)
def cmd_description(session: Session, argv: list[str]) -> None:
    text = " ".join(argv)[:255]
    cfg = session.config.copy()
    if session.mode == "if" and session.interface:
        cfg.interfaces[session.interface].description = text
    elif session.mode == "svi" and session.svi_vlan is not None:
        svi = cfg.svis.setdefault(session.svi_vlan, Svi(session.svi_vlan))
        svi.description = text
    elif session.mode == "vlan" and session.vlan_id is not None:
        cfg.vlans[session.vlan_id].description = text
    elif session.mode == "pc" and session.channel_id is not None:
        pass
    else:
        session.error("no context for description")
        return
    if session.commit(cfg, quiet=True):
        session.info("Description updated.")


@command("no", "description", modes=("if", "svi"), help="Clear the description")
def cmd_no_description(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    if session.mode == "if" and session.interface:
        cfg.interfaces[session.interface].description = ""
    elif session.mode == "svi" and session.svi_vlan is not None and session.svi_vlan in cfg.svis:
        cfg.svis[session.svi_vlan].description = ""
    if session.commit(cfg, quiet=True):
        session.info("Description cleared.")


@command("shutdown", modes=("if", "svi"), help="Administratively disable the port")
def cmd_shutdown(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    if session.mode == "if" and session.interface:
        cfg.interfaces[session.interface].admin_up = False
        target = session.interface
    elif session.mode == "svi" and session.svi_vlan is not None:
        cfg.svis.setdefault(session.svi_vlan, Svi(session.svi_vlan)).admin_up = False
        target = f"Vlan{session.svi_vlan}"
    else:
        session.error("no interface selected")
        return
    if session.commit(cfg, quiet=True):
        session.info(f"{target} administratively down (removed from the forwarding path).")


@command("no", "shutdown", modes=("if", "svi"), help="Enable the port")
def cmd_no_shutdown(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    if session.mode == "if" and session.interface:
        cfg.interfaces[session.interface].admin_up = True
        target = session.interface
    elif session.mode == "svi" and session.svi_vlan is not None:
        cfg.svis.setdefault(session.svi_vlan, Svi(session.svi_vlan)).admin_up = True
        target = f"Vlan{session.svi_vlan}"
    else:
        session.error("no interface selected")
        return
    if session.commit(cfg):
        session.info(f"{target} administratively up.")


@command("switchport", "mode", "<switchport-mode>", modes=("if",), help="access | trunk")
def cmd_switchport_mode(session: Session, argv: list[str]) -> None:
    mode = argv[0].lower() if argv else ""
    if mode not in ("access", "trunk"):
        session.error("mode must be access or trunk")
        return
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    previous_mode = iface.mode
    iface.mode = mode
    iface.attached = True
    # Cisco behaviour: moving a port into trunk mode allows every VLAN until
    # the admin restricts the list explicitly.
    if mode == "trunk" and previous_mode != "trunk":
        iface.trunk_allowed = list(range(1, 4095))
    if session.commit(cfg):
        session.info(f"{iface.name} is now a {mode} switchport.")


@command("switchport", "access", "vlan", "<vlan>", modes=("if",), help="Set the access VLAN")
def cmd_switchport_access(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    try:
        vid = int(argv[0])
        if vid not in cfg.vlans:
            raise ConfigError(f"VLAN {vid} does not exist; create it with 'vlan {vid}' first")
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    iface.access_vlan = vid
    iface.attached = True
    if session.commit(cfg):
        session.info(f"{iface.name} access VLAN {vid}.")


@command("switchport", "trunk", "allowed", "vlan", modes=("if",), help="Set allowed VLAN list (list | all | none | add/remove LIST)", trailing=True)
def cmd_switchport_trunk_allowed(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    try:
        if not argv:
            raise ConfigError("usage: switchport trunk allowed vlan <list|all|none>")
        keyword = argv[0].lower()
        explicit_all = keyword == "all" or (keyword in ("add", "remove") and "all" in {a.lower() for a in argv[1:]})
        explicit_none = keyword == "none" or (keyword in ("add", "remove") and "none" in {a.lower() for a in argv[1:]})
        if keyword == "add":
            vlans = sorted(set(iface.trunk_allowed) | set(parse_vlan_list(",".join(argv[1:]))))
        elif keyword == "remove":
            if explicit_all:
                raise ConfigError("cannot remove 'all'; remove explicit VLAN ids or use 'none'")
            vlans = sorted(set(iface.trunk_allowed) - set(parse_vlan_list(",".join(argv[1:]))))
        elif keyword == "none":
            vlans = []
        else:
            vlans = parse_vlan_list(",".join(argv))
        # An all-VLAN trunk is legal even before those VLANs are created, and it
        # maps onto OVS `trunks=[]` (all).  Never expand it to a snapshot of the
        # VLANs that happen to exist right now.
        missing = [] if explicit_all or explicit_none else [v for v in vlans if v not in cfg.vlans]
        if missing:
            raise ConfigError(f"VLAN(s) {format_vlan_list(missing)} are not configured")
        iface.trunk_allowed = vlans
        iface.attached = True
    except ConfigError as exc:
        session.error(exc.message)
        return
    if session.commit(cfg):
        if not vlans:
            session.info(f"{iface.name} allows no VLANs (port is not forwarding).")
        else:
            session.info(f"{iface.name} allows VLANs {format_vlan_list(vlans)}.")


@command("switchport", "trunk", "native", "vlan", "<vlan>", modes=("if",), help="Set the native VLAN")
def cmd_switchport_native(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    try:
        vid = int(argv[0])
        if vid not in cfg.vlans:
            raise ConfigError(f"VLAN {vid} does not exist")
        if vid in iface.trunk_allowed:
            raise ConfigError(f"VLAN {vid} is in the allowed list; remove it from tagged VLANs first")
        iface.native_vlan = vid
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    if session.commit(cfg):
        session.info(f"{iface.name} native VLAN {vid}.")


@command("no", "switchport", "trunk", "native", "vlan", modes=("if",), help="Remove the native VLAN")
def cmd_no_switchport_native(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    iface.native_vlan = None
    if session.commit(cfg):
        session.info(f"{iface.name} native VLAN removed (all VLANs tagged).")


@command("switchport", "attach", modes=("if",), help="Attach the selected NIC to this switch port", trailing=True)
def cmd_switchport_attach(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    linux = argv[0] if argv else iface.linux_if
    if not linux:
        session.error("no Linux interface known for this port; specify one: switchport attach <nic>")
        return
    known = {row["linux_if"]: row for row in session.engine.port_table()}
    if linux not in known:
        session.error(f"NIC {linux} is not a detected switch port (see 'show interfaces mapping')")
        return
    record = known[linux]
    if record.get("protected"):
        session.error(f"{linux} carries host management connectivity and cannot be enslaved")
        return
    if record["name"] != iface.name:
        session.error(f"{linux} is mapped to {record['name']}, not {iface.name}; refusing to steal a port")
        return
    iface.linux_if = linux
    iface.attached = True
    if session.commit(cfg):
        session.info(f"{iface.name} attached to {linux}.")


@command("no", "switchport", modes=("if",), help="Detach the port from the switch")
def cmd_no_switchport(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    iface.attached = False
    if session.commit(cfg):
        session.info(f"{iface.name} detached from the switch.")


@command("mtu", "<mtu>", modes=("if",), help="Set the port MTU (576-9216)")
def cmd_mtu(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    try:
        value = int(argv[0])
        if not 576 <= value <= 9216:
            raise ConfigError("MTU must be between 576 and 9216")
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    iface.mtu = value
    if session.commit(cfg):
        session.info(f"{iface.name} MTU {value}.")


@command("channel-group", "<group>", "mode", "<mode>", modes=("if",), help="Add the port to a LACP channel")
def cmd_channel_group(session: Session, argv: list[str]) -> None:
    try:
        group = int(argv[0])
        mode = argv[1].lower()
        if mode not in ("active", "passive", "on"):
            raise ConfigError("mode must be active, passive or on")
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    if iface.channel_group and iface.channel_group != group:
        previous = cfg.port_channels.get(iface.channel_group)
        if previous and iface.name in previous.members:
            previous.members.remove(iface.name)
    iface.channel_group = group
    channel = cfg.port_channels.setdefault(group, PortChannel(group, mode=mode))
    channel.mode = mode
    if iface.name not in channel.members:
        channel.members.append(iface.name)
    channel.members.sort()
    if session.commit(cfg):
        members = len(channel.members)
        if members >= 2:
            session.info(f"{iface.name} added to Port-Channel{group} ({mode}), {members} members.")
        else:
            session.info(
                f"{iface.name} added to Port-Channel{group} ({mode}). "
                f"1 member - the LACP bond is created in Open vSwitch once a second member joins; "
                f"until then the port keeps switching on its own."
            )


@command("no", "channel-group", modes=("if",), help="Remove the port from its channel")
def cmd_no_channel_group(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    iface = cfg.interfaces[session.interface]
    if iface.channel_group is None:
        session.error(f"{iface.name} is not in a port-channel")
        return
    channel = cfg.port_channels.get(iface.channel_group)
    if channel and iface.name in channel.members:
        channel.members.remove(iface.name)
    # The port-channel object itself survives (Cisco behaviour): it may hold
    # zero members and is removed explicitly with 'no interface port-channel N'.
    iface.channel_group = None
    if session.commit(cfg):
        session.info(f"{iface.name} removed from its port-channel.")


# --------------------------------------------------------------------------
# VLAN configuration mode
# --------------------------------------------------------------------------
@command("name", modes=("vlan",), help="Set the VLAN name", trailing=True)
def cmd_vlan_name(session: Session, argv: list[str]) -> None:
    if session.vlan_id is None:
        session.error("no VLAN selected")
        return
    name = " ".join(argv).strip()
    cfg = session.config.copy()
    try:
        cfg.vlans[session.vlan_id] = Vlan(session.vlan_id, name, cfg.vlans[session.vlan_id].description)
    except ConfigError as exc:
        session.error(exc.message)
        return
    if session.commit(cfg, quiet=True):
        session.info(f"VLAN {session.vlan_id} name set to {name}.")


# --------------------------------------------------------------------------
# port-channel configuration
# --------------------------------------------------------------------------
@command("lacp", "mode", "<mode>", modes=("pc",), help="LACP mode active|passive|on")
def cmd_lacp_mode(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    channel = cfg.port_channels.get(session.channel_id)
    if not channel:
        session.error("port-channel not found")
        return
    mode = argv[0].lower()
    if mode not in ("active", "passive", "on"):
        session.error("mode must be active, passive or on")
        return
    channel.mode = mode
    if session.commit(cfg):
        session.info(f"Port-Channel{channel.id} LACP mode {mode}.")


@command("load-balance", "<method>", modes=("pc",), help="Hash method")
def cmd_load_balance(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    channel = cfg.port_channels.get(session.channel_id)
    method = argv[0].lower()
    if method not in ("balance-tcp", "balance-slb", "active-backup"):
        session.error("method must be balance-tcp, balance-slb or active-backup")
        return
    channel.lb = method
    if session.commit(cfg):
        session.info(f"Port-Channel{channel.id} load-balance {method}.")


# --------------------------------------------------------------------------
# SVI configuration
# --------------------------------------------------------------------------
@command("ip", "address", "<address>", modes=("svi",), help="Set the SVI IP address")
def cmd_svi_ip(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    try:
        interface = ipaddress.ip_interface(argv[0])
        if interface.version != 4:
            raise ConfigError("only IPv4 addresses are supported")
    except (ValueError, IndexError, ConfigError) as exc:
        session.error(str(exc))
        return
    if not cfg.ip_routing:
        session.error("IP routing is disabled; enable it with 'ip routing' globally first")
        return
    cfg.svis.setdefault(session.svi_vlan, Svi(session.svi_vlan)).ip = str(interface)
    if session.commit(cfg):
        session.info(f"Vlan{session.svi_vlan} address {interface}.")


@command("no", "ip", "address", modes=("svi",), help="Remove the SVI address")
def cmd_no_svi_ip(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    svi = cfg.svis.get(session.svi_vlan)
    if not svi:
        session.error("SVI has no address")
        return
    svi.ip = None
    if session.commit(cfg):
        session.info(f"Vlan{session.svi_vlan} address removed.")


# --------------------------------------------------------------------------
# banners
# --------------------------------------------------------------------------
_BANNER_DELIMITERS = ("^C", "^", "EOF", "#")


def _banner_body(argv: list[str]) -> str | None:
    """Accept ``^Ctext^C``, ``"quoted text"`` or a plain line of text."""
    text = " ".join(argv)
    if not text:
        return None
    for delimiter in _BANNER_DELIMITERS:
        if text.startswith(delimiter):
            rest = text[len(delimiter):]
            if delimiter in rest:
                return rest.split(delimiter, 1)[0]
            return rest or None
    return text


def _set_banner(session: Session, kind: str, text: str) -> None:
    cfg = session.config.copy()
    banner = cfg.banner
    if len(text) > BannerConfig.MAX_LEN:
        session.error(f"banner is limited to {BannerConfig.MAX_LEN} characters")
        return
    setattr(banner, kind, text)
    banner.enabled = bool(banner.login or banner.motd)
    if session.commit(cfg, quiet=True):
        session.info(f"{kind.upper()} banner set ({len(text)} characters).")


@command("banner", "login", modes=("config",), trailing=True, help="Set the login banner (Cisco ^C delimiter)")
def cmd_banner_login(session: Session, argv: list[str]) -> None:
    text = None
    if session.pending_banner and session.pending_banner[0] == "login":
        text = session.pending_banner[1]
    session.pending_banner = None
    text = text if text is not None else _banner_body(argv)
    if not text:
        session.error("incomplete command: use 'banner login ^Ctext^C' or enter a multi-line banner")
        return
    _set_banner(session, "login", text)


@command("banner", "motd", modes=("config",), trailing=True, help="Set the message-of-the-day banner")
def cmd_banner_motd(session: Session, argv: list[str]) -> None:
    text = None
    if session.pending_banner and session.pending_banner[0] == "motd":
        text = session.pending_banner[1]
    session.pending_banner = None
    text = text if text is not None else _banner_body(argv)
    if not text:
        session.error("incomplete command: use 'banner motd ^Ctext^C' or enter a multi-line banner")
        return
    _set_banner(session, "motd", text)


@command("no", "banner", "login", modes=("config",), help="Remove the login banner")
def cmd_no_banner_login(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.banner.login = ""
    cfg.banner.enabled = bool(cfg.banner.motd)
    if session.commit(cfg, quiet=True):
        session.info("Login banner removed.")


@command("no", "banner", "motd", modes=("config",), help="Remove the MOTD banner")
def cmd_no_banner_motd(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.banner.motd = ""
    cfg.banner.enabled = bool(cfg.banner.login)
    if session.commit(cfg, quiet=True):
        session.info("MOTD banner removed.")


# --------------------------------------------------------------------------
# SNMP (read-only v2c agent)
# --------------------------------------------------------------------------
@command("snmp-server", "community", trailing=True, modes=("config",), privilege="admin", help="snmp-server community <name> ro [source-cidr ...]")
def cmd_snmp_community(session: Session, argv: list[str]) -> None:
    if len(argv) < 2:
        session.error("usage: snmp-server community <name> ro [source-cidr ...]")
        return
    name, access, *sources = argv
    cfg = session.config.copy()
    try:
        community = SnmpCommunity(name, access, sources)
    except ConfigError as exc:
        session.error(exc.message)
        return
    cfg.snmp.communities = [c for c in cfg.snmp.communities if c.name != name] + [community]
    cfg.snmp.communities.sort(key=lambda c: c.name)
    cfg.snmp.enabled = True
    if session.commit(cfg, quiet=True):
        acl = ", ".join(community.sources) or "any source"
        session.info(f"SNMP community '{name}' configured read-only ({acl}).")


@command("no", "snmp-server", "community", "<name>", modes=("config",), privilege="admin", help="Remove an SNMP community")
def cmd_no_snmp_community(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    name = argv[0]
    before = len(cfg.snmp.communities)
    cfg.snmp.communities = [c for c in cfg.snmp.communities if c.name != name]
    if len(cfg.snmp.communities) == before:
        session.error(f"SNMP community {name} does not exist")
        return
    if session.commit(cfg, quiet=True):
        session.info(f"SNMP community '{name}' removed.")


@command("snmp-server", "contact", trailing=True, modes=("config",), privilege="admin", help="Set sysContact")
def cmd_snmp_contact(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.snmp.contact = " ".join(argv)
    if session.commit(cfg, quiet=True):
        session.info("SNMP contact set.")


@command("snmp-server", "location", trailing=True, modes=("config",), privilege="admin", help="Set sysLocation")
def cmd_snmp_location(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.snmp.location = " ".join(argv)
    if session.commit(cfg, quiet=True):
        session.info("SNMP location set.")


@command("snmp-server", "name", trailing=True, modes=("config",), privilege="admin", help="Override sysName (default: hostname)")
def cmd_snmp_name(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.snmp.sys_name = " ".join(argv)
    if session.commit(cfg, quiet=True):
        session.info("SNMP sysName set.")


@command("snmp-server", "object-id", trailing=True, modes=("config",), privilege="admin",
         help="Set sysObjectID (1.3.6.1.4.1.<your-PEN>.x); empty resets to the default")
def cmd_snmp_object_id(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.snmp.sys_object_id = " ".join(argv).strip()
    if session.commit(cfg, quiet=True):
        session.info("SNMP sysObjectID set." if cfg.snmp.sys_object_id else "SNMP sysObjectID reset to default.")


@command("snmp-server", "version", "<version>", modes=("config",), privilege="admin",
         help="SNMP protocol version the agent answers (v1 or v2c)")
def cmd_snmp_version(session: Session, argv: list[str]) -> None:
    wanted = argv[0].lower()
    if wanted == "v3":
        session.error("SNMPv3 (USM) is not implemented in this release; use v2c")
        return
    if wanted not in ("v1", "v2c"):
        session.error("SNMP version must be v1 or v2c")
        return
    cfg = session.config.copy()
    try:
        cfg.snmp.version = wanted
        cfg.snmp.__post_init__()
    except ConfigError as exc:
        session.error(getattr(exc, "message", str(exc)))
        return
    if session.commit(cfg, quiet=True):
        session.info(f"SNMP version set to {wanted}.")


@command("snmp-server", "listen", "<address>", trailing=True, modes=("config",), privilege="admin",
         help="snmp-server listen <ip> [port <n>]")
def cmd_snmp_listen(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    values = {"bind": argv[0], "port": cfg.snmp.port}
    rest = [token.lower() for token in argv[1:]]
    if rest:
        if rest[0] == "port" and len(rest) >= 2:
            values["port"] = rest[1]
        else:
            session.error("usage: snmp-server listen <ip> [port <n>]")
            return
    try:
        from makemeswitch.core.models import SnmpConfig

        cfg.snmp = SnmpConfig.from_dict({**cfg.snmp.to_dict(), **values})
    except (ConfigError, ValueError, TypeError) as exc:
        session.error(getattr(exc, "message", str(exc)))
        return
    if session.commit(cfg, quiet=True):
        session.info(f"SNMP agent listening on {cfg.snmp.bind}:{cfg.snmp.port}.")


@command("snmp-server", "enable", modes=("config",), privilege="admin", help="Enable the read-only SNMP agent")
def cmd_snmp_enable(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    if not cfg.snmp.communities:
        session.error("configure a community first: snmp-server community public ro")
        return
    cfg.snmp.enabled = True
    if session.commit(cfg, quiet=True):
        session.info(f"SNMP agent enabled on {cfg.snmp.bind}:{cfg.snmp.port} (read-only v2c).")


@command("no", "snmp-server", modes=("config",), privilege="admin", help="Disable the SNMP agent")
def cmd_no_snmp(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.snmp.enabled = False
    if session.commit(cfg, quiet=True):
        session.info("SNMP agent disabled.")


@command("show", "snmp", modes=("exec", "priv"), help="SNMP agent status and communities")
def cmd_show_snmp(session: Session, argv: list[str]) -> None:
    snmp = session.config.snmp
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Agent", "enabled" if snmp.enabled else "disabled")
    table.add_row("Listen", f"{snmp.bind}:{snmp.port} (UDP, SNMPv2c read-only)")
    table.add_row("sysName", snmp.sys_name or session.config.hostname)
    table.add_row("sysContact", snmp.contact or "-")
    table.add_row("sysLocation", snmp.location or "-")
    session.show(table)
    communities = _table("SNMP communities")
    communities.add_column("Community", no_wrap=True, min_width=12)
    communities.add_column("Access", no_wrap=True, min_width=8)
    communities.add_column("Source ACL", min_width=16)
    for community in snmp.communities:
        # Community strings are credentials: never print them in full.
        masked = community.name[:1] + "*" * (len(community.name) - 1)
        communities.add_row(masked, community.access, ", ".join(community.sources) or "any")
    session.show(communities)


# --------------------------------------------------------------------------
# remote syslog
# --------------------------------------------------------------------------
@command("logging", "host", trailing=True, modes=("config",), help="logging host <ip> [transport udp|tcp] [port N] [severity S] [facility F]")
def cmd_logging_host(session: Session, argv: list[str]) -> None:
    if not argv:
        session.error("usage: logging host <destination> [transport udp|tcp] [port N]")
        return
    cfg = session.config.copy()
    values = {
        "destination": argv[0],
        "transport": cfg.syslog.transport,
        "port": cfg.syslog.port,
        "severity": cfg.syslog.severity,
        "facility": cfg.syslog.facility,
        "fmt": cfg.syslog.fmt,
    }
    index = 1
    keywords = {"transport", "port", "severity", "facility", "format", "fmt"}
    while index < len(argv):
        key = argv[index].lower()
        if key not in keywords or index + 1 >= len(argv):
            session.error(f"unrecognized logging option {argv[index]!r}")
            return
        values["fmt" if key == "format" else key] = argv[index + 1]
        index += 2
    try:
        cfg.syslog = SyslogConfig(enabled=True, source=cfg.syslog.source, **values)
    except (ConfigError, ValueError) as exc:
        session.error(getattr(exc, "message", str(exc)))
        return
    if session.commit(cfg, quiet=True):
        session.info(
            f"Remote syslog enabled: {cfg.syslog.transport.upper()} "
            f"{cfg.syslog.destination}:{cfg.syslog.port} severity {cfg.syslog.severity}."
        )


@command("no", "logging", "host", modes=("config",), help="Disable remote syslog")
def cmd_no_logging_host(session: Session, argv: list[str]) -> None:
    cfg = session.config.copy()
    cfg.syslog.enabled = False
    if session.commit(cfg, quiet=True):
        session.info("Remote syslog disabled.")


@command("show", "logging", "remote", modes=("exec", "priv"), help="Remote syslog destination")
def cmd_show_logging_remote(session: Session, argv: list[str]) -> None:
    cfg = session.config.syslog
    if not cfg.enabled:
        session.info("Remote syslog: disabled.")
        return
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Destination", f"{cfg.destination}:{cfg.port}")
    table.add_row("Transport", cfg.transport.upper())
    table.add_row("Severity", cfg.severity)
    table.add_row("Facility", cfg.facility)
    table.add_row("Format", cfg.fmt)
    table.add_row("Source", cfg.source or session.config.hostname)
    session.show(table)


# --------------------------------------------------------------------------
# local users (Cisco ``username`` syntax, privilege 0-15)
# --------------------------------------------------------------------------
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def _parse_username_args(argv: list[str]) -> tuple[str, int, str, str]:
    """Parse ``<name> [privilege N] secret|password [0|9] <value...>``.

    Returns (username, privilege, secret_type, value).  Raises ``ConfigError``
    with a message that never includes the supplied secret.
    """
    if not argv:
        raise ConfigError("usage: username <name> privilege <0-15> secret [0|9] <value>")
    username = argv[0]
    if not USERNAME_RE.match(username):
        raise ConfigError("username must be 1-32 characters: letters, digits, _ . -")
    index = 1
    privilege = 1
    if index < len(argv) and argv[index].lower() == "privilege":
        if index + 1 >= len(argv):
            raise ConfigError("privilege requires a value 0-15")
        try:
            privilege = int(argv[index + 1])
        except ValueError as exc:
            raise ConfigError("privilege must be a number 0-15") from exc
        if not 0 <= privilege <= 15:
            raise ConfigError("privilege must be 0-15")
        index += 2
    if index >= len(argv) or argv[index].lower() not in ("secret", "password"):
        raise ConfigError("expected 'secret' after the username (or 'secret 0 <text>')")
    index += 1
    secret_type = "0"
    if index < len(argv) and argv[index] in ("0", "9"):
        secret_type = argv[index]
        index += 1
    value = " ".join(argv[index:])
    if not value:
        if secret_type == "9":
            raise ConfigError(
                "type 9 secret expects a Cisco scrypt hash ($9$<salt>$<hash>), not an empty value"
            )
        raise ConfigError("secret value is required")
    return username, privilege, secret_type, value


@command("username", trailing=True, modes=("config",), privilege="admin", help="username <name> privilege 0-15 secret [0|9] <value>")
def cmd_username(session: Session, argv: list[str]) -> None:
    from makemeswitch.core.security import SecurityService, privilege_to_role
    from makemeswitch.core import cisco9

    try:
        username, privilege, secret_type, value = _parse_username_args(argv)
    except ConfigError as exc:
        session.error(exc.message)
        return
    role = privilege_to_role(privilege)
    service = SecurityService(session.engine.store)
    exists = service.get_user(username) is not None
    try:
        if secret_type == "9":
            # Never fall back to treating the value as plaintext: a clear,
            # actionable error is the only correct behaviour.
            service.import_cisco9(username, value, role, create=not exists)
            scheme = "Cisco type 9 (scrypt)"
        else:
            service.set_credentials(username, value, role, create=not exists)
            scheme = "Argon2id"
    except (ValueError, cisco9.Cisco9Error) as exc:
        session.error(str(exc))
        return
    session.engine.store.audit(
        session.username,
        None,
        "user-create" if not exists else "user-update",
        username,
        "success",
        f"privilege {privilege} ({role}), {scheme}",
    )
    session.info(f"User '{username}' {'created' if not exists else 'updated'}: privilege {privilege} ({role}), {scheme}.")


@command("no", "username", "<name>", modes=("config",), privilege="admin", help="Delete a local user")
def cmd_no_username(session: Session, argv: list[str]) -> None:
    from makemeswitch.core.security import SecurityService

    service = SecurityService(session.engine.store)
    username = argv[0]
    if not service.get_user(username):
        session.error(f"user {username} does not exist")
        return
    try:
        service.delete_user(username, actor=session.username)
    except ValueError as exc:
        session.error(str(exc))
        return
    session.engine.store.audit(session.username, None, "user-delete", username, "success")
    session.info(f"User '{username}' deleted.")


@command("show", "users", modes=("priv",), privilege="admin", help="Local users and privilege levels")
def cmd_show_users(session: Session, argv: list[str]) -> None:
    from makemeswitch.core.security import SecurityService, role_to_privilege

    table = _table("Local users")
    for column in ("Username", "Privilege", "Role", "Secret", "Status"):
        table.add_column(column)
    for user in session.engine.store.list_users(include_secrets=True):
        scheme = SecurityService.password_scheme(user.get("password_hash", ""))
        label = {"cisco9": "type 9 (scrypt)", "argon2id": "argon2id"}.get(scheme, "unknown")
        table.add_row(
            user["username"],
            str(role_to_privilege(user["role"])),
            user["role"],
            label,
            "disabled" if user["disabled"] else "active",
        )
    session.show(table)
