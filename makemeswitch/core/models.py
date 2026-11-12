"""Declarative configuration model.

The whole switch configuration is one JSON-serialisable document.  Nothing in
this module talks to the kernel: it only normalises and validates intent so
that both the CLI and the REST API share exactly the same semantics.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from makemeswitch.core import portnames

VLAN_MIN, VLAN_MAX = 1, 4094
DEFAULT_VLAN = 1
IF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")
_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOSTNAME_RE = re.compile(rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*$")
MAX_HOSTNAME_LENGTH = 253
DESCRIPTION_RE = re.compile(r"^[\x20-\x7E]{0,255}$")

MODES = ("access", "trunk")
LACP_MODES = ("active", "passive", "on")
ROLES = ("admin", "operator", "readonly")
STP_MODES = ("stp", "rstp", "none")


class ConfigError(ValueError):
    """Raised when a configuration document or command argument is invalid."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "up", "enabled")
    return False


def _vlan_id_list(value: Any) -> list[int]:
    """Normalise a VLAN list; ``all`` -> 1-4094 and ``none``/``[]`` -> []."""
    if isinstance(value, str):
        return parse_vlan_list(value)
    return sorted({int(v) for v in (value or [])})


@dataclass
class Vlan:
    vid: int
    name: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        self.vid = int(self.vid)
        if not VLAN_MIN <= self.vid <= VLAN_MAX:
            raise ConfigError(f"VLAN ID {self.vid} out of range ({VLAN_MIN}-{VLAN_MAX})")
        if not self.name:
            self.name = f"VLAN{self.vid:04d}"
        if not re.match(r"^[A-Za-z0-9_.:-]{1,32}$", self.name):
            raise ConfigError(f"invalid VLAN name {self.name!r} (letters, digits, _ . : - only)")

    def to_dict(self) -> dict[str, Any]:
        return {"vid": self.vid, "name": self.name, "description": self.description}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Vlan":
        return cls(int(data["vid"]), data.get("name", ""), data.get("description", ""))


