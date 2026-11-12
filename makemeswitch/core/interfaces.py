"""Interface discovery backed by sysfs and ``ip -j``.

Discovery is read-only.  Turning a NIC into a switch port only ever happens
through the configuration engine, after an explicit confirmation.

We deliberately avoid pyroute2: instantiating its netlink sockets inside a
running asyncio/uvloop event loop raises ``invalid family 16`` and leaves
broken sockets behind.  ``ip -j`` gives the same data without any event-loop
interaction, and is always present on Debian/Ubuntu (iproute2 dependency).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from makemeswitch.utils.logging import get_logger

log = get_logger("makemeswitch.netlink")

SYS_NET = Path("/sys/class/net")
VIRTUAL_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "zt", "tun", "tap", "wg", "bond")


def _ip_json(*args: str) -> Any:
    """Run ``ip -j ARGS`` and return parsed JSON, or None on failure."""
    try:
        proc = subprocess.run(
            ["ip", "-j", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        log.debug("ip -j failed", extra={"data": {"args": list(args), "error": str(exc)}})
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "[]")
    except ValueError:  # pragma: no cover
        return None


@dataclass
class LinkInfo:
    name: str
    mac: str = ""
    mtu: int = 1500
    operstate: str = "unknown"
    carrier: bool = False
    speed: int | None = None
    duplex: str | None = None
    driver: str | None = None
    identity: str | None = None
    kind: str = "virtual"
    pci_slot: str | None = None
    usb_path: str | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mac": self.mac,
            "mtu": self.mtu,
            "operstate": self.operstate,
            "carrier": self.carrier,
            "link_up": self.carrier and self.operstate in ("up", "unknown"),
            "speed": self.speed,
            "duplex": self.duplex,
            "driver": self.driver,
            "identity": self.identity,
            "kind": self.kind,
            "pci_slot": self.pci_slot,
            "usb_path": self.usb_path,
            "description": self.description,
        }


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def interface_identity(name: str) -> tuple[str | None, str, str | None, str | None]:
    """Return (identity, kind, pci_slot, usb_path) using stable sysfs topology.

    PCI devices are keyed by their PCI address, USB adapters by their
    bus/port path.  Both survive a reboot, unlike kernel-assigned names.
    """
    link = SYS_NET / name
    try:
        device = (link / "device").resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "virtual", None, None
    path = str(device)
    usb_match = re.search(r"/usb\d+/([0-9]+-[0-9.]+)(?::[0-9.]+)?", path)
    if usb_match:
        return f"usb:{usb_match.group(1)}", "usb", None, usb_match.group(1)
    pci_matches = re.findall(r"(?:^|/)([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])(?=/|$)", path)
    if pci_matches:
        slot = pci_matches[-1]  # endpoint, never a shared upstream bridge
        return f"pci:{slot}", "pci", slot, None
    # Fallback: interface controller directory basename (still topology based).
    return f"path:{path}", "pci", None, None


def describe(name: str) -> LinkInfo:
    base = SYS_NET / name
    mac = _read(base / "address")
    mtu_raw = _read(base / "mtu", "1500")
    try:
        mtu = int(mtu_raw)
    except ValueError:
        mtu = 1500
    operstate = _read(base / "operstate", "unknown")
    carrier = _read(base / "carrier", "0") == "1"
    speed: int | None = None
    speed_raw = _read(base / "speed", "")
    if speed_raw.lstrip("-").isdigit():
        value = int(speed_raw)
        speed = value if value > 0 else None
    duplex = _read(base / "duplex", "") or None
    driver = None
    try:
        resolved = (base / "device" / "driver").resolve()
        if resolved.parent.name == "drivers":
            driver = resolved.name
    except (OSError, RuntimeError):
        driver = None
    identity, kind, pci_slot, usb_path = interface_identity(name)
    desc = driver or kind
    if usb_path:
        desc = f"{driver or 'usb'} @ USB {usb_path}"
    return LinkInfo(
        name=name,
        mac=mac,
        mtu=mtu,
        operstate=operstate,
        carrier=carrier,
        speed=speed,
        duplex=duplex,
        driver=driver,
        identity=identity,
        kind=kind,
        pci_slot=pci_slot,
        usb_path=usb_path,
        description=desc,
    )


def list_links() -> list[LinkInfo]:
    if not SYS_NET.exists():
        return []
    return [describe(entry.name) for entry in sorted(SYS_NET.iterdir()) if entry.is_dir()]


def default_route_interfaces() -> set[str]:
    """Interfaces carrying the default route (management connectivity)."""
    result: set[str] = set()
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 2 and fields[1] == "00000000":
                    result.add(fields[0])
    except OSError:
        pass
    routes = _ip_json("route", "show", "default")
    for route in routes or []:
        dev = route.get("dev")
        if dev:
            result.add(dev)
    return {r for r in result if r}


def addresses_by_interface() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for entry in _ip_json("addr", "show") or []:
        ifname = entry.get("ifname")
        if not ifname:
            continue
        addresses = [
            info["local"]
            for info in entry.get("addr_info", [])
            if info.get("family") == "inet" and info.get("local")
        ]
        if addresses:
            mapping[ifname] = addresses
    return mapping


def protected_interfaces(extra: list[str] | None = None) -> list[str]:
    """Interfaces that must never be enslaved into the switch."""
    protected = set(default_route_interfaces())
    protected.update(addr for addr, ips in addresses_by_interface().items() if ips)
    ssh_conn = os.environ.get("SSH_CONNECTION", "").split()
    if len(ssh_conn) >= 3:
        client_ip, server_ip = ssh_conn[0], ssh_conn[2]
        for ifname, ips in addresses_by_interface().items():
            if server_ip in ips:
                protected.add(ifname)
    for name in extra or []:
        protected.add(name)
    return sorted(protected)


# Interfaces that are never switch ports, whatever their MAC or topology.
INTERNAL_NAMES = {"lo", "ovs-system", "ovs-netdev"}
INTERNAL_PREFIXES = (
    "mms",      # our own OVS bridge
    "osw",      # legacy bridge name
    "br-",
    "veth",
    "docker",
    "virbr",
    "vnet",
    "zt",       # ZeroTier
    "tun",
    "tap",
    "wg",
    "bond",
    "dummy",
)
# A kernel name that means "this really is a NIC".
NIC_NAME_RE = re.compile(r"^(?:en[a-z0-9]*\d\w*|eth\d+\w*|Ethernet\d+|mmstmp\d+x*)$", re.I)


def is_internal_link(name: str) -> bool:
    if re.fullmatch(r"mmstmp[0-9]+x*", name):
        return False  # interrupted 0.1.x rename: recover physical NICs
    return name in INTERNAL_NAMES or name.startswith(INTERNAL_PREFIXES)


def physical_links(include_virtual: bool = False, exclude: set[str] | None = None) -> list[LinkInfo]:
    """Physical Ethernet NICs, excluding management and internal interfaces.

    ``exclude`` carries names that are dynamic (the OVS bridge) on top of the
    static blocklist, so ``mms0``/``ovs-system`` can never be mistaken for a
    switch port again.
    """
    protected = set(protected_interfaces()) | set(exclude or ())
    result = []
    for link in list_links():
        if is_internal_link(link.name) or link.name in protected:
            continue
        if include_virtual:
            # Test mode: only dedicated virtual test interfaces.
            if not re.match(r"^(osw|test)[1-9][0-9]*$", link.name):
                continue
            result.append(link)
            continue
        # A NIC whose sysfs entry cannot be resolved (some virtio/hypervisor
        # setups) is still a real port as long as its kernel name is a NIC name
        # and it carries a MAC; identity then falls back to the MAC.
        if link.kind in ("pci", "usb") and link.identity:
            result.append(link)
        elif NIC_NAME_RE.match(link.name) and link.mac:
            result.append(link)
    return result


def link_by_name(name: str) -> LinkInfo | None:
    for link in list_links():
        if link.name == name:
            return link
    return None


def set_mtu(name: str, mtu: int) -> None:
    """Set MTU on a physical NIC (requires CAP_NET_ADMIN)."""
    if not is_valid_ifname(name):
        raise RuntimeError(f"invalid interface name: {name!r}")
    if not 576 <= int(mtu) <= 9216:
        raise RuntimeError(f"MTU out of range: {mtu}")
    proc = subprocess.run(
        ["ip", "link", "set", "dev", name, "mtu", str(int(mtu))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"failed to set MTU on {name}")


def is_valid_ifname(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_@.-]{1,15}$", name))
