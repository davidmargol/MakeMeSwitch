"""SNMPv2c agent: BER codec, MIB tree and request handling."""
import socket
import threading

import pytest

from makemeswitch.core import snmp_ber as ber
from makemeswitch.core.models import SnmpCommunity
from makemeswitch.core.snmp import (
    PortState,
    Snapshot,
    SnmpAgent,
    build_tree,
    process_datagram,
    snapshot_from_rows,
)


def snap() -> Snapshot:
    return Snapshot(
        sys_descr="MakeMeSwitch test",
        sys_name="cisco.skippy.link",
        sys_contact="noc@example.com",
        sys_location="rack 1",
        uptime_ticks=12345,
        bridge_address="aa:bb:cc:00:00:01",
        ports=[
            PortState(index=1, name="Ethernet1", mac="aa:bb:cc:00:00:11", oper_up=True, speed_mbps=1000, in_octets=5000),
            PortState(index=2, name="Ethernet2", mac="aa:bb:cc:00:00:12", oper_up=False, speed_mbps=100),
        ],
        fdb=[("aa:bb:cc:00:00:99", 2)],
    )


def get_request(oid: str, community: str = "public") -> bytes:
    return (
        ber.encode_tlv(
            ber.SEQUENCE,
            ber.encode_integer(1)
            + ber.encode_octets(community)
            + ber.encode_tlv(
                ber.GET_REQUEST,
                ber.encode_integer(42)
                + ber.encode_integer(0)
                + ber.encode_integer(0)
                + ber.encode_tlv(ber.SEQUENCE, ber.encode_tlv(ber.SEQUENCE, ber.encode_oid(oid) + ber.encode_null())),
            ),
        )
    )


def get(oid: str, community: str = "public", source: str = "10.0.0.5") -> bytes | None:
    return process_datagram(get_request(oid, community), snap(), [SnmpCommunity("public")], source)


def response_value(payload: bytes) -> tuple[int, bytes]:
    """Extract (tag, value) of the first varbind in a response."""
    _, body, _ = ber.decode_tlv(payload, 0)
    items = ber.decode_sequence(body)
    pdu_fields = ber.decode_sequence(items[2][1])
    binds = ber.decode_sequence(pdu_fields[3][1])
    bind = ber.decode_sequence(binds[0][1])
    return bind[1][0], bind[1][1]


def response_error(payload: bytes) -> tuple[int, int]:
    _, body, _ = ber.decode_tlv(payload, 0)
    items = ber.decode_sequence(body)
    pdu_fields = ber.decode_sequence(items[2][1])
    return ber.decode_integer(pdu_fields[1][1]), ber.decode_integer(pdu_fields[2][1])


# --- BER ------------------------------------------------------------------

@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 256, -1, -129, 2**31 - 1])
def test_integer_round_trip(value):
    tag, payload, end = ber.decode_tlv(ber.encode_integer(value), 0)
    assert tag == ber.INTEGER and ber.decode_integer(payload) == value


@pytest.mark.parametrize("oid", ["1.3.6.1.2.1.1.1.0", "1.3.6.1.4.1.99999.1.2", "2.100.3", "0.0"])
def test_oid_round_trip(oid):
    tag, payload, _ = ber.decode_tlv(ber.encode_oid(oid), 0)
    assert tag == ber.OID
    assert ber.oid_to_str(ber.decode_oid(payload)) == oid


def test_long_form_length_round_trip():
    blob = b"x" * 400
    encoded = ber.encode_octets(blob)
    tag, payload, end = ber.decode_tlv(encoded, 0)
    assert tag == ber.OCTET_STRING and payload == blob and end == len(encoded)


def test_counter64_keeps_high_bit_clear():
    tag, payload, _ = ber.decode_tlv(ber.encode_unsigned(ber.COUNTER64, 0x80), 0)
    assert payload[0] == 0x00 and ber.decode_integer(payload) == 0x80


# --- tree -----------------------------------------------------------------

