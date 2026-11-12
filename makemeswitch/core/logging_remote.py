"""Remote syslog (RFC 5424 / RFC 3164) over UDP or TCP.

MakeMeSwitch does not run a local syslog daemon of its own: the installer
points rsyslog at the configured collector, and this module is what the
switch itself emits through (config changes, logins, link events), so an
Infoblox/LibreNMS/SIEM collector sees the switch identity and its events.
"""
from __future__ import annotations

import queue
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

FACILITIES = {
    "kern": 0,
    "user": 1,
    "mail": 2,
    "daemon": 3,
    "auth": 4,
    "syslog": 5,
    "lpr": 6,
    "news": 7,
    "uucp": 8,
    "cron": 9,
    "authpriv": 10,
    "ftp": 11,
    "ntp": 12,
    "audit": 13,
    "alert": 14,
    "clock": 15,
    "local0": 16,
    "local1": 17,
    "local2": 18,
    "local3": 19,
    "local4": 20,
    "local5": 21,
    "local6": 22,
    "local7": 23,
}

SEVERITIES = {
    "emergency": 0,
    "alert": 1,
    "critical": 2,
    "error": 3,
    "warning": 4,
    "notice": 5,
    "info": 6,
    "debug": 7,
}

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def severity_number(name: str) -> int:
    return SEVERITIES.get(str(name).lower(), 6)


def facility_number(name: str) -> int:
    return FACILITIES.get(str(name).lower(), 16)


def rfc5424(hostname: str, app: str, severity: str, facility: str, message: str, when: float | None = None) -> str:
    when = time.time() if when is None else when
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + f".{int(when % 1 * 1000):03d}Z"
    pri = facility_number(facility) * 8 + severity_number(severity)
    safe_host = hostname or "-"
    safe_message = message.replace("\n", " ")
    return f"<{pri}>1 {stamp} {safe_host} {app} - - - {safe_message}"


def rfc3164(hostname: str, app: str, severity: str, facility: str, message: str, when: float | None = None) -> str:
    when = time.time() if when is None else when
    local = time.localtime(when)
    stamp = f"{MONTHS[local.tm_mon - 1]} {local.tm_mday:>2} {local.tm_hour:02d}:{local.tm_min:02d}:{local.tm_sec:02d}"
    pri = facility_number(facility) * 8 + severity_number(severity)
    safe_message = message.replace("\n", " ")
    return f"<{pri}>{stamp} {hostname or '-'} {app}[{threading.get_ident() % 100000}]: {safe_message}"


@dataclass
class RemoteSyslogStats:
    sent: int = 0
    failed: int = 0
    dropped: int = 0


