#!/usr/bin/env python3
"""Porthole agent for small Linux boards (Arduino UNO Q, Raspberry Pi, ...).

Runs on the board and does two jobs over one TCP port:

- Keyboard/mouse: accepts events from the PC app (newline-delimited JSON) and injects
  them through two virtual /dev/uinput devices: a keyboard and an absolute pointer
  (the same shape as QEMU's usb-tablet, so the cursor lands exactly where you point).
- Video over the network: streams the board's screen (X11 or Wayland) or a camera as
  JPEG frames, so a board in a robot can be watched without an HDMI cable.

Config comes from the environment (set by install.sh in /etc/porthole-agent.env):
  PORTHOLE_TOKEN  shared secret the PC must send first (required)
  PORTHOLE_BIND   listen address (default 0.0.0.0)
  PORTHOLE_PORT   listen port (default 7790)
  PORTHOLE_LAYOUT keyboard layout used for pasted text (default: XKBLAYOUT from
              /etc/default/keyboard, so Raspberry Pi OS's "gb" default just works)
"""
import asyncio
import hmac
import json
import logging
import os
import socket
import sys

from evdev import AbsInfo, UInput, ecodes as e

log = logging.getLogger("porthole-agent")


def setting(name, default=""):
    """PORTHOLE_<name>, falling back to the pre-rename UNOQ_<name>."""
    return os.environ.get("PORTHOLE_" + name) or os.environ.get("UNOQ_" + name) or default
ABS_MAX = 32767


def build_code_map():
    """W3C KeyboardEvent.code -> Linux keycode."""
    m = {}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        m["Key" + c] = getattr(e, "KEY_" + c)
    for d in range(10):
        m[f"Digit{d}"] = getattr(e, f"KEY_{d}")
        m[f"Numpad{d}"] = getattr(e, f"KEY_KP{d}")
    for f in range(1, 25):
        m[f"F{f}"] = getattr(e, f"KEY_F{f}")
    m.update({
        "Enter": e.KEY_ENTER, "Escape": e.KEY_ESC, "Backspace": e.KEY_BACKSPACE,
        "Tab": e.KEY_TAB, "Space": e.KEY_SPACE, "Minus": e.KEY_MINUS,
        "Equal": e.KEY_EQUAL, "BracketLeft": e.KEY_LEFTBRACE,
        "BracketRight": e.KEY_RIGHTBRACE, "Backslash": e.KEY_BACKSLASH,
        "IntlHash": e.KEY_BACKSLASH, "Semicolon": e.KEY_SEMICOLON,
        "Quote": e.KEY_APOSTROPHE, "Backquote": e.KEY_GRAVE, "Comma": e.KEY_COMMA,
        "Period": e.KEY_DOT, "Slash": e.KEY_SLASH, "CapsLock": e.KEY_CAPSLOCK,
        "PrintScreen": e.KEY_SYSRQ, "ScrollLock": e.KEY_SCROLLLOCK,
        "Pause": e.KEY_PAUSE, "Insert": e.KEY_INSERT, "Home": e.KEY_HOME,
        "PageUp": e.KEY_PAGEUP, "Delete": e.KEY_DELETE, "End": e.KEY_END,
        "PageDown": e.KEY_PAGEDOWN, "ArrowRight": e.KEY_RIGHT,
        "ArrowLeft": e.KEY_LEFT, "ArrowDown": e.KEY_DOWN, "ArrowUp": e.KEY_UP,
        "NumLock": e.KEY_NUMLOCK, "NumpadDivide": e.KEY_KPSLASH,
        "NumpadMultiply": e.KEY_KPASTERISK, "NumpadSubtract": e.KEY_KPMINUS,
        "NumpadAdd": e.KEY_KPPLUS, "NumpadEnter": e.KEY_KPENTER,
        "NumpadDecimal": e.KEY_KPDOT, "NumpadEqual": e.KEY_KPEQUAL,
        "IntlBackslash": e.KEY_102ND, "IntlRo": e.KEY_RO, "IntlYen": e.KEY_YEN,
        "ContextMenu": e.KEY_COMPOSE,
        "ControlLeft": e.KEY_LEFTCTRL, "ShiftLeft": e.KEY_LEFTSHIFT,
        "AltLeft": e.KEY_LEFTALT, "MetaLeft": e.KEY_LEFTMETA,
        "ControlRight": e.KEY_RIGHTCTRL, "ShiftRight": e.KEY_RIGHTSHIFT,
        "AltRight": e.KEY_RIGHTALT, "MetaRight": e.KEY_RIGHTMETA,
        "OSLeft": e.KEY_LEFTMETA, "OSRight": e.KEY_RIGHTMETA,
        "AudioVolumeMute": e.KEY_MUTE, "AudioVolumeUp": e.KEY_VOLUMEUP,
        "AudioVolumeDown": e.KEY_VOLUMEDOWN, "MediaPlayPause": e.KEY_PLAYPAUSE,
        "MediaTrackNext": e.KEY_NEXTSONG, "MediaTrackPrevious": e.KEY_PREVIOUSSONG,
    })
    return m


