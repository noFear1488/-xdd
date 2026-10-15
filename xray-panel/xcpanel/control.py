"""Keeps nodes in line with the database.

For every node the panel renders the Xray config and Caddyfile it *should*
run, compares their hashes with the last ones applied, and deploys when they
differ. That one rule covers everything: a new client, a disabled one, an
expired one, a client over its traffic limit, a changed path or domain.

A background thread also pulls per-client traffic from each node's Xray API
(resetting the counters as it reads them) and adds it to the totals.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import caddy, xray
from .deploy import CHECK_SCRIPT, STATS_SCRIPT, Result, Runner, deploy_script, parse_stats
from .model import Node
from .store import Store

log = logging.getLogger("xcpanel")

SITE_HTML = (Path(__file__).parent / "static" / "site.html").read_text(encoding="utf-8")
RETRY_AFTER = 120  # seconds before an unchanged config that failed is tried again


class Controller:
    def __init__(self, store: Store, runner: Runner):
        self.store = store
        self.runner = runner
        self._lock = threading.Lock()  # one deploy or stats run at a time
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._failed: dict[int, tuple[str, float]] = {}
        self._thread: threading.Thread | None = None

    # --- rendering -----------------------------------------------------------

    def render(self, node: Node, now: float | None = None) -> tuple[str, str | None]:
        settings = self.store.settings()
        clients = self.store.clients()
        xray_text = xray.dump(xray.build_config(node, clients, settings, now or time.time()))
        caddy_text = caddy.build_caddyfile(node, settings) if node.caddy else None
        return xray_text, caddy_text

    def _hashes(self, node: Node, now: float | None = None) -> tuple[str, str, str | None, str | None]:
        xray_text, caddy_text = self.render(node, now)
        return xray.digest(xray_text), xray.digest(caddy_text) if caddy_text else "", xray_text, caddy_text

    def pending(self, node: Node, now: float | None = None) -> bool:
        xh, ch, _, _ = self._hashes(node, now)
        return xh != node.xray_hash or ch != node.caddy_hash

    # --- actions -------------------------------------------------------------

    def deploy(self, node_id: int, force: bool = False) -> Result:
        with self._lock:
            node = self.store.node(node_id)
            if node is None:
                return Result(False, "no such node")
            xh, ch, xray_text, caddy_text = self._hashes(node)
            send_xray = force or xh != node.xray_hash
            send_caddy = caddy_text is not None and (force or ch != node.caddy_hash)
            stop_caddy = caddy_text is None and bool(node.caddy_hash)
            if not (send_xray or send_caddy or stop_caddy):
                return Result(True, "already up to date")
            if send_xray:
                self._collect(node)  # a restart wipes Xray's counters, read them first

            script = deploy_script(
                xray_text if send_xray else None,
                caddy_text if send_caddy else None,
                SITE_HTML.replace("{{domain}}", node.address),
                caddy_first=node.reality_front,
                stop_caddy=stop_caddy,
            )
            result = self.runner.run(node, script)
            self.store.record_deploy(
                node.id,
                result.ok,
                result.output,
                xh if send_xray else None,
                ch if (send_caddy or stop_caddy) else None,
            )
            if result.ok:
                self._failed.pop(node.id, None)
                log.info("deployed %s: %s", node.name, result.output.splitlines()[-1:] or "")
            else:
                self._failed[node.id] = (xh + ch, time.time())
                log.warning("deploy to %s failed: %s", node.name, result.output[-500:])
            return result

    def check(self, node_id: int) -> Result:
        node = self.store.node(node_id)
        if node is None:
            return Result(False, "no such node")
        return self.runner.run(node, CHECK_SCRIPT, timeout=30)

    def _collect(self, node: Node) -> None:
        if not node.xray_hash or not node.active_transports():
            return  # never deployed: there is no Xray of ours to ask
        result = self.runner.run(node, STATS_SCRIPT, timeout=30)
        if result.ok:
            self.store.add_traffic(parse_stats(result.stdout))

    def collect_all(self) -> None:
        with self._lock:
            for node in self.store.nodes():
                try:
                    self._collect(node)
                except Exception:  # one broken node must not stop the rest
                    log.exception("stats from %s failed", node.name)

    def reconcile(self) -> None:
        now = time.time()
        for node in self.store.nodes():
            xh, ch, _, _ = self._hashes(node, now)
            if xh == node.xray_hash and ch == node.caddy_hash:
                continue
            failed = self._failed.get(node.id)
            if failed and failed[0] == xh + ch and now - failed[1] < RETRY_AFTER:
                continue
            self.deploy(node.id)

    # --- background loop -----------------------------------------------------

    def kick(self) -> None:
        """Something changed: reconcile soon instead of at the next tick."""
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="xcpanel-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        next_stats = 0.0
        while not self._stop.is_set():
            if self._wake.wait(5):
                self._wake.clear()
                time.sleep(1)  # let a burst of edits land in one deploy
            if self._stop.is_set():
                break
            try:
                if time.time() >= next_stats:
                    self.collect_all()
                    interval = int(self.store.settings().get("stats_interval") or 60)
                    next_stats = time.time() + max(interval, 10)
                self.reconcile()
            except Exception:
                log.exception("background run failed")
