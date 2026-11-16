#!/usr/bin/env python3
"""Porthole input agent for small Linux boards (Arduino UNO Q, Raspberry Pi, ...).

Runs on the board. Accepts keyboard/mouse events from the PC app over
TCP (newline-delimited JSON) and injects them through two virtual /dev/uinput
devices: a keyboard and an absolute pointer (the same shape as QEMU's
usb-tablet, so the cursor lands exactly where you point on the PC).

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


async def serve(dev, token, bind, port):
    async def on_client(reader, writer):
        peer = writer.get_extra_info("peername")
        session = Session(dev)

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
            reply({"t": "ok", "host": socket.gethostname(), "layout": current_layout()})
            await writer.drain()
            log.info("client connected: %s", peer)
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    await session.handle(json.loads(line))
                except (ValueError, KeyError, TypeError) as exc:
                    log.debug("bad message %r: %s", line[:200], exc)
        except (asyncio.TimeoutError, ConnectionError, json.JSONDecodeError):
            pass
        finally:
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
    dev = Devices()
    asyncio.run(serve(dev, token, setting("BIND", "0.0.0.0"), int(setting("PORT", "7790"))))


if __name__ == "__main__":
    main()
