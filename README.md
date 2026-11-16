# Porthole

Use small Linux boards (Arduino UNO Q, Raspberry Pi, ...) like VM windows on your
Windows PC, with no monitor, keyboard or mouse on the board.

- **Video** comes from an HDMI capture card. Each board goes into its own input.
- **Keyboard and mouse** go over the network to a tiny agent on the board. The agent
  shows up as a virtual USB keyboard and an absolute-position mouse (`/dev/uinput`), so
  the cursor lands exactly where you point.
- **Several boards:** keep a list of machines and switch between them like a KVM switch.
- **From anywhere:** a built-in web UI lets your phone, tablet or laptop use the same
  boards. It's limited to your machine and your Tailscale network by default.

```
board HDMI ──► capture card input N ──► porthole.py ──► local window
                                             │        └► web UI :8790 ──► other devices (e.g. over Tailscale)
board agent (uinput) ◄────── TCP 7790 ───────┘
```

Tested with an Elgato Cam Link Pro (4 HDMI inputs) and an Arduino UNO Q. Raspberry Pi
OS is supported by the same agent. Other capture cards that Windows sees as cameras
should work too: set `device_filter` in `config.json` to part of their name.

## Requirements

- **PC:** Windows 10/11 with Python 3.10+ and an HDMI capture card.
- **Board:** Debian-based Linux (UNO Q Debian, Raspberry Pi OS, ...) with `python3`,
  reachable from the PC over the network, and SSH for the installer.

## Setup

```powershell
git clone <this repo> porthole
cd porthole
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe porthole.py --list-inputs
```

`config.json` is created on first run with a random agent token. It's ignored by git.
`config.example.json` shows every setting.

### Add a board

1. Plug the board's HDMI into a free capture input and note the port (e.g. HDMI-2).
2. Install the agent and add the machine in one go. Use the board's user and its
   hostname, Tailscale name or IP. You'll be asked for the SSH and sudo passwords:
   ```powershell
   .venv\Scripts\python.exe porthole.py --install-agent pi@raspberrypi --name "Pi 3" --input 2
   ```
   This copies `agent/` to the board, installs `python3-evdev`, sets up the
   `porthole-agent` systemd service, and saves the machine to `config.json`.
3. Manage machines with `--list-machines`,
   `--add-machine "Pi 3" --host raspberrypi --input 2`, and `--remove-machine "Pi 3"`.

Without the installer: copy `agent/` to the board and run
`sudo bash install.sh <agent_token from config.json>`, then use `--add-machine`.

## Use

Double-click **`Porthole.bat`**, or run `.venv\Scripts\python.exe porthole.py`.
Add `--no-window` to run just the web UI, or `--machine NAME` to pick the starting machine.

Local window:

| Keys | |
|---|---|
| Move / click / scroll | goes straight to the current machine |
| Typing | sent while the window is focused |
| **Right Ctrl** | toggle full keyboard capture (Win key, Alt+Tab go to the board) |
| **Right Ctrl + M** | machine picker (switches video input and keyboard/mouse together) |
| **Right Ctrl + I** | input picker for the current machine (or "find automatically") |
| Right Ctrl + 1-9 | jump to HDMI-1 to HDMI-9 |
| Right Ctrl + F | fullscreen |
| Right Ctrl + V | type the PC clipboard on the board |
| Right Ctrl + Q | quit |

The web UI is at `http://<your-pc>:8790` and works the same way. It has a machine
dropdown, an on-screen keyboard for tablets, right-click for touch, key combos, paste,
stream quality and fullscreen. In Chrome/Edge, fullscreen also captures system shortcuts.

"Find automatically" works because every empty input on the card shows the same
"No Signal" screen: the input that looks different is the one with a board. Inputs that
other machines use are skipped.

### Using it from other devices over Tailscale

The web UI only answers loopback and Tailscale addresses (`web_allow` in `config.json`).
Then pick one:

- `tailscale serve --bg 8790` serves it as `https://<your-pc>.<tailnet>.ts.net`. HTTPS
  also enables clipboard paste in the browser.
- Or open the port to Tailscale only, from an admin PowerShell:
  `New-NetFirewallRule -DisplayName "Porthole (Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 8790 -RemoteAddress 100.64.0.0/10 -Action Allow`

## Board notes

### Raspberry Pi
- Works on Raspberry Pi OS (Pi 3 and newer), desktop or Lite.
- **Keyboard layout:** Raspberry Pi OS defaults to a UK layout. Keys are sent by
  position, so with a US keyboard `@ " # \ | ~` come out wrong until you set the Pi to
  US (`sudo raspi-config` → Localisation Options → Keyboard). Pasted text already
  follows the board's layout.
- **No picture?** Add `hdmi_force_hotplug=1` to `/boot/firmware/config.txt`
  (`/boot/config.txt` on older images).

### Arduino UNO Q
- Video comes out of the USB-C port, so you need a USB-C hub or adapter with HDMI.
  Use one with its own power input.
- If the picture is garbled or missing at 1080p, the adapter or cable is probably
  marginal. Try a better cable, or run the desktop at 720p:
  ```bash
  sudo mkdir -p /etc/X11/xorg.conf.d
  printf 'Section "Monitor"\n    Identifier "DP-1"\n    Option "PreferredMode" "1280x720"\nEndSection\n' \
    | sudo tee /etc/X11/xorg.conf.d/10-720p.conf
  sudo systemctl restart lightdm
  ```

## Security

- The agent only accepts connections that present the token from `config.json`.
  Keep that file private. Change the token, then reinstall the agents, to revoke access.
- The agent listens on all interfaces (port 7790). To restrict it, set `PORTHOLE_BIND`
  in `/etc/porthole-agent.env`, or firewall it to your LAN or Tailscale.
- There is no login on the web UI. Rely on `web_allow`, and don't expose port 8790 to
  the internet.

## Troubleshooting

- Agent logs on a board: `journalctl -u porthole-agent -f`
- Window title or status bar says "not running on HOST": the agent isn't installed or
  running there. "can't reach HOST": a network or name problem.
- No audio: Porthole carries video only.
