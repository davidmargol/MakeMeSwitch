"""Remote syslog: formatting, severity filter and real UDP/TCP delivery."""
import socket
import socketserver
import threading
import time

import pytest

from makemeswitch.core.logging_remote import (
    RemoteSyslog,
    build_sender,
    facility_number,
    rfc3164,
    rfc5424,
    severity_number,
)
from makemeswitch.core.models import SyslogConfig


def config(**overrides):
    base = dict(enabled=True, destination="127.0.0.1", transport="udp", port=514,
                severity="info", facility="local0", fmt="rfc5424")
    base.update(overrides)
    return SyslogConfig(**base)


def test_severity_and_facility_numbers():
    assert severity_number("emergency") == 0
    assert severity_number("info") == 6
    assert severity_number("debug") == 7
    assert severity_number("nonsense") == 6
    assert facility_number("local0") == 16
    assert facility_number("syslog") == 5


def test_rfc5424_shape():
    line = rfc5424("cisco.skippy.link", "makemeswitch", "warning", "local0", "port Ethernet3 down", when=1_700_000_000.5)
    pri = facility_number("local0") * 8 + severity_number("warning")
    assert line.startswith(f"<{pri}>1 2023-11-14T22:13:20.500Z cisco.skippy.link makemeswitch")
    assert line.endswith("port Ethernet3 down")


def test_rfc3164_shape():
    line = rfc3164("cisco.skippy.link", "makemeswitch", "error", "local3", "boom", when=1_700_000_000)
    assert line.startswith(f"<{facility_number('local3') * 8 + 3}>")
    assert "cisco.skippy.link makemeswitch[" in line
    assert line.endswith("boom")


def test_newlines_are_flattened():
    assert "\n" not in rfc5424("h", "a", "info", "local0", "line1\nline2")


def test_severity_filter_drops_chatty_messages():
    sender = RemoteSyslog(config(severity="warning"), "sw1")
    assert sender.emit("error", "kept") is True
    assert sender.emit("info", "dropped") is False
    assert sender.emit("debug", "dropped") is False


def test_disabled_config_sends_nothing():
    assert build_sender(config(enabled=False), "sw1") is None
    assert RemoteSyslog(config(enabled=False), "sw1").emit("error", "x") is False


def test_full_queue_drops_without_blocking():
    sender = RemoteSyslog(config(), "sw1", queue_size=1)
    sender.emit("info", "first")
    sender.emit("info", "second")
    assert sender.stats.dropped == 1


def test_udp_delivery_is_real():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(5)
    port = listener.getsockname()[1]
    sender = build_sender(config(port=port, transport="udp"), "cisco.skippy.link")
    assert sender is not None
    try:
        assert sender.emit("warning", "Configuration applied by admin") is True
        sender.flush()
        data, _ = listener.recvfrom(65535)
    finally:
        sender.close()
        listener.close()
    text = data.decode()
    assert text.startswith(f"<{facility_number('local0') * 8 + 4}>")
    assert "cisco.skippy.link" in text
    assert "Configuration applied by admin" in text
    assert sender.stats.sent == 1


class _LineHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        self.server.received.append(line.decode().strip())  # type: ignore[attr-defined]


def test_tcp_delivery_is_real():
    server = socketserver.TCPServer(("127.0.0.1", 0), _LineHandler)
    server.received = []  # type: ignore[attr-defined]
    server.timeout = 5
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    port = server.server_address[1]
    sender = build_sender(config(port=port, transport="tcp", fmt="rfc3164"), "sw1")
    assert sender is not None
    try:
        assert sender.emit("error", "link Ethernet2 down") is True
        sender.flush()
        thread.join(timeout=5)
    finally:
        sender.close()
        server.server_close()
    assert server.received, "collector received nothing"
    assert "link Ethernet2 down" in server.received[0]


def test_dead_collector_does_not_raise():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nobody is listening now
    sender = build_sender(config(port=port), "sw1")
    assert sender is not None
    try:
        sender.emit("error", "into the void")
        sender.flush()
        time.sleep(0.1)
    finally:
        sender.close()
    assert sender.stats.failed + sender.stats.sent == 1


def test_build_sender_requires_destination():
    from makemeswitch.core.models import ConfigError

    with pytest.raises(ConfigError, match="no destination"):
        config(destination="")
    assert build_sender(config(enabled=False), "sw1") is None


def test_rsyslog_dropin_content():
    from makemeswitch.core.logging_remote import rsyslog_config_text

    text = rsyslog_config_text(config(destination="10.0.0.10", transport="tcp", port=6514))
    assert 'type="omfwd"' in text
    assert 'target="10.0.0.10"' in text and 'port="6514"' in text
    assert 'protocol="tcp"' in text
    assert "MMS_RFC5424" in text
    assert "%hostname%" in text and "makemeswitch" in text


def test_rsyslog_dropin_uses_rfc3164_template_when_selected():
    from makemeswitch.core.logging_remote import rsyslog_config_text

    text = rsyslog_config_text(config(destination="log.example.com", fmt="rfc3164"))
    assert 'template="MMS_RFC3164"' in text