CODE_MAP = build_code_map()


def build_us_layout():
    """Character -> (code, needs_shift) for typing pasted text on a US layout."""
    t = {" ": ("Space", False), "\n": ("Enter", False), "\t": ("Tab", False)}
    for c in "abcdefghijklmnopqrstuvwxyz":
        t[c] = ("Key" + c.upper(), False)
        t[c.upper()] = ("Key" + c.upper(), True)
    for plain, shifted in zip("1234567890", "!@#$%^&*()"):
        t[plain] = (f"Digit{plain}", False)
        t[shifted] = (f"Digit{plain}", True)
    for plain, shifted, code in [
        ("-", "_", "Minus"), ("=", "+", "Equal"), ("[", "{", "BracketLeft"),
        ("]", "}", "BracketRight"), ("\\", "|", "Backslash"), (";", ":", "Semicolon"),
        ("'", '"', "Quote"), ("`", "~", "Backquote"), (",", "<", "Comma"),
        (".", ">", "Period"), ("/", "?", "Slash"),
    ]:
        t[plain] = (code, False)
        t[shifted] = (code, True)
    return t


US = build_us_layout()

# UK layout differs from US on a handful of characters (Raspberry Pi OS ships with "gb").
GB = dict(US)
GB.update({
    '"': ("Digit2", True), "@": ("Quote", True), "#": ("Backslash", False), "~": ("Backslash", True),
    "\\": ("IntlBackslash", False), "|": ("IntlBackslash", True), "£": ("Digit3", True),
    "¬": ("Backquote", True),
})
LAYOUTS = {"us": US, "gb": GB}


def current_layout():
    env = setting("LAYOUT")
    if env:
        return env.lower()
    try:
        with open("/etc/default/keyboard", encoding="utf-8") as f:
            for line in f:
                if line.startswith("XKBLAYOUT="):
                    return line.split("=", 1)[1].strip().strip('"').split(",")[0].lower() or "us"
    except OSError:
        pass
    return "us"
BUTTONS = {0: e.BTN_LEFT, 1: e.BTN_MIDDLE, 2: e.BTN_RIGHT, 3: e.BTN_SIDE, 4: e.BTN_EXTRA}


