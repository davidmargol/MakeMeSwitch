"""Integration tests against real Open vSwitch with network namespaces.

Skipped unless running as root with OVS installed.  Each test uses a dedicated
bridge and disposable veth/namespace pairs, never production interfaces.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from makemeswitch.backends.ovs import OvsBackend
from makemeswitch.core.engine import Engine
from makemeswitch.core.models import InterfaceConfig, SwitchConfig, Vlan
from makemeswitch.core.store import Store

BRIDGE = "oswtest"
NS = ["t1", "t2", "t3"]
IFACES = ["oswt1", "oswt2", "oswt3"]

pytestmark = pytest.mark.integration


def root_and_ovs() -> bool:
    return os.geteuid() == 0 and shutil.which("ovs-vsctl") is not None and shutil.which("ip") is not None


pytestmark = [pytestmark, pytest.mark.skipif(not root_and_ovs(), reason="requires root + Open vSwitch")]


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=30)


def ping(ns: str, target: str, count: int = 3) -> bool:
    result = sh("ip", "netns", "exec", ns, "ping", "-c", str(count), "-W", "1", target, check=False)
    return result.returncode == 0


def wait_ping(ns: str, target: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ping(ns, target, count=1):
            return True
        time.sleep(0.5)
    return False


@pytest.fixture()
def topology(tmp_path):
    sh("ovs-vsctl", "--if-exists", "del-br", BRIDGE)
    for ns in NS:
        sh("ip", "netns", "del", ns, check=False)
        sh("ip", "netns", "add", ns)
    for index, (iface, ns) in enumerate(zip(IFACES, NS), start=1):
        sh("ip", "link", "del", iface, check=False)
        sh("ip", "link", "add", iface, "type", "veth", "peer", f"p{index}")
        sh("ip", "link", "set", f"p{index}", "netns", ns)
        sh("ip", "link", "set", iface, "up")
        sh("ip", "netns", "exec", ns, "ip", "link", "set", f"p{index}", "up")
        sh("ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up")
    sh("ip", "netns", "exec", "t1", "ip", "addr", "add", "10.10.10.1/24", "dev", "p1")
    sh("ip", "netns", "exec", "t2", "ip", "addr", "add", "10.10.10.2/24", "dev", "p2")
    sh("ip", "netns", "exec", "t3", "ip", "addr", "add", "10.10.20.3/24", "dev", "p3")

    settings = {
        "bridge": BRIDGE,
        "db_path": str(tmp_path / "integration.db"),
        "log_dir": None,
        "allow_virtual_ports": True,
    }
    engine = Engine(settings=settings, store=Store(settings["db_path"]), ovs=OvsBackend(BRIDGE))
    engine.sync_ports = lambda: []  # type: ignore[assignment]
    yield engine
    try:
        engine.l3.flush(engine.ovs)
    except Exception:  # noqa: BLE001 - best effort cleanup
        pass
    sh("ovs-vsctl", "--if-exists", "del-br", BRIDGE, check=False)
    for ns in NS:
        sh("ip", "netns", "del", ns, check=False)


def config_for(engine: SwitchConfig) -> SwitchConfig:
    cfg = SwitchConfig()
    cfg.set_hostname("INT-SW")
    cfg.vlans[10] = Vlan(10, "TEN")
    cfg.vlans[20] = Vlan(20, "TWENTY")
    cfg.interfaces["Ethernet1"] = InterfaceConfig(name="Ethernet1", linux_if="oswt1", attached=True, access_vlan=10)
    cfg.interfaces["Ethernet2"] = InterfaceConfig(name="Ethernet2", linux_if="oswt2", attached=True, access_vlan=10)
    cfg.interfaces["Ethernet3"] = InterfaceConfig(name="Ethernet3", linux_if="oswt3", attached=True, access_vlan=20)
    return cfg


def test_same_vlan_forwards_and_isolates(topology):
    engine = topology
    engine.apply(config_for(engine), user="test", confirm=True)
    assert wait_ping("t1", "10.10.10.2"), "same-VLAN hosts must communicate"
    assert not ping("t1", "10.10.20.3"), "different VLANs must be isolated"
    entries = engine.mac_table()
    assert entries, "OVS must learn MAC addresses"
    assert {entry["vlan"] for entry in entries} <= {10, 20}


def test_administrative_shutdown_blocks_forwarding(topology):
    engine = topology
    engine.apply(config_for(engine), user="test", confirm=True)
    assert wait_ping("t1", "10.10.10.2")
    cfg = engine.config.copy()
    cfg.interfaces["Ethernet2"].admin_up = False
    engine.apply(cfg, user="test", confirm=True)
    assert not ping("t1", "10.10.10.2"), "shutdown port must not forward"
    cfg = engine.config.copy()
    cfg.interfaces["Ethernet2"].admin_up = True
    engine.apply(cfg, user="test", confirm=True)
    assert wait_ping("t1", "10.10.10.2"), "no shutdown must restore forwarding"


def test_trunk_preserves_vlan_tags(topology):
    engine = topology
    cfg = config_for(engine)
    cfg.interfaces["Ethernet3"].mode = "trunk"
    cfg.interfaces["Ethernet3"].trunk_allowed = [10, 20]
    cfg.interfaces["Ethernet3"].native_vlan = None
    engine.apply(cfg, user="test", confirm=True)
    sh("ip", "netns", "exec", "t3", "ip", "link", "add", "link", "p3", "name", "p3.10", "type", "vlan", "id", "10")
    sh("ip", "netns", "exec", "t3", "ip", "addr", "add", "10.10.10.3/24", "dev", "p3.10")
    sh("ip", "netns", "exec", "t3", "ip", "link", "set", "p3.10", "up")
    assert wait_ping("t3", "10.10.10.1"), "tagged VLAN 10 traffic must cross the trunk"
    details = engine.ovs.port_details()["oswt3"]
    assert details["trunks"] == [10, 20]
    assert details["tag"] is None


def test_configuration_survives_restart(topology, tmp_path):
    engine = topology
    engine.apply(config_for(engine), user="test", confirm=True)
    engine.save_startup("test")
    assert wait_ping("t1", "10.10.10.2")

    # Simulate a service restart: new engine, same SQLite DB, OVS state wiped.
    sh("ovs-vsctl", "--if-exists", "del-br", BRIDGE)
    restarted = Engine(
        settings={**engine.settings, "allow_virtual_ports": True},
        store=Store(engine.store.path),
        ovs=OvsBackend(BRIDGE),
    )
    restarted.sync_ports = lambda: []  # type: ignore[assignment]
    restarted.bootstrap(apply=True)
    assert restarted.config.hostname == "INT-SW"
    assert restarted.ovs.bridge_exists()
    assert wait_ping("t1", "10.10.10.2"), "saved configuration must be restored after restart"


def test_running_config_drift_detected(topology):
    engine = topology
    engine.apply(config_for(engine), user="test", confirm=True)
    engine.ovs.set_port_vlans("oswt1", 20, [])
    issues = engine.diff()
    assert any(issue.get("port") == "Ethernet1" for issue in issues)


def test_layer3_svi_routes_between_vlans(topology):
    from makemeswitch.core.models import Svi

    engine = topology
    cfg = config_for(engine)
    cfg.ip_routing = True
    cfg.svis[10] = Svi(10, "10.10.10.254/24")
    cfg.svis[20] = Svi(20, "10.10.20.254/24")
    engine.apply(cfg, user="test", confirm=True)
    sh("ip", "netns", "exec", "t1", "ip", "route", "add", "default", "via", "10.10.10.254")
    sh("ip", "netns", "exec", "t3", "ip", "route", "add", "default", "via", "10.10.20.254")
    assert wait_ping("t1", "10.10.20.3", timeout=12), "SVIs must route between VLANs"
    assert wait_ping("t3", "10.10.10.1", timeout=12), "reverse direction must route too"
    assert engine.ovs.port_exists("vlan10")
    assert engine.ovs.port_exists("vlan20")
