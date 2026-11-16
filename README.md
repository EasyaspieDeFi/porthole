# Porthole

**Porthole turns headless single-board computers into windows on your Windows PC.**
Porthole shows a board's screen in a resizable window, and your keyboard and mouse
control the board as if they were plugged into it. The picture comes from an HDMI
capture card, or straight over WiFi with no cable at all. That's handy for boards
inside robots and other things that move. No spare monitor, keyboard or mouse needed.

It works with the Arduino UNO Q, Raspberry Pi (3 and newer), and other Debian-based
boards.

## Features

- **VM-style window:** click in it and type. A hotkey captures the whole keyboard,
  so shortcuts like Alt+Tab and the Windows key go to the board.
- **Exact mouse positioning:** the board sees an absolute-position pointer, so the
  cursor lands exactly where you click. No drift, no double cursor.
- **Video over WiFi:** watch a board's desktop, or its camera, over the network with
  no HDMI cable or capture card. Great for robots in motion.
- **Several boards, one window:** give each board its own video source and switch
  between them like a KVM switch.
- **Browser access:** a built-in web UI lets phones, tablets and other computers view
  and control the same boards, with touch and on-screen keyboard support.
- **Finds the right input automatically:** on multi-input capture cards, Porthole
  works out which input the board is on.
- **Paste text** from your PC clipboard into the board. It's typed using the board's
  own keyboard layout.

## How it works

```
 board ──HDMI──► capture card ──┐
   │                            ├──► Porthole (Windows) ──► desktop window
   ├── screen/camera over WiFi ─┘          │             └─► web UI (port 8790)
   ▲                                       │
   └──── keyboard/mouse over the network ◄─┘   (agent port 7790)
```

- **Video** comes one of two ways, chosen per board:
  - **HDMI:** into a capture card, which Porthole reads like a webcam. Full frame rate,
    lowest latency, and works even before the board's network is up.
  - **Network:** the agent on the board captures its own screen (X11, or Wayland via
    `grim`) or a camera (USB webcam, or Raspberry Pi camera module) and streams
    JPEG frames to Porthole. When the link is slow, frames are dropped rather than
    queued, so the picture stays live instead of lagging.
- **Input** travels over your network to a small agent on the board. The agent creates
  a virtual keyboard and mouse through Linux `uinput`, so to the board it looks like
  real USB devices. It works on any desktop, and on the text console too.

## What you need

| | |
|---|---|
| PC | Windows 10 or 11, Python 3.10 or newer |
| Capture card | Optional: any HDMI capture device Windows sees as a camera. Multi-input cards (e.g. Elgato Cam Link Pro) can host several boards at once. Not needed for network video. |
| Board | Debian-based Linux with `python3` and SSH, on the same network as the PC (LAN, or a VPN such as Tailscale). Network video uses `ffmpeg`, which the installer adds. |

## Installation

On the Windows PC:

