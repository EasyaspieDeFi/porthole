# Porthole

**Porthole turns headless single-board computers into windows on your Windows PC.**
Plug a board's HDMI into a capture card and Porthole shows its screen in a resizable
window. Your keyboard and mouse control the board as if they were plugged into it.
No spare monitor, keyboard or mouse needed.

It works with the Arduino UNO Q, Raspberry Pi (3 and newer), and other Debian-based
boards.

## Features

- **VM-style window:** click in it and type. A hotkey captures the whole keyboard,
  so shortcuts like Alt+Tab and the Windows key go to the board.
- **Exact mouse positioning:** the board sees an absolute-position pointer, so the
  cursor lands exactly where you click. No drift, no double cursor.
- **Several boards, one window:** give each board its own capture input and switch
  between them like a KVM switch.
- **Browser access:** a built-in web UI lets phones, tablets and other computers view
  and control the same boards, with touch and on-screen keyboard support.
- **Finds the right input automatically:** on multi-input capture cards, Porthole
  works out which input the board is on.
- **Paste text** from your PC clipboard into the board. It's typed using the board's
  own keyboard layout.

## How it works

```
 board ──HDMI──► capture card ──► Porthole (Windows) ──► desktop window
   ▲                                   │             └─► web UI (port 8790)
   └──── keyboard/mouse over the network (port 7790) ◄┘
```

- **Video** travels over HDMI into a capture card. Porthole reads the card like a webcam.
- **Input** travels over your network to a small agent on the board. The agent creates
  a virtual keyboard and mouse through Linux `uinput`, so to the board it looks like
  real USB devices. It works on any desktop, and on the text console too.

## What you need

| | |
|---|---|
| PC | Windows 10 or 11, Python 3.10 or newer |
| Capture card | Any HDMI capture device Windows sees as a camera. Multi-input cards (e.g. Elgato Cam Link Pro) can host several boards at once. |
| Board | Debian-based Linux with `python3` and SSH, on the same network as the PC (LAN, or a VPN such as Tailscale) |

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

1. Connect the board's HDMI output to a capture input and note its number
   (e.g. `HDMI-2`). On a single-input card, just leave `--input` out.
2. Install the agent on the board and register it, in one step. Use the board's SSH
   user and its hostname or IP address:

   ```powershell
   .venv\Scripts\python.exe porthole.py --install-agent pi@raspberrypi --name "Pi" --input 2
   ```

   This copies the agent over SSH, installs `python3-evdev`, starts the
   `porthole-agent` service, and adds the board to `config.json`. You'll be asked for
   the board's password, for SSH and then for `sudo`.

3. Start Porthole with `Porthole.bat`, or `.venv\Scripts\python.exe porthole.py`.

To install the agent by hand instead, copy the `agent/` folder to the board, run
`sudo bash install.sh <agent_token from config.json>`, then register the board with
`--add-machine`.

### Managing boards

```powershell
.venv\Scripts\python.exe porthole.py --list-machines
.venv\Scripts\python.exe porthole.py --add-machine "Pi" --host raspberrypi --input 2
.venv\Scripts\python.exe porthole.py --remove-machine "Pi"
```

## Using Porthole

### Desktop window

| Keys | Action |
|---|---|
| Mouse / typing | goes to the current board while the window is focused |
| **Right Ctrl** | capture or release the whole keyboard (Win key, Alt+Tab, ...) |
| **Right Ctrl + M** | choose a board (switches video and input together) |
| **Right Ctrl + I** | choose the capture input for this board, or let Porthole find it |
| Right Ctrl + 1–9 | jump straight to HDMI-1 to HDMI-9 |
| Right Ctrl + F | fullscreen |
| Right Ctrl + V | type the PC clipboard on the board |
| Right Ctrl + Q | quit |

The Right Ctrl key never reaches the board. Use Left Ctrl for shortcuts on the board.

Other options: `--no-window` runs only the web UI, and `--machine NAME` starts on a
specific board.

### Web UI

Open `http://<pc-name>:8790` in a browser. The toolbar has the board and input
selectors, stream quality, fullscreen, an on-screen keyboard (for tablets and phones),
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

## Board-specific notes

### Raspberry Pi

- Works on Raspberry Pi OS, desktop or Lite (Pi 3 and newer).
- **Keyboard layout:** Raspberry Pi OS defaults to a UK layout. Keys are sent by their
  position on the keyboard, so if your PC keyboard is US, symbols like `@ " # \ | ~`
  will be wrong until you change the Pi's layout: `sudo raspi-config` → Localisation
  Options → Keyboard. Pasted text always follows the Pi's layout.
- **No picture:** add `hdmi_force_hotplug=1` to `/boot/firmware/config.txt`
  (`/boot/config.txt` on older images), then reboot.

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
| "agent refused: bad token" | The board was set up with a different token. Reinstall the agent. |
| Wrong symbols when typing | The board's keyboard layout doesn't match your keyboard (see the Raspberry Pi notes). |
| Choppy browser video | Lower the quality in the toolbar. |

Agent logs on a board: `journalctl -u porthole-agent -f`

## Limitations

- Video only, no audio.
- One board is shown at a time. The desktop window and every browser follow the same
  selection.
- The PC side is Windows-only for now: capture inputs are found through Media
  Foundation.
