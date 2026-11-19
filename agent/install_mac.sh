#!/bin/bash
# Installs the Porthole agent on a Mac (keyboard/mouse from the PC) as a LaunchAgent, so it
# runs inside the desktop session of the user who is logged in at the Mac's screen.
#
# Usage (on the Mac, as that user, without sudo):
#   bash install_mac.sh --token-file FILE [port] [--ctrl-as-cmd|--no-ctrl-as-cmd]
# Options:
#   --ctrl-as-cmd      PC Ctrl acts as Command (Ctrl+C / Ctrl+V copy and paste), Win key as Control
#   --no-ctrl-as-cmd   back to the default: Ctrl is Control, Win key is Command, Alt is Option
# Options not given keep the Mac's current setting.
set -euo pipefail

USAGE="usage: bash install_mac.sh --token-file FILE [port] [--ctrl-as-cmd|--no-ctrl-as-cmd]"
TOKEN="" PORT="" SWAP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token-file)
      TOKEN="$(cat "${2:?$USAGE}")"
      rm -f "$2"                  # the token lives only in the LaunchAgent plist from here on
      shift 2 ;;
    --ctrl-as-cmd) SWAP=1; shift ;;
    --no-ctrl-as-cmd) SWAP=0; shift ;;
    --wifi-steer|--no-wifi-steer|--wifi-no-powersave)
      echo "!! $1 is for Linux boards; ignored on a Mac"; shift ;;
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
LABEL=com.porthole.agent
DIR="$HOME/.porthole-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/porthole-agent.log"
ME="$(id -un)"
MYUID="$(id -u)"

if [ "$MYUID" -eq 0 ]; then
  echo "run this as your normal Mac user, without sudo: the agent has to run in your desktop session" >&2
  exit 1
fi

# Keep the Ctrl/Command choice as it was unless asked to change it.
if [ -z "$SWAP" ] && [ -f "$PLIST" ]; then
  # (PlistBuddy prints its errors on stdout, so only ask an existing file, and keep only 0 or 1.)
  SWAP=$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:PORTHOLE_CTRL_AS_CMD" "$PLIST" 2>/dev/null || true)
fi
[ "$SWAP" = 1 ] || SWAP=0

# Prefer Apple's python3 (Command Line Tools): its path never changes, so the
# Accessibility permission granted to it survives Homebrew upgrades.
PY=""
for p in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
    PY="$p"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "!! python3 isn't installed on this Mac." >&2
  echo "   Install Apple's Command Line Tools (run 'xcode-select --install' on the Mac and click Install)," >&2
  echo "   then run this installer again." >&2
  exit 1
fi
echo "== using $PY ($("$PY" -V 2>&1))"

echo "== installing agent in $DIR"
mkdir -p "$DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install -m 0755 "$HERE/porthole_agent.py" "$HERE/porthole_mac.py" "$DIR/"

launchctl bootout "gui/$MYUID/$LABEL" 2>/dev/null || true
umask 077
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/porthole_agent.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PORTHOLE_TOKEN</key><string>$TOKEN</string>
    <key>PORTHOLE_PORT</key><string>$PORT</string>
    <key>PORTHOLE_BIND</key><string>0.0.0.0</string>
    <key>PORTHOLE_CTRL_AS_CMD</key><string>$SWAP</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
umask 022

CONSOLE_USER="$(stat -f %Su /dev/console 2>/dev/null || true)"
APP="$PY"
sleep 1
if launchctl bootstrap "gui/$MYUID" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null; then
  sleep 2
  echo "== agent started; last log lines:"
  tail -n 5 "$LOG" 2>/dev/null || true
  # Apple's python3 hands over to a Python.app inside the Command Line Tools: that's the
  # program macOS asks permission for, so show its real path.
  PID="$(pgrep -u "$MYUID" -f "$DIR/porthole_agent.py" | head -n 1 || true)"
  [ -n "$PID" ] && APP="$(ps -o comm= -p "$PID" 2>/dev/null || echo "$PY")"
else
  echo "!! couldn't start the agent from here. It starts by itself the next time $ME logs in at the Mac."
fi
if [ "$CONSOLE_USER" != "$ME" ]; then
  echo
  echo "!! $ME isn't logged in at the Mac's screen right now (${CONSOLE_USER:-nobody} is)."
  echo "   The agent runs in $ME's desktop session, so log in as $ME on the Mac."
fi

cat <<EOF

Porthole agent installed on port $PORT (PC Ctrl acts as $([ "$SWAP" = 1 ] && echo "Command" || echo "Control")).

ONE-TIME STEP ON THE MAC ITSELF: macOS must allow the agent to control the keyboard and mouse.
  System Settings > Privacy & Security > Accessibility > turn on "python3" (or "Python")
  If it isn't listed: click +, press Cmd+Shift+G, and paste
    $APP
macOS doesn't let this be switched on remotely, so use the Mac's own keyboard and mouse
(or Screen Sharing) once. The agent restarts by itself when it's allowed.

Log: $LOG
EOF