```powershell
git clone https://github.com/<user>/porthole.git
cd porthole
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Check that Porthole can see your capture card:

```powershell
.venv\Scripts\python.exe porthole.py --list-inputs
```

The first run creates `config.json` with a random access token for the agents. It's
your personal config: keep it private, and don't commit it (it's in `.gitignore`).
`config.example.json` lists every setting.

If your capture card's name doesn't contain "Cam Link", set `device_filter` in
`config.json` to part of its name. Leave it empty to list every camera.

## Adding a board

1. For HDMI video, connect the board's HDMI output to a capture input and note its
   number (e.g. `HDMI-2`). On a single-input card, just leave `--input` out. For
   network video, skip this step and use `--video screen` or `--video camera`
   instead of `--input` below.
2. Install the agent on the board and register it, in one step. Use the board's SSH
   user and its hostname or IP address:

   ```powershell
   .venv\Scripts\python.exe porthole.py --install-agent pi@raspberrypi --name "Pi" --input 2
   ```

   This copies the agent over SSH, installs `python3-evdev` and `ffmpeg`, starts the
   `porthole-agent` service, and adds the board to `config.json`. You'll be asked for
   the board's password, for SSH and then for `sudo`.

   A robot with no HDMI connection:

   ```powershell
   .venv\Scripts\python.exe porthole.py --install-agent pi@robot --name "Robot" --video camera
   ```

3. Start Porthole with `Porthole.bat`, or `.venv\Scripts\python.exe porthole.py`.

To install the agent by hand instead, copy the `agent/` folder to the board, run
`sudo bash install.sh <agent_token from config.json>`, then register the board with
`--add-machine`.

### Managing boards

```powershell
.venv\Scripts\python.exe porthole.py --list-machines
.venv\Scripts\python.exe porthole.py --add-machine "Pi" --host raspberrypi --input 2
.venv\Scripts\python.exe porthole.py --add-machine "Robot" --host robot --video screen
.venv\Scripts\python.exe porthole.py --remove-machine "Pi"
```

## Using Porthole

### Desktop window

| Keys | Action |
|---|---|
| Mouse / typing | goes to the current board while the window is focused |
| **Right Ctrl** | capture or release the whole keyboard (Win key, Alt+Tab, ...) |
| **Right Ctrl + M** | choose a board (switches video and input together) |
| **Right Ctrl + I** | choose this board's video source: an HDMI input, "find automatically", or W / C / T for its screen, camera or a test pattern over the network |
| Right Ctrl + 1–9 | jump straight to HDMI-1 to HDMI-9 |
| Right Ctrl + F | fullscreen |
| Right Ctrl + V | type the PC clipboard on the board |
| Right Ctrl + Q | quit |

The Right Ctrl key never reaches the board. Use Left Ctrl for shortcuts on the board.

Other options: `--no-window` runs only the web UI, and `--machine NAME` starts on a
specific board.

### Web UI

Open `http://<pc-name>:8790` in a browser. The toolbar has the board and input
selectors (including the network video sources), stream quality, fullscreen, an on-screen keyboard (for tablets and phones),
a right-click toggle for touchscreens, common key combos, and a paste box. In Chrome
and Edge, fullscreen also captures system shortcuts.

By default, the web UI only answers requests from the PC itself and from Tailscale
addresses. Edit `web_allow` in `config.json` to allow other networks, such as your
LAN.

#### Remote access with Tailscale

If you use Tailscale, either of these makes Porthole reachable from your other devices:

- `tailscale serve --bg 8790` publishes it at `https://<pc-name>.<tailnet>.ts.net`.
  HTTPS also enables clipboard paste in the browser.
- Or open the port to Tailscale only, from an administrator PowerShell:
  ```powershell
  New-NetFirewallRule -DisplayName "Porthole (Tailscale)" -Direction Inbound -Protocol TCP -LocalPort 8790 -RemoteAddress 100.64.0.0/10 -Action Allow
  ```

## Network video

Pick it per board with Right Ctrl + I (W = screen, C = camera, T = test pattern), the
web UI's source menu, or `--video` on the command line. Porthole remembers the choice
for each board.

| Source | What it shows | How the board captures it |
|---|---|---|
| `screen` | the board's desktop (and the mouse works on it) | X11: `ffmpeg x11grab`. Wayland (labwc, wayfire, sway): `grim` screenshots, which is slower |
| `camera` | the first camera on the board | USB webcams: `ffmpeg` (V4L2). Raspberry Pi camera module: `rpicam-vid` |
| `test` | a moving test pattern | `ffmpeg`. Use it to check the network path |

Stream settings live in `config.json`: `net_width` (default 960), `net_fps` (12) and
`net_quality` (60). That needs roughly 1.5–2 Mbit/s of upload from the board. Raise
them on fast networks, or lower them for weak WiFi or slow boards. Older boards like the
Raspberry Pi 3 may not reach the full frame rate.

Keyboard and mouse work the same with network video. On a camera view the mouse
still moves the board's pointer, but the pointer isn't in the picture.

## Board-specific notes

### Raspberry Pi

- Works on Raspberry Pi OS, desktop or Lite (Pi 3 and newer).
- **Keyboard layout:** Raspberry Pi OS defaults to a UK layout. Keys are sent by their
  position on the keyboard, so if your PC keyboard is US, symbols like `@ " # \ | ~`
  will be wrong until you change the Pi's layout: `sudo raspi-config` → Localisation
  Options → Keyboard. Pasted text always follows the Pi's layout.
- **No picture:** add `hdmi_force_hotplug=1` to `/boot/firmware/config.txt`
  (`/boot/config.txt` on older images), then reboot.
- **Network screen video on Wayland** (the default desktop on recent Raspberry Pi OS)
  uses `grim` and runs at a few frames per second. For smoother video, switch the Pi
  to X11: `sudo raspi-config` → Advanced Options → Wayland → X11.
- **Camera module:** network `camera` video uses `rpicam-vid`, which ships with
  Raspberry Pi OS.

