"""Authentication, sessions, CSRF and rate limiting for the web panel."""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections import defaultdict, deque
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from makemeswitch.core import cisco9
from makemeswitch.core.store import Store
from makemeswitch.utils.logging import get_logger

log = get_logger("makemeswitch.security")

ROLE_LEVEL = {"readonly": 0, "operator": 1, "admin": 2}
ROLE_PRIVILEGE = {"readonly": 1, "operator": 7, "admin": 15}
COOKIE_NAME = "osw_session"
MIN_PASSWORD_LENGTH = 10


def role_to_privilege(role: str) -> int:
    return ROLE_PRIVILEGE.get(role, 1)


def privilege_to_role(privilege: int) -> str:
    """Cisco privilege 15 is full admin; 2-14 may configure; 0-1 is read-only."""
    value = int(privilege)
    if not 0 <= value <= 15:
        raise ValueError("privilege must be 0-15")
    if value == 15:
        return "admin"
    if value >= 2:
        return "operator"
    return "readonly"


class SecurityError(RuntimeError):
    pass


class RateLimiter:
    """Sliding-window limiter for login attempts, keyed by client IP."""

    def __init__(self, limit: int = 10, window: int = 60):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


class SecurityService:
    def __init__(self, store: Store, session_ttl: int = 28800, login_limit: int = 10):
        self.store = store
        self.session_ttl = session_ttl
        self.limiter = RateLimiter(login_limit)
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)

    # --- passwords -------------------------------------------------------
    def hash_password(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify_password(self, stored_hash: str, password: str) -> bool:
        stored_hash = str(stored_hash or "")
        if stored_hash.startswith("$9$"):
            return cisco9.verify(password, stored_hash)
        try:
            return self.hasher.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    @staticmethod
    def password_scheme(stored_hash: str) -> str:
        value = str(stored_hash or "")
        if value.startswith("$9$"):
            return "cisco9"
        if value.startswith("$argon2"):
            return "argon2id"
        return "unknown"

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        if password.lower() in ("password", "admin", "12345678", "qwertyui"):
            raise ValueError("password is too common")

    # --- users -----------------------------------------------------------
    def get_user(self, username: str) -> dict[str, Any] | None:
        return self.store.get_user(username)

    def list_users(self) -> list[dict[str, Any]]:
        return self.store.list_users()

    def _admin_count(self) -> int:
        return sum(1 for user in self.store.list_users() if user.get("role") == "admin" and not user.get("disabled"))

    def _guard_last_admin(self, username: str, new_role: str | None = None, deleting: bool = False) -> None:
        existing = self.store.get_user(username)
        if not existing or existing.get("role") != "admin" or existing.get("disabled"):
            return
        becomes_non_admin = deleting or (new_role is not None and new_role != "admin")
        if becomes_non_admin and self._admin_count() <= 1:
            raise ValueError("refusing to remove the last enabled administrator")

    def set_credentials(self, username: str, password: str, role: str = "admin", create: bool = False) -> None:
        self.validate_password(password)
        if role not in ROLE_LEVEL:
            raise ValueError(f"invalid role {role!r}")
        if create:
            if self.store.get_user(username):
                raise ValueError(f"user {username} already exists")
        elif not self.store.get_user(username):
            raise ValueError(f"user {username} does not exist")
        self._guard_last_admin(username, new_role=role)
        self.store.upsert_user(username, self.hash_password(password), role)

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLE_LEVEL:
            raise ValueError(f"invalid role {role!r}")
        user = self.store.get_user(username)
        if not user:
            raise ValueError(f"user {username} does not exist")
        self._guard_last_admin(username, new_role=role)
        self.store.set_user_role(username, role)

    def import_cisco9(self, username: str, secret_hash: str, role: str = "admin", create: bool = False) -> None:
        """Import a real Cisco IOS type 9 hash without ever seeing plaintext."""
        digest = cisco9.validate_hash(secret_hash)
        if role not in ROLE_LEVEL:
            raise ValueError(f"invalid role {role!r}")
        if create:
            if self.store.get_user(username):
                raise ValueError(f"user {username} already exists")
        elif not self.store.get_user(username):
            raise ValueError(f"user {username} does not exist")
        self._guard_last_admin(username, new_role=role)
        self.store.upsert_user(username, digest, role)

    def delete_user(self, username: str, actor: str | None = None) -> None:
        if actor and actor == username:
            raise ValueError("cannot delete the account you are logged in with")
        self._guard_last_admin(username, deleting=True)
        self.store.delete_user(username)

    def ensure_bootstrap_admin(self, username: str = "admin", password: str | None = None) -> tuple[str, str] | None:
        if self.store.list_users():
            return None
        password = password or os.environ.get("MMS_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        self.set_credentials(username, password, "admin", create=True)
        return username, password

    # --- authentication --------------------------------------------------
    def authenticate(self, username: str, password: str, ip: str | None = None) -> dict[str, Any] | None:
        key = ip or "unknown"
        if not self.limiter.allow(key):
            self.store.audit(username, ip, "login", None, "rate-limited", "too many attempts")
            raise SecurityError("too many login attempts, try again later")
        user = self.store.get_user(username)
        if not user or user["disabled"]:
            self.store.audit(username, ip, "login", None, "failure", "unknown or disabled user")
            return None
        if not self.verify_password(user["password_hash"], password):
            self.store.audit(username, ip, "login", None, "failure", "bad password")
            return None
        self.limiter.reset(key)
        self.store.audit(username, ip, "login", None, "success", "")
        return user

    # --- sessions --------------------------------------------------------
    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_session(self, username: str, ip: str | None, user_agent: str | None) -> dict[str, Any]:
        self.store.purge_expired_sessions()
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self.store.create_session(self._token_hash(token), username, csrf, self.session_ttl, ip or "", user_agent or "")
        return {"token": token, "csrf": csrf, "expires_in": self.session_ttl, "username": username}

    def validate_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        record = self.store.get_session(self._token_hash(token))
        if not record:
            return None
        user = self.store.get_user(record["username"])
        if not user or user["disabled"]:
            self.store.delete_session(self._token_hash(token))
            return None
        self.store.touch_session(self._token_hash(token))
        return {
            "username": record["username"],
            "role": user["role"],
            "csrf": record["csrf"],
            "expires_at": record["expires_at"],
            "ip": record["ip"],
        }

    def revoke(self, token: str | None) -> None:
        if token:
            self.store.delete_session(self._token_hash(token))

    @staticmethod
    def can(role: str, required: str) -> bool:
        return ROLE_LEVEL.get(role, 0) >= ROLE_LEVEL.get(required, 0)