def test_tree_contains_system_interfaces_and_bridge():
    tree = build_tree(snap())
    assert ber.oid_to_str((1, 3, 6, 1, 2, 1, 1, 5, 0)) in {ber.oid_to_str(k) for k in tree}
    assert any(k[:10] == (1, 3, 6, 1, 2, 1, 2, 2, 1, 2) for k in tree)  # ifDescr
    assert any(k[:8] == (1, 3, 6, 1, 2, 1, 17, 1) for k in tree)  # dot1dBase


def test_sys_services_is_datalink_only():
    tree = build_tree(snap())
    tag, payload = tree[(1, 3, 6, 1, 2, 1, 1, 7, 0)][:1], tree[(1, 3, 6, 1, 2, 1, 1, 7, 0)][2:]
    assert ber.decode_integer(payload) == 2


def test_interface_counters_and_names():
    tree = build_tree(snap())
    descr = tree[(1, 3, 6, 1, 2, 1, 2, 2, 1, 2, 1)]
    assert descr.endswith(b"Ethernet1")
    hc = tree[(1, 3, 6, 1, 2, 1, 31, 1, 1, 1, 6, 1)]
    assert hc[0] == ber.COUNTER64


# --- requests -------------------------------------------------------------

def test_get_sysdescr():
    payload = get("1.3.6.1.2.1.1.1.0")
    assert payload is not None
    tag, value = response_value(payload)
    assert tag == ber.OCTET_STRING and value == b"MakeMeSwitch test"


def test_get_unknown_oid_returns_nosuchobject():
    payload = get("1.3.6.1.4.1.99999.42.0")
    tag, _ = response_value(payload)
    assert tag == ber.NO_SUCH_OBJECT


def test_unknown_community_is_silently_dropped():
    assert get("1.3.6.1.2.1.1.1.0", community="private") is None


def test_source_acl_is_enforced():
    communities = [SnmpCommunity(name="public", sources=["10.9.0.0/16"])]
    request = (
        ber.encode_tlv(
            ber.SEQUENCE,
            ber.encode_integer(1)
            + ber.encode_octets("public")
            + ber.encode_tlv(
                ber.GET_REQUEST,
                ber.encode_integer(1)
                + ber.encode_integer(0)
                + ber.encode_integer(0)
                + ber.encode_tlv(ber.SEQUENCE, ber.encode_tlv(ber.SEQUENCE, ber.encode_oid("1.3.6.1.2.1.1.1.0") + ber.encode_null())),
            ),
        )
    )
    assert process_datagram(request, snap(), communities, "10.9.1.1") is not None
    assert process_datagram(request, snap(), communities, "10.0.0.5") is None


def test_set_is_rejected_as_not_writable():
    request = (
        ber.encode_tlv(
            ber.SEQUENCE,
            ber.encode_integer(1)
            + ber.encode_octets("public")
            + ber.encode_tlv(
                ber.SET_REQUEST,
                ber.encode_integer(7)
                + ber.encode_integer(0)
                + ber.encode_integer(0)
                + ber.encode_tlv(ber.SEQUENCE, ber.encode_tlv(ber.SEQUENCE, ber.encode_oid("1.3.6.1.2.1.1.5.0") + ber.encode_octets("evil"))),
            ),
        )
    )
    payload = process_datagram(request, snap(), [SnmpCommunity("public")], "10.0.0.5")
    assert response_error(payload) == (17, 1)


def test_getnext_walks_past_the_root():
    request = (
        ber.encode_tlv(
            ber.SEQUENCE,
            ber.encode_integer(1)
            + ber.encode_octets("public")
            + ber.encode_tlv(
                ber.GET_NEXT_REQUEST,
                ber.encode_integer(9)
                + ber.encode_integer(0)
                + ber.encode_integer(0)
                + ber.encode_tlv(ber.SEQUENCE, ber.encode_tlv(ber.SEQUENCE, ber.encode_oid("1.3.6.1.2.1.1.1.0") + ber.encode_null())),
            ),
        )
    )
    payload = process_datagram(request, snap(), [SnmpCommunity("public")], "10.0.0.5")
    _, body, _ = ber.decode_tlv(payload, 0)
    pdu = ber.decode_sequence(ber.decode_sequence(body)[2][1])
    bind = ber.decode_sequence(ber.decode_sequence(pdu[3][1])[0][1])
    assert ber.oid_to_str(ber.decode_oid(bind[0][1])) > "1.3.6.1.2.1.1.1.0"