@dataclass
class InterfaceConfig:
    """One switch port (maps 1:1 onto a Linux netdev)."""

    name: str
    identity: str | None = None
    linux_if: str | None = None
    description: str = ""
    admin_up: bool = True
    attached: bool = False
    mode: str = "access"
    access_vlan: int = DEFAULT_VLAN
    trunk_allowed: list[int] = field(default_factory=lambda: [DEFAULT_VLAN])
    native_vlan: int | None = None
    mtu: int = 1500
    channel_group: int | None = None

    def __post_init__(self) -> None:
        self.name = normalize_interface_name(self.name)
        if self.mode not in MODES:
            raise ConfigError(f"invalid switchport mode {self.mode!r}", "mode")
        if not VLAN_MIN <= int(self.access_vlan) <= VLAN_MAX:
            raise ConfigError(f"invalid access VLAN {self.access_vlan}", "access_vlan")
        self.access_vlan = int(self.access_vlan)
        self.trunk_allowed = _vlan_id_list(self.trunk_allowed)

        for vid in self.trunk_allowed:
            if not VLAN_MIN <= vid <= VLAN_MAX:
                raise ConfigError(f"invalid VLAN {vid} in allowed list", "trunk_allowed")
        if self.native_vlan is not None:
            self.native_vlan = int(self.native_vlan)
            if not VLAN_MIN <= self.native_vlan <= VLAN_MAX:
                raise ConfigError(f"invalid native VLAN {self.native_vlan}", "native_vlan")
        self.mtu = int(self.mtu)
        if not 576 <= self.mtu <= 9216:
            raise ConfigError("MTU must be between 576 and 9216", "mtu")
        if not DESCRIPTION_RE.match(self.description):
            raise ConfigError("description must be printable ASCII, max 255 chars", "description")

    @property
    def trunk_all(self) -> bool:
        """Full VLAN range is all; an empty allowed list explicitly means none."""
        return set(self.trunk_allowed) == set(range(VLAN_MIN, VLAN_MAX + 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "identity": self.identity,
            "linux_if": self.linux_if,
            "description": self.description,
            "admin_up": self.admin_up,
            "attached": self.attached,
            "mode": self.mode,
            "access_vlan": self.access_vlan,
            "trunk_allowed": list(self.trunk_allowed),
            "native_vlan": self.native_vlan,
            "mtu": self.mtu,
            "channel_group": self.channel_group,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InterfaceConfig":
        return cls(
            name=data["name"],
            identity=data.get("identity"),
            linux_if=data.get("linux_if"),
            description=data.get("description", ""),
            admin_up=_bool(data.get("admin_up", True)),
            attached=_bool(data.get("attached", False)),
            mode=data.get("mode", "access"),
            access_vlan=int(data.get("access_vlan", DEFAULT_VLAN)),
            trunk_allowed=_vlan_id_list(data.get("trunk_allowed", [DEFAULT_VLAN])),
            native_vlan=None if data.get("native_vlan") in (None, "") else int(data["native_vlan"]),
            mtu=int(data.get("mtu", 1500)),
            channel_group=None if data.get("channel_group") in (None, "") else int(data["channel_group"]),
        )


@dataclass
class PortChannel:
    id: int
    mode: str = "active"
    members: list[str] = field(default_factory=list)
    lb: str = "balance-tcp"

    def __post_init__(self) -> None:
        self.id = int(self.id)
        if not 1 <= self.id <= 128:
            raise ConfigError("port-channel id must be 1-128")
        if self.mode not in LACP_MODES:
            raise ConfigError(f"invalid LACP mode {self.mode!r} (active/passive/on)")
        self.members = [normalize_interface_name(m) for m in self.members]

    @property
    def name(self) -> str:
        return f"bond{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "mode": self.mode, "members": list(self.members), "lb": self.lb}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortChannel":
        return cls(int(data["id"]), data.get("mode", "active"), list(data.get("members", [])), data.get("lb", "balance-tcp"))


@dataclass
class MirrorSession:
    id: int
    source: list[str] = field(default_factory=list)
    destination: str | None = None
    direction: str = "both"  # both|ingress|egress (OVS supports both/ingress/egress via select_*)

    def __post_init__(self) -> None:
        self.id = int(self.id)
        if not 1 <= self.id <= 64:
            raise ConfigError("mirror session id must be 1-64")
        if self.direction not in ("both", "ingress", "egress"):
            raise ConfigError("mirror direction must be both, ingress or egress")
        self.source = [normalize_interface_name(s) for s in self.source]
        if not self.source:
            raise ConfigError("mirror session needs at least one source interface")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source": list(self.source), "destination": self.destination, "direction": self.direction}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MirrorSession":
        return cls(int(data["id"]), list(data.get("source", [])), data.get("destination"), data.get("direction", "both"))


@dataclass
class StpConfig:
    enabled: bool = True
    mode: str = "rstp"
    priority: int = 32768

    def __post_init__(self) -> None:
        self.enabled = _bool(self.enabled)
        if self.mode not in STP_MODES:
            raise ConfigError("spanning-tree mode must be stp, rstp or none")
        self.priority = int(self.priority)
        if not 0 <= self.priority <= 61440 or self.priority % 4096:
            raise ConfigError("spanning-tree priority must be a multiple of 4096 between 0 and 61440")

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "mode": self.mode, "priority": self.priority}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StpConfig":
        return cls(_bool(data.get("enabled", True)), data.get("mode", "rstp"), int(data.get("priority", 32768)))


@dataclass
class Svi:
    vlan: int
    ip: str | None = None
    admin_up: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        self.vlan = int(self.vlan)
        if not VLAN_MIN <= self.vlan <= VLAN_MAX:
            raise ConfigError("SVI VLAN out of range")
        if self.ip:
            self.ip = str(ipaddress.ip_interface(self.ip))

    def to_dict(self) -> dict[str, Any]:
        return {"vlan": self.vlan, "ip": self.ip, "admin_up": self.admin_up, "description": self.description}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Svi":
        return cls(int(data["vlan"]), data.get("ip"), _bool(data.get("admin_up", True)), data.get("description", ""))


@dataclass
class StaticRoute:
    prefix: str
    nexthop: str | None = None
    interface: str | None = None

    def __post_init__(self) -> None:
        self.prefix = str(ipaddress.ip_network(self.prefix, strict=False))
        if self.nexthop:
            self.nexthop = str(ipaddress.ip_address(self.nexthop))
        if not self.nexthop and not self.interface:
            raise ConfigError("static route needs a next-hop or an interface")

    def to_dict(self) -> dict[str, Any]:
        return {"prefix": self.prefix, "nexthop": self.nexthop, "interface": self.interface}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StaticRoute":
        return cls(data["prefix"], data.get("nexthop"), data.get("interface"))


@dataclass
class BannerConfig:
    """Cisco-style login/MOTD banners."""

    enabled: bool = False
    login: str = ""
    motd: str = ""
    MAX_LEN = 2048

    def __post_init__(self) -> None:
        self.enabled = _bool(self.enabled)
        self.login = _banner_text(self.login, "login banner")
        self.motd = _banner_text(self.motd, "motd banner")
        if not self.login and not self.motd:
            self.enabled = False

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "login": self.login, "motd": self.motd}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BannerConfig":
        return cls(_bool(data.get("enabled", False)), data.get("login", ""), data.get("motd", ""))


