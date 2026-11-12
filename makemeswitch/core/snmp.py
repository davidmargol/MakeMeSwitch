"""SNMPv2c agent exposing MakeMeSwitch as an L2 switch.

What is served (real values, no impersonation):
  * MIB-II ``system`` — sysDescr/sysObjectID/sysUpTime/sysContact/sysName/
    sysLocation/sysServices (sysServices=2, datalink only)
  * IF-MIB ``ifTable`` + ``ifXTable`` — one row per switch port, 64-bit
    counters read from ``/sys/class/net/<if>/statistics``
  * BRIDGE-MIB ``dot1dBase``/``dot1dTp`` — bridge address, port table
    (dot1dBasePortIfIndex) and the learned forwarding table from OVS

LLDP-MIB is *not* served over SNMP; LLDP runs through lldpd and is queried
with ``lldpcli``. The README says so instead of pretending otherwise.
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from makemeswitch import __version__
from makemeswitch.core import snmp_ber as ber

DEFAULT_SYS_OBJECT_ID = "1.3.6.1.4.1.32473.1.1"  # IANA "examples" PEN; override per site
IF_TYPE_ETHERNET = 6
BRIDGE_TYPE_TRANSPARENT = 2
SYS_SERVICES_L2 = 2
SYSLOG_LEVEL = 0

SYS = (1, 3, 6, 1, 2, 1, 1)
IF_TABLE = (1, 3, 6, 1, 2, 1, 2, 2, 1)
IFX_TABLE = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1)
DOT1D_BASE = (1, 3, 6, 1, 2, 1, 17, 1)
DOT1D_TP = (1, 3, 6, 1, 2, 1, 17, 4)


@dataclass
class PortState:
    index: int
    name: str
    mac: str | None = None
    admin_up: bool = True
    oper_up: bool = False
    speed_mbps: int = 0
    mtu: int = 1500
    in_octets: int = 0
    out_octets: int = 0
    in_ucast: int = 0
    out_ucast: int = 0
    in_errors: int = 0
    out_errors: int = 0
    in_discards: int = 0
    out_discards: int = 0

    @property
    def mac_bytes(self) -> bytes:
        return mac_to_bytes(self.mac)


@dataclass
class Snapshot:
    sys_descr: str = ""
    sys_object_id: str = DEFAULT_SYS_OBJECT_ID
    sys_name: str = "makemeswitch"
    sys_contact: str = ""
    sys_location: str = ""
    sys_services: int = SYS_SERVICES_L2
    uptime_ticks: int = 0
    bridge_address: str | None = None
    ports: list[PortState] = field(default_factory=list)
    fdb: list[tuple[str, int]] = field(default_factory=list)
    aging_time: int = 300


def mac_to_bytes(mac: str | None) -> bytes:
    if not mac:
        return b"\x00" * 6
    try:
        return bytes(int(part, 16) for part in mac.split(":"))
    except ValueError:
        return b"\x00" * 6


def _counters(ifname: str) -> dict[str, int]:
    base = Path("/sys/class/net") / ifname / "statistics"
    names = {
        "rx_bytes": "in_octets",
        "tx_bytes": "out_octets",
        "rx_packets": "in_ucast",
        "tx_packets": "out_ucast",
        "rx_errors": "in_errors",
        "tx_errors": "out_errors",
        "rx_dropped": "in_discards",
        "tx_dropped": "out_discards",
    }
    values: dict[str, int] = {}
    for source, target in names.items():
        try:
            values[target] = int((base / source).read_text().strip())
        except (OSError, ValueError):
            values[target] = 0
    return values


def _mtu(ifname: str, default: int = 1500) -> int:
    try:
        return int((Path("/sys/class/net") / ifname / "mtu").read_text().strip())
    except (OSError, ValueError):
        return default


def _oper_up(ifname: str) -> bool:
    try:
        return (Path("/sys/class/net") / ifname / "operstate").read_text().strip() == "up"
    except OSError:
        return False


def _speed_mbps(ifname: str) -> int:
    try:
        raw = (Path("/sys/class/net") / ifname / "speed").read_text().strip()
        return max(int(raw), 0)
    except (OSError, ValueError):
        return 0


def build_tree(snap: Snapshot) -> dict[tuple[int, ...], bytes]:
    """Encode the whole MIB view for one snapshot."""
    tree: dict[tuple[int, ...], bytes] = {}

    tree[SYS + (1, 0)] = ber.encode_octets(snap.sys_descr)
    tree[SYS + (2, 0)] = ber.encode_oid(snap.sys_object_id)
    tree[SYS + (3, 0)] = ber.encode_unsigned(ber.TIMETICKS, snap.uptime_ticks)
    tree[SYS + (4, 0)] = ber.encode_octets(snap.sys_contact)
    tree[SYS + (5, 0)] = ber.encode_octets(snap.sys_name)
    tree[SYS + (6, 0)] = ber.encode_octets(snap.sys_location)
    tree[SYS + (7, 0)] = ber.encode_tlv(ber.INTEGER, bytes([snap.sys_services]))

    for port in snap.ports:
        idx = (port.index,)
        tree[IF_TABLE + (1,) + idx] = ber.encode_tlv(ber.INTEGER, _int_bytes(port.index))
        tree[IF_TABLE + (2,) + idx] = ber.encode_octets(port.name)
        tree[IF_TABLE + (3,) + idx] = ber.encode_tlv(ber.INTEGER, _int_bytes(IF_TYPE_ETHERNET))
        tree[IF_TABLE + (4,) + idx] = ber.encode_tlv(ber.INTEGER, _int_bytes(port.mtu))
        tree[IF_TABLE + (5,) + idx] = ber.encode_unsigned(ber.GAUGE32, port.speed_mbps * 1_000_000)
        tree[IF_TABLE + (6,) + idx] = ber.encode_octets(port.mac_bytes)
        tree[IF_TABLE + (7,) + idx] = ber.encode_tlv(ber.INTEGER, _int_bytes(1 if port.admin_up else 2))
        tree[IF_TABLE + (8,) + idx] = ber.encode_tlv(ber.INTEGER, _int_bytes(1 if port.oper_up else 2))
        tree[IF_TABLE + (9,) + idx] = ber.encode_unsigned(ber.TIMETICKS, 0)
        tree[IF_TABLE + (10,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.in_octets & 0xFFFFFFFF)
        tree[IF_TABLE + (11,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.in_ucast & 0xFFFFFFFF)
        tree[IF_TABLE + (12,) + idx] = ber.encode_unsigned(ber.COUNTER32, 0)
        tree[IF_TABLE + (13,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.in_discards & 0xFFFFFFFF)
        tree[IF_TABLE + (14,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.in_errors & 0xFFFFFFFF)
        tree[IF_TABLE + (15,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.out_octets & 0xFFFFFFFF)
        tree[IF_TABLE + (16,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.out_ucast & 0xFFFFFFFF)
        tree[IF_TABLE + (17,) + idx] = ber.encode_unsigned(ber.COUNTER32, 0)
        tree[IF_TABLE + (18,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.out_discards & 0xFFFFFFFF)
        tree[IF_TABLE + (19,) + idx] = ber.encode_unsigned(ber.COUNTER32, port.out_errors & 0xFFFFFFFF)
        tree[IF_TABLE + (20,) + idx] = ber.encode_unsigned(ber.GAUGE32, 0)
        tree[IF_TABLE + (21,) + idx] = ber.encode_oid("0.0")

        tree[IFX_TABLE + (1,) + idx] = ber.encode_octets(port.name)
        tree[IFX_TABLE + (6,) + idx] = ber.encode_unsigned(ber.COUNTER64, port.in_octets)
        tree[IFX_TABLE + (7,) + idx] = ber.encode_unsigned(ber.COUNTER64, port.in_ucast)
        tree[IFX_TABLE + (10,) + idx] = ber.encode_unsigned(ber.COUNTER64, port.out_octets)
        tree[IFX_TABLE + (11,) + idx] = ber.encode_unsigned(ber.COUNTER64, port.out_ucast)
        tree[IFX_TABLE + (15,) + idx] = ber.encode_unsigned(ber.GAUGE32, port.speed_mbps)
        tree[IFX_TABLE + (18,) + idx] = ber.encode_tlv(ber.INTEGER, b"\x02")
        tree[IFX_TABLE + (19,) + idx] = ber.encode_tlv(ber.INTEGER, b"\x01")

    # BRIDGE-MIB
    tree[DOT1D_BASE + (1, 0)] = ber.encode_octets(mac_to_bytes(snap.bridge_address))
    tree[DOT1D_BASE + (2, 0)] = ber.encode_tlv(ber.INTEGER, _int_bytes(len(snap.ports)))
    tree[DOT1D_BASE + (3, 0)] = ber.encode_tlv(ber.INTEGER, _int_bytes(BRIDGE_TYPE_TRANSPARENT))
    # dot1dBasePortEntry = 17.1.4.1, columns dot1dBasePort (.1) and
    # dot1dBasePortIfIndex (.2). Both used to be written one level too high, so
    # a manager could not map bridge ports onto ifIndex.
    bridge_port_entry = DOT1D_BASE + (4, 1)
    for port in snap.ports:
        tree[bridge_port_entry + (1, port.index)] = ber.encode_tlv(ber.INTEGER, _int_bytes(port.index))
        tree[bridge_port_entry + (2, port.index)] = ber.encode_tlv(ber.INTEGER, _int_bytes(port.index))

    tree[DOT1D_TP + (1, 0)] = ber.encode_unsigned(ber.COUNTER32, 0)
    tree[DOT1D_TP + (2, 0)] = ber.encode_tlv(ber.INTEGER, _int_bytes(snap.aging_time))
    # dot1dTpFdbEntry = 17.4.3.1, columns address (.1), port (.2), status (.3);
    # index is the 6-octet MAC, so the column number sits before it.
    fdb_entry = DOT1D_TP + (3, 1)
    for mac, port_index in snap.fdb:
        index = tuple(mac_to_bytes(mac))
        tree[fdb_entry + (1,) + index] = ber.encode_octets(mac_to_bytes(mac))
        tree[fdb_entry + (2,) + index] = ber.encode_tlv(ber.INTEGER, _int_bytes(port_index))
        tree[fdb_entry + (3,) + index] = ber.encode_tlv(ber.INTEGER, b"\x03")  # learned
    return tree


def _int_bytes(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    return value.to_bytes((value.bit_length() + 8) // 8, "big", signed=True)


# --- protocol -------------------------------------------------------------

SNMP_V1 = 0
SNMP_V2C = 1


class Request:
    __slots__ = ("version", "community", "pdu_tag", "request_id", "varbinds", "non_repeaters", "max_repetitions")

    def __init__(self, version, community, pdu_tag, request_id, varbinds, non_repeaters=0, max_repetitions=0):
        self.version = version
        self.community = community
        self.pdu_tag = pdu_tag
        self.request_id = request_id
        self.varbinds = varbinds
        self.non_repeaters = non_repeaters
        self.max_repetitions = max_repetitions


def parse_request(data: bytes) -> Request:
    tag, body, _ = ber.decode_tlv(data, 0)
    if tag != ber.SEQUENCE:
        raise ber.BerError("message is not a SEQUENCE")
    items = ber.decode_sequence(body)
    if len(items) < 3:
        raise ber.BerError("message too short")
    version = ber.decode_integer(items[0][1]) if items[0][0] == ber.INTEGER else -1
    community = items[1][1].decode("utf-8", "replace") if items[1][0] == ber.OCTET_STRING else ""
    pdu_tag, pdu_body = items[2]
    if pdu_tag not in (ber.GET_REQUEST, ber.GET_NEXT_REQUEST, ber.GET_BULK_REQUEST, ber.SET_REQUEST):
        raise ber.BerError(f"unsupported PDU tag 0x{pdu_tag:02x}")
    fields = ber.decode_sequence(pdu_body)
    if len(fields) < 4:
        raise ber.BerError("PDU too short")
    request_id = ber.decode_integer(fields[0][1])
    non_repeaters = ber.decode_integer(fields[1][1]) if pdu_tag == ber.GET_BULK_REQUEST else 0
    max_repetitions = ber.decode_integer(fields[2][1]) if pdu_tag == ber.GET_BULK_REQUEST else 0
    varbinds: list[tuple[int, ...]] = []
    for bind_tag, bind_body in ber.decode_sequence(fields[3][1]):
        bind = ber.decode_sequence(bind_body)
        if not bind or bind[0][0] != ber.OID:
            raise ber.BerError("varbind without OID")
        varbinds.append(ber.decode_oid(bind[0][1]))
    return Request(version, community, pdu_tag, request_id, varbinds, non_repeaters, max_repetitions)


def _sorted_keys(tree: dict[tuple[int, ...], bytes]) -> list[tuple[int, ...]]:
    return sorted(tree)


def _next_key(keys: list[tuple[int, ...]], oid: tuple[int, ...]) -> tuple[int, ...] | None:
    lo, hi = 0, len(keys)
    while lo < hi:
        mid = (lo + hi) // 2
        if keys[mid] > oid:
            hi = mid
        else:
            lo = mid + 1
    return keys[lo] if lo < len(keys) else None


def _lookup(tree: dict[tuple[int, ...], bytes], oid: tuple[int, ...]) -> bytes:
    if oid in tree:
        return tree[oid]
    # An existing subtree without that instance is noSuchInstance; anything
    # else is noSuchObject (RFC 3416 semantics, at the granularity we expose).
    for key in tree:
        if key[: len(oid)] == oid or oid[: len(key)] == key:
            return ber.encode_exception(ber.NO_SUCH_INSTANCE)
    return ber.encode_exception(ber.NO_SUCH_OBJECT)


def build_response(request: Request, varbinds: list[bytes], error_status: int = 0, error_index: int = 0) -> bytes:
    body = ber.encode_integer(request.request_id)
    body += ber.encode_integer(error_status)
    body += ber.encode_integer(error_index)
    encoded_binds = b""
    for oid, value in varbinds:  # type: ignore[misc]
        encoded_binds += ber.encode_tlv(ber.SEQUENCE, ber.encode_oid(oid) + value)
    body += ber.encode_tlv(ber.SEQUENCE, encoded_binds)
    pdu = ber.encode_tlv(ber.GET_RESPONSE, body)
    version = SNMP_V1 if request.version == SNMP_V1 else SNMP_V2C
    message = ber.encode_integer(version) + ber.encode_octets(request.community) + pdu
    return ber.encode_tlv(ber.SEQUENCE, message)


def process_datagram(
    data: bytes,
    snapshot: Snapshot,
    communities,
    source_ip: str,
    version: str = "v2c",
) -> bytes | None:
    """Handle one datagram. ``None`` means: stay silent (drop)."""
    try:
        request = parse_request(data)
    except (ber.BerError, IndexError, ValueError):
        return None
    allowed_versions = (SNMP_V1,) if str(version).lower() == "v1" else (SNMP_V1, SNMP_V2C)
    if request.version not in allowed_versions:
        # SNMPv3 (USM) is not implemented: stay silent rather than answer with
        # a message the client cannot authenticate.
        return None
    allowed = False
    for community in communities:
        if community.name == request.community:
            allowed = community.allows(source_ip) if hasattr(community, "allows") else True
            if not allowed:
                return None
            break
    else:
        return None

    if request.version == SNMP_V1 and request.pdu_tag == ber.GET_BULK_REQUEST:
        return build_response(request, [
            (oid, ber.encode_null()) for oid in request.varbinds
        ], error_status=5)  # genErr: v1 has no GetBulk

    tree = build_tree(snapshot)
    keys = _sorted_keys(tree)

    if request.pdu_tag == ber.SET_REQUEST:
        binds = [(oid, ber.encode_exception(ber.NO_SUCH_OBJECT)) for oid in request.varbinds]
        return build_response(request, binds, error_status=17, error_index=1)

    results: list[tuple[tuple[int, ...], bytes]] = []
    if request.pdu_tag == ber.GET_REQUEST:
        for oid in request.varbinds:
            results.append((oid, _lookup(tree, oid)))
    elif request.pdu_tag == ber.GET_NEXT_REQUEST:
        for oid in request.varbinds:
            key = _next_key(keys, oid)
            if key is None:
                results.append((oid, ber.encode_exception(ber.END_OF_MIB_VIEW)))
            else:
                results.append((key, tree[key]))
    else:  # GETBULK
        non_repeaters = max(0, min(request.non_repeaters, len(request.varbinds)))
        repeaters = request.varbinds[non_repeaters:]
        for oid in request.varbinds[:non_repeaters]:
            key = _next_key(keys, oid)
            results.append((key, tree[key]) if key else (oid, ber.encode_exception(ber.END_OF_MIB_VIEW)))
        current = list(repeaters)
        for _ in range(max(0, min(request.max_repetitions, 64))):
            row: list[tuple[tuple[int, ...], bytes]] = []
            finished = True
            for oid in current:
                key = _next_key(keys, oid)
                if key is None:
                    row.append((oid, ber.encode_exception(ber.END_OF_MIB_VIEW)))
                else:
                    row.append((key, tree[key]))
                    finished = False
            results.extend(row)
            current = [oid for oid, _ in row]
            if finished:
                break
    return build_response(request, results)


class SnmpAgent:
    """UDP SNMPv2c responder with a periodically refreshed snapshot."""

    def __init__(self, provider, communities, bind: str = "0.0.0.0", port: int = 161, refresh: float = 5.0):
        self.provider = provider
        self.communities = communities
        self.bind = bind
        self.port = port
        self.refresh = refresh
        self._snapshot: Snapshot | None = None
        self._snapshot_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._socket: socket.socket | None = None

    def snapshot(self) -> Snapshot:
        with self._lock:
            now = time.monotonic()
            if self._snapshot is None or now - self._snapshot_at >= self.refresh:
                self._snapshot = self.provider()
                self._snapshot_at = now
            return self._snapshot

    def handle(self, data: bytes, source_ip: str) -> bytes | None:
        # A malformed or unexpected datagram must never take the agent down: a
        # single bad packet used to raise out of the receive loop and restart the
        # service, which looked like "SNMP does not work" from the outside.
        try:
            return process_datagram(
                data, self.snapshot(), self.communities, source_ip, getattr(self, "version", "v2c")
            )
        except Exception as exc:  # noqa: BLE001
            print(f"snmp: dropping datagram from {source_ip}: {exc}", flush=True)
            return None

    def serve_forever(self) -> None:  # pragma: no cover - exercised by integration test
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, self.port))
        self._socket = sock
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            reply = self.handle(data, address[0])
            if reply:
                try:
                    sock.sendto(reply, address)
                except OSError:
                    pass
        sock.close()

    def stop(self) -> None:
        self._stop.set()


# --- live state ------------------------------------------------------------

def snapshot_from_rows(config, rows, fdb=None, uptime_seconds: float | None = None, root: Path | None = None) -> Snapshot:
    """Build a Snapshot from stored port rows plus the kernel counters."""
    from makemeswitch.core.portnames import port_number

    base = root or Path("/sys/class/net")
    ports: list[PortState] = []
    for row in sorted(rows, key=lambda r: port_number(r.get("name", "")) or 0):
        if not row.get("present"):
            continue
        index = port_number(row.get("name", "")) or 0
        if not index:
            continue
        ifname = row.get("linux_if") or ""
        has_link = bool(ifname) and (base / ifname).exists()
        port = PortState(
            index=index,
            name=row.get("name", f"Ethernet{index}"),
            mac=row.get("mac") or None,
            admin_up=True,
            oper_up=has_link and _oper_up_in(base, ifname),
            speed_mbps=_speed_in(base, ifname),
            mtu=_mtu_in(base, ifname),
        )
        if has_link:
            for key, value in _counters_in(base, ifname).items():
                setattr(port, key, value)
        ports.append(port)

    if uptime_seconds is None:
        try:
            uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            uptime_seconds = 0.0

    bridge = getattr(config, "bridge_name", "mms0") if config is not None else "mms0"
    try:
        bridge_mac = (base / bridge / "address").read_text().strip()
    except OSError:
        bridge_mac = ports[0].mac if ports else None

    snmp_cfg = getattr(config, "snmp", None)
    return Snapshot(
        sys_descr=f"MakeMeSwitch {__version__} (Open vSwitch L2 switch on Linux)",
        sys_object_id=(snmp_cfg.sys_object_id if snmp_cfg and getattr(snmp_cfg, "sys_object_id", "") else DEFAULT_SYS_OBJECT_ID),
        sys_name=(snmp_cfg.sys_name if snmp_cfg and snmp_cfg.sys_name else getattr(config, "hostname", "makemeswitch")),
        sys_contact=(snmp_cfg.contact if snmp_cfg else ""),
        sys_location=(snmp_cfg.location if snmp_cfg else ""),
        sys_services=SYS_SERVICES_L2,
        uptime_ticks=int(uptime_seconds * 100) % (2**32),
        bridge_address=bridge_mac,
        ports=ports,
        fdb=list(fdb or []),
        aging_time=300,
    )


def _counters_in(base: Path, ifname: str) -> dict[str, int]:
    values: dict[str, int] = {}
    pairs = (
        ("rx_bytes", "in_octets"),
        ("tx_bytes", "out_octets"),
        ("rx_packets", "in_ucast"),
        ("tx_packets", "out_ucast"),
        ("rx_errors", "in_errors"),
        ("tx_errors", "out_errors"),
        ("rx_dropped", "in_discards"),
        ("tx_dropped", "out_discards"),
    )
    for source, target in pairs:
        try:
            values[target] = int((base / ifname / "statistics" / source).read_text().strip())
        except (OSError, ValueError):
            values[target] = 0
    return values


def _oper_up_in(base: Path, ifname: str) -> bool:
    try:
        return (base / ifname / "operstate").read_text().strip() == "up"
    except OSError:
        return False


def _speed_in(base: Path, ifname: str) -> int:
    try:
        return max(int((base / ifname / "speed").read_text().strip()), 0)
    except (OSError, ValueError):
        return 0


def _mtu_in(base: Path, ifname: str) -> int:
    try:
        return int((base / ifname / "mtu").read_text().strip())
    except (OSError, ValueError):
        return 1500


def read_fdb(bridge: str = "mms0", rows=None) -> list[tuple[str, int]]:
    """Return ``[(mac, ifIndex)]`` learned by the OVS bridge.

    BRIDGE-MIB wants the *bridge port* number in ``dot1dTpFdbPort``; OVS reports
    the OpenFlow port number, so the mapping goes ofport -> interface name ->
    ifIndex (the index this agent already publishes in ``ifTable``).
    """
    import subprocess

    def _run(argv: list[str]) -> str:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
            return result.stdout or ""
        except (OSError, subprocess.SubprocessError):
            return ""

    listing = _run(["ovs-vsctl", "--format=csv", "--no-headings", "--columns=name,ofport", "list", "Interface"])
    ofport_to_name: dict[int, str] = {}
    for line in listing.splitlines():
        parts = [piece.strip().strip('"') for piece in line.split(",")]
        if len(parts) < 2:
            continue
        name, ofport = parts[0], parts[1]
        try:
            ofport_to_name[int(ofport)] = name
        except ValueError:
            continue

    from makemeswitch.core.portnames import port_number

    by_name: dict[str, int] = {}
    for row in rows or []:
        name = row.get("name") or ""
        number = port_number(name)
        if name and number:
            by_name[name] = number
    entries: list[tuple[str, int]] = []
    seen: set[str] = set()
    for line in _run(["ovs-appctl", "fdb/show", bridge]).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            ofport = int(parts[0])
        except ValueError:
            continue
        mac = parts[2].lower()
        ifindex = by_name.get(ofport_to_name.get(ofport, ""))
        if ifindex is None or mac in seen:
            continue
        seen.add(mac)
        entries.append((mac, int(ifindex)))
    return entries


class LiveProvider:
    """Reads the store and the kernel on every refresh, so CLI edits apply live."""

    def __init__(self, db_path: str, config_name: str = "running"):
        self.db_path = db_path
        self.config_name = config_name

    def __call__(self) -> Snapshot:
        from makemeswitch.core.store import Store

        store = Store(self.db_path)
        config = store.load_config(self.config_name)
        rows = store.all_ports()
        bridge = getattr(config, "bridge_name", "mms0") if config is not None else "mms0"
        try:
            fdb = read_fdb(bridge, rows)
        except Exception as exc:  # noqa: BLE001 - monitoring must never kill the agent
            print(f"snmp: fdb read failed: {exc}", flush=True)
            fdb = []
        return snapshot_from_rows(config, rows, fdb=fdb)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``makemeswitch-snmpd``."""
    import argparse
    import os
    import signal
    import sys

    parser = argparse.ArgumentParser(prog="makemeswitch-snmpd", description="MakeMeSwitch SNMPv2c agent")
    parser.add_argument("--db", default=os.environ.get("MMS_DB", "/var/lib/makemeswitch/makemeswitch.db"))
    parser.add_argument("--config", default=os.environ.get("MMS_CONFIG", "/etc/makemeswitch/makemeswitch.yaml"))
    parser.add_argument("--once", action="store_true", help="print the served OIDs and exit")
    args = parser.parse_args(argv)

    from makemeswitch.core.engine import load_settings

    settings = load_settings(args.config)
    db_path = args.db if os.path.exists(args.db) else settings.get("db_path", args.db)
    provider = LiveProvider(str(db_path))
    from makemeswitch.core.store import Store

    config = Store(db_path).load_config("running")
    snmp_cfg = getattr(config, "snmp", None)
    communities = list(snmp_cfg.communities) if snmp_cfg else []
    if not communities:
        print("snmp: no communities configured, nothing to serve", file=sys.stderr)
        return 2
    agent = SnmpAgent(provider, communities, bind=snmp_cfg.bind, port=snmp_cfg.port)
    agent.version = getattr(snmp_cfg, "version", "v2c")
    if args.once:
        for oid in sorted(build_tree(provider())):
            print(ber.oid_to_str(oid))
        return 0
    signal.signal(signal.SIGTERM, lambda *_: agent.stop())
    signal.signal(signal.SIGINT, lambda *_: agent.stop())
    print(f"snmp: serving on {snmp_cfg.bind}:{snmp_cfg.port}", flush=True)
    agent.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
