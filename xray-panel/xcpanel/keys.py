"""Random identifiers and X25519 keys for REALITY, using only the standard library.

Xray's own `xray x25519` does the same thing; doing it here means the panel can
create a node before Xray is even installed on it. The ladder below is the
textbook RFC 7748 one. It is not constant-time, which is fine for generating a
key once on the panel host, but it must not be used to handle secrets online.
"""

from __future__ import annotations

import base64
import secrets

_P = 2**255 - 19
_A24 = 121665
_BASE_POINT = (9).to_bytes(32, "little")

# No look-alike characters, so paths and tokens survive being read aloud.
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


def _clamp(scalar: bytes) -> bytes:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return bytes(k)


def x25519(scalar: bytes, u_point: bytes) -> bytes:
    """Scalar multiplication on Curve25519 (RFC 7748, section 5)."""
    if len(scalar) != 32 or len(u_point) != 32:
        raise ValueError("x25519 takes 32-byte inputs")
    k = int.from_bytes(_clamp(scalar), "little")
    u = bytearray(u_point)
    u[31] &= 127
    x1 = int.from_bytes(u, "little") % _P

    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def b64(raw: bytes) -> str:
    """Base64url without padding, the encoding Xray uses for REALITY keys."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_from_private(private_key: str) -> str:
    raw = unb64(private_key)
    if len(raw) != 32:
        raise ValueError("REALITY private key must be 32 bytes")
    return b64(x25519(raw, _BASE_POINT))


def reality_keypair() -> tuple[str, str]:
    """Returns (private_key, public_key) as Xray expects them in config and links."""
    private = _clamp(secrets.token_bytes(32))
    return b64(private), b64(x25519(private, _BASE_POINT))


def random_word(length: int = 12) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def short_id() -> str:
    return secrets.token_hex(8)
