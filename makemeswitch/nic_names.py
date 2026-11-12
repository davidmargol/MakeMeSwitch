"""Simple, stable switch-port names: ``Ethernet1``, ``Ethernet2``, ``Ethernet3`` ...

Cisco-style ``Ethernet1/1`` input is still accepted and normalised onto the
simple names, so existing scripts and muscle memory keep working.

Explicit boot/install naming runs after OVS and before the application. It
rescans present hardware on every boot under a process/thread lock; API reads
never rename. The installer retires legacy .link and sysfs .path rules.
``prepare`` is a legacy opt-in rules writer, not used by the installer.
Management NICs are excluded by discovery.
"""
from __future__ import annotations

import json
import os
import fcntl
import threading
from contextlib import contextmanager

_LOCK = threading.RLock()
_LOCAL = threading.local()


@contextmanager
def naming_lock():
    """Serialize discovery, DB numbering and renames across threads/processes."""
    with _LOCK:
        if getattr(_LOCAL, 'depth', 0):
            yield
            return
        path = os.environ.get('MMS_NAMING_LOCK', '/run/lock/makemeswitch-nicnames.lock')
        with open(path, 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _LOCAL.depth = 1
            try:
                yield
            finally:
                _LOCAL.depth = 0
                fcntl.flock(handle, fcntl.LOCK_UN)

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from makemeswitch.core import portnames

SIMPLE_RE = portnames.SIMPLE_RE
LEGACY_RE = portnames.LEGACY_RE
PREFIX_RE = portnames.PREFIX_RE

MAX_PORT_NUMBER = portnames.MAX_PORT_NUMBER
MAPPING_FILE = Path("/etc/makemeswitch/nic-names.json")


# Name helpers live in core.portnames so the CLI, engine and udev rules cannot
# drift apart.
simple_name = portnames.simple_name
port_number = portnames.port_number
canonical = portnames.canonical
sort_key = portnames.sort_key


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


# --------------------------------------------------------------------------
# kernel renaming
# --------------------------------------------------------------------------
def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)


def _live_name_for_mac(mac: str) -> str | None:
    for entry in sorted(Path("/sys/class/net").iterdir()):
        try:
            if (entry / "address").read_text().strip().lower() == mac.lower():
                return entry.name
        except OSError:
            continue
    return None


def rename_live(mapping: dict[str, str]) -> list[tuple[str, str]]:
    with naming_lock():
        return _rename_live_locked(mapping)


