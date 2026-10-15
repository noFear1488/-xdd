"""Admin password hashing, session tokens and login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

_N, _R, _P = 2**14, 8, 1
SESSION_TTL = 7 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT = 15 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    enc = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${enc(salt)}${enc(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token() -> tuple[str, str]:
    """Returns (token for the cookie, hash for the database)."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def random_password() -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


class Throttle:
    """Locks an address out for a while after too many wrong passwords."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: float = LOCKOUT):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window]
        self._failures[ip] = recent
        return recent

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip, time.time())) >= self.max_failures

    def fail(self, ip: str) -> None:
        with self._lock:
            self._recent(ip, time.time()).append(time.time())

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
