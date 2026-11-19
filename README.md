# Porthole

**Porthole turns headless single-board computers into windows on your Windows PC.**
Porthole shows a board's screen in a resizable window, and your keyboard and mouse
control the board as if they were plugged into it. The picture comes from an HDMI
capture card, or straight over WiFi with no cable at all. That's handy for boards
inside robots and other things that move. No spare monitor, keyboard or mouse needed.

It works with the Arduino UNO Q, Raspberry Pi (3 and newer), other Debian-based
boards, and Macs (such as a Mac mini).

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
| Mac | macOS with Remote Login (SSH) turned on and Apple's `python3` (Command Line Tools). See [Mac](#mac-mac-mini-imac-macbook). |

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

## Adding a device

In Porthole, click **+** next to your devices (or press Right Ctrl + N):

1. **Name** it and pick its **type** (Mac, Raspberry Pi or another Linux board).
2. Type its **network address** (hostname or IP); that's where Porthole sends the
   keyboard and mouse.
3. Under **Video**, click the capture input that shows it. Each input shows a live
   preview with *Picture*, *No signal* or *In use*, so the right one is easy to spot.
   Or pick *Find automatically*, or **Screen / Camera** to get the picture over the
   network with no cable.
4. Choose how its **mouse** works: Porthole controls it, it's shared with another KVM
   such as Deskflow (click-through), or keyboard only.
5. **Add device**. Porthole then offers to **Install agent**: give your user name on the
   device, and a terminal opens that installs the small agent over SSH. It asks for the
   device's password there; Porthole never stores it.

The first time you start Porthole with no devices, it opens this right away. Edit or
remove devices from **Settings** (the gear), or right-click a device's tab.

### From the command line

