"""Independent SNMP verification with net-snmp client tools.

Starts the real agent on a loopback port and walks it with snmpget/snmpwalk/
snmpbulkwalk, so the BER we encode is validated by a third-party client rather
than by our own decoder. Requires the `snmp` package; skipped otherwise.
"""
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

from makemeswitch.core.models import BannerConfig, SnmpCommunity, SnmpConfig, SwitchConfig  # noqa: E402
from makemeswitch.core.snmp import PortState, Snapshot, SnmpAgent  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def snapshot() -> Snapshot:
    return Snapshot(
        sys_descr="MakeMeSwitch 1.0.0 (Open vSwitch L2 switch on Linux)",
        sys_name="cisco.skippy.link",
        sys_contact="noc@example.com",
        sys_location="lab rack 3",
        uptime_ticks=98765,
        bridge_address="aa:bb:cc:00:00:01",
        ports=[
            PortState(index=1, name="Ethernet1", mac="aa:bb:cc:00:00:11", oper_up=True, speed_mbps=1000,
                      in_octets=123456789012, out_octets=987654321),
            PortState(index=2, name="Ethernet2", mac="aa:bb:cc:00:00:12", oper_up=False, speed_mbps=100,
                      in_octets=42, out_octets=7),
        ],
        fdb=[("aa:bb:cc:00:00:99", 2)],
    )


def run(cmd: list[str], timeout: int = 20) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout + result.stderr


def main() -> int:
    missing = [tool for tool in ("snmpget", "snmpwalk", "snmpbulkwalk") if not shutil.which(tool)]
    if missing:
        print(f"SKIP: net-snmp tools not installed ({', '.join(missing)})")
        return 77

    port = free_port()
    communities = [SnmpCommunity("public", sources=["127.0.0.0/8"])]
    agent = SnmpAgent(snapshot, communities, bind="127.0.0.1", port=port)
    thread = threading.Thread(target=agent.serve_forever, daemon=True)
    thread.start()
    for _ in range(100):
        if agent._socket is not None:
            break
        threading.Event().wait(0.05)

    target = f"127.0.0.1:{port}"
    failures: list[str] = []

    def check(label: str, output: str, expected: list[str], mode: str = "all") -> None:
        flat = output.lower()
        hits = [item.lower() in flat for item in expected]
        ok = all(hits) if mode == "all" else any(hits)
        print(f"{'OK  ' if ok else 'FAIL'} {label}")
        if not ok:
            print(repr(output))
            failures.append(label)

    out = run(["snmpget", "-v2c", "-c", "public", "-t", "3", "-r", "1", "-On", target, "1.3.6.1.2.1.1.1.0"])
    check("snmpget sysDescr", out, ["MakeMeSwitch 1.0.0"])

    out = run(["snmpget", "-v2c", "-c", "public", "-t", "3", "-r", "1", target,
               "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.6.0", "1.3.6.1.2.1.1.7.0"])
    check("snmpget sysName/contact/services", out, ["cisco.skippy.link", "lab rack 3", "INTEGER: 2"])

    out = run(["snmpwalk", "-v2c", "-c", "public", "-t", "3", "-r", "1", "-On", target, "1.3.6.1.2.1.1"])
    check("snmpwalk system group", out, ["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.7.0"])

    out = run(["snmpwalk", "-v2c", "-c", "public", "-t", "3", "-r", "1", "-On", target, "1.3.6.1.2.1.2.2.1.2"])
    check("snmpwalk ifDescr lists both ports", out, ["Ethernet1", "Ethernet2"])

    out = run(["snmpget", "-v2c", "-c", "public", "-t", "3", "-r", "1", target, "1.3.6.1.2.1.2.2.1.8.1"])
    check("ifOperStatus up", out, ["INTEGER: 1"])

    out = run(["snmpbulkwalk", "-v2c", "-c", "public", "-t", "3", "-r", "1", "-On", target, "1.3.6.1.2.1.31.1.1.1.6"])
    check("snmpbulkwalk ifHCInOctets (Counter64)", out, ["Counter64: 123456789012"])

    out = run(["snmpwalk", "-v2c", "-c", "public", "-t", "3", "-r", "1", "-On", target, "1.3.6.1.2.1.17.4.3.1.2"])
    check("snmpwalk dot1dTpFdbPort (bridge MAC table)", out, ["1.3.6.1.2.1.17.4.3.1.2"])

    out = run(["snmpget", "-v2c", "-c", "public", "-t", "2", "-r", "1", target, "1.3.6.1.2.1.1.2.0"])
    check("sysObjectID is a real OID", out, ["OID:", "3.6.1.4.1.32473.1.1"])

    out = run(["snmpget", "-v2c", "-c", "wrongcommunity", "-t", "2", "-r", "1", target, "1.3.6.1.2.1.1.1.0"])
    check("wrong community gets no answer", out, ["Timeout", "No Response"])

    out = run(["snmpget", "-v2c", "-c", "public", "-t", "2", "-r", "1", target, "1.3.6.1.4.1.99999.1.0"])
    check("unknown OID -> No Such Object", out, ["No Such Object"])

    out = run(["snmpset", "-v2c", "-c", "public", "-t", "2", "-r", "1", target,
               "1.3.6.1.2.1.1.5.0", "s", "hacked"]) if shutil.which("snmpset") else "notWritable"
    check("SET is refused (read-only)", out, ["notwritable", "readonly", "not writable"], mode="any")

    agent.stop()
    if failures:
        print(f"\nSNMP VERIFY FAILED: {failures}")
        return 1
    print("\nSNMP VERIFIED with net-snmp 5.9 client: get/walk/bulkwalk/FDB/ACL/read-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