@dataclass
class SnmpCommunity:
    """One read-only SNMPv2c community with an optional source ACL."""

    name: str
    access: str = "ro"
    sources: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = str(self.name)
        if not 1 <= len(self.name) <= 32 or not re.fullmatch(r"[!-~]+", self.name):
            raise ConfigError("SNMP community must be 1-32 printable characters without spaces")
        self.access = str(self.access).lower()
        if self.access not in ("ro",):
            raise ConfigError("only read-only (ro) SNMP communities are supported")
        normalised: list[str] = []
        for source in self.sources or []:
            try:
                normalised.append(str(ipaddress.ip_network(str(source), strict=False)))
            except ValueError as exc:
                raise ConfigError(f"invalid SNMP source ACL entry {source!r}") from exc
        self.sources = sorted(set(normalised))

    def allows(self, address: str) -> bool:
        if not self.sources:
            return True
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(cidr) for cidr in self.sources)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "access": self.access, "sources": list(self.sources)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnmpCommunity":
        return cls(str(data.get("name", "")), data.get("access", "ro"), list(data.get("sources") or []))


@dataclass
class SnmpConfig:
    # ON by default with the conventional read-only "public" community so an
    # NMS discovers the switch out of the box; restrict it with
    # ``snmp-server community public ro <cidr>`` or turn it off entirely.
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 161
    contact: str = ""
    location: str = ""
    sys_name: str = ""
    sys_object_id: str = ""
    communities: list[SnmpCommunity] = field(default_factory=lambda: [SnmpCommunity("public")])
    #: "v2c" answers SNMPv1 and v2c (exactly like IOS); "v1" restricts the
    #: agent to SNMPv1. SNMPv3/USM is not implemented in this release.
    version: str = "v2c"

    def __post_init__(self) -> None:
        self.enabled = _bool(self.enabled)
        try:
            self.bind = str(ipaddress.ip_address(str(self.bind)))
        except ValueError as exc:
            raise ConfigError("SNMP bind address must be an IP address") from exc
        self.port = int(self.port)
        if not 1 <= self.port <= 65535:
            raise ConfigError("SNMP port must be 1-65535")
        self.contact = _short_text(self.contact, "SNMP contact", 255)
        self.location = _short_text(self.location, "SNMP location", 255)
        self.sys_name = _short_text(self.sys_name, "SNMP sysName override", 255)
        self.sys_object_id = str(self.sys_object_id).strip().lstrip(".")
        if self.sys_object_id:
            if not re.fullmatch(r"(?:[0-9]+)(?:\.[0-9]+)+", self.sys_object_id):
                raise ConfigError("SNMP sysObjectID must be a numeric OID such as 1.3.6.1.4.1.<PEN>")
            if len(self.sys_object_id) > 128:
                raise ConfigError("SNMP sysObjectID is too long")
        self.version = str(self.version or "v2c").lower().replace("snmp", "")
        if self.version not in ("v1", "v2c"):
            if self.version == "v3":
                raise ConfigError(
                    "SNMPv3 (USM) is not implemented in this release; use v2c"
                )
            raise ConfigError("SNMP version must be v1 or v2c")
        self.communities = [c if isinstance(c, SnmpCommunity) else SnmpCommunity.from_dict(c) for c in self.communities]
        names = [c.name for c in self.communities]
        if len(names) != len(set(names)):
            raise ConfigError("SNMP community names must be unique")

    def community(self, name: str) -> SnmpCommunity | None:
        for community in self.communities:
            if community.name == name:
                return community
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "bind": self.bind,
            "port": self.port,
            "contact": self.contact,
            "location": self.location,
            "sys_name": self.sys_name,
            "sys_object_id": self.sys_object_id,
            "communities": [c.to_dict() for c in self.communities],
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnmpConfig":
        return cls(
            _bool(data.get("enabled", False)),
            data.get("bind", "0.0.0.0"),
            int(data.get("port", 161)),
            data.get("contact", ""),
            data.get("location", ""),
            data.get("sys_name", ""),
            data.get("sys_object_id", ""),
            [SnmpCommunity.from_dict(c) for c in data.get("communities") or [{"name": "public"}]],
            str(data.get("version", "v2c")),
        )