class RemoteSyslog:
    """Background queue + worker so a dead collector never blocks the CLI."""

    def __init__(self, config, hostname: str, app: str = "makemeswitch", queue_size: int = 1000):
        self.config = config
        self.hostname = hostname or "makemeswitch"
        self.app = app
        self.stats = RemoteSyslogStats()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max(queue_size, 1))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()

    # --- formatting -------------------------------------------------------
    def format(self, severity: str, message: str) -> str:
        cfg = self.config
        formatter = rfc3164 if str(getattr(cfg, "fmt", "rfc5424")).lower() == "rfc3164" else rfc5424
        return formatter(
            getattr(cfg, "source", "") or self.hostname,
            self.app,
            severity,
            getattr(cfg, "facility", "local0"),
            message,
        )

    def allows(self, severity: str) -> bool:
        """Send only messages at or above the configured severity."""
        return severity_number(severity) <= severity_number(getattr(self.config, "severity", "info"))

    # --- public API -------------------------------------------------------
    def emit(self, severity: str, message: str) -> bool:
        if not getattr(self.config, "enabled", False) or not getattr(self.config, "destination", ""):
            return False
        if not self.allows(severity):
            return False
        try:
            self._queue.put_nowait(self.format(severity, message))
            return True
        except queue.Full:
            self.stats.dropped += 1
            return False

    def start(self) -> "RemoteSyslog":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mms-syslog", daemon=True)
            self._thread.start()
        return self

    def flush(self, timeout: float = 3.0) -> None:
        """Block until the queue drains (used by tests and clean shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                time.sleep(0.05)
                if self._queue.empty():
                    return
            time.sleep(0.02)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._close_socket()

    # --- transport --------------------------------------------------------
    def _close_socket(self) -> None:
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None

    def _connect(self) -> socket.socket:
        cfg = self.config
        transport = str(getattr(cfg, "transport", "udp")).lower()
        address = (cfg.destination, int(cfg.port))
        if transport == "tcp":
            sock = socket.create_connection(address, timeout=5)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with self._lock:
            self._socket = sock
        return sock

    def _send(self, payload: str) -> bool:
        transport = str(getattr(self.config, "transport", "udp")).lower()
        data = payload.encode("utf-8", "replace")
        for attempt in (1, 2):
            try:
                sock = self._socket
                if sock is None or transport == "tcp" and self._broken(sock):
                    sock = self._connect()
                if transport == "tcp":
                    sock.sendall(data + b"\n")
                else:
                    sock.sendto(data, (self.config.destination, int(self.config.port)))
                self.stats.sent += 1
                return True
            except OSError:
                self._close_socket()
                if attempt == 2:
                    self.stats.failed += 1
                    return False
                time.sleep(0.2)
        return False

    @staticmethod
    def _broken(sock: socket.socket) -> bool:
        try:
            return sock.fileno() < 0
        except OSError:
            return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._send(payload)
            finally:
                self._queue.task_done()
        self._close_socket()


def build_sender(config, hostname: str, app: str = "makemeswitch", queue_size: int = 1000) -> "RemoteSyslog | None":
    """Return a started sender for an enabled config, else ``None``."""
    if config is None or not getattr(config, "enabled", False):
        return None
    if not getattr(config, "destination", ""):
        return None
    return RemoteSyslog(config, hostname, app=app, queue_size=queue_size).start()


# --- process-wide sender ---------------------------------------------------

_sender: "RemoteSyslog | None" = None
_sender_config: object | None = None
_sender_lock = threading.Lock()


def configure_sender(config, hostname: str = "makemeswitch") -> "RemoteSyslog | None":
    """(Re)configure the process-wide sender. Safe to call on every apply."""
    global _sender, _sender_config
    with _sender_lock:
        if _sender is not None and config is not None and _sender_config == config.to_dict():
            return _sender
        if _sender is not None:
            _sender.close()
            _sender = None
        _sender_config = config.to_dict() if config is not None and hasattr(config, "to_dict") else None
        _sender = build_sender(config, hostname)
        return _sender


def emit(severity: str, message: str) -> bool:
    """Send one message through the configured sender, if any."""
    sender = _sender
    if sender is None:
        return False
    return sender.emit(severity, message)


def current() -> "RemoteSyslog | None":
    return _sender


# --- rsyslog forwarding ----------------------------------------------------

RSYSLOG_FILE = "/etc/rsyslog.d/50-makemeswitch.conf"


def rsyslog_config_text(config, hostname: str = "makemeswitch") -> str:
    """Content of the rsyslog drop-in that forwards host+switch logs."""
    protocol = "tcp" if str(getattr(config, "transport", "udp")).lower() == "tcp" else "udp"
    template = "MMS_RFC5424" if str(getattr(config, "fmt", "rfc5424")).lower() != "rfc3164" else "MMS_RFC3164"
    return (
        "# Managed by MakeMeSwitch - regenerated whenever remote logging changes.\n"
        'template(name="MMS_RFC5424" type="string" '
        'string="<%pri%>1 %timestamp:::date-rfc3339% %hostname% makemeswitch - - - %msg%\\n")\n'
        'template(name="MMS_RFC3164" type="string" '
        'string="<%pri%>%timestamp:::date-rfc3164% %hostname% makemeswitch: %msg%\\n")\n'
        f"*.* action(type=\"omfwd\" target=\"{config.destination}\" port=\"{int(config.port)}\" "
        f'protocol="{protocol}" template="{template}" '
        'action.resumeRetryCount="-1" queue.type="linkedlist" queue.filename="mmsremote")\n'
    )


def apply_rsyslog(config, hostname: str = "makemeswitch") -> bool:
    """Write/remove the rsyslog drop-in. No-op (False) unless running as root."""
    import os
    import subprocess

    if os.geteuid() != 0:
        return False
    path = Path(RSYSLOG_FILE)
    enabled = config is not None and getattr(config, "enabled", False) and getattr(config, "destination", "")
    try:
        if not enabled:
            if path.exists():
                path.unlink()
            else:
                return False
        else:
            path.write_text(rsyslog_config_text(config, hostname))
        subprocess.run(["systemctl", "try-restart", "rsyslog"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def configure(config, hostname: str = "makemeswitch"):
    """(Re)configure the process-wide sender and the rsyslog drop-in."""
    (configure_sender(config, hostname))
    apply_rsyslog(config, hostname)
    return current()
