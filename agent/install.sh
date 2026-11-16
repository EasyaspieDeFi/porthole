#!/bin/bash
# Installs the Porthole agent (keyboard/mouse from the PC, network video) as a systemd service.
# Works on the Arduino UNO Q, Raspberry Pi OS (Pi 3 and newer), or any Debian-based board.
#
# Usage (on the board):
#   sudo bash install.sh --token-file FILE [port] [options]
#   sudo bash install.sh <token> [port] [options]
# Options:
#   --wifi-steer          keep the board on the mesh WiFi node with the fastest link
#   --no-wifi-steer       turn that off again
#   --wifi-no-powersave   stop WiFi power saving (it delays incoming input by up to 0.5 s)
# Options not given keep the board's current setting. Set NO_VIDEO=1 to skip installing ffmpeg.
set -euo pipefail

USAGE="usage: sudo bash install.sh --token-file FILE [port] [--wifi-steer|--no-wifi-steer] [--wifi-no-powersave]"
TOKEN="" PORT="" STEER="" NOPOWERSAVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --token-file)
      TOKEN="$(cat "${2:?$USAGE}")"
      rm -f "$2"                  # the token lives only in /etc/porthole-agent.env from here on
      shift 2 ;;
    --wifi-steer) STEER=1; shift ;;
    --no-wifi-steer) STEER=0; shift ;;
    --wifi-no-powersave) NOPOWERSAVE=1; shift ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
    *)
      if [ -z "$TOKEN" ]; then TOKEN="$1"
      elif [ -z "$PORT" ]; then PORT="$1"
      else echo "$USAGE" >&2; exit 2
      fi
      shift ;;
  esac
done
[ -n "$TOKEN" ] || { echo "$USAGE" >&2; exit 2; }
PORT="${PORT:-7790}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo" >&2
  exit 1
fi

# Keep WiFi steering as it was unless asked to change it.
if [ -z "$STEER" ]; then
  STEER=$(grep -s '^PORTHOLE_WIFI_STEER=' /etc/porthole-agent.env | cut -d= -f2 || true)
  STEER="${STEER:-0}"
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

if [ "$NOPOWERSAVE" = "1" ]; then
  if command -v nmcli >/dev/null; then
    echo "== turning off WiFi power saving"
    printf '# Added by Porthole: WiFi power saving delayed incoming packets (remote input) by up to 0.5 s.\n[connection]\nwifi.powersave = 2\n' \
      > /etc/NetworkManager/conf.d/90-wifi-powersave-off.conf
    CONN=$(nmcli -t -f NAME,TYPE connection show --active | grep ':802-11-wireless$' | cut -d: -f1 || true)
    if [ -n "$CONN" ]; then
      nmcli connection modify "$CONN" 802-11-wireless.powersave 2
      echo "   applies to '$CONN' after its next reconnect (or reboot)"
    fi
  else
    echo "!! --wifi-no-powersave needs NetworkManager; skipped"
  fi
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
PORTHOLE_WIFI_STEER=$STEER
EOF
umask 022

cat > /etc/systemd/system/porthole-agent.service <<'EOF'
[Unit]
Description=Porthole agent (keyboard/mouse and video for the PC)
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
echo "Porthole agent installed on port $PORT (WiFi steering: $([ "$STEER" = 1 ] && echo on || echo off))."