class Devices:
    def __init__(self):
        self.kbd = UInput({e.EV_KEY: sorted(set(CODE_MAP.values()))},
                          name="Porthole Keyboard", vendor=0x1209, product=0x7790)
        self.ptr = UInput({
            e.EV_KEY: sorted(BUTTONS.values()),
            e.EV_ABS: [(e.ABS_X, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
                       (e.ABS_Y, AbsInfo(0, 0, ABS_MAX, 0, 0, 0))],
            e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
        }, name="Porthole Pointer", vendor=0x1209, product=0x7791)

    def key(self, code, down):
        self.kbd.write(e.EV_KEY, code, 1 if down else 0)
        self.kbd.syn()

    def move(self, x, y):
        self.ptr.write(e.EV_ABS, e.ABS_X, round(min(max(x, 0.0), 1.0) * ABS_MAX))
        self.ptr.write(e.EV_ABS, e.ABS_Y, round(min(max(y, 0.0), 1.0) * ABS_MAX))
        self.ptr.syn()

    def button(self, btn, down):
        self.ptr.write(e.EV_KEY, btn, 1 if down else 0)
        self.ptr.syn()

    def wheel(self, dx, dy):
        if dy:
            self.ptr.write(e.EV_REL, e.REL_WHEEL, dy)
        if dx:
            self.ptr.write(e.EV_REL, e.REL_HWHEEL, dx)
        self.ptr.syn()


class Session:
    """One PC connection. Tracks what it holds so a dropped link never leaves stuck keys."""

    def __init__(self, dev):
        self.dev = dev
        self.keys = set()
        self.buttons = set()

    def release_all(self):
        for code in list(self.keys):
            self.dev.key(code, False)
        for btn in list(self.buttons):
            self.dev.button(btn, False)
        self.keys.clear()
        self.buttons.clear()

    async def type_text(self, text):
        shift = e.KEY_LEFTSHIFT
        table = LAYOUTS.get(current_layout(), US)
        for ch in text[:20000]:
            mapped = table.get(ch)
            if not mapped:
                continue
            code, needs_shift = mapped
            if needs_shift:
                self.dev.key(shift, True)
            self.dev.key(CODE_MAP[code], True)
            self.dev.key(CODE_MAP[code], False)
            if needs_shift:
                self.dev.key(shift, False)
            await asyncio.sleep(0.006)

    async def handle(self, msg):
        t = msg.get("t")
        if t == "k":
            code = CODE_MAP.get(msg.get("c"))
            if code is None:
                return
            down = bool(msg.get("d"))
            self.dev.key(code, down)
            (self.keys.add if down else self.keys.discard)(code)
        elif t == "m":
            self.dev.move(float(msg["x"]), float(msg["y"]))
        elif t == "b":
            btn = BUTTONS.get(int(msg.get("b", 0)))
            if btn is None:
                return
            down = bool(msg.get("d"))
            self.dev.button(btn, down)
            (self.buttons.add if down else self.buttons.discard)(btn)
        elif t == "w":
            dx = max(-20, min(20, int(msg.get("dx", 0))))
            dy = max(-20, min(20, int(msg.get("dy", 0))))
            self.dev.wheel(dx, dy)
        elif t == "r":
            self.release_all()
        elif t == "type":
            await self.type_text(str(msg.get("s", "")))


# --------------------------------------------------------------------------- video over the network
#
# A video client sends {"t": "hello", "mode": "video", "src": "screen"|"camera"|"test", ...}.
# The agent answers with one JSON line, then sends frames as
#     b"J" + 4-byte big-endian length + JPEG       (a frame)
#     b"E" + 4-byte big-endian length + UTF-8 text (an error; the stream ends)
# and the client answers each frame with a line ("a\n") so at most two frames are ever in
# flight. On a slow WiFi link frames get dropped instead of piling up as lag.

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def ffmpeg_quality(q):
    """JPEG-style quality 20..95 -> ffmpeg -q:v 2..31 (lower is better)."""
    return max(2, min(31, round((100 - q) / 6)))


def _proc_cmdlines():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if args:
            yield pid, args


def active_vt():
    """Number of the virtual console on screen right now (e.g. 7), or None."""
    try:
        with open("/sys/class/tty/tty0/active") as f:
            return int(f.read().strip().removeprefix("tty"))
    except (OSError, ValueError):
        return None


def find_x_display():
    """(DISPLAY, XAUTHORITY) of the X server that's on screen, or (None, None).

    A board can run several X servers: when the desktop locks (light-locker) or another
    user logs in, the display manager starts another one on a different virtual console.
    Capture the one on the active console, so the picture matches what HDMI would show.
    """
    servers = []
    for _, args in _proc_cmdlines():
        if os.path.basename(args[0]) not in ("Xorg", "X", "Xvfb", "Xtigervnc", "Xvnc"):
            continue
        display = next((a for a in args[1:] if a.startswith(":")), ":0")
        auth = args[args.index("-auth") + 1] if "-auth" in args[:-1] else None
        vt = next((int(a[2:]) for a in args[1:] if a.startswith("vt") and a[2:].isdigit()), None)
        if auth and not os.access(auth, os.R_OK):
            # Not root: fall back to this user's own X credentials.
            home_auth = os.path.expanduser("~/.Xauthority")
            auth = os.environ.get("XAUTHORITY") or (home_auth if os.path.exists(home_auth) else None)
        servers.append((vt, display, auth))
    if servers:
        on_screen = active_vt()
        vt, display, auth = next((s for s in servers if s[0] == on_screen), servers[0])
        return display, auth
    if os.environ.get("DISPLAY"):
        return os.environ["DISPLAY"], os.environ.get("XAUTHORITY")
    return None, None


def find_wayland():
    """Environment for talking to a running wlroots compositor (labwc, wayfire, sway), or None."""
    for pid, args in _proc_cmdlines():
        if os.path.basename(args[0]) not in ("labwc", "wayfire", "sway", "Hyprland", "weston"):
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = dict(kv.split(b"=", 1) for kv in f.read().split(b"\0") if b"=" in kv)
        except OSError:
            continue
        runtime = env.get(b"XDG_RUNTIME_DIR", b"").decode()
        display = env.get(b"WAYLAND_DISPLAY", b"wayland-0").decode()
        if runtime:
            return {"XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": display}
    return None


def find_camera():
    """Path of the first V4L2 capture device (a USB webcam or similar), or None."""
    base = "/sys/class/video4linux"
    try:
        names = sorted(os.listdir(base), key=lambda n: int("".join(c for c in n if c.isdigit()) or 0))
    except OSError:
        return None
    for name in names:
        try:
            with open(f"{base}/{name}/index") as f:
                if f.read().strip() != "0":
                    continue            # metadata node of a camera, not the picture
            with open(f"{base}/{name}/name") as f:
                label = f.read().strip().lower()
        except OSError:
            continue
        if any(skip in label for skip in ("codec", "isp", "unicam", "bcm2835", "hevc", "venus", "iris")):
            continue                    # hardware encoders/ISPs, not cameras
        return f"/dev/{name}"
    return None


def _program_name(pid):
    """'cam_server.py' for 'python3 /opt/bot/cam_server.py', 'v4l2-ctl' for '/usr/bin/v4l2-ctl ...'."""
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    if not args:
        return str(pid)
    script = next((a for a in args[1:] if a.endswith((".py", ".sh", ".js"))), None)
    return os.path.basename(script or args[0])


def device_users(dev):
    """Programs that have `dev` open (all of them when running as root), e.g. ['cam_server.py (via v4l2-ctl)']."""
    users = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == str(os.getpid()):
            continue
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
            if not any(os.readlink(f"/proc/{pid}/fd/{fd}") == dev for fd in fds):
                continue
            name = _program_name(pid)
            if name in ("v4l2-ctl", "ffmpeg", "gst-launch-1.0", "rpicam-vid", "libcamera-vid"):
                # A helper tool: the program that started it is the one people recognize.
                with open(f"/proc/{pid}/stat") as f:
                    ppid = f.read().rsplit(")", 1)[1].split()[1]
                parent = _program_name(ppid)
                if parent not in ("bash", "sh", "systemd", "1"):
                    name = f"{parent} (via {name})"
        except (OSError, IndexError):
            continue
        users.add(name)
    return sorted(users)


def have(tool):
    return any(os.access(os.path.join(d, tool), os.X_OK) for d in os.environ.get("PATH", "/usr/bin:/bin").split(":"))


def jpeg_size(data):
    """(width, height) from a JPEG's SOF marker, or None."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


class VideoError(Exception):
    pass


def video_command(src, width, fps, quality, camera_mjpeg=True):
    """(argv, extra_env, description) for an ffmpeg/rpicam process that writes MJPEG to stdout."""
    qv = str(ffmpeg_quality(quality))
    scale = f"scale='min({width},iw)':-2"
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    out = ["-f", "mjpeg", "-"]
    if src == "test":
        if not have("ffmpeg"):
            raise VideoError("ffmpeg is not installed on the board (sudo apt install ffmpeg)")
        return (base + ["-re", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}",
                        "-vf", scale, "-q:v", qv] + out, {}, "test pattern")
    if src == "screen":
        display, auth = find_x_display()
        if display:
            if not have("ffmpeg"):
                raise VideoError("ffmpeg is not installed on the board (sudo apt install ffmpeg)")
            env = {"DISPLAY": display}
            if auth:
                env["XAUTHORITY"] = auth
            return (base + ["-f", "x11grab", "-draw_mouse", "1", "-framerate", str(fps), "-i", display,
                            "-vf", scale, "-q:v", qv] + out, env, f"X11 screen {display}")
        if find_wayland():
            return None, find_wayland(), "wayland"      # handled by the grim loop
        raise VideoError("no desktop session found on the board (X11 or Wayland)")
    if src == "camera":
        if have("rpicam-vid") and os.path.exists("/dev/media0") and not find_camera():
            # Raspberry Pi camera module (CSI) through libcamera.
            return (["rpicam-vid", "-n", "-t", "0", "--codec", "mjpeg", "--width", str(width),
                     "--height", str(width * 9 // 16), "--framerate", str(fps),
                     "--quality", str(quality), "-o", "-"], {}, "Pi camera (rpicam-vid)")
        dev = find_camera()
        if not dev:
            raise VideoError("no camera found on the board")
        if not have("ffmpeg"):
            raise VideoError("ffmpeg is not installed on the board (sudo apt install ffmpeg)")
        # Ask USB webcams for their own MJPEG mode first (much cheaper for the camera's
        # USB bandwidth at higher resolutions); fall back to raw frames if it has none.
        fmt = ["-input_format", "mjpeg"] if camera_mjpeg else []
        return (base + ["-f", "v4l2"] + fmt + ["-framerate", str(fps),
                        "-video_size", f"{width}x{width * 9 // 16}", "-i", dev,
                        "-vf", scale, "-q:v", qv] + out, {}, f"camera {dev}")
    raise VideoError(f"unknown video source {src!r}")


async def mjpeg_frames(argv, env):
    """Run argv and yield complete JPEG frames from its stdout."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env})
    buf = b""
    try:
        while True:
            chunk = await proc.stdout.read(1 << 16)
            if not chunk:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                raise VideoError(err.splitlines()[-1] if err else f"{argv[0]} exited")
            buf += chunk
            while True:
                start = buf.find(SOI)
                if start < 0:
                    buf = b""
                    break
                end = buf.find(EOI, start + 2)
                if end < 0:
                    buf = buf[start:]
                    break
                yield buf[start:end + 2]
                buf = buf[end + 2:]
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def grim_frames(env, width, fps, quality):
    """Wayland fallback: one screenshot per frame with grim (slower than X11 capture)."""
    if not have("grim"):
        raise VideoError("Wayland desktop: install grim (sudo apt install grim) or switch the board to X11")
    scale = 1.0
    while True:
        t = asyncio.get_running_loop().time()
        proc = await asyncio.create_subprocess_exec(
            "grim", "-t", "jpeg", "-q", str(quality), "-s", f"{scale:.3f}", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, **env})
        data, err = await proc.communicate()
        if proc.returncode != 0 or not data.startswith(SOI):
            raise VideoError("grim failed: " + err.decode(errors="replace").strip()[-200:])
        size = jpeg_size(data)
        if size and size[0] > width + 8:
            scale = scale * width / size[0]       # shrink from the next frame on
        yield data
        await asyncio.sleep(max(0, 1 / fps - (asyncio.get_running_loop().time() - t)))


