"""Command line: set up the panel, run it, and fix things when the UI is out of reach."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__, auth, keys
from .control import Controller
from .deploy import Runner, ensure_ssh_key
from .model import Node, ValidationError, check_node, clean_email, clean_hostname, default_transports
from .store import Store
from .web import App, panel_url

DEFAULT_DATA = os.environ.get("XCPANEL_DATA", "/var/lib/xcpanel")


class CliError(Exception):
    """Printed to the user without a traceback."""


def _open(args: argparse.Namespace) -> tuple[Store, Path]:
    data = Path(args.data)
    db = data / "panel.db"
    if not db.exists():
        raise CliError(f"no panel database in {data}; run `xcpanel init --domain ...` first")
    return Store(db), data


def cmd_init(args: argparse.Namespace) -> int:
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    db = data / "panel.db"
    if db.exists() and not args.force:
        raise CliError(f"{db} already exists; the panel is set up (use --force to start over)")
    if db.exists():
        db.unlink()

    domain = clean_hostname(args.domain)
    host, _, port = args.listen.rpartition(":")
    if not host or not port.isdigit():
        raise CliError("--listen must be host:port, e.g. 127.0.0.1:8740")

    store = Store(db)
    password = args.password or auth.random_password()
    panel_path = keys.random_word(12)
    sub_path = keys.random_word(12)
    store.update_settings(
        {
            "domain": domain,
            "panel_path": panel_path,
            "sub_path": sub_path,
            "panel_listen": args.listen,
            "admin_user": args.user,
            "admin_hash": auth.hash_password(password),
            "sub_title": domain,
            "acme_email": clean_email(args.email),
        }
    )
    node = Node(id=0, name="Main", address=domain, transports=default_transports(reality=not args.no_reality))
    check_node(node, store.settings())
    store.add_node(node)

    try:
        ensure_ssh_key(data)
    except (OSError, Exception) as exc:
        print(f"note: no SSH key for remote nodes yet ({exc}); it is created when you add one", file=sys.stderr)

    print(f"Panel:     {panel_url(store.settings())}")
    print(f"Username:  {args.user}")
    print(f"Password:  {password}")
    print("Keep these. `xcpanel info` shows the URL again, `xcpanel passwd` resets the password.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store, data = _open(args)
    settings = store.settings()
    host, _, port = (args.listen or settings["panel_listen"]).rpartition(":")
    controller = Controller(store, Runner(data))
    app = App(store, controller, data)
    httpd = app.server(host, int(port))

    def stop(*_: object) -> None:
        controller.stop()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    if not args.no_worker:
        controller.start()
    logging.info("xcpanel %s listening on %s:%s, panel at %s", __version__, host, port, panel_url(settings))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    store, _ = _open(args)
    password = args.password or auth.random_password()
    if len(password) < 8:
        raise CliError("the password needs at least 8 characters")
    values = {"admin_hash": auth.hash_password(password)}
    if args.user:
        values["admin_user"] = args.user
    store.update_settings(values)
    store.delete_all_sessions()
    settings = store.settings()
    print(f"Username:  {settings['admin_user']}")
    print(f"Password:  {password}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    store, data = _open(args)
    settings = store.settings()
    print(f"Panel:         {panel_url(settings)}")
    print(f"Username:      {settings['admin_user']}")
    print(f"Subscriptions: https://{settings['domain']}/{settings['sub_path']}/<client token>")
    for node in store.nodes():
        where = "this server" if node.local else f"ssh {node.ssh_host}:{node.ssh_port}"
        state = {None: "never deployed", True: "deployed", False: "LAST DEPLOY FAILED"}[node.deploy_ok]
        print(f"Node #{node.id} {node.name} ({node.address}, {where}): {', '.join(node.active_transports()) or 'no transports'}; {state}")
    pub = data / "id_ed25519.pub"
    if pub.exists():
        print(f"Panel SSH key: {pub.read_text().strip()}")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    store, data = _open(args)
    controller = Controller(store, Runner(data))
    nodes = [n for n in store.nodes() if args.node in (None, n.id)]
    if not nodes:
        raise CliError("no such node")
    failed = 0
    for node in nodes:
        result = controller.deploy(node.id, force=args.force)
        print(f"== {node.name}: {'ok' if result.ok else 'FAILED'}")
        print(result.output)
        failed += not result.ok
    return 1 if failed else 0


def cmd_render(args: argparse.Namespace) -> int:
    store, data = _open(args)
    node = store.node(args.node)
    if node is None:
        raise CliError("no such node")
    xray_text, caddy_text = Controller(store, Runner(data)).render(node)
    if args.caddy:
        if caddy_text is None:
            raise CliError("this node does not run Caddy")
        sys.stdout.write(caddy_text)
    else:
        sys.stdout.write(xray_text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xcpanel", description="Xray + Caddy proxy panel")
    parser.add_argument("--version", action="version", version=f"xcpanel {__version__}")
    parser.add_argument("--data", default=DEFAULT_DATA, help=f"data directory (default {DEFAULT_DATA})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the database, admin login and the first node")
    p.add_argument("--domain", required=True, help="the panel's domain, e.g. nordpine.site")
    p.add_argument("--email", default="", help="email for Let's Encrypt notices (optional)")
    p.add_argument("--listen", default="127.0.0.1:8740", help="where the panel listens (Caddy proxies to it)")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", help="admin password (random if omitted)")
    p.add_argument("--no-reality", action="store_true", help="start with XHTTP only")
    p.add_argument("--force", action="store_true", help="wipe an existing database")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="run the web panel and the background worker")
    p.add_argument("--listen", help="override the stored host:port")
    p.add_argument("--no-worker", action="store_true", help="do not deploy or collect stats (for UI work)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("passwd", help="reset the admin password")
    p.add_argument("--password", help="new password (random if omitted)")
    p.add_argument("--user", help="also change the username")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("info", help="print the panel URL and node status")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("deploy", help="push configs to nodes now")
    p.add_argument("--node", type=int, help="node id (default: all)")
    p.add_argument("--force", action="store_true", help="push even if nothing changed")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("render", help="print a node's generated config")
    p.add_argument("--node", type=int, required=True)
    p.add_argument("--caddy", action="store_true", help="print the Caddyfile instead of the Xray config")
    p.set_defaults(func=cmd_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
