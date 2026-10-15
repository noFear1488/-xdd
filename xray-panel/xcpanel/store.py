"""SQLite storage for settings, nodes, clients and admin sessions.

One connection guarded by a lock: the panel serves a handful of requests a
minute, so there is nothing to gain from a pool and a lot to lose in locking bugs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any, Iterable

from . import keys
from .model import Client, Node, ValidationError

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    address     TEXT NOT NULL,
    ssh_host    TEXT NOT NULL DEFAULT '',
    ssh_port    INTEGER NOT NULL DEFAULT 22,
    caddy       INTEGER NOT NULL DEFAULT 1,
    transports  TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    xray_hash   TEXT NOT NULL DEFAULT '',
    caddy_hash  TEXT NOT NULL DEFAULT '',
    deployed_at INTEGER NOT NULL DEFAULT 0,
    deploy_ok   INTEGER,
    deploy_log  TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    uuid        TEXT NOT NULL UNIQUE,
    sub_token   TEXT NOT NULL UNIQUE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    limit_bytes INTEGER NOT NULL DEFAULT 0,
    expire_at   INTEGER NOT NULL DEFAULT 0,
    up_bytes    INTEGER NOT NULL DEFAULT 0,
    down_bytes  INTEGER NOT NULL DEFAULT 0,
    last_active INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "domain": "",
    "panel_path": "",
    "sub_path": "",
    "panel_listen": "127.0.0.1:8740",
    "admin_user": "admin",
    "admin_hash": "",
    "sub_title": "",
    "sub_update_hours": 12,
    "stats_interval": 60,
    "acme_email": "",
    "block_torrents": True,
    "fingerprint": "chrome",
}

_NODE_FIELDS = {"name", "address", "ssh_host", "ssh_port", "caddy", "transports", "enabled"}
_CLIENT_FIELDS = {"name", "uuid", "sub_token", "enabled", "limit_bytes", "expire_at", "note"}


def _now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(params))

    # --- settings ------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        for row in self._exec("SELECT key, value FROM settings"):
            result[row["key"]] = json.loads(row["value"])
        return result

    def update_settings(self, values: dict[str, Any]) -> None:
        with self._lock:
            for key, value in values.items():
                self._exec(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )

    # --- nodes ---------------------------------------------------------------

    @staticmethod
    def _node(row: sqlite3.Row) -> Node:
        return Node(
            id=row["id"],
            name=row["name"],
            address=row["address"],
            ssh_host=row["ssh_host"],
            ssh_port=row["ssh_port"],
            caddy=bool(row["caddy"]),
            transports=json.loads(row["transports"]),
            enabled=bool(row["enabled"]),
            xray_hash=row["xray_hash"],
            caddy_hash=row["caddy_hash"],
            deployed_at=row["deployed_at"],
            deploy_ok=None if row["deploy_ok"] is None else bool(row["deploy_ok"]),
            deploy_log=row["deploy_log"],
            created_at=row["created_at"],
        )

    def nodes(self) -> list[Node]:
        return [self._node(r) for r in self._exec("SELECT * FROM nodes ORDER BY id")]

    def node(self, node_id: int) -> Node | None:
        row = self._exec("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._node(row) if row else None

    def add_node(self, node: Node) -> Node:
        if node.local and any(n.local for n in self.nodes()):
            raise ValidationError("only one node can be this server; give the others an SSH target")
        cur = self._exec(
            "INSERT INTO nodes(name, address, ssh_host, ssh_port, caddy, transports, enabled, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node.name,
                node.address,
                node.ssh_host,
                node.ssh_port,
                int(node.caddy),
                json.dumps(node.transports),
                int(node.enabled),
                _now(),
            ),
        )
        return self.node(cur.lastrowid)  # type: ignore[return-value]

    def update_node(self, node_id: int, **values: Any) -> Node:
        unknown = set(values) - _NODE_FIELDS
        if unknown:
            raise KeyError(f"unknown node fields: {unknown}")
        if values.get("ssh_host") == "":
            if any(n.local and n.id != node_id for n in self.nodes()):
                raise ValidationError("only one node can be this server; give the others an SSH target")
        if "transports" in values:
            values["transports"] = json.dumps(values["transports"])
        for flag in ("caddy", "enabled"):
            if flag in values:
                values[flag] = int(bool(values[flag]))
        if values:
            assignments = ", ".join(f"{k} = ?" for k in values)
            self._exec(f"UPDATE nodes SET {assignments} WHERE id = ?", [*values.values(), node_id])
        node = self.node(node_id)
        if node is None:
            raise KeyError(node_id)
        return node

    def delete_node(self, node_id: int) -> None:
        self._exec("DELETE FROM nodes WHERE id = ?", (node_id,))

    def record_deploy(self, node_id: int, ok: bool, log: str, xray_hash: str | None, caddy_hash: str | None) -> None:
        with self._lock:
            self._exec(
                "UPDATE nodes SET deployed_at = ?, deploy_ok = ?, deploy_log = ? WHERE id = ?",
                (_now(), int(ok), log[-8000:], node_id),
            )
            if ok and xray_hash is not None:
                self._exec("UPDATE nodes SET xray_hash = ? WHERE id = ?", (xray_hash, node_id))
            if ok and caddy_hash is not None:
                self._exec("UPDATE nodes SET caddy_hash = ? WHERE id = ?", (caddy_hash, node_id))

    # --- clients -------------------------------------------------------------

    @staticmethod
    def _client(row: sqlite3.Row) -> Client:
        return Client(
            id=row["id"],
            name=row["name"],
            uuid=row["uuid"],
            sub_token=row["sub_token"],
            enabled=bool(row["enabled"]),
            limit_bytes=row["limit_bytes"],
            expire_at=row["expire_at"],
            up_bytes=row["up_bytes"],
            down_bytes=row["down_bytes"],
            last_active=row["last_active"],
            note=row["note"],
            created_at=row["created_at"],
        )

    def clients(self) -> list[Client]:
        return [self._client(r) for r in self._exec("SELECT * FROM clients ORDER BY id")]

    def client(self, client_id: int) -> Client | None:
        row = self._exec("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        return self._client(row) if row else None

    def client_by_token(self, token: str) -> Client | None:
        row = self._exec("SELECT * FROM clients WHERE sub_token = ?", (token,)).fetchone()
        return self._client(row) if row else None

    def add_client(self, name: str, *, limit_bytes: int = 0, expire_at: int = 0, note: str = "", enabled: bool = True) -> Client:
        try:
            cur = self._exec(
                "INSERT INTO clients(name, uuid, sub_token, enabled, limit_bytes, expire_at, note, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (name, str(uuidlib.uuid4()), keys.random_word(24), int(enabled), limit_bytes, expire_at, note, _now()),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(f"a client named {name!r} already exists") from None
        return self.client(cur.lastrowid)  # type: ignore[return-value]

    def update_client(self, client_id: int, **values: Any) -> Client:
        unknown = set(values) - _CLIENT_FIELDS
        if unknown:
            raise KeyError(f"unknown client fields: {unknown}")
        if "enabled" in values:
            values["enabled"] = int(bool(values["enabled"]))
        if values:
            assignments = ", ".join(f"{k} = ?" for k in values)
            try:
                self._exec(f"UPDATE clients SET {assignments} WHERE id = ?", [*values.values(), client_id])
            except sqlite3.IntegrityError:
                raise ValidationError(f"a client named {values.get('name')!r} already exists") from None
        client = self.client(client_id)
        if client is None:
            raise KeyError(client_id)
        return client

    def regenerate_client(self, client_id: int) -> Client:
        return self.update_client(client_id, uuid=str(uuidlib.uuid4()), sub_token=keys.random_word(24))

    def delete_client(self, client_id: int) -> None:
        self._exec("DELETE FROM clients WHERE id = ?", (client_id,))

    def reset_traffic(self, client_id: int) -> None:
        self._exec("UPDATE clients SET up_bytes = 0, down_bytes = 0 WHERE id = ?", (client_id,))

    def add_traffic(self, usage: dict[int, tuple[int, int]], now: int | None = None) -> None:
        now = now or _now()
        with self._lock:
            for client_id, (up, down) in usage.items():
                if up <= 0 and down <= 0:
                    continue
                self._exec(
                    "UPDATE clients SET up_bytes = up_bytes + ?, down_bytes = down_bytes + ?, last_active = ? "
                    "WHERE id = ?",
                    (max(up, 0), max(down, 0), now, client_id),
                )

    # --- sessions ------------------------------------------------------------

    def add_session(self, token_hash: str, expires_at: int) -> None:
        with self._lock:
            self._exec("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
            self._exec("INSERT INTO sessions(token_hash, expires_at) VALUES(?, ?)", (token_hash, expires_at))

    def session_valid(self, token_hash: str) -> bool:
        row = self._exec("SELECT expires_at FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        return bool(row) and row["expires_at"] > _now()

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_all_sessions(self) -> None:
        self._exec("DELETE FROM sessions")
