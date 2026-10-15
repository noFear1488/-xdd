"""HTTP side of the panel: the admin UI and API, and public subscriptions.

The server listens on 127.0.0.1 only. Caddy forwards two secret prefixes to it:
  /<panel_path>/  the admin UI and its JSON API (cookie session)
  /<sub_path>/    subscriptions, one unguessable token per client
Anything else gets a 404, so the panel never answers on a guessable URL.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import mimetypes
import re
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__, auth, deploy, xray
from .control import Controller
from .model import (
    Client,
    Node,
    ValidationError,
    check_node,
    clean_address,
    clean_email,
    clean_hostname,
    clean_name,
    clean_port,
    clean_segment,
    clean_ssh_host,
    clean_transports,
    default_transports,
)
from .store import Store

log = logging.getLogger("xcpanel")

STATIC = Path(__file__).parent / "static"
PANEL_FILES = {"app.js", "app.css", "qrcode.js"}
PUBLIC_FILES = {"sub.js", "app.css", "qrcode.js"}
COOKIE = "xcp_session"
GB = 1024**3
FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "random", "randomized")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def sub_url(settings: dict[str, Any], client: Client) -> str:
    return f"https://{settings['domain']}/{settings['sub_path']}/{client.sub_token}"


def panel_url(settings: dict[str, Any]) -> str:
    return f"https://{settings['domain']}/{settings['panel_path']}/"


class App:
    def __init__(self, store: Store, controller: Controller, data_dir: Path):
        self.store = store
        self.controller = controller
        self.data_dir = data_dir
        self.throttle = auth.Throttle()

    def server(self, host: str, port: int) -> ThreadingHTTPServer:
        app = self

        class BoundHandler(Handler):
            pass

        BoundHandler.app = app
        httpd = ThreadingHTTPServer((host, port), BoundHandler)
        httpd.daemon_threads = True
        return httpd

    # --- JSON views ----------------------------------------------------------

    def client_json(self, c: Client, settings: dict[str, Any], now: float) -> dict[str, Any]:
        interval = int(settings.get("stats_interval") or 60)
        return {
            "id": c.id,
            "name": c.name,
            "uuid": c.uuid,
            "enabled": c.enabled,
            "limit_bytes": c.limit_bytes,
            "expire_at": c.expire_at,
            "up_bytes": c.up_bytes,
            "down_bytes": c.down_bytes,
            "used": c.used,
            "last_active": c.last_active,
            "online": bool(c.last_active) and now - c.last_active <= 2 * interval + 5,
            "note": c.note,
            "created_at": c.created_at,
            "status": c.status(now),
            "sub_url": sub_url(settings, c),
        }

    def node_json(self, n: Node, now: float) -> dict[str, Any]:
        transports = {k: {kk: vv for kk, vv in v.items() if kk != "private_key"} for k, v in n.transports.items()}
        return {
            "id": n.id,
            "name": n.name,
            "address": n.address,
            "ssh_host": n.ssh_host,
            "ssh_port": n.ssh_port,
            "caddy": n.caddy,
            "enabled": n.enabled,
            "local": n.local,
            "reality_front": n.reality_front,
            "transports": transports,
            "deploy": {
                "at": n.deployed_at,
                "ok": n.deploy_ok,
                "log": n.deploy_log,
                "pending": self.controller.pending(n, now),
            },
        }

    def public_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "domain", "panel_path", "sub_path", "sub_title", "sub_update_hours",
            "stats_interval", "acme_email", "block_torrents", "fingerprint", "admin_user",
        )  # fmt: skip
        return {k: settings.get(k) for k in keys}

    # --- request bodies ------------------------------------------------------

    @staticmethod
    def client_fields(body: dict[str, Any]) -> dict[str, Any]:
        try:
            limit_gb = float(body.get("limit_gb") or 0)
            expire_at = int(body.get("expire_at") or 0)
        except (TypeError, ValueError):
            raise ValidationError("traffic limit and expiry must be numbers") from None
        if limit_gb < 0 or expire_at < 0:
            raise ValidationError("traffic limit and expiry cannot be negative")
        return {
            "name": clean_name(body.get("name"), "client name"),
            "limit_bytes": int(round(limit_gb * GB)),
            "expire_at": expire_at,
            "note": str(body.get("note") or "").strip()[:500],
            "enabled": bool(body.get("enabled", True)),
        }

    def node_from_body(self, body: dict[str, Any], current: Node | None, settings: dict[str, Any]) -> Node:
        local = current.local if current else not str(body.get("ssh_host") or "").strip()
        node = Node(
            id=current.id if current else 0,
            name=clean_name(body.get("name"), "node name"),
            address=clean_address(body.get("address")),
            ssh_host="" if local else clean_ssh_host(body.get("ssh_host")),
            ssh_port=clean_port(body.get("ssh_port") or 22, "SSH"),
            caddy=True if local else bool(body.get("caddy", True)),
            transports=clean_transports(body.get("transports") or {}, current.transports if current else None),
            enabled=bool(body.get("enabled", True)),
        )
        check_node(node, settings)
        return node

    def settings_from_body(self, body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        try:
            hours = int(body.get("sub_update_hours", current["sub_update_hours"]))
            interval = int(body.get("stats_interval", current["stats_interval"]))
        except (TypeError, ValueError):
            raise ValidationError("intervals must be whole numbers") from None
        if not 1 <= hours <= 168:
            raise ValidationError("subscription update interval must be 1..168 hours")
        if not 10 <= interval <= 3600:
            raise ValidationError("stats interval must be 10..3600 seconds")
        fingerprint = str(body.get("fingerprint", current["fingerprint"]))
        if fingerprint not in FINGERPRINTS:
            raise ValidationError(f"fingerprint must be one of: {', '.join(FINGERPRINTS)}")
        out = {
            "domain": clean_hostname(body.get("domain", current["domain"])),
            "panel_path": clean_segment(body.get("panel_path", current["panel_path"]), "panel path"),
            "sub_path": clean_segment(body.get("sub_path", current["sub_path"]), "subscription path"),
            "sub_title": str(body.get("sub_title", current["sub_title"]) or "").strip()[:64],
            "sub_update_hours": hours,
            "stats_interval": interval,
            "acme_email": clean_email(body.get("acme_email", current["acme_email"])),
            "block_torrents": bool(body.get("block_torrents", current["block_torrents"])),
            "fingerprint": fingerprint,
        }
        if out["panel_path"] == out["sub_path"]:
            raise ValidationError("panel and subscription paths must differ")
        return out


# --- routing -----------------------------------------------------------------

Route = tuple[str, "re.Pattern[str]", str, bool]
ROUTES: list[Route] = [
    (method, re.compile(f"^{pattern}$"), name, needs_auth)
    for method, pattern, name, needs_auth in [
        ("POST", r"/api/login", "login", False),
        ("POST", r"/api/logout", "logout", False),
        ("GET", r"/api/state", "state", True),
        ("POST", r"/api/clients", "client_create", True),
        ("PUT", r"/api/clients/(\d+)", "client_update", True),
        ("DELETE", r"/api/clients/(\d+)", "client_delete", True),
        ("POST", r"/api/clients/(\d+)/reset", "client_reset", True),
        ("POST", r"/api/clients/(\d+)/regenerate", "client_regenerate", True),
        ("GET", r"/api/clients/(\d+)/links", "client_links", True),
        ("GET", r"/api/node-defaults", "node_defaults", True),
        ("GET", r"/api/node-setup", "node_setup", True),
        ("POST", r"/api/nodes", "node_create", True),
        ("PUT", r"/api/nodes/(\d+)", "node_update", True),
        ("DELETE", r"/api/nodes/(\d+)", "node_delete", True),
        ("POST", r"/api/nodes/(\d+)/deploy", "node_deploy", True),
        ("POST", r"/api/nodes/(\d+)/check", "node_check", True),
        ("GET", r"/api/nodes/(\d+)/config", "node_config", True),
        ("PUT", r"/api/settings", "settings_update", True),
        ("POST", r"/api/password", "password", True),
    ]
]


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "xcpanel"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet: no access log of secret URLs
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    # --- plumbing ------------------------------------------------------------

    def _client_ip(self) -> str:
        ip = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded and ip in ("127.0.0.1", "::1"):
            ip = forwarded.split(",")[-1].strip()
        return ip

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged = {"Cache-Control": "no-store", **(headers or {})}
        self._send(status, body, "application/json; charset=utf-8", merged)

    def _not_found(self) -> None:
        self._send(404, b"404 page not found\n", "text/plain; charset=utf-8")

    def _file(self, name: str) -> None:
        path = STATIC / name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _session(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(COOKIE)
        if morsel and self.app.store.session_valid(auth.token_hash(morsel.value)):
            return morsel.value
        return None

    def _cookie(self, settings: dict[str, Any], token: str, max_age: int) -> str:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return (
            f"{COOKIE}={token}; Path=/{settings['panel_path']}/; Max-Age={max_age}; "
            f"HttpOnly; SameSite=Strict{secure}"
        )

    def _dispatch(self, method: str) -> None:
        try:
            settings = self.app.store.settings()
            path = urlsplit(self.path).path
            panel, sub = "/" + settings["panel_path"], "/" + settings["sub_path"]
            if settings["panel_path"] and path == panel:
                self._send(308, b"", "text/plain", {"Location": panel + "/"})
            elif settings["panel_path"] and path.startswith(panel + "/"):
                self._panel(method, path[len(panel) :], settings)
            elif settings["sub_path"] and path.startswith(sub + "/"):
                self._subscription(method, path[len(sub) :], settings)
            else:
                self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("request failed: %s %s", method, self.path)
            try:
                self._json(500, {"error": "internal error, see the panel log (journalctl -u xcpanel)"})
            except OSError:
                pass

    # --- admin panel ---------------------------------------------------------

    def _panel(self, method: str, rest: str, settings: dict[str, Any]) -> None:
        html_headers = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY", "Cache-Control": "no-cache"}
        if rest == "/" and method in ("GET", "HEAD"):
            body = (STATIC / "index.html").read_bytes()
            return self._send(200, body, "text/html; charset=utf-8", html_headers)
        if rest.startswith("/static/") and method in ("GET", "HEAD"):
            name = rest[len("/static/") :]
            return self._file(name) if name in PANEL_FILES else self._not_found()
        if not rest.startswith("/api/"):
            return self._not_found()

        for route_method, pattern, name, needs_auth in ROUTES:
            match = pattern.match(rest)
            if not match or route_method != method:
                continue
            # A custom header is something a cross-site form cannot send.
            if self.headers.get("X-Requested-With") != "xcpanel":
                return self._json(403, {"error": "missing X-Requested-With header"})
            token = self._session()
            if needs_auth and token is None:
                return self._json(401, {"error": "not logged in"})
            try:
                handler: Callable[..., Any] = getattr(self, f"api_{name}")
                args = [int(g) for g in match.groups()]
                result = handler(settings, *args) if not needs_auth else handler(settings, token, *args)
            except ValidationError as exc:
                return self._json(400, {"error": str(exc)})
            except ApiError as exc:
                return self._json(exc.status, {"error": str(exc)})
            except KeyError:
                return self._json(404, {"error": "not found"})
            status, payload, headers = result if len(result) == 3 else (*result, None)
            return self._json(status, payload, headers)
        return self._json(404, {"error": "unknown API call"})

    # Each api_* returns (status, payload) or (status, payload, headers).

    def api_login(self, settings: dict[str, Any]) -> tuple:
        ip = self._client_ip()
        if self.app.throttle.blocked(ip):
            raise ApiError(429, "too many wrong passwords, try again in 15 minutes")
        body = self._body()
        user_ok = str(body.get("username", "")) == settings["admin_user"]
        password_ok = auth.verify_password(str(body.get("password", "")), settings["admin_hash"])
        if not (user_ok and password_ok):
            self.app.throttle.fail(ip)
            log.warning("failed login from %s", ip)
            raise ApiError(401, "wrong username or password")
        self.app.throttle.clear(ip)
        token, digest = auth.new_token()
        self.app.store.add_session(digest, int(time.time()) + auth.SESSION_TTL)
        return 200, {"ok": True}, {"Set-Cookie": self._cookie(settings, token, auth.SESSION_TTL)}

    def api_logout(self, settings: dict[str, Any]) -> tuple:
        token = self._session()
        if token:
            self.app.store.delete_session(auth.token_hash(token))
        return 200, {"ok": True}, {"Set-Cookie": self._cookie(settings, "", 0)}

    def api_state(self, settings: dict[str, Any], _token: str) -> tuple:
        now = time.time()
        store = self.app.store
        return 200, {
            "version": __version__,
            "now": int(now),
            "panel_url": panel_url(settings),
            "settings": self.app.public_settings(settings),
            "clients": [self.app.client_json(c, settings, now) for c in store.clients()],
            "nodes": [self.app.node_json(n, now) for n in store.nodes()],
        }

    def api_client_create(self, settings: dict[str, Any], _token: str) -> tuple:
        fields = self.app.client_fields(self._body())
        client = self.app.store.add_client(fields.pop("name"), **fields)
        self.app.controller.kick()
        return 201, self.app.client_json(client, settings, time.time())

    def api_client_update(self, settings: dict[str, Any], _token: str, client_id: int) -> tuple:
        client = self.app.store.update_client(client_id, **self.app.client_fields(self._body()))
        self.app.controller.kick()
        return 200, self.app.client_json(client, settings, time.time())

    def api_client_delete(self, settings: dict[str, Any], _token: str, client_id: int) -> tuple:
        self.app.store.delete_client(client_id)
        self.app.controller.kick()
        return 200, {"ok": True}

    def api_client_reset(self, settings: dict[str, Any], _token: str, client_id: int) -> tuple:
        self.app.store.reset_traffic(client_id)
        self.app.controller.kick()
        client = self.app.store.client(client_id)
        if client is None:
            raise KeyError(client_id)
        return 200, self.app.client_json(client, settings, time.time())

    def api_client_regenerate(self, settings: dict[str, Any], _token: str, client_id: int) -> tuple:
        client = self.app.store.regenerate_client(client_id)
        self.app.controller.kick()
        return 200, self.app.client_json(client, settings, time.time())

    def api_client_links(self, settings: dict[str, Any], _token: str, client_id: int) -> tuple:
        client = self.app.store.client(client_id)
        if client is None:
            raise KeyError(client_id)
        links = xray.client_links(client, self.app.store.nodes(), settings)
        return 200, {"sub_url": sub_url(settings, client), "links": links}

    def api_node_defaults(self, settings: dict[str, Any], _token: str) -> tuple:
        transports = default_transports(reality=True)
        for t in transports.values():
            t.pop("private_key", None)
            t.pop("public_key", None)
            t.pop("short_id", None)
        return 200, {"transports": transports}

    def api_node_setup(self, settings: dict[str, Any], _token: str) -> tuple:
        query = parse_qs(urlsplit(self.path).query)
        with_caddy = query.get("caddy", ["1"])[0] != "0"
        try:
            public_key = deploy.ensure_ssh_key(self.app.data_dir)
        except (OSError, Exception) as exc:  # ssh-keygen missing or failing
            raise ApiError(500, f"could not create the panel SSH key: {exc}") from None
        return 200, {"public_key": public_key, "script": deploy.node_setup_script(public_key, with_caddy)}

    def api_node_create(self, settings: dict[str, Any], _token: str) -> tuple:
        node = self.app.node_from_body(self._body(), None, settings)
        if not node.local:
            deploy.ensure_ssh_key(self.app.data_dir)
        node = self.app.store.add_node(node)
        return 201, self.app.node_json(node, time.time())

    def api_node_update(self, settings: dict[str, Any], _token: str, node_id: int) -> tuple:
        current = self.app.store.node(node_id)
        if current is None:
            raise KeyError(node_id)
        node = self.app.node_from_body(self._body(), current, settings)
        node = self.app.store.update_node(
            node_id,
            name=node.name,
            address=node.address,
            ssh_host=node.ssh_host,
            ssh_port=node.ssh_port,
            caddy=node.caddy,
            transports=node.transports,
            enabled=node.enabled,
        )
        self.app.controller.kick()
        return 200, self.app.node_json(node, time.time())

    def api_node_delete(self, settings: dict[str, Any], _token: str, node_id: int) -> tuple:
        node = self.app.store.node(node_id)
        if node is None:
            raise KeyError(node_id)
        if node.local:
            raise ValidationError("this server hosts the panel and cannot be removed; disable its transports instead")
        self.app.store.delete_node(node_id)
        return 200, {"ok": True}

    def api_node_deploy(self, settings: dict[str, Any], _token: str, node_id: int) -> tuple:
        result = self.app.controller.deploy(node_id, force=True)
        node = self.app.store.node(node_id)
        if node is None:
            raise KeyError(node_id)
        return 200, {"ok": result.ok, "output": result.output, "node": self.app.node_json(node, time.time())}

    def api_node_check(self, settings: dict[str, Any], _token: str, node_id: int) -> tuple:
        result = self.app.controller.check(node_id)
        return 200, {"ok": result.ok, "output": result.output}

    def api_node_config(self, settings: dict[str, Any], _token: str, node_id: int) -> tuple:
        node = self.app.store.node(node_id)
        if node is None:
            raise KeyError(node_id)
        xray_text, caddy_text = self.app.controller.render(node)
        return 200, {"xray": xray_text, "caddy": caddy_text}

    def api_settings_update(self, settings: dict[str, Any], _token: str) -> tuple:
        new = self.app.settings_from_body(self._body(), settings)
        merged = {**settings, **new}
        for node in self.app.store.nodes():
            if node.local:
                check_node(node, merged)
        routing = ("domain", "panel_path", "sub_path")
        moved = any(new[k] != settings[k] for k in routing)
        self.app.store.update_settings(new)
        if not moved:
            self.app.controller.kick()
            return 200, {"ok": True, "settings": self.app.public_settings(merged)}

        # The panel's own address changed: Caddy must pick it up right now,
        # or the admin is left on a URL that no longer leads anywhere.
        local = next((n for n in self.app.store.nodes() if n.local), None)
        if local is not None:
            result = self.app.controller.deploy(local.id)
            if not result.ok:
                self.app.store.update_settings({k: settings[k] for k in routing})
                raise ApiError(502, "Caddy did not accept the new address, nothing changed:\n" + result.output[-1500:])
        self.app.controller.kick()
        return 200, {
            "ok": True,
            "settings": self.app.public_settings(merged),
            "redirect": panel_url(merged),
        }, {"Set-Cookie": self._cookie(settings, "", 0)}

    def api_password(self, settings: dict[str, Any], _token: str) -> tuple:
        body = self._body()
        if not auth.verify_password(str(body.get("current", "")), settings["admin_hash"]):
            raise ApiError(400, "current password is wrong")
        new = str(body.get("new", ""))
        if len(new) < 8:
            raise ValidationError("the new password needs at least 8 characters")
        username = clean_name(body.get("username") or settings["admin_user"], "username")
        self.app.store.update_settings({"admin_hash": auth.hash_password(new), "admin_user": username})
        self.app.store.delete_all_sessions()
        token, digest = auth.new_token()
        self.app.store.add_session(digest, int(time.time()) + auth.SESSION_TTL)
        return 200, {"ok": True}, {"Set-Cookie": self._cookie(settings, token, auth.SESSION_TTL)}

    # --- subscriptions -------------------------------------------------------

    def _subscription(self, method: str, rest: str, settings: dict[str, Any]) -> None:
        if method not in ("GET", "HEAD"):
            return self._not_found()
        if rest.startswith("/assets/"):
            name = rest[len("/assets/") :]
            return self._file(name) if name in PUBLIC_FILES else self._not_found()
        token = rest.strip("/")
        client = self.app.store.client_by_token(token) if token else None
        if client is None:
            return self._not_found()

        now = time.time()
        status = client.status(now)
        links = xray.client_links(client, self.app.store.nodes(), settings) if status == "active" else []
        title = settings.get("sub_title") or settings["domain"]
        url = sub_url(settings, client)
        headers = {
            "Cache-Control": "no-store",
            "Profile-Title": "base64:" + base64.b64encode(title.encode("utf-8")).decode("ascii"),
            "Profile-Update-Interval": str(settings.get("sub_update_hours") or 12),
            "Subscription-Userinfo": (
                f"upload={client.up_bytes}; download={client.down_bytes}; "
                f"total={client.limit_bytes}; expire={client.expire_at}"
            ),
            "Profile-Web-Page-Url": url,
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(title)}",
        }
        query = parse_qs(urlsplit(self.path).query)
        wants_page = "text/html" in self.headers.get("Accept", "") and "raw" not in query
        if not wants_page:
            body = xray.subscription([link["url"] for link in links]).encode("ascii")
            return self._send(200, body, "text/plain; charset=utf-8", headers)

        page = render_sub_page(client, status, links, url, title, now)
        headers.pop("Content-Disposition")
        headers.update({"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"})
        return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", headers)


# --- the page a client sees when opening their link in a browser -------------

_STATUS_TEXT = {
    "active": "Active",
    "disabled": "Disabled by the admin",
    "expired": "Expired",
    "limited": "Traffic limit reached",
}


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.2f} {unit}"
        value /= 1024
    return f"{n} B"


def render_sub_page(client: Client, status: str, links: list[dict[str, str]], url: str, title: str, now: float) -> str:
    esc = html.escape
    used = _size(client.used) + (f" of {_size(client.limit_bytes)}" if client.limit_bytes else " (no limit)")
    if client.expire_at:
        days = int((client.expire_at - now) // 86400)
        expiry = time.strftime("%Y-%m-%d", time.gmtime(client.expire_at))
        expiry += f" ({days} days left)" if days >= 0 else ""
    else:
        expiry = "never"
    data = json.dumps({"sub": url, "links": links}).replace("</", "<\\/")
    rows = "".join(
        f'<li><span class="badge">{esc(link["transport"])}</span><span class="grow">{esc(link["node"])}</span>'
        f'<button class="btn small" data-copy="{esc(link["url"])}">Copy</button>'
        f'<button class="btn small ghost" data-qr="{esc(link["url"])}">QR</button></li>'
        for link in links
    )
    apps = [
        ("Hiddify", f"hiddify://import/{url}"),
        ("v2RayTun", f"v2raytun://import/{url}"),
        ("Happ", f"happ://add/{url}"),
        ("Streisand", f"streisand://import/{url}"),
    ]
    app_buttons = "".join(f'<a class="btn" href="{esc(href)}">{esc(name)}</a>' for name, href in apps)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{esc(title)} · {esc(client.name)}</title>
<link rel="stylesheet" href="assets/app.css">
</head>
<body class="sub-page">
<main class="subpage">
  <header class="sub-head">
    <h1>{esc(title)}</h1>
    <p class="muted">Subscription for <strong>{esc(client.name)}</strong></p>
  </header>
  <section class="card">
    <dl class="facts">
      <div><dt>Status</dt><dd><span class="pill {esc(status)}">{esc(_STATUS_TEXT[status])}</span></dd></div>
      <div><dt>Traffic used</dt><dd>{esc(used)}</dd></div>
      <div><dt>Expires</dt><dd>{esc(expiry)}</dd></div>
    </dl>
  </section>
  <section class="card">
    <h2>1. Install an app</h2>
    <p class="muted">Android / iOS / Windows / macOS: Hiddify, v2RayTun, Happ, Streisand, v2rayNG or NekoBox all work.</p>
    <h2>2. Add this subscription</h2>
    <div class="sub-qr" id="sub-qr" aria-label="QR code of the subscription link"></div>
    <div class="copy-row"><input readonly value="{esc(url)}" aria-label="Subscription link">
      <button class="btn primary" data-copy="{esc(url)}">Copy link</button></div>
    <p class="muted">Scan the code, paste the link into the app's "add subscription" screen, or open it directly:</p>
    <div class="app-buttons">{app_buttons}</div>
  </section>
  <section class="card">
    <h2>Single configs</h2>
    <ul class="link-list">{rows or '<li class="muted">No active configs.</li>'}</ul>
  </section>
</main>
<div class="modal" id="qr-modal" hidden><div class="modal-box narrow"><div id="qr-big" class="qr-big"></div>
<button class="btn" data-close>Close</button></div></div>
<script type="application/json" id="sub-data">{data}</script>
<script src="assets/qrcode.js"></script>
<script src="assets/sub.js"></script>
</body>
</html>
"""