# --- live state -----------------------------------------------------------

def make_sys(tmp_path, ifname="mmstest1", rx=1234, state="up"):
    root = tmp_path / "sys" / "class" / "net"
    stats = root / ifname / "statistics"
    stats.mkdir(parents=True)
    for name, value in (("rx_bytes", rx), ("tx_bytes", 99), ("rx_packets", 7), ("tx_packets", 8),
                        ("rx_errors", 0), ("tx_errors", 0), ("rx_dropped", 1), ("tx_dropped", 0)):
        (stats / name).write_text(str(value))
    (root / ifname / "operstate").write_text(state)
    (root / ifname / "speed").write_text("1000")
    (root / ifname / "mtu").write_text("1500")
    (root / ifname / "address").write_text("aa:bb:cc:00:00:11")
    return root


def test_snapshot_reads_kernel_counters(tmp_path):
    root = make_sys(tmp_path)
    rows = [{"name": "Ethernet1", "linux_if": "mmstest1", "mac": "aa:bb:cc:00:00:11", "present": 1}]
    result = snapshot_from_rows(None, rows, root=root, uptime_seconds=10)
    assert result.ports[0].in_octets == 1234
    assert result.ports[0].oper_up is True
    assert result.ports[0].speed_mbps == 1000
    assert result.uptime_ticks == 1000


def test_absent_ports_are_not_polled(tmp_path):
    root = make_sys(tmp_path)
    rows = [
        {"name": "Ethernet1", "linux_if": "mmstest1", "mac": "aa:bb:cc:00:00:11", "present": 1},
        {"name": "Ethernet2", "linux_if": "gone", "mac": "aa:bb:cc:00:00:12", "present": 0},
    ]
    result = snapshot_from_rows(None, rows, root=root, uptime_seconds=1)
    assert [p.name for p in result.ports] == ["Ethernet1"]


def test_agent_caches_snapshot_and_answers():
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return snap()

    agent = SnmpAgent(provider, [SnmpCommunity("public")], port=0)
    assert agent.handle(get_request("1.3.6.1.2.1.1.5.0"), "10.0.0.5") is not None
    assert agent.handle(get_request("1.3.6.1.2.1.1.5.0"), "10.0.0.5") is not None
    assert calls["n"] == 1