def _rename_live_locked(mapping: dict[str, str]) -> list[tuple[str, str]]:
    """Rename live interfaces so ``mac -> EthernetN`` holds. Returns renames.

    Two passes through temporary names, so a permutation (``Ethernet6`` becomes
    ``Ethernet8`` while a new NIC takes ``Ethernet6``) cannot collide.
    """
    plan: list[tuple[str, str, str]] = []
    for mac, target in mapping.items():
        current = _live_name_for_mac(mac)
        if not current or current == target:
            continue
        plan.append((mac, current, target))
    if not plan:
        return []
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('Duplicate rename targets')
    moving = {mac for mac, _, _ in plan}
    for mac, current, target in plan:
        if not SIMPLE_RE.fullmatch(target) or len(target) > 15:
            raise ValueError(f'Invalid target: {target}')
        owner = _live_name_for_mac_of(target)
        if owner is not None and owner not in moving:
            raise RuntimeError(f'{target} is already used by another NIC')
    journal = []
    states = {}

    def checked(proc):
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout).strip())

    def move(current, target):
        checked(_set_name(current, target))
        journal.append((current, target))

    try:
        parked = []
        reserved = set(mapping.values())
        for index, (mac, current, target) in enumerate(plan):
            seq = index
            temp = f'mmstmp{seq}'
            while _live_name_for_mac_of(temp) is not None or temp in reserved:
                seq += len(plan)
                temp = f'mmstmp{seq}'
            reserved.add(temp)
            proc = _run(['ip', '-j', 'link', 'show', 'dev', current])
            checked(proc)
            was_up = 'UP' in json.loads(proc.stdout)[0]['flags']
            states[mac] = (was_up, _addresses_of(current))
            checked(_run(['ip', 'link', 'set', 'dev', current, 'down']))
            move(current, temp)
            parked.append((current, temp, target))
        for original, temp, target in parked:
            move(temp, target)
        for mac, (was_up, addresses) in states.items():
            name = _live_name_for_mac(mac)
            # Same netdev retains IPv4 on rename. A down transition can remove
            if name is None:
                raise RuntimeError(f'NIC disappeared during rename: {mac}')
            # IPv6; restore only addresses actually missing, not duplicate IPv4.
            missing = set(addresses) - set(_addresses_of(name))
            for address in missing:
                checked(_run(['ip', 'addr', 'add', address, 'dev', name]))
            if was_up:
                checked(_run(['ip', 'link', 'set', 'dev', name, 'up']))
    except Exception as exc:
        errors = []
        # Reverse every successful move, including both permutation phases.
        for old, new in reversed(journal):
            try:
                checked(_run(['ip', 'link', 'set', 'dev', new, 'down']))
                checked(_set_name(new, old))
            except Exception as rollback:
                errors.append(str(rollback))
        for mac, (was_up, addresses) in states.items():
            name = _live_name_for_mac(mac)
            try:
                if name is None:
                    raise RuntimeError(f'NIC disappeared during rollback: {mac}')
                for address in set(addresses) - set(_addresses_of(name)):
                    checked(_run(['ip', 'addr', 'add', address, 'dev', name]))
                checked(_run(['ip', 'link', 'set', 'dev', name, 'up' if was_up else 'down']))
            except Exception as rollback:
                errors.append(str(rollback))
        raise RuntimeError(f'Rename failed: {exc}; rollback: {errors or "complete"}') from exc
    return [(current, target) for _, current, target in plan]


def _mac_of(name: str) -> str | None:
    return _live_name_for_mac_of(name)


def _set_name(current: str, target: str):
    return _run(["ip", "link", "set", "dev", current, "name", target])


def _addresses_of(name: str) -> list[str]:
    """IPv4/IPv6 addresses configured on ``name`` (excluding bare link-local)."""
    proc = _run(["ip", "-o", "addr", "show", "dev", name])
    if proc.returncode != 0:
        return []
    skip = "scope link " if " " in proc.stdout else ""
    return [
        match
        for match in re.findall(r"inet6? ([0-9a-fA-F:.]+/\d+)", proc.stdout)
        if not (skip and "fe80:" in match)
    ]


def _live_name_for_mac_of(name: str) -> str | None:
    path = Path("/sys/class/net") / name / "address"
    try:
        return path.read_text().strip().lower()
    except OSError:
        return None


def write_mapping(mapping: dict[str, str], path: Path = MAPPING_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k.lower(): v for k, v in sorted(mapping.items())}, indent=2) + "\n")
    path.chmod(0o644)


def read_mapping(path: Path = MAPPING_FILE) -> dict[str, str]:
    try:
        return {k.lower(): v for k, v in json.loads(path.read_text()).items()}
    except (OSError, ValueError):
        return {}


def desired_mapping(ports: list[dict], protected: set[str]) -> dict[str, str]:
    """MAC -> EthernetN for every present, non-management switch port."""
    mapping: dict[str, str] = {}
    names: set[str] = set()
    for port in ports:
        linux = port.get("linux_if")
        if not port.get("present") or linux in protected or port.get("protected"):
            continue
        number = port_number(port["name"])
        if number is None or not 1 <= number <= MAX_PORT_NUMBER:
            raise ValueError(f"Unsupported stable port name: {port['name']}")
        mac = (port.get("link") or {}).get("mac", "")
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
            raise ValueError(f"No valid MAC for {linux}")
        target = simple_name(number)
        if target in names:
            raise ValueError(f"Duplicate target {target}")
        names.add(target)
        if mac.lower() in mapping:
            raise ValueError(f'Duplicate MAC: {mac}')
        mapping[mac.lower()] = target
    return mapping


