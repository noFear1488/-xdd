"""Xray server config for a node, and the vless:// links clients import.

Every inbound carries the same list of active clients, so one subscription
covers every node and transport. TLS transports listen on 127.0.0.1 only:
Caddy terminates TLS for the domain and hands matching paths to them.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import quote

from . import caddy
from .model import API_PORT, CADDY_BEHIND_PORT, LABELS, Client, Node

# Clients must not reach the server's own services (the panel, the Xray API)
# or its private network through the proxy.
PRIVATE_NETS = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
]

VISION = "xtls-rprx-vision"


def active_clients(clients: list[Client], now: float) -> list[Client]:
    return [c for c in clients if c.status(now) == "active"]


def reality_server_names(node: Node, settings: dict[str, Any]) -> list[str]:
    if node.reality_front:
        return caddy.hosts(node, settings)
    return [node.transports["reality"]["sni"]]


def _inbound(kind: str, node: Node, clients: list[Client], settings: dict[str, Any]) -> dict[str, Any]:
    t = node.transports[kind]
    users = [{"id": c.uuid, "email": c.email} for c in clients]
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}

    if kind == "reality":
        for user in users:
            user["flow"] = VISION
        if node.reality_front:
            target, xver = f"127.0.0.1:{CADDY_BEHIND_PORT}", 1
        else:
            target, xver = t["dest"], 0
        return {
            "tag": kind,
            "listen": "0.0.0.0",
            "port": t["port"],
            "protocol": "vless",
            "settings": {"clients": users, "decryption": "none"},
            "streamSettings": {
                "network": "raw",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "target": target,
                    "xver": xver,
                    "serverNames": reality_server_names(node, settings),
                    "privateKey": t["private_key"],
                    "shortIds": [t["short_id"]],
                },
            },
            "sniffing": sniffing,
        }

    if kind == "xhttp":
        stream = {"network": "xhttp", "xhttpSettings": {"path": t["path"], "mode": "auto"}}
    elif kind == "ws":
        stream = {"network": "ws", "wsSettings": {"path": t["path"]}}
    elif kind == "grpc":
        stream = {"network": "grpc", "grpcSettings": {"serviceName": t["service"]}}
    else:
        raise ValueError(f"unknown transport {kind!r}")
    return {
        "tag": kind,
        "listen": "127.0.0.1",
        "port": t["port"],
        "protocol": "vless",
        "settings": {"clients": users, "decryption": "none"},
        "streamSettings": stream,
        "sniffing": sniffing,
    }


def build_config(node: Node, clients: list[Client], settings: dict[str, Any], now: float) -> dict[str, Any]:
    # A disabled node is still kept in sync, just with nobody allowed in, so
    # switching it off really cuts access instead of freezing its last config.
    users = active_clients(clients, now) if node.enabled else []
    rules: list[dict[str, Any]] = [{"type": "field", "ip": PRIVATE_NETS, "outboundTag": "block"}]
    if settings.get("block_torrents", True):
        rules.append({"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"})
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "api": {"tag": "api", "listen": f"127.0.0.1:{API_PORT}", "services": ["StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
        },
        "inbounds": [_inbound(kind, node, users, settings) for kind in node.active_transports()],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"domainStrategy": "IPIfNonMatch", "rules": rules},
    }


def dump(config: dict[str, Any]) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- share links -------------------------------------------------------------


def _host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def link(client: Client, node: Node, kind: str, settings: dict[str, Any]) -> str:
    t = node.transports[kind]
    fingerprint = settings.get("fingerprint") or "chrome"
    params: dict[str, str] = {"encryption": "none"}
    port = 443
    if kind == "reality":
        port = t["port"]
        params.update(
            type="tcp",
            security="reality",
            flow=VISION,
            sni=reality_server_names(node, settings)[0],
            fp=fingerprint,
            pbk=t["public_key"],
            sid=t["short_id"],
        )
    else:
        params.update(security="tls", sni=node.address, fp=fingerprint)
        if kind == "xhttp":
            params.update(type="xhttp", host=node.address, path=t["path"], mode="auto")
        elif kind == "ws":
            params.update(type="ws", host=node.address, path=t["path"], alpn="http/1.1")
        elif kind == "grpc":
            params.update(type="grpc", serviceName=t["service"], mode="gun", alpn="h2")
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    remark = quote(f"{node.name} · {LABELS[kind]}", safe="")
    return f"vless://{client.uuid}@{_host(node.address)}:{port}?{query}#{remark}"


def client_links(client: Client, nodes: list[Node], settings: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for node in nodes:
        if not node.enabled:
            continue
        for kind in node.active_transports():
            out.append(
                {
                    "node": node.name,
                    "transport": LABELS[kind],
                    "url": link(client, node, kind, settings),
                }
            )
    return out


def subscription(links: list[str]) -> str:
    """The plain base64 list that v2rayNG, Hiddify, Happ, Streisand, NekoBox... all read."""
    return base64.b64encode("\n".join(links).encode("utf-8")).decode("ascii")