The same, without the app. Install the agent on a board and register it in one step
(use the board's SSH user and its hostname or IP address):

```powershell
.venv\Scripts\python.exe porthole.py --install-agent pi@raspberrypi --name "Pi" --input 2
```

This copies the agent over SSH, installs `python3-evdev` and `ffmpeg`, starts the
`porthole-agent` service, and adds the board to `config.json`. You'll be asked for the
board's password, for SSH and then for `sudo`. More examples:

```powershell
.venv\Scripts\python.exe porthole.py --install-agent you@mac-mini.local --name "Mac mini" --input 3
.venv\Scripts\python.exe porthole.py --install-agent pi@robot --name "Robot" --video camera
.venv\Scripts\python.exe porthole.py --list-machines
.venv\Scripts\python.exe porthole.py --add-machine "Pi" --host raspberrypi --input 2
.venv\Scripts\python.exe porthole.py --remove-machine "Pi"
```

(Use `--video screen` or `--video camera` instead of `--input` for network video; leave
`--input` out on a single-input capture card. The Mac needs one permission switched on
at the Mac itself: see [Mac](#mac-mac-mini-imac-macbook).) To install the agent by hand,
copy the `agent/` folder to the board, run `sudo bash install.sh <agent_token from
config.json>`, then register the board with `--add-machine`.

Start Porthole with `Porthole.bat`, or `.venv\Scripts\python.exe porthole.py`. It starts
in the tray: press **Ctrl+Alt+K** to pull it out.

## Using Porthole

### The Porthole window

Across the top are your **devices** (the green dot means keyboard and mouse are
connected) and **+** to add one. On the right: video source, type the clipboard,
capture the whole keyboard, keyboard shortcuts (**?**), settings, and dock / hide. The status bar at the bottom
shows the connection, the video source and frame rate, and whether the keyboard is
captured. Hover over any button for its name and shortcut.

| Keys | Action |
|---|---|
| Mouse / typing | goes to the current device while Porthole has focus |
| **Right Ctrl** | capture or release the whole keyboard (Win key, Alt+Tab, ...) |
| **Right Ctrl + M** | device menu (switch, add, manage) |
| **Right Ctrl + I** | video source: an HDMI input, *Find automatically*, or W / C / T for screen, camera or a test pattern over the network |
| Right Ctrl + N | add a device |
| Right Ctrl + , | settings |
| Right Ctrl + / | all keyboard shortcuts (also the **?** button) |
| Right Ctrl + 1–9 | jump straight to HDMI-1 to HDMI-9 |
| Right Ctrl + F | panel: open as a window. Window: fullscreen |
| Right Ctrl + V | type the PC clipboard on the device |
| Right Ctrl + Q | quit |

The Right Ctrl key never reaches the device. Use Left Ctrl for shortcuts on the device.

Other options: `--no-window` runs only the web UI, and `--machine NAME` starts on a
specific device.

### Tray, side panel and window

Porthole stays out of the way. It starts hidden, with a porthole icon in the system tray,
and you pull it out when you need it, even in the middle of a game:

| Keys | Action |
|---|---|
| **Ctrl+Alt+K** | slide the side panel out; press it again to slide it back in (the game gets focus back) |
| **Ctrl+Alt+1–9** | slide it out on device 1–9; the same key again slides it back in |
| **Right Ctrl + F** (in the panel) | pop out into a normal, resizable window |
| Tray icon | left click: panel in / out. Right click: *Show panel*, *Open window*, *Quit Porthole* |

- **The panel** slides out from the edge of the monitor you're on, with the live picture
  and your devices across the top. It takes the keyboard and mouse right away, so you
  can click and type on the device at once.
- **The window** (Right Ctrl + F, the panel's window button, or the tray menu) is the
  full-size view; its dock button turns it back into the panel. Ctrl+Alt+K or the close
  button puts it away; Porthole keeps running in the tray. Quit from the tray menu or
  with Right Ctrl + Q.
- Games must run in **borderless** or **windowed** mode. Nothing can draw over a game in
  exclusive fullscreen: pulling out the panel would minimize it.

### Settings

The gear opens Settings. Changes apply at once:

- **Slides out from:** Left or Right. An open panel moves straight there.
- **Width:** Compact, Standard or Wide (a quarter to two-fifths of the monitor). The
  panel is 16:9, so it fits beside a 16:9 game on an ultrawide.
- **Shortcut:** click *Change* and press the new combination (for example Ctrl+Shift+F12).
  The device keys (1–9) use the same modifiers.
- **Start with Windows:** Porthole starts when you sign in, ready in the tray (a shortcut
  in your Startup folder; `porthole.py --autostart on` or `off` does the same).
- **Start hidden in the tray:** off opens the window when Porthole starts (or run
  `porthole.py --window` once).
- **Create shortcuts:** adds Porthole, with its icon, to the Start menu and the desktop
  (the same as `porthole.py --create-shortcut`).
- **Devices:** edit or remove each one, or add another.

They're saved in `config.json` as `panel_side`, `panel_width`, `panel_hotkey` and
`start_hidden`.

To add Porthole to the Start menu and the desktop, with its icon:

```powershell
.venv\Scripts\python.exe porthole.py --create-shortcut
```

Only one Porthole runs at a time. Starting it again (shortcut, `Porthole.bat`, the Xbox
app) doesn't open a second copy: the running one pulls out its side panel instead, or its
window if you add `--window`. `--panel` starts Porthole with the panel already out.

### From the Xbox button

On Windows 11 the Xbox button on a controller opens **Xbox Game Bar**. Away from a game
that's the controller bar: a row with your three most recent games, then the Xbox app
and your game launchers. `--create-shortcut` (above) puts Porthole at the front of that
row, with its icon:

- It builds `Porthole.exe` in the Porthole folder: a tiny launcher with Porthole's icon
  that starts Porthole with its side panel out, or pulls the panel out of the copy that's
  already running. It's compiled with the C# compiler that comes with Windows, so
  there's nothing to install.
- It adds Porthole.exe to Game Bar's list of recent games
  (`%LOCALAPPDATA%\Microsoft\GameDVR\GameMRU\LocalMruGameList.json`), which is where the
  controller bar gets that row.
- While Porthole runs it keeps itself first in that list, so playing other games doesn't
  push it off the bar. To take it off, set `"game_bar": false` in `config.json` and
  restart Porthole.

So: Xbox button → **Porthole**. Game Bar has no supported way to pin an app, so this
uses its recent-games list. A Game Bar update could change how that list works; if
Porthole disappears from the bar, run `--create-shortcut` again.

If you also want it in the **Xbox app**'s library (Xbox button → Xbox app → My Library):
choose **+**, **Browse**, pick `Porthole.exe` from the Porthole folder, and use
`porthole.png` (written next to it) as the picture.

In the middle of a game, Ctrl+Alt+K is still quicker: there the Xbox button opens Game
Bar's full overlay rather than the controller bar.

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

### Mac (Mac mini, iMac, MacBook)

The Mac's picture comes in over HDMI like any board (an HDMI splitter works fine, so a
monitor can stay connected). Keyboard and mouse go through a small agent that runs in
your login session and injects input the way a USB keyboard and mouse would.

1. **On the Mac, once:**
   - Turn on SSH: System Settings → General → Sharing → **Remote Login**.
   - Make sure `python3` works in Terminal. If macOS offers to install the Command Line
     Tools, click Install (or run `xcode-select --install`).
2. **On the PC:** find which capture input the Mac is on (`--list-inputs`, or Right Ctrl + I
   in the window), then install and register it. Use your Mac username and its name on
   the network (System Settings → General → Sharing shows it, e.g. `mac-mini.local`):
   ```powershell
   .venv\Scripts\python.exe porthole.py --install-agent you@mac-mini.local --name "Mac mini" --input 3
   ```
   The installer detects the Mac and sets the agent up as a LaunchAgent. No `sudo`.
3. **On the Mac, once:** macOS asks whether to let "python3" control the computer. Open
   System Settings → Privacy & Security → **Accessibility** and turn it on. macOS
   doesn't allow this remotely, so use the Mac's own keyboard and mouse (or Screen
   Sharing) for this one step. The agent restarts by itself once it's allowed. Until
   then Porthole shows "agent refused: the Mac hasn't allowed Porthole to control it yet".

Notes:

- **Keys:** by default the PC keyboard maps the way macOS maps any PC keyboard: Alt is
  Option and the Windows key is Command (press Right Ctrl first to capture the
  keyboard, so the Windows key reaches the Mac). To use Ctrl+C / Ctrl+V like on
  Windows, reinstall with `--ctrl-as-cmd`. That swaps Ctrl and Command, so the
  Windows key becomes Control. `--no-ctrl-as-cmd` switches back.
- **Paste** (Right Ctrl + V) types the characters themselves, so it works whatever the
  Mac's keyboard layout is.
- **Deskflow, Barrier, Synergy or another KVM** that shares the Mac's own keyboard and
  mouse with the PC: only one thing can drive the Mac's pointer, or the two pointers fight
  and clicks land on the wrong computer. Let the KVM keep the pointer and switch Porthole to
  click-through for the Mac:
  ```powershell
  .venv\Scripts\python.exe porthole.py --add-machine "Mac mini" --mouse clicks
  ```
  Your clicks, double-clicks, right-clicks and scrolling on the Porthole picture still
  reach the Mac, along with what you type in the Porthole window, and the Mac's pointer
  never moves (the KVM would pass any movement on to the PC as a jump). Buttons, menus,
  the menu bar and the Dock are pressed through macOS accessibility, and every other click
  goes straight to the app whose window is under it. Hover effects, browser tab
  selection and dragging, and moving windows by their title bars can require native
  Deskflow control (see below). `--mouse off` sends keys only, and
  `--mouse on` gives Porthole the pointer again (for when the keyboard and mouse are
  plugged into the PC).

  **To use the Mac natively,** let Deskflow carry your mouse onto it and have Porthole
  show its screen at that moment: edit the Mac (Settings, the pencil) and turn on **Show it
  when my mouse moves onto it**. Moving your mouse across Deskflow's edge then slides the
  preview out. Keep using the mouse across the Deskflow edge to select browser tabs
  or drag windows on the Mac. Porthole pauses its input forwarding for the Mac while
  Deskflow is in control. Move back to the PC and the panel slides away. Porthole
  follows Deskflow's log on the PC (`C:\ProgramData\Deskflow\deskflow-daemon.log`) for this.
- **Stays logged in:** the agent runs in your desktop session, so it works whenever
  you're logged in at the Mac (including at the lock screen). After a reboot it starts
  when you log in. For a headless Mac mini, turn on automatic login (System Settings →
  Users & Groups; not available while FileVault is on).
- **Several displays:** the mouse targets the main display. If the HDMI capture shows a
  different one, add `PORTHOLE_DISPLAY` (1, 2, ...) to the plist's
  `EnvironmentVariables` in `~/Library/LaunchAgents/com.porthole.agent.plist`.
- **No picture or "No Signal":** the splitter may pass on a 4K mode the capture card
  can't take. Set the Mac's display to 1080p in System Settings → Displays.
- **Network video** (W in Right Ctrl + I) needs `ffmpeg` from Homebrew
  (`brew install ffmpeg`), and macOS asks for Screen Recording permission the first
  time. HDMI needs neither.
- Logs: `tail -f ~/Library/Logs/porthole-agent.log`. Uninstall:
  `launchctl bootout gui/$(id -u)/com.porthole.agent; rm -rf ~/.porthole-agent ~/Library/LaunchAgents/com.porthole.agent.plist`

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
   power saving is the cause. The installer can turn it off: add `--wifi-no-powersave`
   to `--install-agent`. To do it by hand on boards that use NetworkManager
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
   satellite, [WiFi steering](#wifi-steering-mesh-networks) (below), or, for a board
   that doesn't move, lock it to the main node with
   `sudo nmcli connection modify "<wifi name>" 802-11-wireless.bssid <AP MAC>`.

### WiFi steering (mesh networks)

WiFi normally joins whichever access point is *loudest*. On a mesh, that can be a
satellite with a slow link back to the router. With steering on, the agent judges
access points by what actually matters, the round trip to the router, and moves
the board when the link stays slow. That's useful for robots that drive around.

```powershell
.venv\Scripts\python.exe porthole.py --install-agent pi@robot --name "Robot" --wifi-steer --wifi-no-powersave
```

How it behaves:

- Pings the router every 10 s. If the round trip stays above 40 ms (or packets are
  lost) for 30 s, it tries the other access points with the same WiFi name: ones it
  has seen perform well first, then 5 GHz, then by signal. It keeps the first one
  that's fast.
- If the current access point's signal fades (the robot drove away), it acts at once.
- At most one switch every 2 minutes, so it never flaps. Each switch interrupts
  WiFi for a few seconds, and Porthole reconnects by itself.
- If a switch fails, it releases the lock and lets normal roaming take over, so a
  robot is never left stranded.
- Porthole's window title and web status show the board's access point, signal and
  router latency.
- Needs NetworkManager on the board. Tune the threshold with `PORTHOLE_WIFI_MAX_MS`
  in `/etc/porthole-agent.env`. Turn it off with `--no-wifi-steer`, or by setting
  `PORTHOLE_WIFI_STEER=0` and restarting `porthole-agent`.

## Limitations

- Video only, no audio.
- Network video runs at a lower frame rate and with more latency than HDMI capture.
- One board is shown at a time. The desktop window and every browser follow the same
  selection.
- The PC side is Windows-only for now: capture inputs are found through Media
  Foundation.

## License

MIT. See [LICENSE](LICENSE).