def write_rules(ports: list[dict], protected: set[str]) -> dict[str, str]:
    """Write the udev ``.link`` rules and the MAC -> name map. Idempotent."""
    mapping = desired_mapping(ports, protected)
    directory = Path("/etc/systemd/network")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for mac, target in mapping.items():
            path = directory / f"10-makemeswitch-{port_number(target):03d}.link"
            content = f"[Match]\nMACAddress={mac}\n\n[Link]\nNamePolicy=\nName={target}\n"
            if path.exists() and path.read_text() == content:
                continue
            temp = path.with_suffix(".tmp")
            temp.write_text(content)
            temp.chmod(0o644)
            temp.replace(path)
    except OSError:
        # The mapping file is still useful without root: the boot oneshot reads it.
        pass
    try:
        write_mapping(mapping)
    except OSError:
        pass
    return mapping


def prepare() -> int:
    """Write udev ``.link`` files + the MAC map for the ports the engine sees."""
    engine, protected = _sync_engine()
    mapping = write_rules(engine.port_table(), protected)
    print(f"Prepared {len(mapping)} stable port names ({', '.join(sorted(mapping.values(), key=sort_key))})")
    return 0


def _sync_engine():
    """Run the engine's port sync so numbering has exactly one authority."""
    from makemeswitch.core import interfaces as netif
    from makemeswitch.core.engine import Engine

    engine = Engine()
    engine.sync_ports()
    return engine, set(netif.protected_interfaces())


def _topology_key(link: Any) -> tuple:
    """Physical NIC order: PCI bus/slot first, then USB topology."""
    identity = getattr(link, "identity", None) or ""
    if identity.startswith("pci:"):
        numbers = [int(part, 16) for part in re.findall(r"[0-9a-fA-F]+", identity[4:])]
        while len(numbers) < 4:
            numbers.insert(0, 0)
        return (0, *numbers[:4])
    if identity.startswith("usb:"):
        return (1, *[int(part) for part in re.findall(r"\d+", identity[4:])])
    return (2, 0, 0, 0, getattr(link, "mac", "") or "")


def apply_engine(engine) -> int:
    """Explicit boot/install naming, after OVS starts and before the API starts.

    No global udev rules are written. Discovery always includes newly added NICs.
    """
    from makemeswitch.core import interfaces as netif
    with naming_lock():
        engine.refresh()
        engine.sync_ports()
        mapping = desired_mapping(engine.port_table(), set(netif.protected_interfaces()))
        live_ports = set(engine.ovs.list_ports())
        try:
            for mac, target in mapping.items():
                current = _live_name_for_mac(mac)
                if current != target and current in live_ports:
                    engine.ovs.del_port(current)
            changes = rename_live(mapping)
        except Exception:
            # rename_live restores names; restore OVS membership as well.
            engine.sync_ports()
            engine.reconcile(engine.config, raise_on_error=True)
            raise
        engine.sync_ports()
        engine.reconcile(engine.config, raise_on_error=True)
        print(f'Naming complete: {len(mapping)} NICs, {len(changes)} renamed')
        return 0


def apply_mapping() -> int:
    from makemeswitch.core.engine import Engine
    try:
        return apply_engine(Engine())
    except Exception as exc:
        print(f'makemeswitch-nicnames: {exc}', file=sys.stderr)
        return 1


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if action == "prepare":
        return prepare()
    if action == "apply":
        return apply_mapping()
    print("usage: python -m makemeswitch.nic_names [prepare|apply]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    # -m creates a separate __main__ module. Engine imports the canonical
    # module; running here would create two lock states and self-deadlock.
    from makemeswitch.nic_names import main as canonical_main
    raise SystemExit(canonical_main())