async def stream_video(hello, reader, writer):
    src = str(hello.get("src", "screen"))
    width = max(320, min(1920, int(hello.get("w", 1280))))
    fps = max(1, min(30, int(hello.get("fps", 15))))
    quality = max(20, min(95, int(hello.get("q", 70))))

    def send(kind, payload):
        writer.write(kind + len(payload).to_bytes(4, "big") + payload)

    try:
        argv, env, what = video_command(src, width, fps, quality)
    except VideoError as exc:
        writer.write((json.dumps({"t": "err", "e": str(exc)}) + "\n").encode())
        await writer.drain()
        return
    writer.write((json.dumps({"t": "ok", "host": socket.gethostname(), "src": src, "via": what}) + "\n").encode())
    await writer.drain()
    log.info("video: streaming %s at up to %dpx/%dfps", what, width, fps)

    inflight = 0
    acked = asyncio.Event()

    async def read_acks():
        nonlocal inflight
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionResetError("video client went away")
            inflight = max(0, inflight - 1)
            acked.set()

    acks = asyncio.create_task(read_acks())
    sent_any = False
    retry_raw = src == "camera"
    loop = asyncio.get_running_loop()
    try:
        while True:
            frames = mjpeg_frames(argv, env) if argv else grim_frames(env, width, fps, quality)
            vt, next_vt_check = active_vt(), loop.time() + 2
            try:
                switched = False
                async for frame in frames:
                    if acks.done():
                        return
                    if src == "screen" and loop.time() > next_vt_check:
                        next_vt_check = loop.time() + 2
                        if active_vt() != vt:
                            switched = True   # lock screen / unlock switched consoles: follow it
                            break
                    if inflight >= 2:
                        continue          # client is behind: drop this frame
                    inflight += 1
                    sent_any = True
                    send(b"J", frame)
                    await writer.drain()
                if not switched:
                    return
                argv, env, what = video_command(src, width, fps, quality)
                log.info("video: console changed, now capturing %s", what)
            except VideoError:
                if sent_any or not retry_raw:
                    raise
                retry_raw = False         # camera has no MJPEG mode: try raw frames
                argv, env, what = video_command(src, width, fps, quality, camera_mjpeg=False)
                log.info("video: retrying as %s without MJPEG input", what)
            finally:
                await frames.aclose()
    except VideoError as exc:
        message = str(exc)
        if src == "camera" and "busy" in message.lower():
            dev = find_camera()
            holders = device_users(dev) if dev else []
            message = (f"the camera is in use by {', '.join(holders)}; only one program can stream it at a time"
                       if holders else "the camera is in use by another program")
        log.warning("video: %s", message)
        send(b"E", message.encode())
        await writer.drain()
    finally:
        acks.cancel()


