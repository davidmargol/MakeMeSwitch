"""Simple, stable switch-port names: ``Ethernet1``, ``Ethernet2``, ``Ethernet3`` ...

Cisco-style ``Ethernet1/1`` input is still accepted and normalised onto the
simple names, so existing scripts and muscle memory keep working.
"""
from __future__ import annotations

import re
from typing import Any

SIMPLE_RE = re.compile(r"^ethernet(\d{1,4})$", re.I)
LEGACY_RE = re.compile(r"^ethernet([12])/(\d{1,3})$", re.I)
PREFIX_RE = re.compile(r"^(?:ethernet|eth|et|e)?(\d{1,4})(?:/(\d{1,4}))?$", re.I)

MAX_PORT_NUMBER = 4096  # switch ports are discovered, not limited


def simple_name(number: int) -> str:
    return f"Ethernet{int(number)}"


def port_number(name: str) -> int | None:
    """Numeric slot for a port name (``Ethernet7`` / ``Ethernet1/7`` -> 7)."""
    match = LEGACY_RE.match(str(name).strip())
    if match:
        return (int(match.group(1)) - 1) * 64 + int(match.group(2))
    match = SIMPLE_RE.match(str(name).strip())
    return int(match.group(1)) if match else None


def canonical(name: str) -> str | None:
    """Map any physical-port spelling onto the simple name, else ``None``.

    ``Ethernet1/2`` (Cisco slot/port) and ``Eth2`` both land on the flat
    ``Ethernet`` numbering: slot 1 ports 1-64 → Ethernet1-64, slot 2 → 65+.
    """
    compact = str(name).strip().replace(" ", "")
    match = LEGACY_RE.match(compact)
    if match:
        return simple_name((int(match.group(1)) - 1) * 64 + int(match.group(2)))
    match = PREFIX_RE.match(compact)
    if not match:
        return None
    slot = int(match.group(1))
    port = match.group(2)
    if port is None:  # bare "3" / "eth3" -> Ethernet3
        return simple_name(slot) if 1 <= slot <= MAX_PORT_NUMBER else None
    port_number_ = int(port)
    # Cisco slot/port spelling is accepted for both slot 1 and 2 (and beyond):
    # Ethernet1/5 == Ethernet5, Ethernet2/1 == Ethernet65.
    if port_number_ < 1:
        return None
    number = (slot - 1) * 64 + port_number_
    return simple_name(number) if number <= MAX_PORT_NUMBER else None


def sort_key(name: str) -> tuple[int, int, str]:
    number = port_number(name)
    if number is not None:
        return (0, number, "")
    return (1, 0, str(name))


def migrate_config(config: Any) -> bool:
    """Rename legacy ``Ethernet1/N`` interface keys to ``EthernetN`` in place."""
    changed = False
    for name in list(config.interfaces.keys()):
        target = canonical(name)
        if not target or target == name:
            continue
        iface = config.interfaces.pop(name)
        iface.name = target
        config.interfaces[target] = iface
        changed = True
    return changed


def migrate_store(store: Any) -> int:
    """Rename port rows in the port map so the GUI/CLI show the simple names."""
    renamed = 0
    for row in store.all_ports():
        target = canonical(row.get("name", ""))
        if not target or target == row.get("name"):
            continue
        if store.port_by_name(target):  # never merge two identities
            continue
        store.rename_port(row["identity"], target)
        renamed += 1
    return renamed


def needs_migration(config: Any, store: Any) -> bool:
    if any(LEGACY_RE.match(name) for name in config.interfaces):
        return True
    return any(LEGACY_RE.match(row.get("name", "")) for row in store.all_ports())