SYSLOG_SEVERITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
SYSLOG_FACILITIES = (
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news",
    "uucp", "cron", "authpriv", "ftp", "local0", "local1", "local2",
    "local3", "local4", "local5", "local6", "local7",
)


@dataclass
class SyslogConfig:
    """Remote syslog (RFC 5424/3164) over UDP or TCP."""

    enabled: bool = False
    destination: str = ""
    transport: str = "udp"
    port: int = 514
    severity: str = "info"
    facility: str = "local0"
    fmt: str = "rfc5424"
    source: str = ""

    def __post_init__(self) -> None:
        self.enabled = _bool(self.enabled)
        self.destination = str(self.destination).strip()
        self.transport = str(self.transport).lower()
        if self.transport not in ("udp", "tcp"):
            raise ConfigError("syslog transport must be udp or tcp")
        self.port = int(self.port)
        if not 1 <= self.port <= 65535:
            raise ConfigError("syslog port must be 1-65535")
        self.severity = str(self.severity).lower()
        if self.severity not in SYSLOG_SEVERITIES:
            raise ConfigError("syslog severity must be one of " + ", ".join(SYSLOG_SEVERITIES))
        self.facility = str(self.facility).lower()
        if self.facility not in SYSLOG_FACILITIES:
            raise ConfigError("invalid syslog facility")
        self.fmt = str(self.fmt).lower()
        if self.fmt not in ("rfc5424", "rfc3164"):
            raise ConfigError("syslog format must be rfc5424 or rfc3164")
        self.source = _short_text(self.source, "syslog source hostname", 255)
        if self.destination:
            if re.search(r"[\s/]", self.destination) or len(self.destination) > 253:
                raise ConfigError("invalid syslog destination")
        if self.enabled and not self.destination:
            raise ConfigError("remote syslog is enabled but no destination is configured")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "destination": self.destination,
            "transport": self.transport,
            "port": self.port,
            "severity": self.severity,
            "facility": self.facility,
            "format": self.fmt,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyslogConfig":
        return cls(
            _bool(data.get("enabled", False)),
            data.get("destination", ""),
            data.get("transport", "udp"),
            int(data.get("port", 514)),
            data.get("severity", "info"),
            data.get("facility", "local0"),
            data.get("format", data.get("fmt", "rfc5424")),
            data.get("source", ""),
        )


