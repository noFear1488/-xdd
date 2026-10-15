# xcpanel: an Xray + Caddy panel for your own domain

A small self-hosted web panel that runs your Xray proxy nodes behind your domain
(written for `nordpine.site`, works with any domain). From the browser you add
people, set traffic limits and expiry dates, and give each person one
subscription link. That link works in Hiddify, v2RayTun, Happ, Streisand,
v2rayNG and NekoBox, and covers every node you run.

* **No dependencies.** Python 3.10+ standard library only. The UI is plain
  HTML/JS with no build step and no external CDNs (those can be blocked).
* **Hides behind a normal website.** Anyone who opens `https://nordpine.site`
  sees an ordinary little site. The panel, the subscriptions and the proxy
  transports each live on their own random secret path.
* **Several servers.** The panel's own server is one node. More servers are
  managed over SSH with a key the panel creates.
* **Safe deploys.** New configs are checked with `xray run -test` and
  `caddy validate` before they replace the running ones. A bad change leaves
  the node on its previous config, and the panel shows the error.

## How traffic flows

The default layout puts **Reality on port 443 in front of Caddy**:

```
                         ┌────────────── your server ──────────────────────────┐
 client (Reality) ──443──► Xray  ── authenticated Reality client → internet     │
 browser / ACME /        │   │                                                   │
 XHTTP client     ──443──►   └─ everything else, with PROXY header ─►            │
                         │      Caddy 127.0.0.1:9443 (real cert for the domain)  │
                         │        /<panel>/   → panel (127.0.0.1:8740)           │
                         │        /<sub>/     → subscriptions                    │
                         │        /<xhttp>/   → Xray XHTTP inbound → internet    │
                         │        anything else → the cover website             │
                         │      Caddy :80 → HTTP→HTTPS redirect + certificates   │
                         └───────────────────────────────────────────────────────┘
```

Reality presents your domain's own real certificate, borrowed from Caddy
(a setup sometimes called "steal oneself"). That looks far more natural than
another company's certificate on a VPS IP, and both transports share
port 443. Xray itself warns that Reality on ports other than 443 gets IPs
blocked.

If you set the Reality port to something else, Caddy goes back to owning 443,
and Reality borrows the handshake of the target site you choose (by default
`www.microsoft.com`). A node without Caddy can run Reality only, with just
an IP address.

| Transport | Through | Notes |
|---|---|---|
| **XHTTP** | Caddy (TLS, your domain) | Recommended, on by default |
| **VLESS Reality + Vision** | Xray directly | On by default, port 443 |
| WebSocket | Caddy | Legacy: Xray marks it deprecated |
| gRPC | Caddy | Legacy: Xray marks it deprecated |

All four were tested end to end against real Xray 26.3 and Caddy 2.11 builds
in both layouts.

## Install

You need a Debian or Ubuntu VPS with root access, and a domain whose DNS
`A` record (plus `AAAA` for IPv6) points at that VPS.

```bash
git clone <this repo> && cd <repo>/xray-panel
sudo ./install.sh --domain nordpine.site --email you@example.com
```

The installer sets up Caddy (from Caddy's apt repo) and Xray (from the
official `XTLS/Xray-install` script). It copies the panel to `/opt/xcpanel`,
keeps its data in `/var/lib/xcpanel`, creates a systemd service and opens
80/443 if `ufw` is on. Then it deploys the configs and prints something like:

```
Panel:     https://nordpine.site/k3vq8xw2mfz7/
Username:  admin
Password:  •••••••••••••••• (also in /var/lib/xcpanel/.first-login)
```

Caddy gets the Let's Encrypt certificate on its own; give it a minute after DNS
is in place. To upgrade, `git pull` and run `sudo ./install.sh` again. Your
settings and clients are kept.

`--no-reality` starts with XHTTP only. You can switch transports on and off
later in the UI.

## Using it

**Clients tab.** Add a person and optionally give them a traffic limit (GB)
and an expiry date. **Share** shows their subscription link and QR code, plus
one-off links per node. Opening the subscription link in a browser shows a
page with usage, a QR code and "open in app" buttons, so you can just send
someone the link. Apps also show remaining traffic and days, via the
`Subscription-Userinfo` header.

The panel reads traffic from each node every 60 seconds. When someone runs
out of traffic or their expiry date passes, they are removed from every node
automatically. Raise their limit, extend their date or reset their traffic
and they come back.

**Nodes tab.** Each node shows whether its config is up to date, what's
pending, and the last deploy log. Changes are applied a few seconds after
you save. **Deploy now** forces a push. **Check** shows whether xray/caddy
are installed and running. **View config** shows exactly what will be
written.

### Adding another server

1. Point a subdomain at it, e.g. `de.nordpine.site`. Not needed for a
   Reality-only node, which can use an IP address.
2. Nodes → **Add node** → **Setup script for the server**. Paste that block
   into the new server's root shell. It installs Xray (and Caddy) and
   authorizes the panel's SSH key.
3. Fill in the name (shown in apps, so flags work: `🇩🇪 Germany`), the
   address, and the SSH target (`root@203.0.113.7`). Save, then **Deploy now**.

Every client's subscription now includes the new node.

## Recovery from the command line

The installer adds an `xcpanel` command:

```bash
sudo xcpanel info               # panel URL, nodes and their deploy state
sudo xcpanel passwd             # new random admin password (signs everyone out)
sudo xcpanel deploy --force     # push configs to every node now
sudo xcpanel render --node 1    # print the Xray config (add --caddy for the Caddyfile)
journalctl -u xcpanel -f        # panel log; also: journalctl -u xray / -u caddy
```

## Security notes

* The panel listens on `127.0.0.1` only and is reachable only through Caddy
  at its secret path. A wrong path gets a plain 404.
* Logins are rate-limited: 5 wrong passwords lock that IP out for 15
  minutes. Passwords are hashed with scrypt. Sessions use HttpOnly,
  SameSite=Strict cookies, and every API call needs a custom header (CSRF
  protection).
* Proxy users cannot reach the server's own private addresses
  (`127.0.0.0/8`, `10/8`, `192.168/16`, …), so they can't get at the panel or
  the Xray API through the proxy. BitTorrent is blocked by default because
  hosting providers send abuse letters for it (Settings to change).
* The panel runs as root because it rewrites `/usr/local/etc/xray/config.json`
  and `/etc/caddy/Caddyfile` and restarts services. It reaches other nodes as
  root over SSH, with its own ed25519 key (`/var/lib/xcpanel/id_ed25519`).
* Nothing is logged per request; Xray's access log is off.

## Layout of the code

```
install.sh            installer / upgrader for the panel's server
xcpanel/
  model.py            clients, nodes, validation of all admin input
  xray.py             Xray server config, vless:// links, subscription body
  caddy.py            Caddyfile (both layouts)
  deploy.py           bash scripts run locally or over SSH; stats parsing
  control.py          "desired vs applied" reconcile loop, traffic collection
  web.py              admin API, UI, subscription endpoint + its HTML page
  store.py            SQLite storage
  keys.py             X25519 (Reality) keys in pure Python, random ids
  node-setup.sh       what the "setup script" button gives you
  static/             UI (app.js, app.css), cover site, vendored qrcode.js (MIT)
tests/                python3 -m unittest discover -s tests -t .
```
