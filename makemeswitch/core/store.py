"""SQLite-backed persistence: configs, port map, users, sessions, audit."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from makemeswitch.core.models import SwitchConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS configs (
    name TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system'
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    user TEXT NOT NULL DEFAULT 'system',
    config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ports (
    identity TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    linux_if TEXT,
    kind TEXT NOT NULL DEFAULT 'ethernet',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    note TEXT DEFAULT '',
    mac TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'readonly',
    created_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip TEXT,
    user_agent TEXT,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    username TEXT NOT NULL,
    ip TEXT,
    action TEXT NOT NULL,
    target TEXT,
    result TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stats_samples (
    ts REAL NOT NULL,
    port TEXT NOT NULL,
    rx_bytes REAL NOT NULL,
    tx_bytes REAL NOT NULL,
    rx_packets REAL NOT NULL,
    tx_packets REAL NOT NULL,
    rx_errors REAL NOT NULL,
    tx_errors REAL NOT NULL,
    PRIMARY KEY (ts, port)
);
CREATE INDEX IF NOT EXISTS idx_stats_port_ts ON stats_samples(port, ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


class Store:
    """Thread-safe repository.  All writes are transactional."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.path = str(db_path)
        self._lock = threading.RLock()
        self._local = threading.local()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        conn = self._connect()
        with self._lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(SCHEMA)
            # Additive migration for databases created before ports carried a MAC.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(ports)")}
            if "mac" not in columns:
                conn.execute("ALTER TABLE ports ADD COLUMN mac TEXT DEFAULT ''")

    # --- configuration ---------------------------------------------------
    def save_config(self, name: str, config: SwitchConfig, user: str = "system") -> None:
        payload = json.dumps(config.to_dict(), sort_keys=True)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO configs(name, json, updated_at, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (name, payload, time.time(), user),
            )
            if name == "running":
                conn.execute(
                    "INSERT INTO history(ts, action, user, config) VALUES (?,?,?,?)",
                    (time.time(), "apply", user, payload),
                )
                conn.execute(
                    "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 50)"
                )

    def load_config(self, name: str) -> SwitchConfig | None:
        row = self._connect().execute("SELECT json FROM configs WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        return SwitchConfig.from_dict(json.loads(row["json"]))

    def config_meta(self, name: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT updated_at, updated_by FROM configs WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT id, ts, action, user FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def history_config(self, history_id: int) -> SwitchConfig | None:
        row = self._connect().execute("SELECT config FROM history WHERE id=?", (history_id,)).fetchone()
        return SwitchConfig.from_dict(json.loads(row["config"])) if row else None

    # --- port map --------------------------------------------------------
    def all_ports(self) -> list[dict[str, Any]]:
        rows = self._connect().execute("SELECT * FROM ports ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def port_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM ports WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def port_by_identity(self, identity: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM ports WHERE identity=?", (identity,)).fetchone()
        return dict(row) if row else None

    def register_port(
        self,
        identity: str,
        name: str,
        linux_if: str,
        kind: str = "ethernet",
        note: str = "",
        mac: str = "",
    ) -> None:
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO ports(identity, name, linux_if, kind, first_seen, last_seen, present, note, mac) "
                "VALUES (?,?,?,?,?,?,1,?,?) ON CONFLICT(identity) DO UPDATE SET "
                "linux_if=excluded.linux_if, last_seen=excluded.last_seen, present=1, "
                "mac=CASE WHEN excluded.mac != '' THEN excluded.mac ELSE ports.mac END",
                (identity, name, linux_if, kind, now, now, note, mac),
            )

    def rename_port(self, identity: str, new_name: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE ports SET name=? WHERE identity=?", (new_name, identity))

    def renumber_ports(self, mapping: dict[str, str]) -> None:
        """Apply ``identity -> new name`` in one go (unique names need 2 phases)."""
        if not mapping:
            return
        with self.transaction() as conn:
            for index, identity in enumerate(mapping):
                conn.execute("UPDATE ports SET name=? WHERE identity=?", (f"__renumber_tmp_{index}__", identity))
            for identity, new_name in mapping.items():
                conn.execute("UPDATE ports SET name=? WHERE identity=?", (new_name, identity))

    def mark_port_present(self, identity: str, present: bool) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE ports SET present=?, last_seen=? WHERE identity=?", (1 if present else 0, time.time(), identity))

    def port_by_mac(self, mac: str) -> dict[str, Any] | None:
        if not mac:
            return None
        row = self._connect().execute("SELECT * FROM ports WHERE mac=? LIMIT 1", (mac.lower(),)).fetchone()
        return dict(row) if row else None

    def update_port_identity(self, old_identity: str, new_identity: str, linux_if: str, mac: str) -> None:
        """Re-key a port row when a NIC gains a more precise topology identity."""
        with self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM ports WHERE identity=?", (new_identity,)).fetchone()
            if exists:
                conn.execute("DELETE FROM ports WHERE identity=?", (old_identity,))
                return
            conn.execute(
                "UPDATE ports SET identity=?, linux_if=?, mac=? WHERE identity=?",
                (new_identity, linux_if, mac.lower(), old_identity),
            )

    def update_port_linux(self, identity: str, linux_if: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE ports SET linux_if=? WHERE identity=?", (linux_if, identity))

    def delete_port(self, identity: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM ports WHERE identity=?", (identity,))

    # --- users / sessions ------------------------------------------------
    def get_user(self, username: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def upsert_user(self, username: str, password_hash: str, role: str, must_change: bool = False) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at, disabled, must_change_password) "
                "VALUES (?,?,?,?,0,?) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, role=excluded.role",
                (username, password_hash, role, time.time(), 1 if must_change else 0),
            )

    def set_password(self, username: str, password_hash: str, must_change: bool = False) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=? WHERE username=?",
                (password_hash, 1 if must_change else 0, username),
            )

    def list_users(self, include_secrets: bool = False) -> list[dict[str, Any]]:
        columns = "username, role, created_at, disabled, must_change_password"
        if include_secrets:
            columns += ", password_hash"
        rows = self._connect().execute(
            f"SELECT {columns} FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_user_role(self, username: str, role: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE users SET role=? WHERE username=?", (role, username))

    def delete_user(self, username: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM users WHERE username=?", (username,))

    def create_session(self, token_hash: str, username: str, csrf: str, ttl: int, ip: str, ua: str) -> None:
        now = time.time()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash, username, csrf, created_at, expires_at, ip, user_agent, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (token_hash, username, csrf, now, now + ttl, ip, ua, now),
            )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if not row:
            return None
        if row["expires_at"] < time.time():
            self.delete_session(token_hash)
            return None
        return dict(row)

    def touch_session(self, token_hash: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (time.time(), token_hash))

    def delete_session(self, token_hash: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def purge_expired_sessions(self) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

    # --- audit / events --------------------------------------------------
    def audit(self, username: str, ip: str | None, action: str, target: str | None, result: str, detail: str = "") -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audit(ts, username, ip, action, target, result, detail) VALUES (?,?,?,?,?,?,?)",
                (time.time(), username, ip, action, target, result, detail[:2000]),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connect().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def add_event(self, severity: str, source: str, message: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events(ts, severity, source, message) VALUES (?,?,?,?)",
                (time.time(), severity, source, message[:1000]),
            )
            conn.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")
        # Mirror to the configured collector (no-op when remote syslog is off).
        try:
            from makemeswitch.core import logging_remote

            logging_remote.emit(severity, f"{source}: {message}")
        except Exception:  # noqa: BLE001 - logging must never break an operation
            pass

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connect().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # --- stats -----------------------------------------------------------
    def add_stats(self, samples: Iterable[dict[str, Any]]) -> None:
        now = time.time()
        rows = [
            (
                now,
                s["port"],
                float(s.get("rx_bytes", 0)),
                float(s.get("tx_bytes", 0)),
                float(s.get("rx_packets", 0)),
                float(s.get("tx_packets", 0)),
                float(s.get("rx_errors", 0)),
                float(s.get("tx_errors", 0)),
            )
            for s in samples
        ]
        if not rows:
            return
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stats_samples(ts, port, rx_bytes, tx_bytes, rx_packets, tx_packets, rx_errors, tx_errors) "
                "VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            # keep 24h of samples
            conn.execute("DELETE FROM stats_samples WHERE ts < ?", (now - 86400,))

    def stats_history(self, port: str | None = None, seconds: int = 900) -> list[dict[str, Any]]:
        since = time.time() - seconds
        if port:
            rows = self._connect().execute(
                "SELECT * FROM stats_samples WHERE ts >= ? AND port=? ORDER BY ts", (since, port)
            ).fetchall()
        else:
            rows = self._connect().execute("SELECT * FROM stats_samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        return [dict(r) for r in rows]

    def meta_get(self, key: str) -> str | None:
        row = self._connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