def _banner_text(value: Any, field_name: str) -> str:
    text = str(value or "")
    if len(text) > BannerConfig.MAX_LEN:
        raise ConfigError(f"{field_name} is limited to {BannerConfig.MAX_LEN} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ConfigError(f"{field_name} contains control characters")
    return text


def _short_text(value: Any, field_name: str, limit: int) -> str:
    text = str(value or "")
    if len(text) > limit:
        raise ConfigError(f"{field_name} is limited to {limit} characters")
    if any(ord(char) < 32 for char in text):
        raise ConfigError(f"{field_name} contains control characters")
    return text


@dataclass
class SwitchConfig:
    hostname: str = "Switch"
    domain_name: str = ""
    vlans: dict[int, Vlan] = field(default_factory=lambda: {DEFAULT_VLAN: Vlan(DEFAULT_VLAN, "default")})
    interfaces: dict[str, InterfaceConfig] = field(default_factory=dict)
    port_channels: dict[int, PortChannel] = field(default_factory=dict)
    mirrors: dict[int, MirrorSession] = field(default_factory=dict)
    stp: StpConfig = field(default_factory=StpConfig)
    lldp_enabled: bool = False
    ip_routing: bool = False
    svis: dict[int, Svi] = field(default_factory=dict)
    routes: list[StaticRoute] = field(default_factory=list)
    protected_interfaces: list[str] = field(default_factory=list)
    banner: BannerConfig = field(default_factory=BannerConfig)
    snmp: SnmpConfig = field(default_factory=SnmpConfig)
    syslog: SyslogConfig = field(default_factory=SyslogConfig)
    mgmt: dict[str, Any] = field(default_factory=lambda: {"web_enabled": True})
    schema_version: int = 2

    def __post_init__(self) -> None:
        self.set_hostname(self.hostname)
        self.reindex_interfaces()
        if not self.vlans:
            self.vlans = {DEFAULT_VLAN: Vlan(DEFAULT_VLAN, "default")}
        if DEFAULT_VLAN not in self.vlans:
            self.vlans[DEFAULT_VLAN] = Vlan(DEFAULT_VLAN, "default")

    # --- helpers ---------------------------------------------------------
    def set_hostname(self, hostname: str) -> None:
        hostname = hostname.strip()
        if len(hostname) > MAX_HOSTNAME_LENGTH or not HOSTNAME_RE.match(hostname):
            raise ConfigError(
                "invalid hostname: dot-separated labels of 1-63 chars "
                "(letters, digits, hyphens; no leading/trailing '-' or '.'), "
                f"up to {MAX_HOSTNAME_LENGTH} chars total"
            )
        self.hostname = hostname

    def vlan(self, vid: int) -> Vlan:
        return self.vlans[int(vid)]

    def interface(self, name: str) -> InterfaceConfig:
        key = normalize_interface_name(name)
        if key not in self.interfaces:
            raise ConfigError(f"interface {key} does not exist")
        return self.interfaces[key]

    def add_vlan(self, vid: int, name: str | None = None) -> Vlan:
        vid = int(vid)
        if vid in self.vlans:
            if name:
                self.vlans[vid].name = name
            return self.vlans[vid]
        self.vlans[vid] = Vlan(vid, name or f"VLAN{vid:04d}")
        return self.vlans[vid]

    def delete_vlan(self, vid: int) -> None:
        vid = int(vid)
        if vid == DEFAULT_VLAN:
            raise ConfigError("VLAN 1 is the default VLAN and cannot be removed")
        if vid not in self.vlans:
            raise ConfigError(f"VLAN {vid} does not exist")
        for iface in self.interfaces.values():
            if iface.mode == "access" and iface.access_vlan == vid:
                raise ConfigError(f"VLAN {vid} is the access VLAN of {iface.name}; move the port first")
            if vid in iface.trunk_allowed and not iface.trunk_all:
                raise ConfigError(f"VLAN {vid} is allowed on trunk {iface.name}; remove it from the trunk first")
            if iface.native_vlan == vid:
                raise ConfigError(f"VLAN {vid} is the native VLAN of {iface.name}")
        if vid in self.svis:
            raise ConfigError(f"VLAN {vid} has an SVI; remove the interface vlan first")
        del self.vlans[vid]

    def reindex_interfaces(self) -> bool:
        """Re-key ``interfaces`` by the canonical port name (see ``portnames``).

        Old configurations used ``Ethernet1/1``; the switch now names ports
        ``Ethernet1``, ``Ethernet2`` …  Loading an old config must keep the port
        settings and identities, only the key changes.
        """
        changed = False
        for key in list(self.interfaces):
            iface = self.interfaces[key]
            if iface.name == key:
                continue
            if iface.name in self.interfaces:
                raise ConfigError(f"two interfaces resolve to the same name {iface.name}")
            self.interfaces.pop(key)
            self.interfaces[iface.name] = iface
            changed = True
        for channel in self.port_channels.values():
            members = [normalize_interface_name(member) for member in channel.members]
            if members != channel.members:
                channel.members = members
                changed = True
        return changed

    def validate(self) -> list[str]:
        errors: list[str] = []
        self.reindex_interfaces()  # self-heal legacy Ethernet1/1 keys
        try:
            SwitchConfig.from_dict(self.to_dict())
        except ConfigError as exc:  # pragma: no cover - defensive
            return [exc.message]
        if not self.vlans:
            errors.append("at least VLAN 1 must exist")
        for vid, vlan in self.vlans.items():
            if vid != vlan.vid:
                errors.append(f"VLAN key {vid} does not match id {vlan.vid}")
            if not VLAN_MIN <= vid <= VLAN_MAX:
                errors.append(f"VLAN {vid} out of range")
        names = [v.name.lower() for v in self.vlans.values()]
        if len(names) != len(set(names)):
            errors.append("VLAN names must be unique")
        for name, iface in self.interfaces.items():
            if name != iface.name:
                errors.append(f"interface key {name} != {iface.name}")
            if iface.mode == "access" and iface.access_vlan not in self.vlans:
                errors.append(f"{iface.name}: access VLAN {iface.access_vlan} is not configured")
            if iface.mode == "trunk":
                missing = [] if iface.trunk_all else [v for v in iface.trunk_allowed if v not in self.vlans]
                if missing:
                    errors.append(f"{iface.name}: trunk allows undefined VLANs {missing}")
                if iface.native_vlan is not None:
                    if iface.native_vlan not in self.vlans:
                        errors.append(f"{iface.name}: native VLAN {iface.native_vlan} is not configured")
                    if iface.native_vlan in iface.trunk_allowed and not iface.trunk_all:
                        errors.append(f"{iface.name}: native VLAN {iface.native_vlan} must not also be tagged in the allowed list")
            if iface.channel_group is not None and iface.channel_group not in self.port_channels:
                errors.append(f"{iface.name}: channel-group {iface.channel_group} does not exist")
            if iface.linux_if and iface.linux_if in self.protected_interfaces:
                errors.append(f"{iface.name}: refusing to manage protected management interface {iface.linux_if}")
            if iface.attached and not iface.linux_if:
                errors.append(f"{iface.name}: port is attached but no physical NIC is mapped to it")
        for pc in self.port_channels.values():
            # An empty port-channel is legal (Cisco lets you pre-create it and
            # add members later); it simply is not programmed into OVS until
            # two or more members exist.
            for member in pc.members:
                if member not in self.interfaces:
                    errors.append(f"port-channel {pc.id}: member {member} does not exist")
                elif self.interfaces[member].channel_group != pc.id:
                    errors.append(f"port-channel {pc.id}: member {member} not bound to the channel")
        for mirror in self.mirrors.values():
            if mirror.destination and mirror.destination not in self.interfaces:
                errors.append(f"mirror {mirror.id}: destination {mirror.destination} does not exist")
            for src in mirror.source:
                if src not in self.interfaces:
                    errors.append(f"mirror {mirror.id}: source {src} does not exist")
                if src == mirror.destination:
                    errors.append(f"mirror {mirror.id}: source and destination must differ")
        for svi in self.svis.values():
            if svi.vlan not in self.vlans:
                errors.append(f"SVI {svi.vlan}: VLAN is not configured")
        seen_prefixes = set()
        for route in self.routes:
            if route.prefix in seen_prefixes:
                errors.append(f"duplicate static route {route.prefix}")
            seen_prefixes.add(route.prefix)
        if self.snmp.enabled and not self.snmp.communities:
            errors.append("SNMP is enabled but no read-only community is configured")
        if self.syslog.enabled and not self.syslog.destination:
            errors.append("remote syslog is enabled but no destination is configured")
        return errors

    # --- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hostname": self.hostname,
            "domain_name": self.domain_name,
            "vlans": {str(vid): vlan.to_dict() for vid, vlan in sorted(self.vlans.items())},
            "interfaces": {name: iface.to_dict() for name, iface in self.interfaces.items()},
            "port_channels": {str(pc.id): pc.to_dict() for pc in self.port_channels.values()},
            "mirrors": {str(m.id): m.to_dict() for m in self.mirrors.values()},
            "stp": self.stp.to_dict(),
            "lldp_enabled": self.lldp_enabled,
            "ip_routing": self.ip_routing,
            "svis": {str(s.vlan): s.to_dict() for s in self.svis.values()},
            "routes": [r.to_dict() for r in self.routes],
            "protected_interfaces": list(self.protected_interfaces),
            "banner": self.banner.to_dict(),
            "snmp": self.snmp.to_dict(),
            "syslog": self.syslog.to_dict(),
            "mgmt": dict(self.mgmt),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SwitchConfig":
        data = data or {}
        cfg = cls.__new__(cls)
        cfg.schema_version = int(data.get("schema_version", 1))
        cfg.hostname = data.get("hostname", "Switch")
        cfg.domain_name = data.get("domain_name", "")
        cfg.vlans = {int(k): Vlan.from_dict(v) for k, v in (data.get("vlans") or {}).items()}
        cfg.interfaces = {k: InterfaceConfig.from_dict(v) for k, v in (data.get("interfaces") or {}).items()}
        cfg.port_channels = {int(k): PortChannel.from_dict(v) for k, v in (data.get("port_channels") or {}).items()}
        cfg.mirrors = {int(k): MirrorSession.from_dict(v) for k, v in (data.get("mirrors") or {}).items()}
        cfg.stp = StpConfig.from_dict(data.get("stp") or {})
        cfg.lldp_enabled = _bool(data.get("lldp_enabled", False))
        cfg.ip_routing = _bool(data.get("ip_routing", False))
        cfg.svis = {int(k): Svi.from_dict(v) for k, v in (data.get("svis") or {}).items()}
        cfg.routes = [StaticRoute.from_dict(r) for r in data.get("routes") or []]
        cfg.protected_interfaces = list(data.get("protected_interfaces") or [])
        cfg.banner = BannerConfig.from_dict(data.get("banner") or {})
        cfg.snmp = SnmpConfig.from_dict(data.get("snmp") or {})
        cfg.syslog = SyslogConfig.from_dict(data.get("syslog") or {})
        cfg.mgmt = dict(data.get("mgmt") or {"web_enabled": True})
        cfg.__post_init__()
        return cfg

    def copy(self) -> "SwitchConfig":
        return SwitchConfig.from_dict(self.to_dict())

    def summary(self) -> dict[str, Any]:
        total = len(self.interfaces)
        attached = sum(1 for i in self.interfaces.values() if i.attached)
        return {
            "hostname": self.hostname,
            "vlans": len(self.vlans),
            "ports": total,
            "ports_assigned": attached,
            "trunks": sum(1 for i in self.interfaces.values() if i.mode == "trunk"),
        }


def normalize_interface_name(name: str) -> str:
    """Accept ``Ethernet1``, ``Ethernet1/1``, ``ethernet 1/1``, ``Eth1`` or ``1``."""
    raw = str(name).strip()
    if not raw:
        raise ConfigError("empty interface name")
    physical = portnames.canonical(raw)
    if physical is not None:
        return physical
    compact = raw.replace(" ", "")
    if re.match(r"^port-channel\d+$", compact, re.I):
        return "Port-Channel" + str(int(re.sub(r"\D", "", compact)))
    if re.match(r"^po\d+$", compact, re.I):
        return "Port-Channel" + str(int(re.sub(r"\D", "", compact)))
    if re.match(r"^vlan\d+$", compact, re.I):
        return "Vlan" + str(int(re.sub(r"\D", "", compact)))
    if not IF_NAME_RE.match(raw):
        raise ConfigError(f"invalid interface name {name!r}")
    return raw


def parse_vlan_list(text: str) -> list[int]:
    """Parse ``10,20,30-32`` into a sorted list of VLAN ids."""
    if str(text).strip().lower() == "all":
        return list(range(VLAN_MIN, VLAN_MAX + 1))
    if str(text).strip().lower() == "none":
        return []
    result: set[int] = set()
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            try:
                start_i, end_i = int(start), int(end)
            except ValueError as exc:
                raise ConfigError(f"invalid VLAN range {chunk!r}") from exc
            if start_i > end_i:
                raise ConfigError(f"invalid VLAN range {chunk!r} (start > end)")
            if not (VLAN_MIN <= start_i <= VLAN_MAX and VLAN_MIN <= end_i <= VLAN_MAX):
                raise ConfigError(f"VLAN range {chunk!r} out of bounds")
            result.update(range(start_i, end_i + 1))
        else:
            try:
                vid = int(chunk)
            except ValueError as exc:
                raise ConfigError(f"invalid VLAN id {chunk!r}") from exc
            if not VLAN_MIN <= vid <= VLAN_MAX:
                raise ConfigError(f"VLAN id {vid} out of range")
            result.add(vid)
    if not result:
        raise ConfigError("empty VLAN list")
    return sorted(result)


def format_vlan_list(vlans: list[int]) -> str:
    """Render ``[1,2,3,10,20]`` as ``1-3,10,20``."""
    if not vlans:
        return "none"
    if set(vlans) == set(range(VLAN_MIN, VLAN_MAX + 1)):
        return "all"
    ordered = sorted(set(int(v) for v in vlans))
    chunks: list[str] = []
    start = prev = ordered[0]
    for vid in ordered[1:]:
        if vid == prev + 1:
            prev = vid
            continue
        chunks.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = vid
    chunks.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(chunks)
