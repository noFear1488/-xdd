import tempfile
from pathlib import Path

from xcpanel import auth
from xcpanel.deploy import Result, Runner
from xcpanel.model import Node, default_transports
from xcpanel.store import Store

SETTINGS = {
    "domain": "nordpine.site",
    "panel_path": "panelpath123",
    "sub_path": "subpath45678",
    "panel_listen": "127.0.0.1:8740",
    "admin_user": "admin",
    "admin_hash": "",
    "sub_title": "nordpine",
    "acme_email": "",
    "block_torrents": True,
    "fingerprint": "chrome",
}


def local_node(**transport_changes) -> Node:
    t = default_transports(reality=True)
    for kind, values in transport_changes.items():
        t[kind].update(values)
    return Node(id=1, name="Main", address="nordpine.site", transports=t)


class FakeRunner(Runner):
    """Records scripts instead of running them; answers stats queries from a queue."""

    def __init__(self):
        super().__init__(Path(tempfile.mkdtemp()))
        self.scripts: list[str] = []
        self.stats: list[str] = []
        self.fail_next = False

    def run(self, node, script, timeout=180):
        self.scripts.append(script)
        if "statsquery" in script:
            out = self.stats.pop(0) if self.stats else '{"stat": []}'
            return Result(True, out, out)
        if self.fail_next:
            self.fail_next = False
            return Result(False, "xray rejected the new config, nothing changed")
        return Result(True, "xray: config applied, restarted")

    def deploys(self):
        return [s for s in self.scripts if "statsquery" not in s]


def make_store(password="correct horse") -> Store:
    store = Store(Path(tempfile.mkdtemp()) / "panel.db")
    store.update_settings({**SETTINGS, "admin_hash": auth.hash_password(password)})
    store.add_node(local_node())
    return store
