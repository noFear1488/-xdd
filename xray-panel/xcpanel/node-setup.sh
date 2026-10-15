# Prepares a server to be managed by xcpanel: installs Xray (and Caddy if
# asked), then authorizes the panel's SSH key for root. Safe to run again.
# The panel prepends PANEL_KEY and WITH_CADDY when it shows this script.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
	echo "Run this as root (sudo -i first)." >&2
	exit 1
fi
if ! command -v apt-get > /dev/null; then
	echo "This script knows Debian/Ubuntu only. Install xray (and caddy) by hand," >&2
	echo "then add this line to /root/.ssh/authorized_keys:" >&2
	echo "$PANEL_KEY" >&2
	exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q curl ca-certificates unzip

if ! command -v xray > /dev/null; then
	bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi
systemctl enable xray > /dev/null 2>&1 || true

if [ "$WITH_CADDY" = 1 ] && ! command -v caddy > /dev/null; then
	apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https gnupg
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key |
		gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		> /etc/apt/sources.list.d/caddy-stable.list
	chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
	apt-get update -q
	apt-get install -y -q caddy
fi

install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
grep -qxF "$PANEL_KEY" /root/.ssh/authorized_keys || echo "$PANEL_KEY" >> /root/.ssh/authorized_keys

if command -v ufw > /dev/null && ufw status | grep -q "Status: active"; then
	ufw allow 443/tcp > /dev/null
	if [ "$WITH_CADDY" = 1 ]; then ufw allow 80/tcp > /dev/null; fi
	echo "ufw: opened 443/tcp (and 80/tcp for Caddy). Open the Reality port too if it is not 443."
fi

echo
echo "This server is ready. Add it in the panel (Nodes -> Add node), then press Deploy."
