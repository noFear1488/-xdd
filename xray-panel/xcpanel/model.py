"""Clients, nodes and the validation of everything the admin can type in."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from . import keys

TRANSPORTS = ("xhttp", "ws", "grpc", "reality")
# Transports that terminate TLS in Caddy; REALITY listens on its own port.
TLS_TRANSPORTS = ("xhttp", "ws", "grpc")
LABELS = {"xhttp": "XHTTP", "ws": "WS", "grpc": "gRPC", "reality": "Reality"}

API_PORT = 10085
DEFAULT_REALITY_SNI = "www.microsoft.com"
DEFAULT_REALITY_PORT = 443
# With Reality on 443 Caddy moves behind Xray and listens here, on 127.0.0.1 only.
CADDY_BEHIND_PORT = 9443

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~-]{1,64}$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SSH_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9._-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.:-]*$")
_EMAIL_RE = re.compile(r"^[^@\s{}\"'#]+@[^@\s{}\"'#]+\.[^@\s{}\"'#]+$")
_SHORT_ID_RE = re.compile(r"^([0-9a-f]{2}){0,8}$")


class ValidationError(ValueError):
    """Bad input from the admin; the message is shown in the UI as is."""


@dataclass
class Client:
    id: int
    name: str
    uuid: str
    sub_token: str
    enabled: bool = True
    limit_bytes: int = 0
    expire_at: int = 0
    up_bytes: int = 0
    down_bytes: int = 0
    last_active: int = 0
    note: str = ""
    created_at: int = 0

    @property
    def used(self) -> int:
        return self.up_bytes + self.down_bytes

    def status(self, now: float) -> str:
        if not self.enabled:
            return "disabled"
        if self.expire_at and now >= self.expire_at:
            return "expired"
        if self.limit_bytes and self.used >= self.limit_bytes:
            return "limited"
        return "active"

    @property
    def email(self) -> str:
        """The Xray user label. It starts with the id so stats map back reliably."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")[:32]
        return f"{self.id}.{slug}" if slug else str(self.id)


def client_id_from_email(email: str) -> int | None:
    head = email.split(".", 1)[0]
    return int(head) if head.isdigit() else None


@dataclass
class Node:
    id: int
    name: str
    address: str
    ssh_host: str = ""
    ssh_port: int = 22
    caddy: bool = True
    transports: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: bool = True
    xray_hash: str = ""
    caddy_hash: str = ""
    deployed_at: int = 0
    deploy_ok: bool | None = None
    deploy_log: str = ""
    created_at: int = 0

    @property
    def local(self) -> bool:
        return not self.ssh_host

    def active_transports(self) -> list[str]:
        return [t for t in TRANSPORTS if self.transports.get(t, {}).get("enabled")]

    def tls_transports(self) -> list[str]:
        return [t for t in self.active_transports() if t in TLS_TRANSPORTS]

    @property
    def reality_front(self) -> bool:
        """Reality owns port 443 and passes every other TLS connection on to Caddy.

        Browsers, ACME and the XHTTP/WS/gRPC clients all reach Caddy through
        Xray's fallback, and Reality "borrows" the domain's own certificate,
        which looks far more natural than another site's on a foreign port.
        """
        reality = self.transports.get("reality", {})
        return bool(self.caddy and reality.get("enabled") and reality.get("port") == 443)


# --- defaults ----------------------------------------------------------------


def default_transports(reality: bool = True) -> dict[str, dict[str, Any]]:
    private, public = keys.reality_keypair()
    return {
        "xhttp": {"enabled": True, "path": "/" + keys.random_word(10), "port": 10001},
        "ws": {"enabled": False, "path": "/" + keys.random_word(10), "port": 10002},
        "grpc": {"enabled": False, "service": keys.random_word(10), "port": 10003},
        "reality": {
            "enabled": reality,
            "port": DEFAULT_REALITY_PORT,
            "sni": DEFAULT_REALITY_SNI,
            "dest": f"{DEFAULT_REALITY_SNI}:443",
            "private_key": private,
            "public_key": public,
            "short_id": keys.short_id(),
        },
    }


# --- validation --------------------------------------------------------------


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return True


def clean_hostname(value: Any, what: str = "domain") -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not _HOST_RE.match(host):
        raise ValidationError(f"{what} must be a domain name like nordpine.site, got {host!r}")
    return host