### Arduino UNO Q

- Video comes out of the USB-C port, so you need a USB-C hub or adapter with HDMI
  output. Use one with its own power input.
- **Garbled picture, or "No Signal" at 1080p:** the adapter or cable may not carry a
  clean 1080p signal. Try a shorter or better HDMI cable, or run the desktop at 720p:
  ```bash
  sudo mkdir -p /etc/X11/xorg.conf.d
  printf 'Section "Monitor"\n    Identifier "DP-1"\n    Option "PreferredMode" "1280x720"\nEndSection\n' \
    | sudo tee /etc/X11/xorg.conf.d/10-720p.conf
  sudo systemctl restart lightdm
  ```
  To undo this, delete the file and restart `lightdm` again.

## Security

- Agents only accept connections that present the token from your `config.json`.
  This covers both keyboard/mouse and network video, since anyone holding the token
  can watch the board's screen and camera.
  To revoke access, change the token and reinstall the agents.
- Agents listen on all network interfaces on port 7790. To limit that, set
  `PORTHOLE_BIND` in `/etc/porthole-agent.env` on the board, or firewall the port.
- The web UI has no login of its own. Rely on `web_allow` and your firewall, and never
  expose port 8790 to the internet.

## Troubleshooting

| Symptom | What to check |
|---|---|
| "No signal" banner | The board isn't sending video to that input. Check the cable and the input number, or press Right Ctrl + I → find automatically. |
| Keyboard/mouse "not running on HOST" | The agent isn't installed or has stopped. On the board: `systemctl status porthole-agent` |
| Keyboard/mouse "can't reach HOST" | Hostname or network problem. Try the board's IP address with `--add-machine`. |
| Keyboard/mouse "slow link (N ms)" | The network to the board is laggy. Porthole keeps working: clicks and keys still arrive in order, and the cursor jumps to where you are now instead of replaying old motion. On WiFi boards, see [Laggy WiFi boards](#laggy-wifi-boards). |
| "agent refused: bad token" | The board was set up with a different token. Reinstall the agent. |
| Wrong symbols when typing | The board's keyboard layout doesn't match your keyboard (see the Raspberry Pi notes). |
| Choppy browser video | Lower the quality in the toolbar. |
| Network video: "ffmpeg is not installed" | On the board: `sudo apt install ffmpeg` (the installer normally does this) |
| Network video: "no desktop session found" | The board has no graphical desktop running. Use `camera`, or start the desktop. |
| Network video: "no camera found" | No camera is plugged in, or it isn't recognized: check `ls /dev/video*` |
| Network video is slow or choppy | Lower `net_width` / `net_fps` / `net_quality` in `config.json`, or use HDMI when you're close by. |

Agent logs on a board: `journalctl -u porthole-agent -f`

### Laggy WiFi boards

If "slow link" keeps showing (round trips of 100+ ms that jump around), the board's
WiFi is usually the cause, not Porthole. Two common culprits:

1. **WiFi power saving.** The board's WiFi chip dozes between beacons, so the access
   point holds every incoming packet (your keystrokes) until it wakes, adding up to
   half a second. To check, ping the board, then ping it again while it pings its
   router (`ping -i 0.05 <router>` on the board). If the second run is far faster,
   power saving is the cause. To turn it off on boards that use NetworkManager
   (Raspberry Pi OS, UNO Q):
   ```bash
   printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/90-wifi-powersave-off.conf
   sudo nmcli connection modify "<wifi name>" 802-11-wireless.powersave 2
   sudo nmcli connection up "<wifi name>"
   ```
   This costs a little power (roughly 0.1–0.2 W), which matters on battery-powered robots.
2. **Mesh WiFi hops.** On mesh systems, a board on a satellite node reaches the
   rest of your network through the satellite's own wireless link back to the main
   node. That link can be much slower than the board's signal suggests. Compare
   `ping <router>` from the board on different nodes. Fixes: a wired link for the
   satellite, or, for a board that doesn't move, lock it to the main node with
   `sudo nmcli connection modify "<wifi name>" 802-11-wireless.bssid <AP MAC>`.

## Limitations

- Video only, no audio.
- Network video runs at a lower frame rate and with more latency than HDMI capture.
- One board is shown at a time. The desktop window and every browser follow the same
  selection.
- The PC side is Windows-only for now: capture inputs are found through Media
  Foundation.

## License

MIT. See [LICENSE](LICENSE).
