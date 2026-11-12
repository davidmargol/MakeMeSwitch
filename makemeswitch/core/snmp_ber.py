"""Minimal BER/ASN.1 codec for SNMPv2c.

Written in-house on purpose: pysnmp 4.x does not import on modern pyasn1 and
pysnmp 7 pulls a large dependency tree into an appliance that must install
offline on arbitrary Debian/Ubuntu. Only what SNMPv2c GET/GETNEXT/GETBULK
needs is implemented, and every path is covered by round-trip tests.
"""
from __future__ import annotations

# Universal tags
INTEGER = 0x02
OCTET_STRING = 0x04
NULL = 0x05
OID = 0x06
SEQUENCE = 0x30

# Application tags
IPADDRESS = 0x40
COUNTER32 = 0x41
GAUGE32 = 0x42
TIMETICKS = 0x43
OPAQUE = 0x44
COUNTER64 = 0x46

# Exception tags (context specific, primitive)
NO_SUCH_OBJECT = 0x80
NO_SUCH_INSTANCE = 0x81
END_OF_MIB_VIEW = 0x82

# PDU tags
GET_REQUEST = 0xA0
GET_NEXT_REQUEST = 0xA1
GET_RESPONSE = 0xA2
SET_REQUEST = 0xA3
GET_BULK_REQUEST = 0xA5

ERROR_STATUS = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
    6: "noAccess",
    16: "wrongValue",
    17: "notWritable",
    18: "inconsistentValue",
}


class BerError(ValueError):
    """Raised when a datagram is not valid BER we can serve."""


def encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    payload = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(payload)]) + payload


def encode_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(value)) + value


def encode_integer(value: int) -> bytes:
    if value == 0:
        return encode_tlv(INTEGER, b"\x00")
    length = (value.bit_length() + 8) // 8  # room for the sign bit
    return encode_tlv(INTEGER, value.to_bytes(length, "big", signed=True))


def encode_octets(value: str | bytes) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8", "replace")
    return encode_tlv(OCTET_STRING, value)


def encode_null() -> bytes:
    return encode_tlv(NULL, b"")


def _encode_subid(value: int) -> bytes:
    if value < 0:
        raise BerError("negative OID sub-identifier")
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(out)


def encode_oid(oid: str | tuple[int, ...]) -> bytes:
    parts = oid_to_tuple(oid)
    if len(parts) < 2 or parts[0] > 2 or (parts[0] < 2 and parts[1] > 39):
        raise BerError(f"invalid OID {oid!r}")
    body = bytearray(_encode_subid(parts[0] * 40 + parts[1]))
    for part in parts[2:]:
        body += _encode_subid(part)
    return encode_tlv(OID, bytes(body))


def oid_to_tuple(oid: str | tuple[int, ...]) -> tuple[int, ...]:
    if isinstance(oid, tuple):
        return oid
    text = oid.strip().lstrip(".")
    if not text:
        raise BerError("empty OID")
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError as exc:
        raise BerError(f"invalid OID {oid!r}") from exc


def oid_to_str(oid: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in oid)


def _decode_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise BerError("truncated length")
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    if count == 0 or count > 4 or offset + count > len(data):
        raise BerError("unsupported length form")
    return int.from_bytes(data[offset : offset + count], "big"), offset + count


def decode_tlv(data: bytes, offset: int = 0) -> tuple[int, bytes, int]:
    if offset >= len(data):
        raise BerError("truncated TLV")
    tag = data[offset]
    length, offset = _decode_length(data, offset + 1)
    end = offset + length
    if end > len(data):
        raise BerError("TLV length exceeds datagram")
    return tag, data[offset:end], end


def decode_integer(value: bytes) -> int:
    if not value:
        return 0
    return int.from_bytes(value, "big", signed=True)


def decode_oid(value: bytes) -> tuple[int, ...]:
    if not value:
        raise BerError("empty OID value")
    parts: list[int] = []
    current = 0
    for byte in value:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(current)
            current = 0
    if not parts:
        raise BerError("truncated OID")
    first = parts[0]
    head = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    return tuple(head + parts[1:])


def decode_sequence(value: bytes) -> list[tuple[int, bytes]]:
    items: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(value):
        tag, payload, offset = decode_tlv(value, offset)
        items.append((tag, payload))
    return items


def encode_unsigned(tag: int, value: int) -> bytes:
    """Encode Counter32/Gauge32/TimeTicks/Counter64 (may need a leading zero)."""
    if value < 0:
        raise BerError("negative value for unsigned type")
    if value == 0:
        return encode_tlv(tag, b"\x00")
    payload = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if payload[0] & 0x80:  # keep it positive
        payload = b"\x00" + payload
    return encode_tlv(tag, payload)


def encode_exception(tag: int) -> bytes:
    return encode_tlv(tag, b"")