def clean_address(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if is_ip(raw):
        return raw.strip("[]")
    return clean_hostname(raw, "address")


def clean_port(value: Any, what: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{what}: port must be a number") from None
    if not 1 <= port <= 65535:
        raise ValidationError(f"{what}: port must be between 1 and 65535")
    return port


def clean_path(value: Any, what: str) -> str:
    path = str(value or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    if not _PATH_RE.match(path):
        raise ValidationError(f"{what}: path must look like /abc123 (letters, digits, . _ ~ -)")
    return path


def clean_segment(value: Any, what: str) -> str:
    """A single URL segment such as the panel or subscription prefix."""
    return clean_path(value, what)[1:]


def clean_email(value: Any) -> str:
    email = str(value or "").strip()
    if email and not _EMAIL_RE.match(email):
        raise ValidationError("ACME email does not look like an email address")
    return email


def clean_name(value: Any, what: str = "name") -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ValidationError(f"{what} is required")
    if len(name) > 64:
        raise ValidationError(f"{what} is too long (64 characters max)")
    return name


def clean_ssh_host(value: Any) -> str:
    host = str(value or "").strip()
    if host and not _SSH_RE.match(host):
        raise ValidationError("SSH target must look like root@203.0.113.7 or root@de.nordpine.site")
    return host


def clean_transports(raw: Any, current: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Validates the transport section of a node form, keeping secrets the UI never sees."""
    if not isinstance(raw, dict):
        raise ValidationError("transports must be an object")
    current = current or default_transports(reality=False)
    out: dict[str, dict[str, Any]] = {}

    for kind in ("xhttp", "ws"):
        src = raw.get(kind) or {}
        cur = current.get(kind, {})
        out[kind] = {
            "enabled": bool(src.get("enabled", False)),
            "path": clean_path(src.get("path") or cur.get("path"), LABELS[kind]),
            "port": clean_port(src.get("port") or cur.get("port"), LABELS[kind]),
        }

    src = raw.get("grpc") or {}
    cur = current.get("grpc", {})
    service = str(src.get("service") or cur.get("service") or "").strip().lstrip("/")
    if not _SERVICE_RE.match(service):
        raise ValidationError("gRPC: service name may contain letters, digits, . _ -")
    out["grpc"] = {
        "enabled": bool(src.get("enabled", False)),
        "service": service,
        "port": clean_port(src.get("port") or cur.get("port"), "gRPC"),
    }

    src = raw.get("reality") or {}
    cur = current.get("reality", {})
    sni = clean_hostname(src.get("sni") or cur.get("sni") or DEFAULT_REALITY_SNI, "Reality SNI")
    dest = str(src.get("dest") or "").strip() or f"{sni}:443"
    host, _, dest_port = dest.rpartition(":")
    if not host or not dest_port.isdigit():
        raise ValidationError("Reality: target must be host:port, e.g. www.microsoft.com:443")
    clean_port(dest_port, "Reality target")

    private = cur.get("private_key", "")
    if src.get("regenerate") or not private:
        private, public = keys.reality_keypair()
        short = keys.short_id()
    else:
        public = keys.public_from_private(private)
        short = str(src.get("short_id", cur.get("short_id", ""))).strip().lower()
        if not _SHORT_ID_RE.match(short):
            raise ValidationError("Reality: short id must be up to 16 hex characters, even length")
    out["reality"] = {
        "enabled": bool(src.get("enabled", False)),
        "port": clean_port(src.get("port") or cur.get("port") or DEFAULT_REALITY_PORT, "Reality"),
        "sni": sni,
        "dest": dest,
        "private_key": private,
        "public_key": public,
        "short_id": short,
    }
    return out


def check_node(node: Node, settings: dict[str, Any]) -> None:
    """Cross-field rules: ports that collide, paths that shadow the panel, etc."""
    active = node.active_transports()
    if node.local and not node.caddy:
        raise ValidationError("the panel's own server always runs Caddy")
    if node.tls_transports():
        if not node.caddy:
            raise ValidationError("XHTTP, WS and gRPC go through Caddy: enable Caddy for this node")
        if is_ip(node.address):
            raise ValidationError(
                "XHTTP, WS and gRPC need a domain name (Caddy gets the certificate for it); "
                "an IP address only works with Reality"
            )

    if node.reality_front and is_ip(node.address) and not node.local:
        raise ValidationError(
            "Reality on 443 hides behind this node's own website, so the node needs a domain "
            "name; use another port or turn Caddy off for an IP-only node"
        )

    taken: dict[int, str] = {API_PORT: "Xray API"}
    if node.local:
        panel_port = int(str(settings.get("panel_listen", "")).rpartition(":")[2] or 0)
        taken[panel_port] = "the panel"
    if node.caddy:
        taken[80] = "Caddy"
        if node.reality_front:
            taken[CADDY_BEHIND_PORT] = "Caddy (behind Reality)"
        else:
            taken[443] = "Caddy"
    for kind in active:
        port = node.transports[kind]["port"]
        if port in taken:
            raise ValidationError(f"{LABELS[kind]}: port {port} is already used by {taken[port]}")
        taken[port] = LABELS[kind]

    reserved = {}
    if node.local:
        reserved[settings.get("panel_path")] = "the panel"
        reserved[settings.get("sub_path")] = "subscriptions"
    for kind in ("xhttp", "ws", "grpc"):
        if kind not in active:
            continue
        t = node.transports[kind]
        segment = t["path"][1:] if kind != "grpc" else t["service"]
        if segment in reserved:
            raise ValidationError(f"{LABELS[kind]}: path /{segment} is already used by {reserved[segment]}")
        reserved[segment] = LABELS[kind]