# --------------------------------------------------------------------------- server

async def serve(dev, token, bind, port):
    async def on_client(reader, writer):
        peer = writer.get_extra_info("peername")
        session = None

        def reply(obj):
            writer.write((json.dumps(obj) + "\n").encode())

        try:
            hello = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if hello.get("t") != "hello" or not hmac.compare_digest(
                    str(hello.get("token", "")).encode(), token.encode()):
                log.warning("rejected %s: bad token", peer)
                reply({"t": "err", "e": "bad token"})
                await writer.drain()
                return
            if hello.get("mode") == "video":
                log.info("video client connected: %s", peer)
                await stream_video(hello, reader, writer)
                return
            if dev is None:
                reply({"t": "err", "e": "keyboard/mouse unavailable on the board (agent not running as root?)"})
                await writer.drain()
                return
            session = Session(dev)
            reply({"t": "ok", "host": socket.gethostname(), "layout": current_layout(), "caps": ["pong"]})
            await writer.drain()
            writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log.info("input client connected: %s", peer)
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    if msg.get("t") == "p":
                        # Keepalive: echo it so the PC can measure the round trip and spot a dead link.
                        reply({"t": "p", "ts": msg.get("ts")})
                        continue
                    await session.handle(msg)
                except (ValueError, KeyError, TypeError) as exc:
                    log.debug("bad message %r: %s", line[:200], exc)
        except (asyncio.TimeoutError, ConnectionError, json.JSONDecodeError):
            pass
        finally:
            if session:
                session.release_all()
            log.info("client gone: %s", peer)
            writer.close()

    server = await asyncio.start_server(on_client, bind, port)
    log.info("listening on %s:%d", bind, port)
    async with server:
        await server.serve_forever()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = setting("TOKEN")
    if len(token) < 16:
        sys.exit("PORTHOLE_TOKEN is missing or too short (set it in /etc/porthole-agent.env)")
    try:
        dev = Devices()
    except Exception as exc:  # noqa: BLE001  (no /dev/uinput access: video still works)
        log.warning("keyboard/mouse disabled: %s", exc)
        dev = None
    asyncio.run(serve(dev, token, setting("BIND", "0.0.0.0"), int(setting("PORT", "7790"))))


if __name__ == "__main__":
    main()