def test_agent_serves_over_udp():
    agent = SnmpAgent(lambda: snap(), [SnmpCommunity("public")], bind="127.0.0.1", port=0)
    thread = threading.Thread(target=agent.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        if agent._socket is not None:
            break
        threading.Event().wait(0.05)
    port = agent._socket.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(3)
    client.sendto(
        ber.encode_tlv(
            ber.SEQUENCE,
            ber.encode_integer(1)
            + ber.encode_octets("public")
            + ber.encode_tlv(
                ber.GET_REQUEST,
                ber.encode_integer(1)
                + ber.encode_integer(0)
                + ber.encode_integer(0)
                + ber.encode_tlv(ber.SEQUENCE, ber.encode_tlv(ber.SEQUENCE, ber.encode_oid("1.3.6.1.2.1.1.1.0") + ber.encode_null())),
            ),
        ),
        ("127.0.0.1", port),
    )
    data, _ = client.recvfrom(65535)
    tag, value = response_value(data)
    assert tag == ber.OCTET_STRING and value == b"MakeMeSwitch test"
    agent.stop()
    client.close()


def test_read_fdb_maps_ofport_to_ifindex(monkeypatch):
    """BRIDGE-MIB wants the bridge port; OVS knows only OpenFlow port numbers."""
    import subprocess

    from makemeswitch.core import snmp

    def fake_run(argv, **kwargs):
        if argv[0] == "ovs-vsctl":
            out = "Ethernet1,3\nEthernet2,4\n"
        else:
            out = " port  VLAN  MAC                Age\n    3     1  50:65:f3:2f:48:66  295\n    4     1  bc:24:11:4d:ff:47  270\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # real store rows carry the port name, not an ifIndex column
    rows = [{"name": "Ethernet1", "present": 1, "linux_if": "Ethernet1"},
            {"name": "Ethernet2", "present": 1, "linux_if": "Ethernet2"}]
    assert snmp.read_fdb("mms0", rows) == [("50:65:f3:2f:48:66", 1), ("bc:24:11:4d:ff:47", 2)]


def test_read_fdb_survives_a_missing_ovs(monkeypatch):
    import subprocess

    from makemeswitch.core import snmp

    def boom(*args, **kwargs):
        raise FileNotFoundError("ovs-appctl")

    monkeypatch.setattr(subprocess, "run", boom)
    assert snmp.read_fdb("mms0", []) == []


def _v1_request(oid: str = "1.3.6.1.2.1.1.1.0", community: str = "public",
                pdu_tag: int = ber.GET_REQUEST) -> bytes:
    varbind = ber.encode_tlv(
        ber.SEQUENCE, ber.encode_tlv(ber.OID, ber.encode_oid(oid)) + ber.encode_tlv(ber.NULL, b"")
    )
    body = ber.encode_integer(1) + ber.encode_integer(0) + ber.encode_integer(0) + ber.encode_tlv(ber.SEQUENCE, varbind)
    return ber.encode_tlv(
        ber.SEQUENCE,
        ber.encode_integer(0) + ber.encode_octets(community) + ber.encode_tlv(pdu_tag, body),
    )


def test_snmp_v1_get_is_answered_with_a_v1_reply():
    reply = process_datagram(_v1_request(), snap(), [SnmpCommunity("public")], "10.9.1.1")
    assert reply is not None
    assert reply.startswith(b"\x30")
    assert b"\x02\x01\x00" in reply[:12]   # version INTEGER 0 == SNMPv1
    assert b"\x02\x01\x01" not in reply[:12]


def test_snmp_v1_does_not_offer_getbulk():
    # a v1 client asking for GetBulk gets a genErr, never a crash or silence
    reply = process_datagram(
        _v1_request(pdu_tag=ber.GET_BULK_REQUEST), snap(), [SnmpCommunity("public")], "10.9.1.1"
    )
    assert reply is not None
    assert b"\x02\x01\x00" in reply[:12]


def test_agent_survives_a_malformed_datagram():
    agent = SnmpAgent(snap, [SnmpCommunity("public")])
    assert agent.handle(b"\x30\x03garbage", "10.9.1.1") is None


def test_bridge_port_table_uses_the_mib_columns():
    """dot1dBasePortIfIndex must be 17.1.4.1.2.<port>, not 17.1.4.2.<port>."""
    tree = build_tree(snap())
    assert (1, 3, 6, 1, 2, 1, 17, 1, 4, 1, 1, 1) in tree      # dot1dBasePort.1
    assert (1, 3, 6, 1, 2, 1, 17, 1, 4, 1, 2, 1) in tree      # dot1dBasePortIfIndex.1
    assert (1, 3, 6, 1, 2, 1, 17, 1, 4, 1, 1, 2) in tree


def test_fdb_table_uses_the_mac_as_index_after_the_column():
    """dot1dTpFdbPort is 17.4.3.1.2.<mac>; the column must not be replaced by the MAC."""
    tree = build_tree(snap())
    mac = (0xAA, 0xBB, 0xCC, 0x00, 0x00, 0x99)
    assert tree[(1, 3, 6, 1, 2, 1, 17, 4, 3, 1, 1) + mac]
    assert tree[(1, 3, 6, 1, 2, 1, 17, 4, 3, 1, 2) + mac]
    # the old, one-level-short keys must be gone
    assert (1, 3, 6, 1, 2, 1, 17, 4, 3, 1) + mac not in tree
