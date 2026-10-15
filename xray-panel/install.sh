#!/usr/bin/env bash
# Installs (or upgrades) xcpanel with Caddy and Xray on a Debian/Ubuntu server.
#
#   sudo ./install.sh --domain nordpine.site [--email you@example.com] [--no-reality]
#
# Run it again after `git pull` to upgrade: the database and settings are kept.
set -euo pipefail

DOMAIN=""
EMAIL=""
INIT_ARGS=()
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/xcpanel
DATA=/var/lib/xcpanel

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
	case "$1" in
		--domain) DOMAIN="${2:-}"; shift 2 ;;
		--email) EMAIL="${2:-}"; shift 2 ;;
		--no-reality) INIT_ARGS+=(--no-reality); shift ;;
		-h | --help) sed -n '2,7p' "$0"; exit 0 ;;
		*) die "unknown option $1" ;;
	esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./install.sh ...)"
command -v apt-get > /dev/null || die "this installer supports Debian and Ubuntu"
[ -f "$SRC/xcpanel/__init__.py" ] || die "run it from the xray-panel directory of the repository"
if [ ! -f "$DATA/panel.db" ] && [ -z "$DOMAIN" ]; then
	die "first install needs --domain, e.g. --domain nordpine.site"
fi

export DEBIAN_FRONTEND=noninteractive
echo "==> packages"
apt-get update -q
apt-get install -y -q python3 curl ca-certificates unzip openssh-client \
	debian-keyring debian-archive-keyring apt-transport-https gnupg

if ! command -v caddy > /dev/null; then
	echo "==> Caddy (official repository)"
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key |
		gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		> /etc/apt/sources.list.d/caddy-stable.list
	chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
	apt-get update -q
	apt-get install -y -q caddy
fi

if ! command -v xray > /dev/null; then
	echo "==> Xray (official install script)"
	bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi
systemctl enable xray caddy > /dev/null 2>&1 || true

echo "==> panel files -> $PREFIX"
rm -rf "$PREFIX/xcpanel.new"
install -d "$PREFIX"
cp -r "$SRC/xcpanel" "$PREFIX/xcpanel.new"
rm -rf "$PREFIX/xcpanel.new/__pycache__"
rm -rf "$PREFIX/xcpanel" && mv "$PREFIX/xcpanel.new" "$PREFIX/xcpanel"

printf '#!/bin/sh\nPYTHONPATH=%s exec python3 -m xcpanel --data %s "$@"\n' "$PREFIX" "$DATA" > /usr/local/bin/xcpanel
chmod 755 /usr/local/bin/xcpanel
xcpanel() { /usr/local/bin/xcpanel "$@"; }

FIRST=0
if [ ! -f "$DATA/panel.db" ]; then
	FIRST=1
	echo "==> first-time setup for $DOMAIN"
	xcpanel init --domain "$DOMAIN" --email "$EMAIL" "${INIT_ARGS[@]}" | tee "$DATA/.first-login"
	chmod 600 "$DATA/.first-login"
fi

if command -v ufw > /dev/null && ufw status | grep -q "Status: active"; then
	echo "==> ufw: opening 80/tcp and 443/tcp"
	ufw allow 80/tcp > /dev/null
	ufw allow 443/tcp > /dev/null
fi

# Deploy before the service starts, so two processes never deploy at once.
systemctl stop xcpanel 2> /dev/null || true
echo "==> deploying Caddy and Xray configs"
if ! xcpanel deploy; then
	echo "The deploy failed (output above). The panel will retry; fix the cause or see the Nodes tab." >&2
fi

cat > /etc/systemd/system/xcpanel.service <<EOF
[Unit]
Description=xcpanel (Xray + Caddy panel)
After=network-online.target
Wants=network-online.target

[Service]
Environment=PYTHONPATH=$PREFIX
ExecStart=/usr/bin/python3 -m xcpanel --data $DATA serve
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable xcpanel > /dev/null
systemctl restart xcpanel

echo
xcpanel info
if [ "$FIRST" = 1 ]; then
	echo
	echo "The admin password was printed above and saved in $DATA/.first-login; delete that file once noted."
fi
cat <<EOF

DNS: point an A record (and AAAA if you have IPv6) for the domain to this server.
Caddy fetches the certificate by itself on the first visit; that can take a minute.
EOF
