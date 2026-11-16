#!/bin/bash
# Installs the Porthole input agent (keyboard/mouse from the PC) as a systemd service.
# Works on the Arduino UNO Q, Raspberry Pi OS (Pi 3 and newer), or any Debian-based board.
# Usage (on the board):  sudo bash install.sh --token-file FILE [port]
#                        sudo bash install.sh <token> [port]
set -euo pipefail

USAGE="usage: sudo bash install.sh --token-file FILE [port]   (or: install.sh <token> [port])"
if [ "${1:-}" = "--token-file" ]; then
  TOKEN="$(cat "${2:?$USAGE}")"
  rm -f "$2"                      # the token lives only in /etc/porthole-agent.env from here on
  PORT="${3:-7790}"
else
  TOKEN="${1:?$USAGE}"
  PORT="${2:-7790}"
fi
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo" >&2
  exit 1
fi

if ! python3 -c "import evdev" 2>/dev/null; then
  echo "== installing python3-evdev"
  apt-get update -qq
  apt-get install -y -qq python3-evdev
fi

# ffmpeg captures the screen/camera for video over the network. Skip with NO_VIDEO=1.
if [ "${NO_VIDEO:-0}" != "1" ] && ! command -v ffmpeg >/dev/null; then
  echo "== installing ffmpeg (for video over the network; NO_VIDEO=1 skips this)"
  apt-get update -qq
  apt-get install -y -qq ffmpeg || echo "!! ffmpeg install failed: network video won't work until it's installed"
fi

echo "== loading uinput"
modprobe uinput
echo uinput > /etc/modules-load.d/uinput.conf

# Pre-rename versions installed as "unoq-agent"; replace them so the port is free.
if [ -f /etc/systemd/system/unoq-agent.service ]; then
  echo "== removing old unoq-agent service"
  systemctl disable --now unoq-agent.service || true
  rm -f /etc/systemd/system/unoq-agent.service /etc/unoq-agent.env
  rm -rf /opt/unoq-agent
fi

echo "== installing agent"
install -d /opt/porthole-agent
install -m 0755 "$HERE/porthole_agent.py" /opt/porthole-agent/porthole_agent.py
umask 077
cat > /etc/porthole-agent.env <<EOF
PORTHOLE_TOKEN=$TOKEN
PORTHOLE_PORT=$PORT
PORTHOLE_BIND=0.0.0.0
EOF
umask 022

cat > /etc/systemd/system/porthole-agent.service <<'EOF'
[Unit]
Description=Porthole input agent (keyboard/mouse from PC)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/porthole-agent.env
ExecStart=/usr/bin/python3 /opt/porthole-agent/porthole_agent.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now porthole-agent.service
systemctl restart porthole-agent.service
sleep 1
systemctl --no-pager --lines=5 status porthole-agent.service || true
echo
echo "Porthole agent installed on port $PORT."
