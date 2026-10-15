"""Running things on nodes: deploy configs, collect traffic stats, check health.

Everything is a bash script fed to `bash -s`, either locally (the panel's own
server) or over SSH. Payloads travel base64-encoded inside quoted heredocs, so
no admin-supplied text is ever interpreted by the shell. A config is tested
with `xray run -test` / `caddy validate` before it replaces the live one, so a
bad deploy leaves the node running its previous config.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .model import API_PORT, Node, client_id_from_email

XRAY_CONFIG = "/usr/local/etc/xray/config.json"
CADDYFILE = "/etc/caddy/Caddyfile"
SITE_ROOT = "/var/www/xcpanel"

_PRELUDE = """set -euo pipefail
export PATH="/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH"
"""


@dataclass
class Result:
    ok: bool
    output: str
    stdout: str = ""


def _heredoc(target: str, payload: str) -> str:
    data = base64.encodebytes(payload.encode("utf-8")).decode("ascii")
    return f"base64 -d > {target} <<'XCPANEL_EOF'\n{data}XCPANEL_EOF\n"


def deploy_script(
    xray_config: str | None,
    caddyfile: str | None,
    site_html: str,
    caddy_first: bool,
    stop_caddy: bool = False,
) -> str:
    """Tests everything first, then swaps configs in and restarts what changed.

    caddy_first matters when port 443 changes hands (Reality in front of Caddy
    or not): whoever gives the port up has to go first. stop_caddy is for a
    node whose Caddy the panel no longer manages, so it stops holding 80/443.
    """
    parts = [_PRELUDE, 'work="$(mktemp -d)"\ntrap \'rm -rf "$work"\' EXIT\n']
    if xray_config is not None:
        parts.append(_heredoc('"$work/config.json"', xray_config))
        parts.append(
            'if ! xray run -test -c "$work/config.json" > "$work/xray.log" 2>&1; then\n'
            '  tail -n 20 "$work/xray.log"; echo "xray rejected the new config, nothing changed"; exit 1\n'
            "fi\n"
        )
    if caddyfile is not None:
        parts.append(_heredoc('"$work/Caddyfile"', caddyfile))
        parts.append(_heredoc('"$work/index.html"', site_html))
        parts.append(
            'if ! caddy validate --adapter caddyfile --config "$work/Caddyfile" > "$work/caddy.log" 2>&1; then\n'
            '  tail -n 20 "$work/caddy.log"; echo "caddy rejected the new config, nothing changed"; exit 1\n'
            "fi\n"
        )

    apply_xray = (
        f'install -d {Path(XRAY_CONFIG).parent}\n'
        f'install -m 644 "$work/config.json" {XRAY_CONFIG}\n'
        "systemctl restart xray\n"
        "for _ in 1 2 3 4 5; do sleep 1; systemctl is-active --quiet xray && break; done\n"
        "if ! systemctl is-active --quiet xray; then\n"
        '  journalctl -u xray -n 30 --no-pager || true; echo "xray failed to start"; exit 1\n'
        "fi\n"
        'echo "xray: config applied, restarted"\n'
    )
    apply_caddy = (
        f"install -d {Path(CADDYFILE).parent} {SITE_ROOT}\n"
        f'[ -e {SITE_ROOT}/index.html ] || install -m 644 "$work/index.html" {SITE_ROOT}/index.html\n'
        f'install -m 644 "$work/Caddyfile" {CADDYFILE}\n'
        "if systemctl is-active --quiet caddy; then systemctl reload caddy; else systemctl restart caddy; fi\n"
        'echo "caddy: config applied, reloaded"\n'
    )
    steps = []
    if stop_caddy:
        steps.append('systemctl disable --now caddy > /dev/null 2>&1 || true\necho "caddy: stopped"\n')
    if xray_config is not None:
        steps.append(apply_xray)
    if caddyfile is not None:
        steps.insert(0 if caddy_first else len(steps), apply_caddy)
    return "".join(parts + steps)


STATS_SCRIPT = _PRELUDE + f"xray api statsquery -s 127.0.0.1:{API_PORT} -reset\n"

CHECK_SCRIPT = _PRELUDE + """echo "host: $(hostname)"
if command -v xray > /dev/null; then xray version | head -n 1; else echo "xray: NOT INSTALLED"; fi
if command -v caddy > /dev/null; then echo "caddy $(caddy version | cut -d' ' -f1)"; else echo "caddy: not installed"; fi
for unit in xray caddy; do echo "$unit service: $(systemctl is-active "$unit" 2>/dev/null || true)"; done
"""


def parse_stats(text: str) -> dict[int, tuple[int, int]]:
    """`xray api statsquery` output -> {client id: (uplink, downlink)} bytes."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    usage: dict[int, list[int]] = {}
    for item in data.get("stat") or []:
        parts = str(item.get("name", "")).split(">>>")
        if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
            continue
        client_id = client_id_from_email(parts[1])
        if client_id is None:
            continue
        try:
            value = int(item.get("value") or 0)  # int64 arrives as a JSON string
        except (TypeError, ValueError):
            continue
        slot = usage.setdefault(client_id, [0, 0])
        slot[0 if parts[3] == "uplink" else 1] += value
    return {k: (v[0], v[1]) for k, v in usage.items()}


class Runner:
    """Runs a script on a node: `bash -s` here, or `ssh node bash -s` elsewhere."""

    def __init__(self, data_dir: Path):
        self.key = data_dir / "id_ed25519"
        self.known_hosts = data_dir / "known_hosts"

    def argv(self, node: Node) -> list[str]:
        if node.local:
            return ["bash", "-s"]
        target = node.ssh_host if "@" in node.ssh_host else f"root@{node.ssh_host}"
        return [
            "ssh",
            "-i", str(self.key),
            "-p", str(node.ssh_port),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            target,
            "bash -s",
        ]  # fmt: skip

    def run(self, node: Node, script: str, timeout: float = 180) -> Result:
        argv = self.argv(node)
        try:
            proc = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return Result(False, f"{argv[0]} is not installed on the panel server")
        except subprocess.TimeoutExpired:
            return Result(False, f"no answer from {node.name} within {int(timeout)}s")
        output = "\n".join(s for s in (proc.stdout.strip(), proc.stderr.strip()) if s)
        if proc.returncode == 255 and not node.local:
            output += "\n(ssh could not connect: check the SSH target and that the panel key is authorized)"
        return Result(proc.returncode == 0, output, proc.stdout)


def ensure_ssh_key(data_dir: Path) -> str:
    """Creates the panel's SSH key on first use and returns the public half."""
    key = data_dir / "id_ed25519"
    if not key.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "xcpanel", "-f", str(key)],
            check=True,
            capture_output=True,
        )
    return (data_dir / "id_ed25519.pub").read_text().strip()


def node_setup_script(public_key: str, with_caddy: bool) -> str:
    """A paste-into-the-terminal block that prepares a fresh server as a node."""
    body = (Path(__file__).parent / "node-setup.sh").read_text()
    head = f"PANEL_KEY='{public_key}'\nWITH_CADDY={1 if with_caddy else 0}\n"
    return f"bash <<'XCPANEL_SETUP'\n{head}{body}XCPANEL_SETUP\n"
