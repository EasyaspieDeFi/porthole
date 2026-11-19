#!/usr/bin/env python3
"""Porthole: use small Linux boards (Arduino UNO Q, Raspberry Pi, ...) like VM windows.

Video comes from a capture card (each board's HDMI out on its own input) or over the
network from the board's agent (its screen or a camera, no cable needed). Keyboard and
mouse go over the network to that agent, which injects them via uinput. You keep a list
of machines (name, host, video source) and switch between them.

  - Local window (pygame): click in it and type. Right Ctrl toggles full keyboard
    capture (Win key, Alt+Tab go to the board). Right Ctrl + M switches machine,
    Right Ctrl + I picks the capture input, Right Ctrl + F fullscreen,
    Right Ctrl + V types the PC clipboard, Right Ctrl + Q quits.
  - Porthole starts hidden in the tray. Ctrl+Alt+K (works while a game has focus) slides it
    out as a side panel on that monitor and puts it away again; Ctrl+Alt+1..9 jumps to a
    machine. Right Ctrl + F pops the panel out into a normal window. --window starts there.
  - Web UI on http://<this-pc>:8790 for phones, tablets and other computers
    (loopback and Tailscale addresses by default; see web_allow in config.json).

Run:  .venv\\Scripts\\python.exe porthole.py            (window + web)
      .venv\\Scripts\\python.exe porthole.py --no-window
      .venv\\Scripts\\python.exe porthole.py --install-agent pi@raspberrypi --name "Pi 3" --input 2
      .venv\\Scripts\\python.exe porthole.py --list-machines
"""
import argparse
import asyncio
import ipaddress
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from aiohttp import WSMsgType, web

from porthole_ui import C, UI, mix

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
NOSIGNAL_PATH = HERE / "nosignal.npy"   # fingerprint of the capture card's 'No Signal' card
log = logging.getLogger("porthole")

DEFAULTS = {
    "device_filter": "Cam Link",   # only consider capture inputs whose name contains this
    "device_index": None,          # remembered input
    "auto_input": True,            # find the input with a picture by itself
    "width": 1920,
    "height": 1080,
    "fps": 60,
    "agent_host": "",              # legacy single-machine setting (migrated into "machines")
    "agent_port": 7790,
    "agent_token": "",
    "web_bind": "0.0.0.0",
    "web_port": 8790,
    "web_max_fps": 30,
    # Video over the network (machines with "video": "screen" / "camera")
    "net_width": 960,
    "net_fps": 12,
    "net_quality": 60,
    # [{"name": "Pi 3", "host": "raspberrypi", "input": 2}, ...]; input = HDMI port, null = auto
    "machines": [],
    "machine": "",                 # the one currently shown
    # Side panel: a hotkey that works even while a game has focus slides Porthole out from
    # the edge of that monitor; <same modifiers>+1..9 jumps straight to machine 1..9.
    "panel_hotkey": "ctrl+alt+k",
    "panel_side": "right",         # "right" or "left"
    "panel_width": 0.32,           # fraction of the monitor's width
    "start_hidden": True,          # start in the tray (False: open the window)
    # Who may open the web UI. Loopback + Tailscale ranges by default.
    "web_allow": ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"],
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if not cfg["agent_token"]:
        cfg["agent_token"] = secrets.token_urlsafe(24)
        save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- capture

NET_SOURCES = {"screen": "WiFi screen", "camera": "WiFi camera", "test": "WiFi test pattern"}

def list_inputs(name_filter):
    """[(index, name)] of Media Foundation capture inputs matching the filter."""
    try:
        from cv2_enumerate_cameras import enumerate_cameras
        cams = [(c.index, c.name) for c in enumerate_cameras(cv2.CAP_MSMF)]
    except Exception as exc:  # noqa: BLE001
        log.warning("could not enumerate cameras (%s); assuming index 0", exc)
        cams = [(0, "camera 0")]
    hits = [c for c in cams if name_filter.lower() in c[1].lower()] or cams
    named = [(i, short_name(n)) for i, n in hits]
    return sorted(named, key=lambda c: (port_of(c[1]) or 99, c[0]))


def port_of(name):
    """'Cam Link Pro HDMI-4' -> 4."""
    m = re.search(r"HDMI-(\d+)", name)
    return int(m.group(1)) if m else None


def short_name(name):
    port = port_of(name)
    if port is None:
        return name
    return f"HDMI-{port}" + (" (Multiview)" if "multiview" in name.lower() else "")


def resolve_input(inputs, spec):
    """'4' / 4 / 'HDMI-4' -> capture index of HDMI-4."""
    spec = str(spec).strip()
    for idx, name in inputs:
        if spec.isdigit() and port_of(name) == int(spec):
            return idx
        if not spec.isdigit() and spec.lower() in name.lower():
            return idx
    return None


class Capture(threading.Thread):
    """Owns the capture card. Everyone else reads `latest()`."""

    def __init__(self, cfg):
        super().__init__(daemon=True, name="capture")
        self.cfg = cfg
        self.inputs = list_inputs(cfg["device_filter"])
        self.index = cfg["device_index"]
        self.cmd = None                    # ("select", idx) | ("scan",)
        self.cond = threading.Condition()
        self.frame = None
        self.seq = 0
        self.fps = 0.0
        self.status = "starting"
        self.nosignal = None               # thumbnail of the capture card's 'No Signal' card
        if NOSIGNAL_PATH.exists():
            try:
                self.nosignal = np.load(NOSIGNAL_PATH)
            except Exception:  # noqa: BLE001
                pass
        self._jpeg = {}                   # (seq, width, quality) -> bytes
        self.mode = "hdmi"                 # "hdmi" (capture card) or "net" (frames from the agent)
        self.net_src = None
        self._raw = (None, None)           # (seq, jpeg) of the last network frame
        self._net_frames = 0
        self.on_input = None               # callback(index, picked_by_hand)
        self.reserved = lambda: set()      # HDMI ports other machines use (auto-scan skips them)
        self._jpeg_lock = threading.Lock()

    # -- public API (any thread)
    def latest(self):
        with self.cond:
            return self.seq, self.frame

    def select(self, idx):
        self.cmd = ("select", int(idx))

    def select_port(self, port):
        idx = resolve_input(self.inputs, port)
        if idx is not None:
            self.select(idx)
        return idx

    def scan(self):
        self.cmd = ("scan",)

    def use_network(self, src):
        """Stop using the capture card; frames will arrive through push_jpeg()."""
        self.cmd = ("net", src)

    def push_jpeg(self, data):
        """A JPEG frame from the network (called from the backend thread)."""
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or self.mode != "net":
            return
        with self.cond:
            self.frame = frame
            self.seq += 1
            self._raw = (self.seq, data)
            self.cond.notify_all()
        self._net_frames += 1

    def _learn_nosignal(self, thumb):
        self.nosignal = thumb
        try:
            np.save(NOSIGNAL_PATH, thumb)
        except OSError:
            pass

    def input_name(self):
        if self.mode == "net":
            return NET_SOURCES.get(self.net_src, "WiFi")
        return dict(self.inputs).get(self.index, f"input {self.index}")

    def jpeg(self, width, quality):
        seq, frame = self.latest()
        if frame is None:
            return seq, None
        raw_seq, raw = self._raw
        if self.mode == "net" and raw_seq == seq and (not width or width >= frame.shape[1]):
            return seq, raw                # already a JPEG: pass it straight through
        key = (seq, width, quality)
        with self._jpeg_lock:
            if key in self._jpeg:
                return seq, self._jpeg[key]
        h, w = frame.shape[:2]
        if width and width < w:
            frame = cv2.resize(frame, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        data = buf.tobytes() if ok else None
        with self._jpeg_lock:
            self._jpeg = {k: v for k, v in self._jpeg.items() if k[0] == seq}
            self._jpeg[key] = data
        return seq, data

    # -- capture thread
    def _open(self, idx):
        cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg["height"])
        cap.set(cv2.CAP_PROP_FPS, self.cfg["fps"])
        return cap

    @staticmethod
    def _thumb(frame):
        return cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (48, 27),
                          interpolation=cv2.INTER_AREA).astype(np.float32)

    def _autodetect(self):
        """Pick the input whose picture differs from the others.

        With no source, every Cam Link input shows the same 'No Signal' card, so the
        odd one out is the one the board is plugged into.
        """
        self.status = "scanning inputs"
        thumbs = {}
        for idx, name in self.inputs:
            cap = self._open(idx)
            if cap is None:
                continue
            frame = None
            for _ in range(20):
                ok, f = cap.read()
                if ok:
                    frame = f
            cap.release()
            if frame is not None:
                thumbs[idx] = self._thumb(frame)
        if not thumbs:
            return self.inputs[0][0] if self.inputs else 0
        scores = {}
        for i, ti in thumbs.items():
            scores[i] = float(np.median([np.abs(ti - tj).mean() for j, tj in thumbs.items() if j != i] or [0]))
        names = dict(self.inputs)
        taken = self.reserved()
        candidates = {i: sc for i, sc in scores.items() if port_of(names.get(i, "")) not in taken} or scores
        best = max(candidates, key=candidates.get)
        log.info("input scan: %s", {names.get(i, i): round(sc, 1) for i, sc in scores.items()})
        if len(thumbs) > 1 and scores[best] > 6:
            others = [t for j, t in thumbs.items() if j != best]
            self._learn_nosignal(others[0])
            return best
        if len(thumbs) > 1:
            # Every input looks the same: that's the 'No Signal' card. Remember it so
            # we can rescan by ourselves once the board starts sending a picture.
            self._learn_nosignal(next(iter(thumbs.values())))
            log.info("no input has a signal yet; will keep checking")
        return self.index if self.index is not None else next(iter(thumbs))

    def _looks_like_nosignal(self, frame):
        return self.nosignal is not None and float(np.abs(self._thumb(frame) - self.nosignal).mean()) < 4

    def run(self):
        cap = None
        if self.mode != "net" and (self.index is None or self.cfg.get("auto_input", True)):
            self.cmd = ("scan",)
        frames, t0 = 0, time.time()
        nosignal_since = None
        while True:
            cmd, self.cmd = self.cmd, None
            if cmd:
                if cap:
                    cap.release()
                    cap = None
                with self.cond:
                    self.frame = None      # don't show the previous source while switching
                    self.seq += 1
                if cmd[0] == "net":
                    self.mode, self.net_src = "net", cmd[1]
                    self.status = "connecting"
                    log.info("using network video (%s)", cmd[1])
                    continue
                self.mode = "hdmi"
                if cmd[0] == "scan":
                    self.index = self._autodetect()
                    self.cfg["auto_input"] = True
                else:
                    self.index = cmd[1]
                    self.cfg["auto_input"] = False  # picked by hand: stop auto-switching
                self.cfg["device_index"] = self.index
                if self.on_input:
                    self.on_input(self.index, cmd[0] == "select")
                save_config(self.cfg)
                nosignal_since = None
                log.info("using capture input %s (%s)", self.index, self.input_name())
            if self.mode == "net":
                time.sleep(0.05)
                now = time.time()
                if now - t0 >= 1:
                    self.fps = self._net_frames / (now - t0)
                    self._net_frames, t0 = 0, now
                continue
            if cap is None:
                cap = self._open(self.index)
                if cap is None:
                    self.status = f"cannot open {self.input_name()}"
                    time.sleep(2)
                    continue
                self.status = "ok"
            ok, frame = cap.read()
            if not ok:
                self.status = "capture read failed, reopening"
                cap.release()
                cap = None
                time.sleep(1)
                continue
            with self.cond:
                self.frame = frame
                self.seq += 1
                self.cond.notify_all()
            frames += 1
            now = time.time()
            if now - t0 >= 1:
                self.fps = frames / (now - t0)
                frames, t0 = 0, now
                if self._looks_like_nosignal(frame):
                    self.status = "no signal"
                    nosignal_since = nosignal_since or now
                    if self.cfg.get("auto_input", True) and now - nosignal_since > 15:
                        self.cmd = ("scan",)
                else:
                    nosignal_since = None
                    if self.status == "no signal":
                        self.status = "ok"


# --------------------------------------------------------------------------- machines

class Machines:
    """Named boards: which capture input shows each one and which host gets its keyboard/mouse."""

    def __init__(self, cfg, cap):
        self.cfg, self.cap = cfg, cap
        self.agent = None
        self.netvideo = None
        ms = cfg.setdefault("machines", [])
        if not ms and cfg.get("agent_host"):          # migrate the old single-machine config
            port = None
            if not cfg.get("auto_input", True) and cfg.get("device_index") is not None:
                port = port_of(dict(cap.inputs).get(cfg["device_index"], ""))
            ms.append({"name": "Board", "host": cfg["agent_host"], "input": port})
            cfg["machine"] = "Board"
            save_config(cfg)
        if ms and not self.get(cfg.get("machine")):
            cfg["machine"] = ms[0]["name"]
        cap.on_input = self._input_changed
        cap.reserved = self.reserved_ports
        # Start on the current machine's video source.
        m = self.current
        idx = resolve_input(cap.inputs, m["input"]) if m.get("input") else None
        if self.video_of(m) != "hdmi":
            cap.mode, cap.net_src = "net", self.video_of(m)
            cap.status = "connecting"
        elif idx is not None:
            cap.index = idx
            cfg["auto_input"] = False
        elif m["name"]:
            cfg["auto_input"] = True

    def get(self, name):
        return next((m for m in self.cfg["machines"] if m["name"] == name), None)

    @property
    def current(self):
        return self.get(self.cfg.get("machine")) or {"name": "", "host": "", "input": None}

    @property
    def name(self):
        return self.current["name"] or "Porthole"

    def names(self):
        return [m["name"] for m in self.cfg["machines"]]

    @staticmethod
    def video_of(m):
        return m.get("video") or "hdmi"

    def net_target(self, m):
        v = self.video_of(m)
        return (m.get("host", ""), self.port_for(m), v) if v != "hdmi" and m.get("host") else None

    def port_for(self, m):
        return int(m.get("port") or self.cfg["agent_port"])

    def reserved_ports(self):
        cur = self.cfg.get("machine")
        return {m.get("input") for m in self.cfg["machines"] if m["name"] != cur and m.get("input")}

    def _input_changed(self, idx, by_hand):
        """The capture card input was picked (by hand or by scanning): this machine is on HDMI."""
        m = self.get(self.cfg.get("machine"))
        if m is not None:
            m["input"] = port_of(dict(self.cap.inputs).get(idx, "")) if by_hand else None
            m["video"] = "hdmi"
        if self.netvideo:
            self.netvideo.set_target(None)

    def set_video(self, src):
        """Show the current machine over the network ('screen', 'camera', 'test')."""
        m = self.get(self.cfg.get("machine"))
        if m is None or src not in NET_SOURCES:
            return False
        m["video"] = src
        save_config(self.cfg)
        self._apply_video(m)
        return True

    def _apply_video(self, m):
        if self.video_of(m) != "hdmi":
            self.cap.use_network(self.video_of(m))
            if self.netvideo:
                self.netvideo.set_target(self.net_target(m))
            if not m.get("host"):
                self.cap.status = "this machine has no host for network video"
            return
        if self.netvideo:
            self.netvideo.set_target(None)
        if not (m.get("input") and self.cap.select_port(m["input"]) is not None):
            self.cap.scan()

    def switch(self, name):
        m = self.get(name)
        if m is None:
            return False
        if self.agent:
            self.agent.send({"t": "r"})          # let go of anything held on the old machine
        self.cfg["machine"] = name
        save_config(self.cfg)
        self._apply_video(m)
        if self.agent:
            self.agent.mouse = m.get("mouse", True)
            self.agent.retarget(m.get("host", ""), self.port_for(m))
        log.info("switched to %s", name)
        return True


# --------------------------------------------------------------------------- agent link

_SWITCH = object()   # queue sentinel: drop the current connection, a new host was chosen
_MOVE = object()     # queue sentinel: send the latest pointer position (AgentLink._move)


class AgentLink:
    """Persistent TCP link to the agent on the current machine. Thread-safe `send()`."""

    def __init__(self, cfg, loop, host, port):
        self.cfg = cfg
        self.loop = loop
        self.queue = asyncio.Queue(maxsize=2000)
        self.host, self.port = host, port
        self.gen = 0
        self._wake = asyncio.Event()
        self.state = "not configured" if not host else "connecting"
        self.remote_host = ""
        self.rtt_ms = None                 # round trip to the agent, from keepalive pings
        self.wifi = None                   # the board's own WiFi report (when it steers WiFi)
        self._move = None                  # latest pointer position not yet sent
        # True: Porthole drives this machine's pointer. False: something else does (e.g. Deskflow),
        # so send keys only. "clicks": something else does, but clicks and scrolling still go
        # through (the agent puts its pointer on the spot for each one and straight back).
        self.mouse = True

    def retarget(self, host, port):
        """Point the link at another machine (any thread)."""
        self.loop.call_soon_threadsafe(self._retarget, host, port)

    def _retarget(self, host, port):
        self.host, self.port = host, port
        self.gen += 1
        self.remote_host = ""
        self.state = "connecting" if host else "not configured"
        self._wake.set()
        try:
            self.queue.put_nowait(_SWITCH)
        except asyncio.QueueFull:
            pass

    @property
    def connected(self):
        return self.state == "connected"

    def send(self, msg):
        """Call from any thread."""
        self.loop.call_soon_threadsafe(self._put, msg)

    def _put(self, msg):
        if not self.connected:
            return
        if self.mouse is False and msg.get("t") in ("m", "b", "w"):
            return
        if msg.get("t") == "m":
            # Only the newest pointer position matters. Keep one slot instead of queueing
            # every move, so a laggy link doesn't replay seconds of stale motion afterwards.
            first = self._move is None
            self._move = msg
            if not first:
                return
            msg = _MOVE
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    @property
    def status_text(self):
        if self.connected and self.rtt_ms is not None and self.rtt_ms >= 150:
            return f"connected, slow link ({self.rtt_ms} ms)"
        return self.state

    async def _pause(self, gen, seconds):
        """Sleep, but wake early if the target machine changes."""
        if gen != self.gen:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _pump(self, gen, reader, writer, has_pong):
        """Send queued input until the machine changes or the link dies.

        Sends a ping every 2 s. Agents that answer (has_pong) let us measure the round trip
        and notice a dead link within seconds instead of waiting for TCP to give up.
        """
        loop = asyncio.get_running_loop()
        last_heard = loop.time()
        move_ts = None     # ping sent right after a pointer move; further moves wait for its echo

        async def read_pongs():
            nonlocal last_heard, move_ts
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionResetError("agent closed the connection")
                last_heard = loop.time()
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("t") == "p" and msg.get("ts") is not None:
                    self.rtt_ms = round((loop.time() - float(msg["ts"])) * 1000)
                    self.wifi = msg.get("wifi")
                    if msg["ts"] == move_ts:
                        # The board has everything up to the last move: send the newest position.
                        move_ts = None
                        if self._move is not None:
                            self.queue.put_nowait(_MOVE)

        def dump(msg):
            return json.dumps(msg, separators=(",", ":"))

        pongs = asyncio.create_task(read_pongs()) if has_pong else None
        try:
            while gen == self.gen:
                try:
                    first = await asyncio.wait_for(self.queue.get(), 2)
                except asyncio.TimeoutError:
                    first = {"t": "p", "ts": loop.time()}   # keepalive / latency probe
                batch = [first]
                while not self.queue.empty():               # send everything waiting in one go
                    batch.append(self.queue.get_nowait())
                lines, sent_move = [], False
                for msg in batch:
                    if msg is _SWITCH:
                        return
                    if msg is _MOVE:
                        if self._move is None or (has_pong and move_ts is not None):
                            continue                        # wait for the board to catch up
                        lines.append(dump(self._move))
                        self._move, sent_move = None, True
                        continue
                    if msg.get("t") != "p" and self._move is not None:
                        # A click or key goes out now; deliver it at the newest pointer position.
                        lines.append(dump(self._move))
                        self._move, sent_move = None, True
                    lines.append(dump(msg))
                if sent_move and has_pong:
                    move_ts = loop.time()
                    lines.append(dump({"t": "p", "ts": move_ts}))
                if lines:
                    writer.write(("\n".join(lines) + "\n").encode())
                    await asyncio.wait_for(writer.drain(), 5)
                if pongs:
                    if pongs.done():
                        pongs.result()                      # re-raise why the reader stopped
                    if loop.time() - last_heard > 8:
                        raise asyncio.TimeoutError("agent stopped answering")
        finally:
            if pongs:
                pongs.cancel()

    async def run(self):
        while True:
            gen, host, port = self.gen, self.host, self.port
            if not host:
                self.state = "not configured"
                await self._pause(gen, 3600)
                continue
            writer = None
            try:
                self.state = "connecting"
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
                if gen != self.gen:
                    continue
                sock = writer.get_extra_info("socket")
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                hello = {"t": "hello", "token": self.cfg["agent_token"]}
                if self.mouse == "clicks":
                    hello["pointer"] = "clicks"
                writer.write((json.dumps(hello) + "\n").encode())
                await writer.drain()
                reply = json.loads(await asyncio.wait_for(reader.readline(), 5) or b"{}")
                if gen != self.gen:
                    continue
                if reply.get("t") != "ok":
                    self.state = f"agent refused: {reply.get('e', 'no reply')}"
                    writer.close()
                    await self._pause(gen, 5)
                    continue
                self.remote_host = reply.get("host", host)
                while not self.queue.empty():
                    self.queue.get_nowait()
                self._move = None
                self.rtt_ms = None
                self.wifi = None
                self.state = "connected"
                log.info("input agent connected (%s)", self.remote_host)
                await self._pump(gen, reader, writer, "pong" in reply.get("caps", []))
                continue
            except (OSError, asyncio.TimeoutError, ValueError) as exc:
                if gen != self.gen:
                    continue
                if self.connected:
                    log.info("input agent link lost: %s", exc)
                if isinstance(exc, ConnectionRefusedError):
                    self.state = f"not running on {host} (install the agent)"
                elif isinstance(exc, (asyncio.TimeoutError, socket.gaierror)):
                    self.state = f"can't reach {host}"
                else:
                    self.state = f"offline ({type(exc).__name__})"
            finally:
                if writer:
                    writer.close()
            await self._pause(gen, 2)


# --------------------------------------------------------------------------- network video

class NetVideo:
    """Pulls JPEG frames of a board's screen or camera from its agent and feeds Capture."""

    def __init__(self, cfg, loop, cap):
        self.cfg, self.loop, self.cap = cfg, loop, cap
        self.target = None                 # (host, port, src) or None
        self.gen = 0
        self._wake = asyncio.Event()

    def set_target(self, target):
        """Any thread."""
        self.loop.call_soon_threadsafe(self._set, target)

    def _set(self, target):
        if target != self.target:
            self.target = target
            self.gen += 1
            self._wake.set()

    async def _pause(self, gen, seconds):
        if gen != self.gen:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def _status(self, gen, text):
        if gen == self.gen and self.cap.mode == "net":
            self.cap.status = text

    async def run(self):
        while True:
            gen, target = self.gen, self.target
            if not target:
                await self._pause(gen, 3600)
                continue
            host, port, src = target
            writer = None
            try:
                self._status(gen, f"connecting to {host}")
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
                writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                hello = {"t": "hello", "token": self.cfg["agent_token"], "mode": "video", "src": src,
                         "w": self.cfg["net_width"], "fps": self.cfg["net_fps"], "q": self.cfg["net_quality"]}
                writer.write((json.dumps(hello) + "\n").encode())
                await writer.drain()
                reply = json.loads(await asyncio.wait_for(reader.readline(), 10) or b"{}")
                if reply.get("t") != "ok":
                    self._status(gen, f"{host}: {reply.get('e', 'agent does not support network video')}")
                    await self._pause(gen, 10)
                    continue
                log.info("network video from %s: %s", host, reply.get("via", src))
                self._status(gen, "waiting for first frame")
                while gen == self.gen:
                    kind = await asyncio.wait_for(reader.readexactly(1), 15)
                    size = int.from_bytes(await reader.readexactly(4), "big")
                    if size > 20 << 20:
                        raise ValueError("bad frame size")
                    data = await reader.readexactly(size)
                    if kind == b"J":
                        writer.write(b"a\n")
                        self.cap.push_jpeg(data)
                        self._status(gen, "ok")
                    elif kind == b"E":
                        self._status(gen, f"{host}: {data.decode(errors='replace')}")
                        await self._pause(gen, 10)
                        break
            except asyncio.TimeoutError:
                self._status(gen, f"can't reach {host}" if writer is None else f"{host} stopped sending video")
            except ConnectionRefusedError:
                self._status(gen, f"agent not running on {host}")
            except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
                self._status(gen, f"{host}: {type(exc).__name__}")
            finally:
                if writer:
                    writer.close()
            await self._pause(gen, 2)


# --------------------------------------------------------------------------- web

ALLOWED_TYPES = {"k", "m", "b", "w", "r", "type"}


def build_web(cfg, cap, agent, machines):
    nets = [ipaddress.ip_network(n) for n in cfg["web_allow"]]

    @web.middleware
    async def only_allowed(request, handler):
        try:
            ip = ipaddress.ip_address((request.remote or "").split("%")[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            allowed = any(ip in n for n in nets)
        except ValueError:
            allowed = False
        if not allowed:
            return web.Response(status=403, text="not allowed from this network")
        return await handler(request)

    app = web.Application(middlewares=[only_allowed])

    async def index(_):
        return web.FileResponse(HERE / "web" / "index.html", headers={"Cache-Control": "no-store"})

    def status():
        return {"t": "status", "agent": agent.status_text, "agent_ok": agent.connected, "agent_rtt": agent.rtt_ms, "board_wifi": agent.wifi, "agent_host": agent.remote_host or agent.host,
                "machine": machines.current["name"], "machines": machines.names(),
                "fps": round(cap.fps, 1), "video": cap.status, "input": cap.index,
                "video_src": cap.net_src if cap.mode == "net" else "hdmi",
                "auto": bool(cfg.get("auto_input", True)),
                "inputs": cap.inputs}

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        settings = {"w": 1280, "q": 70}
        # Flow control: at most 2 frames un-acked by the browser, so a slow link
        # drops frames instead of building up seconds of lag.
        flow = {"inflight": 0}
        acked = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def pump_frames():
            last = -1
            min_dt = 1 / max(1, cfg["web_max_fps"])
            while not ws.closed:
                t = loop.time()
                seq, _ = cap.latest()
                if seq == last:
                    await asyncio.sleep(0.004)
                    continue
                while flow["inflight"] >= 2:
                    acked.clear()
                    try:
                        await asyncio.wait_for(acked.wait(), 3)
                    except asyncio.TimeoutError:
                        flow["inflight"] = 0  # lost acks; don't stall forever
                seq, data = await loop.run_in_executor(None, cap.jpeg, settings["w"], settings["q"])
                last = seq
                if data is None:
                    continue
                flow["inflight"] += 1
                await ws.send_bytes(data)
                await asyncio.sleep(max(0, min_dt - (loop.time() - t)))

        async def pump_status():
            while not ws.closed:
                await ws.send_json(status())
                await asyncio.sleep(1)

        tasks = [asyncio.create_task(pump_frames()), asyncio.create_task(pump_status())]
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                t = data.get("t")
                if t == "ack":
                    flow["inflight"] = max(0, flow["inflight"] - 1)
                    acked.set()
                elif t == "cfg":
                    settings["w"] = max(320, min(3840, int(data.get("w", 1280))))
                    settings["q"] = max(20, min(95, int(data.get("q", 70))))
                elif t == "sel":
                    cap.select(int(data["i"]))
                elif t == "scan":
                    cap.scan()
                elif t == "machine":
                    machines.switch(str(data.get("name", "")))
                elif t == "net":
                    machines.set_video(str(data.get("src", "")))
                elif t in ALLOWED_TYPES:
                    agent.send(data)
        finally:
            for task in tasks:
                task.cancel()
            agent.send({"t": "r"})
        return ws

    async def snapshot(_):
        _, data = await asyncio.get_running_loop().run_in_executor(None, cap.jpeg, 0, 90)
        if data is None:
            return web.Response(status=503, text="no frame yet")
        return web.Response(body=data, content_type="image/jpeg")

    async def status_json(_):
        return web.json_response(status())

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/snapshot.jpg", snapshot)
    app.router.add_get("/status", status_json)
    return app


def start_backend(cfg, cap, machines):
    """Run asyncio (agent link + web server) on a background thread."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def runner():
        asyncio.set_event_loop(loop)
        m = machines.current
        agent = AgentLink(cfg, loop, m.get("host", ""), machines.port_for(m))
        agent.mouse = m.get("mouse", True)
        machines.agent = agent
        netvideo = NetVideo(cfg, loop, cap)
        machines.netvideo = netvideo
        netvideo.target = machines.net_target(m)
        holder["agent"] = agent
        app = build_web(cfg, cap, agent, machines)

        async def main():
            runner_ = web.AppRunner(app, access_log=None)
            await runner_.setup()
            site = web.TCPSite(runner_, cfg["web_bind"], cfg["web_port"])
            await site.start()
            log.info("web UI on http://%s:%d", socket.gethostname().lower(), cfg["web_port"])
            ready.set()
            await asyncio.gather(agent.run(), netvideo.run())

        loop.run_until_complete(main())

    threading.Thread(target=runner, daemon=True, name="backend").start()
    ready.wait(10)
    return holder["agent"]


# --------------------------------------------------------------------------- local window

# SDL scancode (= USB HID usage) -> W3C KeyboardEvent.code
SDL_TO_CODE = {i + 4: "Key" + c for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
SDL_TO_CODE.update({30 + i: f"Digit{d}" for i, d in enumerate("1234567890")})
SDL_TO_CODE.update({58 + i: f"F{i + 1}" for i in range(12)})
SDL_TO_CODE.update({104 + i: f"F{i + 13}" for i in range(12)})
SDL_TO_CODE.update({89 + i: f"Numpad{d}" for i, d in enumerate("1234567890")})
SDL_TO_CODE.update({
    40: "Enter", 41: "Escape", 42: "Backspace", 43: "Tab", 44: "Space", 45: "Minus",
    46: "Equal", 47: "BracketLeft", 48: "BracketRight", 49: "Backslash", 50: "IntlHash",
    51: "Semicolon", 52: "Quote", 53: "Backquote", 54: "Comma", 55: "Period", 56: "Slash",
    57: "CapsLock", 70: "PrintScreen", 71: "ScrollLock", 72: "Pause", 73: "Insert",
    74: "Home", 75: "PageUp", 76: "Delete", 77: "End", 78: "PageDown", 79: "ArrowRight",
    80: "ArrowLeft", 81: "ArrowDown", 82: "ArrowUp", 83: "NumLock", 84: "NumpadDivide",
    85: "NumpadMultiply", 86: "NumpadSubtract", 87: "NumpadAdd", 88: "NumpadEnter",
    99: "NumpadDecimal", 100: "IntlBackslash", 101: "ContextMenu", 103: "NumpadEqual",
    127: "AudioVolumeMute", 128: "AudioVolumeUp", 129: "AudioVolumeDown",
    135: "IntlRo", 137: "IntlYen",
    224: "ControlLeft", 225: "ShiftLeft", 226: "AltLeft", 227: "MetaLeft",
    229: "ShiftRight", 230: "AltRight", 231: "MetaRight",
    # 228 (Right Ctrl) is the host key and is never forwarded.
})
HOST_KEY = 228


def keycode_fallback(pygame, key):
    """Code for keys that arrive without a hardware scancode (SendKeys, RDP, on-screen keyboards)."""
    if pygame.K_a <= key <= pygame.K_z:
        return "Key" + chr(key).upper()
    if pygame.K_0 <= key <= pygame.K_9:
        return "Digit" + chr(key)
    named = {
        "RETURN": "Enter", "ESCAPE": "Escape", "BACKSPACE": "Backspace", "TAB": "Tab",
        "SPACE": "Space", "MINUS": "Minus", "EQUALS": "Equal", "LEFTBRACKET": "BracketLeft",
        "RIGHTBRACKET": "BracketRight", "BACKSLASH": "Backslash", "SEMICOLON": "Semicolon",
        "QUOTE": "Quote", "BACKQUOTE": "Backquote", "COMMA": "Comma", "PERIOD": "Period",
        "SLASH": "Slash", "DELETE": "Delete", "HOME": "Home", "END": "End", "PAGEUP": "PageUp",
        "PAGEDOWN": "PageDown", "INSERT": "Insert", "UP": "ArrowUp", "DOWN": "ArrowDown",
        "LEFT": "ArrowLeft", "RIGHT": "ArrowRight", "LSHIFT": "ShiftLeft", "RSHIFT": "ShiftRight",
        "LCTRL": "ControlLeft", "LALT": "AltLeft", "RALT": "AltRight", "LGUI": "MetaLeft",
        "RGUI": "MetaRight", "CAPSLOCK": "CapsLock",
    }
    named.update({f"F{i}": f"F{i}" for i in range(1, 13)})
    for name, code in named.items():
        if key == getattr(pygame, "K_" + name, None):
            return code
    return None


# --------------------------------------------------------------------------- side panel (Windows)

HOTKEY_MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
# key name in panel_hotkey -> (Windows virtual-key code, SDL key name)
HOTKEY_KEYS = {"space": (0x20, "Space"), "scrolllock": (0x91, "ScrollLock"), "pause": (0x13, "Pause"),
               "insert": (0x2D, "Insert"), "home": (0x24, "Home"), "end": (0x23, "End"),
               "pageup": (0x21, "PageUp"), "pagedown": (0x22, "PageDown"), "tab": (0x09, "Tab"),
               "backquote": (0xC0, "`"), "`": (0xC0, "`")}
HOTKEY_KEYS.update({c.lower(): (ord(c), c.lower()) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"})
HOTKEY_KEYS.update({f"f{n}": (0x6F + n, f"F{n}") for n in range(1, 25)})
WM_HOTKEY, MOD_NOREPEAT = 0x0312, 0x4000


def parse_hotkey(spec):
    """'ctrl+alt+k' -> (modifier bits, virtual-key code, SDL key name, label), or None."""
    parts = [p for p in str(spec).lower().replace(" ", "").split("+") if p]
    if not parts or parts[-1] not in HOTKEY_KEYS or any(p not in HOTKEY_MODS for p in parts[:-1]):
        return None
    mods = 0
    for p in parts[:-1]:
        mods |= HOTKEY_MODS[p]
    vk, sdl_name = HOTKEY_KEYS[parts[-1]]
    return mods, vk, sdl_name, "+".join(p.capitalize() for p in parts)


class GlobalHotkeys(threading.Thread):
    """System-wide hotkeys through RegisterHotKey, so they work while a game has focus.

    Each press posts a pygame event carrying the binding's payload plus `fg`, the window
    that had focus (the game), so the panel can hand focus back to it when it closes.
    """

    WM_REBIND = 0x8005                    # WM_APP + 5: re-register (hotkeys belong to the thread)

    def __init__(self, pygame, event_type, bindings):
        super().__init__(daemon=True, name="hotkeys")
        self.pygame, self.event_type, self.bindings = pygame, event_type, bindings   # [(mods, vk, payload)]
        self.failed = []
        self.ready = threading.Event()
        self._tid = None
        self._pending = None
        self._rebound = threading.Event()

    def _register(self, user32):
        self.failed = []
        for n, (mods, vk, payload) in enumerate(self.bindings, 1):
            if not user32.RegisterHotKey(None, n, mods | MOD_NOREPEAT, vk):
                self.failed.append(payload)

    def rebind(self, bindings, timeout=2.0):
        """Swap the hotkeys while running (from any thread). Returns the payloads that failed."""
        import ctypes
        if self._tid is None:
            return [b[2] for b in bindings]
        self._pending = bindings
        self._rebound.clear()
        ctypes.windll.user32.PostThreadMessageW(self._tid, self.WM_REBIND, 0, 0)
        self._rebound.wait(timeout)
        return list(self.failed)

    def run(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)          # make sure the queue exists
        self._register(user32)
        self.ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_REBIND and self._pending is not None:
                for n in range(1, len(self.bindings) + 1):
                    user32.UnregisterHotKey(None, n)
                self.bindings, self._pending = self._pending, None
                self._register(user32)
                self._rebound.set()
                continue
            if msg.message != WM_HOTKEY or not 1 <= msg.wParam <= len(self.bindings):
                continue
            fg = user32.GetForegroundWindow()
            # This process just received the hotkey, so it may hand out focus: let the
            # window thread take it.
            user32.AllowSetForegroundWindow(wintypes.DWORD(0xFFFFFFFF))     # ASFW_ANY
            self.pygame.event.post(self.pygame.event.Event(self.event_type, self.bindings[msg.wParam - 1][2],
                                                           fg=fg or 0))


# --------------------------------------------------------------------------- app icon

ICON_PATH = HERE / "porthole.ico"
COVER_PATH = HERE / "porthole.png"      # tile art for the Xbox app library


def porthole_icon_rgba(size):
    """Porthole's icon, drawn in code: a round ship's window with bolts and a glint. RGBA, size x size."""
    ss = 4                                                          # supersample for smooth edges
    n = size * ss
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) + 0.5
    c = n / 2
    d = np.hypot(xx - c, yy - c) / c                                # 0 at the centre, 1 at the edge
    img = np.zeros((n, n, 4), np.float32)

    def paint(mask, rgb):
        img[mask] = (*rgb, 255)

    paint(d <= 0.98, (40, 64, 168))                                 # outer rim (dark blue)
    paint(d <= 0.90, (72, 110, 232))                                # rim face (Porthole blue)
    ang = np.arctan2(yy - c, xx - c)
    for k in range(8):                                              # 8 bolts around the rim
        a = k * np.pi / 4
        bx, by = c + np.cos(a) * 0.80 * c, c + np.sin(a) * 0.80 * c
        paint(np.hypot(xx - bx, yy - by) <= 0.065 * c, (205, 215, 245))
    paint(d <= 0.66, (28, 40, 92))                                  # inner lip
    glass = d <= 0.60
    t = np.clip((yy - (c - 0.6 * c)) / (1.2 * c), 0, 1)             # glass: sky-to-sea gradient
    img[glass] = np.stack([18 + 30 * t, 70 + 40 * t, 130 + 50 * (1 - t), np.full_like(t, 255)], -1)[glass]
    glint = glass & (np.abs(ang + 2.2) < 0.45) & (d > 0.36) & (d < 0.50)
    paint(glint, (235, 242, 255))
    img = img.reshape(size, ss, size, ss, 4).mean(axis=(1, 3))      # downsample: anti-aliased
    rgb, alpha = img[..., :3], img[..., 3:4]
    img[..., :3] = np.where(alpha > 0, rgb * 255 / np.maximum(alpha, 1e-6), 0)   # un-premultiply
    return np.clip(img, 0, 255).astype(np.uint8)


def porthole_icon_bits(size=32):
    """The icon as Windows icon-resource bytes: BITMAPINFOHEADER + BGRA pixels (bottom-up) + AND mask."""
    import struct
    bgra = porthole_icon_rgba(size)[..., [2, 1, 0, 3]]
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    mask = bytes(((size + 31) // 32) * 4 * size)                   # all-zero AND mask (alpha does the work)
    return header + bgra[::-1].tobytes() + mask


def set_dark_title_bar(hwnd):
    """Windows draws the title bar dark, to match the app (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    import ctypes
    try:
        on = ctypes.c_int(1)
        for attr in (20, 19):                                       # 19 on early Windows 10 builds
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(on), 4) == 0:
                break
        dark = ctypes.c_int(0x00231D1A)                             # COLORREF (BGR) of the header colour
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 35, ctypes.byref(dark), 4)  # caption
    except (OSError, AttributeError):
        pass


def set_floating_style(hwnd, on):
    """The side panel as a floating card: Windows 11's rounded corners and a thin border
    (DWMWA_WINDOW_CORNER_PREFERENCE / DWMWA_BORDER_COLOR). Off: Windows' defaults again.
    Older Windows ignores this and the panel stays square."""
    import ctypes
    try:
        corner = ctypes.c_int(2 if on else 0)                      # DWMWCP_ROUND / DEFAULT
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(corner), 4)
        border = ctypes.c_uint(0x003A302C if on else 0xFFFFFFFF)  # COLORREF of C.BORDER / DWMWA_COLOR_DEFAULT
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 34, ctypes.byref(border), 4)
    except (OSError, AttributeError):
        pass


def set_window_icons(hwnd):
    """Title-bar (small) and Alt+Tab/taskbar (big) icons, each drawn at the size Windows wants."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.CreateIconFromResourceEx.restype = wintypes.HICON
    u.CreateIconFromResourceEx.argtypes = [ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    try:
        dpi = u.GetDpiForWindow(wintypes.HWND(hwnd)) or 96
    except AttributeError:
        dpi = 96
    for which, base in ((0, 16), (1, 32)):                          # ICON_SMALL, ICON_BIG
        size = round(base * dpi / 96)
        bits = porthole_icon_bits(size)
        icon = u.CreateIconFromResourceEx(bits, len(bits), True, 0x00030000, size, size, 0)
        if icon:
            u.SendMessageW(hwnd, 0x0080, which, icon)                # WM_SETICON


def write_icon_file(path=ICON_PATH, sizes=(16, 24, 32, 48, 64, 256)):
    """porthole.ico with every size Windows asks for (shortcuts, Start menu, taskbar)."""
    import struct
    images = [porthole_icon_bits(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for s, data in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(out + b"".join(images))
    return path


def create_shortcuts():
    """Start menu and desktop shortcuts that start Porthole (hidden in the tray) with its icon."""
    pythonw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = pathlib.Path(sys.executable)
    icon = write_icon_file()
    made = []
    for folder in ("Programs", "Desktop"):
        ps = ("$s=(New-Object -ComObject WScript.Shell);"
              f"$d=[Environment]::GetFolderPath('{folder}');"
              "$l=$s.CreateShortcut((Join-Path $d 'Porthole.lnk'));"
              f"$l.TargetPath='{pythonw}';$l.Arguments='\"{HERE / 'porthole.py'}\"';"
              f"$l.WorkingDirectory='{HERE}';$l.IconLocation='{icon},0';"
              "$l.Description='Porthole: your boards and Macs in a window';$l.Save();"
              "Join-Path $d 'Porthole.lnk'")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        if out.returncode == 0:
            made.append(out.stdout.strip())
        else:
            print(f"Couldn't create the {folder} shortcut: {out.stderr.strip()[-300:]}")
    for path in made:
        print("Created", path)
    try:
        exe = build_launcher(pythonw, icon)
    except (OSError, RuntimeError) as exc:
        print(f"Couldn't build Porthole.exe, so it can't go on the Xbox controller bar: {exc}")
        return
    print("Created", exe)
    if pin_to_game_bar():
        print("Added Porthole to the Xbox controller bar (Xbox button, recent games). "
              "Porthole keeps it there while it runs; set \"game_bar\": false in config.json to take it off.")
    art = write_cover_art()
    print("\nTo also find it in the Xbox app's library: My Library, the + button, Browse, and add it as an app:")
    print(f"  Program:    {exe}")
    print(f"  Image:      {art}")
    print("(Either one starts Porthole with its side panel out, or pulls the panel out if it's already running.)")


# --------------------------------------------------------------------------- Xbox controller bar (Windows)

LAUNCHER_PATH = HERE / "Porthole.exe"
# Game Bar's list of recently played games. Its controller bar (Xbox button) shows the newest
# three and starts one by running its exe, so Porthole needs an exe of its own: Porthole.exe.
GAMEBAR_MRU = pathlib.Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\GameDVR\GameMRU\LocalMruGameList.json"))

LAUNCHER_CS = r'''
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

[assembly: System.Reflection.AssemblyTitle("Porthole")]
[assembly: System.Reflection.AssemblyProduct("Porthole")]

static class PortholeLauncher
{
    [DllImport("user32.dll")]
    static extern bool AllowSetForegroundWindow(uint pid);

    [STAThread]
    static int Main(string[] args)
    {
        var line = new StringBuilder("\"" + @"%SCRIPT%" + "\" --panel");
        foreach (var a in args)
            line.Append(" \"").Append(a.Replace("\"", "\\\"")).Append('"');
        var psi = new ProcessStartInfo(@"%PYTHONW%", line.ToString());
        psi.UseShellExecute = false;
        psi.WorkingDirectory = @"%HERE%";
        AllowSetForegroundWindow(0xFFFFFFFF);   // ASFW_ANY: let Porthole's panel take focus
        Process.Start(psi);
        return 0;
    }
}
'''


def build_launcher(pythonw, icon):
    """Porthole.exe: a tiny program with Porthole's icon that runs `pythonw porthole.py --panel`.

    Compiled with the C# compiler that ships with Windows (.NET Framework 4).
    """
    import tempfile
    csc = pathlib.Path(os.environ.get("WINDIR", r"C:\Windows")) / r"Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if not csc.exists():
        raise RuntimeError(f"the .NET Framework C# compiler isn't there ({csc})")
    source = (LAUNCHER_CS.replace("%SCRIPT%", str(HERE / "porthole.py").replace('"', '""'))
              .replace("%PYTHONW%", str(pythonw).replace('"', '""')).replace("%HERE%", str(HERE).replace('"', '""')))
    with tempfile.TemporaryDirectory() as tmp:
        cs = pathlib.Path(tmp) / "launcher.cs"
        cs.write_text(source, encoding="utf-8")
        out = subprocess.run([str(csc), "-nologo", "-target:winexe", f"-win32icon:{icon}",
                              f"-out:{LAUNCHER_PATH}", str(cs)], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError((out.stdout + out.stderr).strip()[-400:])
    return LAUNCHER_PATH


def _gamebar_time(entry):
    import calendar
    try:
        return calendar.timegm(time.strptime(entry.get("LastLaunched", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def pin_to_game_bar(pin=True):
    """Put Porthole first in Game Bar's recent games (pin=True) or take it out. True if the list changed."""
    try:
        data = json.loads(GAMEBAR_MRU.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if not pin:
            return False
        data = {"titles": []}
    titles = data.get("titles") or []
    exe = str(LAUNCHER_PATH)

    def ours(t):
        return t.get("Title") == "Porthole" or str(t.get("ExePath", "")).lower() == exe.lower()

    mine = [t for t in titles if ours(t)]
    others = [t for t in titles if not ours(t)]
    if not pin:
        if not mine:
            return False
        data["titles"] = others
    else:
        newest = max((_gamebar_time(t) for t in others), default=0)
        if (len(mine) == 1 and mine[0].get("ExePath") == exe and titles and ours(titles[0])
                and _gamebar_time(mine[0]) > newest):
            return False                                            # already first
        # Newer than every game in the list (and never in the future): the controller bar shows it first.
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(time.time(), newest + 1)))
        data["titles"] = [{"LastLaunched": when, "Title": "Porthole", "Aumid": "", "ExePath": exe}] + others
    tmp = GAMEBAR_MRU.with_name(GAMEBAR_MRU.name + ".porthole-tmp")
    GAMEBAR_MRU.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8-sig")
    os.replace(tmp, GAMEBAR_MRU)
    return True


def keep_on_game_bar(cfg):
    """While Porthole runs, keep it first on the Xbox controller bar (after --create-shortcut built
    Porthole.exe), so playing other games doesn't push it off. "game_bar": false takes it off."""
    pin = cfg.get("game_bar", True) is not False

    def loop():
        seen = None
        while True:
            try:
                stamp = GAMEBAR_MRU.stat().st_mtime if GAMEBAR_MRU.exists() else None
                if stamp != seen:                                   # Game Bar added a game (or first look)
                    if (LAUNCHER_PATH.exists() or not pin) and pin_to_game_bar(pin):
                        log.info("%s the Xbox controller bar", "Porthole is first on" if pin else "Took Porthole off")
                    seen = GAMEBAR_MRU.stat().st_mtime if GAMEBAR_MRU.exists() else None
            except (OSError, ValueError) as exc:
                log.warning("couldn't update the Xbox controller bar list: %s", exc)
            if not pin:
                return
            time.sleep(15)

    threading.Thread(target=loop, daemon=True, name="gamebar").start()


def write_cover_art(path=COVER_PATH, size=600):
    """porthole.png: the icon on a dark square, for the Xbox app library tile."""
    rgba = porthole_icon_rgba(round(size * 0.74)).astype(np.float32)
    art = np.empty((size, size, 3), np.float32)
    art[:] = (48, 22, 16)                                           # BGR: deep navy
    o = (size - rgba.shape[0]) // 2
    a = rgba[..., 3:4] / 255
    tile = art[o:o + rgba.shape[0], o:o + rgba.shape[1]]
    tile[:] = tile * (1 - a) + rgba[..., [2, 1, 0]] * a
    cv2.imwrite(str(path), np.clip(art, 0, 255).astype(np.uint8))
    return path


# --------------------------------------------------------------------------- one copy at a time (Windows)

INSTANCE_MUTEX = "Local\\Porthole.PC"   # held by the running Porthole (window or --no-window)
HANDOFF_MSG = "Porthole.Handoff"        # registered message a second launch sends to the running copy's tray
HANDOFF_ACTIONS = {1: "show", 2: "window"}
TRAY_CLASS = "PortholeTray"           # window class of the tray icon's hidden window
_instance_mutex = None


def claim_single_instance():
    """True if no other Porthole is running in this Windows session. Keeps the claim for our lifetime."""
    global _instance_mutex
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = k.CreateMutexW(None, False, INSTANCE_MUTEX)
    return ctypes.get_last_error() != 183                           # ERROR_ALREADY_EXISTS


def hand_off_to_running(action, wait_s=20):
    """Ask the Porthole that's already running to show its panel ("show") or window ("window").

    It may still be starting up (e.g. launched twice in a row), so look for its tray window for
    a while. False if it never showed up (it runs with --no-window, or has no tray icon).
    """
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.FindWindowW.restype = wintypes.HWND
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    msg = u.RegisterWindowMessageW(HANDOFF_MSG)
    code = {v: k for k, v in HANDOFF_ACTIONS.items()}[action]
    deadline = time.time() + wait_s
    while True:
        hwnd = u.FindWindowW(TRAY_CLASS, None)
        if hwnd:
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # We were just launched from the foreground (Xbox app, Start menu): pass that right on,
            # so the panel can take focus.
            u.AllowSetForegroundWindow(pid.value)
            if u.PostMessageW(hwnd, msg, code, 0):
                return True
        if time.time() > deadline:
            return False
        time.sleep(0.25)


class TrayIcon(threading.Thread):
    """Notification-area icon: left click shows the panel, right click opens a small menu.

    Posts pygame events with action "panel", "window" or "quit". Pure Win32 through ctypes.
    """
    WM_TRAY = 0x8001                       # WM_APP + 1
    MENU = [(1, "Show panel"), (2, "Open window"), (None, None), (3, "Quit Porthole")]
    ACTIONS = {1: "panel", 2: "window", 3: "quit"}

    def __init__(self, pygame, event_type, tooltip, hotkey_label=""):
        super().__init__(daemon=True, name="tray")
        self.pygame, self.event_type, self.tooltip, self.hotkey_label = pygame, event_type, tooltip, hotkey_label
        self.ok = False
        self.ready = threading.Event()
        self.hwnd = None

    def _nid(self, flags=0, title="", text=""):
        import ctypes
        from ctypes import wintypes

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                        ("hBalloonIcon", wintypes.HICON)]

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        nid.uCallbackMessage, nid.hIcon = self.WM_TRAY, self.icon
        nid.szTip = self.tooltip[:127]
        if title or text:
            nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = title[:63], text[:255], 0x1   # NIIF_INFO
        return nid

    def _shell(self, op, nid):
        import ctypes
        return ctypes.windll.shell32.Shell_NotifyIconW(op, ctypes.byref(nid))

    def notify(self, title, text):
        """Show a Windows notification from the tray icon (any thread)."""
        if self.ok:
            self._shell(1, self._nid(0x10, title, text))                    # NIM_MODIFY, NIF_INFO

    def remove(self):
        if self.ok:
            self._shell(2, self._nid())                                     # NIM_DELETE
            self.ok = False

    def _post(self, action, fg=0):
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)            # ASFW_ANY
        self.pygame.event.post(self.pygame.event.Event(self.event_type, {"action": action, "fg": fg or 0}))

    def _menu(self):
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        menu = u.CreatePopupMenu()
        for item_id, text in self.MENU:
            if item_id is None:
                u.AppendMenuW(menu, 0x800, 0, None)                         # MF_SEPARATOR
            else:
                label = text + (f"\t{self.hotkey_label}" if item_id == 1 and self.hotkey_label else "")
                u.AppendMenuW(menu, 0, item_id, label)
        u.SetMenuDefaultItem(menu, 1, 0)
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)                                    # so the menu closes on click-away
        chosen = u.TrackPopupMenu(menu, 0x100 | 0x80 | 0x20, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD|NONOTIFY|RIGHTBUTTON
        u.PostMessageW(self.hwnd, 0, 0, 0)
        u.DestroyMenu(menu)
        if chosen in self.ACTIONS:
            self._post(self.ACTIONS[chosen])

    def run(self):
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateIconFromResourceEx.restype = wintypes.HICON
        u.CreateIconFromResourceEx.argtypes = [ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.HWND, ctypes.c_void_p]
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        handoff = u.RegisterWindowMessageW(HANDOFF_MSG)
        u.GetForegroundWindow.restype = ctypes.c_void_p

        def proc(hwnd, msg, wparam, lparam):
            if msg == handoff and wparam in HANDOFF_ACTIONS:             # a second launch: show ourselves
                self._post(HANDOFF_ACTIONS[wparam], u.GetForegroundWindow())
                return 0
            if msg == self.WM_TRAY:
                if lparam == 0x202:                                         # WM_LBUTTONUP
                    self._post("panel")
                elif lparam == 0x205:                                       # WM_RBUTTONUP
                    self._menu()
                return 0
            if msg == taskbar_created and self.ok:                          # Explorer restarted
                self._shell(0, self._nid(0x7))
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)                                          # keep it alive

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        try:
            bits = porthole_icon_bits(32)
            self.icon = u.CreateIconFromResourceEx(bits, len(bits), True, 0x00030000, 32, 32, 0)
            cls = WNDCLASSW(lpfnWndProc=self._proc, hInstance=ctypes.windll.kernel32.GetModuleHandleW(None),
                            lpszClassName=TRAY_CLASS)
            u.RegisterClassW(ctypes.byref(cls))
            self.hwnd = u.CreateWindowExW(0, TRAY_CLASS, "Porthole", 0, 0, 0, 0, 0, None, None,
                                          cls.hInstance, None)
            self.ok = bool(self.hwnd and self._shell(0, self._nid(0x7)))    # NIM_ADD: MESSAGE|ICON|TIP
        except Exception as exc:  # noqa: BLE001  (no tray: Porthole falls back to showing its window)
            log.warning("tray icon unavailable: %s", exc)
        self.ready.set()
        if not self.ok:
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))


def win_monitor_rect(hwnd):
    """(left, top, right, bottom) of the monitor showing hwnd (or under the mouse), in physical pixels."""
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if hwnd:
        mon = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)            # MONITOR_DEFAULTTONEAREST
    else:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        mon = user32.MonitorFromPoint(pt, 2)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    r = info.rcMonitor
    return r.left, r.top, r.right, r.bottom


def win_focus(hwnd):
    """Bring hwnd to the front with keyboard focus, if it still exists."""
    import ctypes
    user32 = ctypes.windll.user32
    if hwnd and user32.IsWindow(ctypes.c_void_p(hwnd)):
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))


def read_clipboard():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.replace("\r\n", "\n").rstrip("\n")
    except Exception:  # noqa: BLE001
        return ""


PANEL_SLIDE_S = 0.2       # slide-in/out time
PANEL_WIDTHS = [(0.25, "Compact"), (0.32, "Standard"), (0.42, "Wide")]
HEADER_H, STATUS_H = 52, 30
DEVICE_KINDS = [("mac", "Mac", "laptop"), ("pi", "Raspberry Pi", "board"), ("linux", "Other Linux", "terminal")]
MOUSE_MODES = [(True, "Porthole controls it"), ("clicks", "Shared with a KVM"), (False, "Keyboard only")]


def machine_kind(m):
    """'mac' | 'pi' | 'linux' for a machine entry (older entries have no kind: guess from the name)."""
    kind = m.get("kind")
    if kind in ("mac", "pi", "linux"):
        return kind
    name = (m.get("name", "") + " " + m.get("host", "")).lower()
    if "mac" in name:
        return "mac"
    if "pi" in name.split() or "raspberry" in name:
        return "pi"
    return "linux"


def kind_icon(m):
    return {"mac": "laptop", "pi": "board", "linux": "terminal"}[machine_kind(m)]


class InputScanner(threading.Thread):
    """Grabs one frame from every capture input (in the background) for the Add device thumbnails."""

    def __init__(self, cap):
        super().__init__(daemon=True, name="input-scan")
        self.cap = cap
        self.results = {}                  # capture index -> (thumb RGB array or None, state)
        self.done = False

    def run(self):
        for idx, name in list(self.cap.inputs):
            if self.cap.mode != "net" and idx == self.cap.index:
                _, frame = self.cap.latest()
            else:
                frame = None
                vc = cv2.VideoCapture(idx, cv2.CAP_MSMF)
                if vc.isOpened():
                    vc.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                    vc.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                    t = time.time()
                    while time.time() - t < 1.6:
                        ok, fr = vc.read()
                        if ok:
                            frame = fr
                vc.release()
            if frame is None:
                self.results[idx] = (None, "unavailable")
                continue
            state = "no signal" if self.cap._looks_like_nosignal(frame) else "signal"
            thumb = cv2.cvtColor(cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
            self.results[idx] = (thumb, state)
        self.done = True


class PortholeWindow:
    """The local window, in one of three states: hidden (tray only), the side panel, or a
    normal window. The panel hotkey (or the tray icon) slides the panel out, and the same key
    slides it back in. Right Ctrl + F (or the header's window button) pops the panel out into a
    normal window; closing that window hides it again.

    Layout, top to bottom: header (devices, tools), the picture, a status bar. Dialogs
    (Settings, Add device) and menus draw over it and take all input while open.
    """

    def __init__(self, cfg, cap, agent, machines, start_in_window=False, start_panel=False):
        os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")   # sharp on scaled monitors
        import pygame
        self.pg = pygame
        self.cfg, self.cap, self.agent, self.machines = cfg, cap, agent, machines
        pygame.init()
        self.on_windows = sys.platform == "win32"
        self.PANEL_EVENT = pygame.event.custom_type()
        self.ui = UI()

        # Panel hotkey (system-wide, works while a game has focus) and the tray icon.
        self.hotkeys = None
        self.panel_key = parse_hotkey(cfg.get("panel_hotkey", "ctrl+alt+k")) if self.on_windows else None
        if self.panel_key:
            self.hotkeys = GlobalHotkeys(pygame, self.PANEL_EVENT, self._bindings(self.panel_key))
            self.hotkeys.start()
            self.hotkeys.ready.wait(2)
            if any(p["action"] == "toggle" for p in self.hotkeys.failed):
                log.warning("panel hotkey %s is taken by another app; change it in Settings", self.panel_key[3])
                self.panel_key = None
            else:
                log.info("side panel: %s (and %s+1..9 for a device)", self.panel_key[3],
                         self.panel_key[3].rsplit("+", 1)[0])
        self.tray = None
        if self.on_windows:
            label = self.panel_key[3] if self.panel_key else ""
            self.tray = TrayIcon(pygame, self.PANEL_EVENT, "Porthole" + (f" ({label})" if label else ""), label)
            self.tray.start()
            self.tray.ready.wait(3)
            if not self.tray.ok:
                self.tray = None

        hidden_by_default = cfg.get("start_hidden", True)
        start_hidden = (not start_in_window and (hidden_by_default or start_panel)
                        and bool(self.panel_key or self.tray))
        if start_panel and start_hidden:
            fg = 0
            if self.on_windows:
                import ctypes
                ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
                fg = ctypes.windll.user32.GetForegroundWindow() or 0
            pygame.event.post(pygame.event.Event(self.PANEL_EVENT, {"action": "show", "fg": fg}))
        if self.on_windows:
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Porthole.App")
            except (OSError, AttributeError):
                pass
        self.window = pygame.Window("Porthole", (1280, 800), resizable=True, hidden=start_hidden)
        icon_px = porthole_icon_rgba(64)
        self.window.set_icon(pygame.image.frombuffer(icon_px.tobytes(), (64, 64), "RGBA"))
        logo = porthole_icon_rgba(64)
        self.logo = pygame.image.frombuffer(logo.tobytes(), (64, 64), "RGBA").copy()
        if self.on_windows:
            set_window_icons(self.window.handle)
            set_dark_title_bar(self.window.handle)
        self.window.minimum_size = (640, 420)
        self.window_visible = not start_hidden
        self.normal_geom = None
        self.told_about_tray = False
        if start_hidden and not start_panel:
            hint = (f"Press {self.panel_key[3]} to pull it out" if self.panel_key
                    else "Click the tray icon to show it")
            log.info("Porthole is in the tray. %s.", hint)
            if self.tray:
                self.tray.notify("Porthole is running", f"{hint}, even in a game. Right-click the tray icon for more.")

        self.clock = pygame.time.Clock()
        self.panel = None                  # side panel geometry and slide state, None when closed
        self.held, self.keymap = set(), {}
        self.grabbed = self.fullscreen = False
        self.host_down = self.host_combo = False
        self.last_seq = -1
        self.video_rect = pygame.Rect(0, 0, 1, 1)
        self.frame_surf = None
        self.frame_key = None
        self.pending_move = None
        self.ui_pressing = False
        self.last_caption = ""
        self.last_dpi_check = 0.0
        self.running = True
        # Overlays: one menu and/or one dialog at a time.
        self.menu = None                   # {"kind", "anchor": Rect, "items": [...], "right": bool}
        self.dialog = None                 # {"kind": "settings"|"device"|"remove", ...}
        self.toasts = []                   # [(text, icon, color, until)]
        self.capture_hotkey = False        # Settings: waiting for a new shortcut
        if not cfg["machines"]:
            self.open_device_dialog(None)
        elif self.panel_key:
            self.toast(f"{self.panel_key[3]} shows and hides Porthole", "info")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _bindings(key):
        mods, vk, _, _ = key
        bindings = [(mods, vk, {"action": "toggle"})]
        if mods:                           # never steal the bare number keys
            bindings += [(mods, 0x31 + n, {"action": "machine", "index": n}) for n in range(9)]
        return bindings

    def toast(self, text, icon="info", color=None, seconds=3.5):
        self.toasts = [(text, icon, color or C.ACCENT, time.time() + seconds)]

    @property
    def modal(self):
        return self.dialog is not None or self.menu is not None

    def release_all(self):
        self.held.clear()
        self.agent.send({"t": "r"})

    def set_grab(self, on):
        self.grabbed = on
        self.window.grab_keyboard = on
        if on:
            self.toast(f"Keyboard captured: Win and Alt+Tab go to {self.machines.name}. Right Ctrl releases it",
                       "keyboard")
        else:
            self.toast("Keyboard released", "keyboard", C.TEXT_2, 2)

    def let_go(self):
        """Stop sending input before the window goes away."""
        self.release_all()
        if self.grabbed:
            self.set_grab(False)
        self.close_overlays()

    def close_overlays(self):
        self.menu = None
        if self.dialog is not None:
            self.dialog = None
            self.capture_hotkey = False
            self.ui.focus = None
            self.pg.key.stop_text_input()

    def choose_machine(self, name):
        self.held.clear()
        if name != self.machines.current["name"] and self.machines.switch(name):
            self.last_seq = -1
            self.toast(f"Switched to {name}", kind_icon(self.machines.current))

    def choose_port(self, port):
        if self.cap.select_port(port) is None:
            self.toast(f"There's no HDMI-{port} on the capture card", "warning", C.WARN)
        else:
            self.toast(f"{self.machines.name} is on HDMI-{port}", "video")

    def choose_auto(self):
        self.cap.scan()
        self.toast("Looking for the input with a picture…", "search")

    def choose_net(self, src):
        self.machines.set_video(src)
        self.toast(f"{self.machines.name}: {NET_SOURCES[src]}", "wifi")

    def paste(self):
        text = read_clipboard()
        if text:
            self.agent.send({"t": "type", "s": text})
            self.toast(f"Typing {len(text)} characters from the clipboard", "paste")
        else:
            self.toast("The clipboard has no text", "paste", C.TEXT_2)

    def bring_forward(self):
        win_focus(self.window.handle)
        self.window.focus()

    def side(self):
        return "left" if self.cfg.get("panel_side") == "left" else "right"

    # ------------------------------------------------------------------ panel / window states
    def panel_geometry(self, monitor):
        left, top, right, bottom = monitor
        mw, mh = right - left, bottom - top
        try:
            frac = float(self.cfg.get("panel_width", 0.32))
        except (TypeError, ValueError):
            frac = 0.32
        gap = self.panel_gap()
        w = max(min(self.ui.px(560), mw - gap * 2), min(mw - gap * 2, round(mw * frac)))
        chrome = self.ui.px(HEADER_H + STATUS_H)
        h = min(mh - gap * 2, chrome + round(w * 9 / 16))
        return w, h, top + (mh - h) // 2

    def panel_gap(self):
        """The floating panel's distance from the screen edge."""
        return self.ui.px(12)

    def panel_open(self, fg):
        """Slide the panel out on the monitor of window `fg` (the game), or the one under the mouse."""
        if self.panel:                                  # it was closing: turn around
            self.panel.update(state="opening", t0=time.time(), start=self.panel["w"])
            self.bring_forward()
            return
        game = fg if fg and fg != self.window.handle else 0
        monitor = win_monitor_rect(game)
        self.update_scale(monitor_hint=game)
        w, h, y = self.panel_geometry(monitor)
        self.panel = {"state": "opening", "t0": time.time(), "start": 0, "w": 1, "W": w, "H": h, "y": y,
                      "monitor": monitor, "side": self.side(), "game": game}
        self.window.borderless = True
        self.window.resizable = False
        self.window.always_on_top = True
        self.panel_place(1)
        self.window.show()
        if self.on_windows:
            set_floating_style(self.window.handle, True)
        self.bring_forward()
        self.last_seq = -1
        hint = {True: "Click the picture to control it", "clicks": "Clicks, scrolling and typing go to the device",
                False: "Typing here goes to the device"}.get(self.agent.mouse, "")
        if self.cfg["machines"] and not self.dialog:
            self.toast(hint, "mouse" if self.agent.mouse else "keyboard", C.ACCENT, 2.5)

    def panel_relayout(self):
        """Settings changed the panel's side or width: move it there now."""
        if not self.panel:
            return
        w, h, y = self.panel_geometry(self.panel["monitor"])
        self.panel.update(W=w, H=h, y=y, side=self.side())
        if self.panel["state"] == "open":
            self.panel_place(w)
        self.last_seq = -1

    def panel_place(self, w):
        p = self.panel
        p["w"] = w = max(1, min(p["W"], w))
        self.window.size = (w, p["H"])
        left, _, right, _ = p["monitor"]
        gap = self.panel_gap()
        # Slides in from the edge: while it's narrow it hugs the edge, then settles a gap away.
        inset = round(gap * w / p["W"])
        self.window.position = (right - inset - w if p["side"] == "right" else left + inset, p["y"])

    def panel_close(self):
        if not self.panel or self.panel["state"] == "closing":
            return
        self.let_go()
        self.panel.update(state="closing", t0=time.time(), start=self.panel["w"])

    def normal_frame(self):
        if self.on_windows:
            set_floating_style(self.window.handle, False)
        self.window.always_on_top = False
        self.window.borderless = False
        self.window.resizable = True
        if self.on_windows:
            set_window_icons(self.window.handle)       # the frame change drops the title-bar icon
            set_dark_title_bar(self.window.handle)

    def panel_finished_closing(self):
        game = self.panel["game"]
        self.panel = None
        self.window.hide()
        self.normal_frame()
        self.last_seq = -1
        win_focus(game)                                 # straight back into the game

    def panel_step(self):
        """Advance the slide animation. True while it's moving."""
        p = self.panel
        if not p or p["state"] == "open":
            return False
        t = min(1.0, (time.time() - p["t0"]) / PANEL_SLIDE_S)
        eased = 1 - (1 - t) ** 3
        goal = p["W"] if p["state"] == "opening" else 0
        w = round(p["start"] + (goal - p["start"]) * eased)
        if t >= 1:
            if p["state"] == "closing":
                self.panel_finished_closing()
                return False
            p["state"] = "open"
            self.panel_place(p["W"])
            return False
        self.panel_place(w)
        return True

    def show_window(self):
        """Pop out into a normal, resizable window (from the panel or from hidden)."""
        if self.window_visible:
            self.bring_forward()
            return
        area = self.panel["monitor"] if self.panel else win_monitor_rect(0)
        self.panel = None
        self.normal_frame()
        if self.normal_geom:
            self.window.size, self.window.position = self.normal_geom
        else:
            w, h = min(1440, area[2] - area[0] - 80), min(900, area[3] - area[1] - 120)
            self.window.size = (w, h)
            self.window.position = (area[0] + (area[2] - area[0] - w) // 2, area[1] + (area[3] - area[1] - h) // 2)
        self.window.show()
        self.window_visible = True
        self.bring_forward()
        self.last_seq = -1

    def hide_window(self):
        """Put the normal window away (it keeps running in the tray)."""
        self.let_go()
        if self.fullscreen:
            self.window.set_windowed()
            self.fullscreen = False
        self.normal_geom = (self.window.size, self.window.position)
        self.window.hide()
        self.window_visible = False
        if self.tray and not self.told_about_tray:
            self.told_about_tray = True
            self.tray.notify("Porthole is still running",
                             (f"{self.panel_key[3]} brings it back. " if self.panel_key else "")
                             + "Right-click the tray icon to quit.")

    def dock_as_panel(self):
        """From the normal window back to the side panel."""
        self.normal_geom = (self.window.size, self.window.position)
        if self.fullscreen:
            self.window.set_windowed()
            self.fullscreen = False
        self.window.hide()
        self.window_visible = False
        self.panel_open(0)

    def put_away(self):
        if self.panel:
            self.panel_close()
        elif self.window_visible:
            self.hide_window()

    def on_panel_event(self, ev):
        """A global hotkey or a tray action. The hotkey toggles: out if hidden, back in if shown."""
        action = ev.action
        if action == "quit":
            self.running = False
            return
        if action == "window":
            self.show_window()
            return
        shown = self.window_visible or (self.panel and self.panel["state"] != "closing")
        if action == "machine":
            ms = self.cfg["machines"]
            if ev.index >= len(ms):
                if shown:
                    self.toast(f"There's no device {ev.index + 1}", "info", C.TEXT_2)
                return
            target = ms[ev.index]["name"]
            if shown and target == self.machines.current["name"]:
                self.put_away()                         # same device again: slide back in
                return
            self.choose_machine(target)
            if shown:
                self.bring_forward()
                return
            self.panel_open(ev.fg)
            return
        if action in ("toggle", "panel"):
            if shown:
                self.put_away()
            else:
                self.panel_open(ev.fg)
            return
        # "show" (a second launch, the Xbox app entry): out, or to the front.
        if self.window_visible:
            self.bring_forward()
        elif not self.panel or self.panel["state"] == "closing":
            self.panel_open(ev.fg)
        else:
            self.bring_forward()

    def sdl_hotkey(self, ev):
        """The panel hotkey as seen inside the window (Windows doesn't fire it while the keyboard is captured)."""
        if not self.panel_key or ev.key != self.pg.key.key_code(self.panel_key[2]):
            return False
        mods = self.panel_key[0]
        pg = self.pg
        want = {pg.KMOD_CTRL: mods & 0x2, pg.KMOD_ALT: mods & 0x1, pg.KMOD_SHIFT: mods & 0x4, pg.KMOD_GUI: mods & 0x8}
        return all(bool(ev.mod & m) == bool(on) for m, on in want.items())

    def update_scale(self, monitor_hint=None):
        """Match the UI's size to the monitor's DPI (Windows display scaling)."""
        if not self.on_windows:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            dpi = 0
            if monitor_hint is not None and not self.window_visible and not self.panel:
                u.MonitorFromWindow.restype = ctypes.c_void_p
                mon = u.MonitorFromWindow(ctypes.c_void_p(monitor_hint or None), 2)
                x, y = ctypes.c_uint(), ctypes.c_uint()
                if mon and ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0, ctypes.byref(x),
                                                                  ctypes.byref(y)) == 0:
                    dpi = x.value
            if not dpi:
                dpi = u.GetDpiForWindow(ctypes.c_void_p(self.window.handle))
            if dpi:
                self.ui.set_scale(dpi / 96)
        except (OSError, AttributeError):
            pass

    # ------------------------------------------------------------------ status text
    def link_status(self):
        """(dot colour, text) for keyboard/mouse."""
        a = self.agent
        state = a.state
        if not self.machines.current.get("host"):
            return C.TEXT_3, "No network address for keyboard and mouse"
        if a.connected:
            if a.rtt_ms is not None and a.rtt_ms >= 150:
                return C.WARN, f"Connected · slow link, {a.rtt_ms} ms"
            return C.GOOD, "Connected" + (f" · {a.rtt_ms} ms" if a.rtt_ms is not None else "")
        if state.startswith("connecting"):
            return C.WARN, "Connecting…"
        if state.startswith("not running"):
            return C.BAD, "Agent isn't running on the device"
        if state.startswith("can't reach"):
            return C.BAD, "Can't reach the device"
        if state.startswith("agent refused"):
            return C.BAD, "Refused: " + state.split(":", 1)[-1].strip()
        return C.BAD, state.capitalize()

    def video_label(self):
        if self.cap.mode == "net":
            return NET_SOURCES.get(self.cap.net_src, "Network video")
        name = self.cap.input_name()
        return name + (" · auto" if self.cfg.get("auto_input", True) else "")

    # ------------------------------------------------------------------ menus
    def open_menu(self, kind, anchor, right=False, data=None):
        if self.menu and self.menu["kind"] == kind and (data is None or self.menu.get("data") == data):
            self.menu = None
            return
        self.menu = {"kind": kind, "anchor": self.pg.Rect(anchor), "right": right, "data": data}
        self.release_all()

    def menu_items(self):
        kind = self.menu["kind"]
        items = []
        if kind == "source":
            auto = self.cfg.get("auto_input", True)
            net = self.cap.mode == "net"
            items.append({"header": "Capture card"})
            for idx, name in self.cap.inputs:
                port = port_of(name)
                items.append({"label": name, "icon": "video", "hint": str(port or ""),
                              "check": idx == self.cap.index and not auto and not net,
                              "do": (lambda p=port: self.choose_port(p)) if port else None})
            items.append({"label": "Find automatically", "icon": "search", "hint": "S",
                          "check": auto and not net, "do": self.choose_auto})
            items.append({"header": "Over the network"})
            for key, src, icon in (("W", "screen", "monitor"), ("C", "camera", "camera"), ("T", "test", "tune")):
                items.append({"label": {"screen": "Screen", "camera": "Camera", "test": "Test pattern"}[src],
                              "icon": icon, "hint": key, "check": net and self.cap.net_src == src,
                              "do": lambda s=src: self.choose_net(s)})
        elif kind == "devices":
            for n, m in enumerate(self.cfg["machines"]):
                items.append({"label": m["name"], "icon": kind_icon(m), "hint": str(n + 1) if n < 9 else "",
                              "check": m["name"] == self.machines.current["name"],
                              "do": lambda nm=m["name"]: self.choose_machine(nm)})
            if items:
                items.append({"sep": True})
            items.append({"label": "Add device…", "icon": "add", "hint": "N", "do": lambda: self.open_device_dialog(None)})
            items.append({"label": "Manage devices…", "icon": "settings", "do": self.open_settings})
        elif kind == "device":
            name = self.menu["data"]
            items.append({"label": "Edit…", "icon": "edit", "do": lambda: self.open_device_dialog(name)})
            items.append({"label": "Remove…", "icon": "delete", "danger": True,
                          "do": lambda: self.open_remove_dialog(name)})
        return items

    def draw_menu(self, surf):
        ui, pg = self.ui, self.pg
        items = self.menu_items()
        row, pad = ui.px(34), ui.px(6)
        w = ui.px(220)
        for it in items:
            if "label" in it:
                w = max(w, ui.measure(it["label"], 13)[0] + ui.px(110))
        h = pad * 2
        for it in items:
            h += ui.px(9) if it.get("sep") else ui.px(28) if it.get("header") else row
        anchor = self.menu["anchor"]
        r = pg.Rect(0, 0, w, h)
        if self.menu["right"]:
            r.topright = (anchor.right, anchor.bottom + ui.px(6))
        else:
            r.topleft = (anchor.left, anchor.bottom + ui.px(6))
        r.clamp_ip(surf.get_rect().inflate(-ui.px(8), -ui.px(8)))
        ui.card(surf, r, 10, C.OVERLAY, C.BORDER)
        y = r.y + pad
        chosen = None
        for i, it in enumerate(items):
            if it.get("sep"):
                pg.draw.line(surf, C.BORDER, (r.x + pad, y + ui.px(4)), (r.right - pad, y + ui.px(4)), 1)
                y += ui.px(9)
                continue
            if it.get("header"):
                ui.text(surf, it["header"].upper(), (r.x + ui.px(14), y + ui.px(16)), 10.5, C.TEXT_3, "semibold",
                        "midleft")
                y += ui.px(28)
                continue
            ir = pg.Rect(r.x + pad, y, r.w - pad * 2, row)
            enabled = it.get("do") is not None
            if enabled and ui.hovered(ir):
                ui.rrect(surf, ir, C.HOVER, 6)
            fg = (255, 160, 164) if it.get("danger") else (C.TEXT if enabled else C.TEXT_3)
            ui.icon(surf, it.get("icon", "info"), (ir.x + ui.px(16), ir.centery), 14, fg)
            ui.text(surf, it["label"], (ir.x + ui.px(36), ir.centery), 13, fg, anchor="midleft",
                    max_w=ir.w - ui.px(90))
            if it.get("check"):
                ui.icon(surf, "check", (ir.right - ui.px(16), ir.centery), 12, C.ACCENT_HOVER)
            elif it.get("hint"):
                ui.text(surf, it["hint"], (ir.right - ui.px(12), ir.centery), 12, C.TEXT_3, anchor="midright")
            if enabled and ui.clicked(ir):
                chosen = it
            y += row
        self.menu["rect"] = r
        if chosen is not None:
            self.menu = None
            chosen["do"]()
        elif ui.release_pos is not None and not r.collidepoint(ui.release_pos):
            self.menu = None                            # clicked outside: close
            ui.release_pos = None

    def menu_key(self, ev):
        if ev.key == self.pg.K_ESCAPE:
            self.menu = None
            return
        pressed = (ev.unicode or "").upper()
        for it in self.menu_items():
            if pressed and it.get("hint") == pressed and it.get("do"):
                self.menu = None
                it["do"]()
                return

    # ------------------------------------------------------------------ dialogs
    def open_settings(self):
        self.menu = None
        self.release_all()
        self.dialog = {"kind": "settings", "scroll": 0, "t0": time.time()}

    def open_device_dialog(self, name):
        self.menu = None
        self.release_all()
        m = self.machines.get(name) if name else None
        if m:
            video = m.get("video") or "hdmi"
            form = {"name": m["name"], "kind": machine_kind(m), "host": m.get("host", ""),
                    "video": ("port", m["input"]) if video == "hdmi" and m.get("input") else
                    ("auto", None) if video == "hdmi" else ("net", video),
                    "mouse": m.get("mouse", True), "user": m.get("user", "")}
        else:
            form = {"name": "", "kind": "mac", "host": "", "video": ("auto", None), "mouse": True, "user": ""}
        scanner = InputScanner(self.cap)
        scanner.start()
        self.dialog = {"kind": "device", "edit": name, "form": form, "scroll": 0, "scanner": scanner, "t0": time.time(),
                       "thumbs": {}, "added": None}
        self.ui.focus = "dev-name" if not name else None
        self.pg.key.start_text_input()

    def open_help(self):
        self.menu = None
        self.release_all()
        self.dialog = {"kind": "help", "scroll": 0, "t0": time.time()}

    def draw_help(self, surf):
        """Every shortcut, as keycaps."""
        ui, pg = self.ui, self.pg
        key = self.panel_key[3] if self.panel_key else None
        mods = key.rsplit("+", 1)[0] if key and "+" in key else None
        groups = [("Anywhere, even in a game", [
                      (key, "Slide Porthole out, and back in") if key else None,
                      (f"{mods}+1–9", "Slide out on device 1–9; again to slide in") if mods else None]),
                  ("In Porthole", [
                      ("Right Ctrl", "Capture the whole keyboard (Win, Alt+Tab…), or release it"),
                      ("Right Ctrl+M", "Devices"), ("Right Ctrl+I", "Video source"),
                      ("Right Ctrl+N", "Add a device"), ("Right Ctrl+,", "Settings"),
                      ("Right Ctrl+V", "Type the clipboard on the device"),
                      ("Right Ctrl+F", "Panel: open as a window. Window: fullscreen"),
                      ("Right Ctrl+1–9", "Use HDMI-1 to HDMI-9"), ("Right Ctrl+/", "This list"),
                      ("Right Ctrl+Q", "Quit Porthole")])]
        groups = [(g, [r for r in rows if r]) for g, rows in groups]
        rows = sum(len(r) for _, r in groups)
        content_h = ui.px(20) + len(groups) * ui.px(36) + rows * ui.px(40) + ui.px(8)
        card, body, closed = self.dialog_frame(surf, "Keyboard shortcuts", ui.px(620), content_h, "keyboard")
        x, w = body.x + ui.px(24), body.w - ui.px(48)
        y = self.scroll_body(surf, body, content_h) + ui.px(20)
        col = ui.px(190)
        for title, items in groups:
            y += self.section(surf, x, y, title) + ui.px(8)
            for combo, what in items:
                ui.keycaps(surf, (x, y + ui.px(14)), combo.replace("Right Ctrl", "Right Ctrl"), size=11.5)
                ui.text(surf, what, (x + col, y + ui.px(14)), 13, C.TEXT_2, anchor="midleft", max_w=w - col)
                y += ui.px(40)
        self.end_scroll(surf, body)
        done = pg.Rect(0, 0, ui.px(96), ui.px(36))
        done.bottomright = (card.right - ui.px(24), card.bottom - ui.px(16))
        ui.text(surf, "Right Ctrl never reaches the device; use Left Ctrl there.", (card.x + ui.px(24), done.centery),
                12, C.TEXT_3, anchor="midleft", max_w=done.x - card.x - ui.px(36))
        if ui.button(surf, done, "Got it", "help-done", "primary") or closed:
            self.close_overlays()

    def open_remove_dialog(self, name):
        self.menu = None
        self.dialog = {"kind": "remove", "name": name, "t0": time.time()}

    def save_device(self, d):
        f = d["form"]
        name = f["name"].strip()
        entry = self.machines.get(d["edit"]) if d["edit"] else None
        if entry is None:
            entry = {}
            self.cfg["machines"].append(entry)
        old_name = entry.get("name")
        kind, value = f["video"]
        entry.update({"name": name, "host": f["host"].strip(), "kind": f["kind"], "mouse": f["mouse"],
                      "input": value if kind == "port" else None,
                      "video": value if kind == "net" else "hdmi"})
        if f["user"].strip():
            entry["user"] = f["user"].strip()
        if entry["mouse"] is True:
            entry.pop("mouse", None)
        if old_name and self.cfg.get("machine") == old_name:
            self.cfg["machine"] = name
        save_config(self.cfg)
        if self.cfg.get("machine") == name or len(self.cfg["machines"]) == 1 or not old_name:
            self.cfg["machine"] = ""                     # force switch() to re-apply everything
            self.machines.switch(name)
        self.last_seq = -1
        return entry

    def remove_device(self, name):
        self.cfg["machines"] = [m for m in self.cfg["machines"] if m["name"] != name]
        if self.cfg.get("machine") == name:
            nxt = self.cfg["machines"][0]["name"] if self.cfg["machines"] else ""
            self.cfg["machine"] = ""
            if nxt:
                self.machines.switch(nxt)
            else:
                self.agent.retarget("", self.cfg["agent_port"])
        save_config(self.cfg)
        self.toast(f"Removed {name}", "delete", C.TEXT_2)

    def install_agent_in_terminal(self, entry):
        """Open a terminal that installs the agent over SSH (it asks for the device's password there)."""
        user = entry.get("user") or ""
        target = f"{user}@{entry['host']}" if user else entry["host"]
        py = pathlib.Path(sys.executable)
        if py.name.lower() == "pythonw.exe":
            py = py.with_name("python.exe")
        cmd = (f"& '{py}' '{HERE / 'porthole.py'}' --install-agent '{target}' --name '{entry['name']}' "
               f"--skip-register; Write-Host ''; Write-Host 'You can close this window.'")
        try:
            subprocess.Popen(["powershell", "-NoExit", "-NoProfile", "-Command", cmd],
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), cwd=str(HERE))
            self.toast("Finish the install in the terminal window (it asks for the device's password)", "terminal")
        except OSError as exc:
            self.toast(f"Couldn't open a terminal: {exc}", "warning", C.BAD)

    def dialog_frame(self, surf, title, width, body_h, icon=None):
        """Backdrop + card + title bar. Returns (card rect, body rect, close clicked)."""
        ui, pg = self.ui, self.pg
        t = min(1.0, (time.time() - self.dialog.get("t0", 0)) / 0.16)
        ease = 1 - (1 - t) ** 3
        ui.dim(surf, int(150 * ease))
        sw, sh = surf.get_size()
        margin = ui.px(16)
        head, foot = ui.px(60), ui.px(68)
        w = min(width, sw - margin * 2)
        h = min(head + body_h + foot, sh - margin * 2)
        card = pg.Rect(0, 0, w, h)
        card.center = (sw // 2, sh // 2 + round(ui.px(12) * (1 - ease)))
        ui.card(surf, card, 14, C.RAISED, C.BORDER)
        x = card.x + ui.px(24)
        if icon:
            ui.icon(surf, icon, (x + ui.px(9), card.y + head // 2), 18, C.ACCENT_HOVER)
            x += ui.px(30)
        ui.text(surf, title, (x, card.y + head // 2), 17, C.TEXT, "semibold", "midleft")
        close = pg.Rect(0, 0, ui.px(32), ui.px(32))
        close.center = (card.right - ui.px(28), card.y + head // 2)
        closed = ui.icon_button(surf, close, "close", "dlg-close", "Close (Esc)")
        pg.draw.line(surf, C.BORDER_SOFT, (card.x, card.y + head), (card.right - 1, card.y + head), 1)
        pg.draw.line(surf, C.BORDER_SOFT, (card.x, card.bottom - foot), (card.right - 1, card.bottom - foot), 1)
        body = pg.Rect(card.x, card.y + head + 1, card.w, card.h - head - foot - 1)
        return card, body, closed

    def scroll_body(self, surf, body, content_h):
        """Clip to the body; returns the y offset to draw at. Mouse wheel scrolls (see on_wheel)."""
        d = self.dialog
        d["max_scroll"] = max(0, content_h - body.h)
        d["scroll"] = max(0, min(d.get("scroll", 0), d["max_scroll"]))
        d["body"] = body
        surf.set_clip(body)
        self.ui.clip = body
        return body.y - d["scroll"]

    def end_scroll(self, surf, body):
        d = self.dialog
        surf.set_clip(None)
        self.ui.clip = None
        if d.get("max_scroll"):
            ui = self.ui
            track = self.pg.Rect(body.right - ui.px(8), body.y + ui.px(6), ui.px(4), body.h - ui.px(12))
            thumb_h = max(ui.px(30), track.h * body.h // (body.h + d["max_scroll"]))
            ty = track.y + (track.h - thumb_h) * d["scroll"] // d["max_scroll"]
            ui.rrect(surf, (track.x, ty, track.w, thumb_h), C.BORDER, 2)

    def setting_row(self, surf, x, y, w, title, desc=None):
        """Left-hand label of a settings row. Returns the row height."""
        ui = self.ui
        ui.text(surf, title, (x, y), 14, C.TEXT, "semibold")
        if desc:
            ui.text(surf, desc, (x, y + ui.px(22)), 12, C.TEXT_2, max_w=w)
        return ui.px(52 if desc else 36)

    def section(self, surf, x, y, label):
        self.ui.text(surf, label.upper(), (x, y), 11, C.TEXT_3, "semibold")
        return self.ui.px(28)

    def draw_settings(self, surf):
        ui, pg, cfg = self.ui, self.pg, self.cfg
        devices = cfg["machines"]
        content_h = ui.px(24 + 28 + 64 * 3 + 20 + 28 + 64 * 2 + 20 + 28 + 60 * max(1, len(devices)) + 56 + 16)
        card, body, closed = self.dialog_frame(surf, "Settings", ui.px(640), content_h, "settings")
        pad = ui.px(24)
        x, w = body.x + pad, body.w - pad * 2
        ctrl_w = min(ui.px(300), w // 2)
        y = self.scroll_body(surf, body, content_h) + ui.px(20)

        # -- Side panel
        y += self.section(surf, x, y, "Side panel")
        self.setting_row(surf, x, y, w - ctrl_w - pad, "Slides out from", "The edge of the screen it appears at")
        side = ui.segmented(surf, (x + w - ctrl_w, y + ui.px(6), ctrl_w, ui.px(36)),
                            [("left", "Left", "dock_left"), ("right", "Right", "dock_right")], self.side(), "side")
        if side != self.side():
            cfg["panel_side"] = side
            save_config(cfg)
            self.panel_relayout()
            self.toast(f"The panel now slides out from the {side}", "dock_left" if side == "left" else "dock_right")
        y += ui.px(64)
        self.setting_row(surf, x, y, w - ctrl_w - pad, "Width", "How much of the screen it covers")
        cur = float(cfg.get("panel_width", 0.32))
        nearest = min(PANEL_WIDTHS, key=lambda o: abs(o[0] - cur))[0]
        width = ui.segmented(surf, (x + w - ctrl_w, y + ui.px(6), ctrl_w, ui.px(36)),
                             [(v, label, None) for v, label in PANEL_WIDTHS], nearest, "width")
        if width != nearest:
            cfg["panel_width"] = width
            save_config(cfg)
            self.panel_relayout()
        y += ui.px(64)
        self.setting_row(surf, x, y, w - ctrl_w - pad, "Shortcut",
                         "Shows and hides Porthole, even in a game")
        btn_w = ui.px(92)
        bx = x + w - btn_w
        if self.capture_hotkey:
            field = pg.Rect(x + w - ctrl_w, y + ui.px(6), ctrl_w - btn_w - ui.px(8), ui.px(36))
            ui.rrect(surf, field, C.FIELD, 8, C.ACCENT, 2)
            ui.text(surf, "Press the new shortcut…", field.center, 13, C.ACCENT_HOVER, anchor="center")
            if ui.button(surf, (bx, y + ui.px(6), btn_w, ui.px(36)), "Cancel", "hk-cancel"):
                self.capture_hotkey = False
        else:
            label = self.panel_key[3] if self.panel_key else "Not set"
            if self.panel_key:
                ui.keycaps(surf, (bx - ui.px(12), y + ui.px(24)), label, anchor="midright")
            else:
                ui.text(surf, label, (bx - ui.px(12), y + ui.px(24)), 13, C.WARN, anchor="midright")
            if ui.button(surf, (bx, y + ui.px(6), btn_w, ui.px(36)), "Change", "hk-change"):
                self.capture_hotkey = True
        y += ui.px(64) + ui.px(20)

        # -- General
        y += self.section(surf, x, y, "General")
        self.setting_row(surf, x, y, w - ui.px(80), "Start hidden in the tray",
                         "Porthole stays out of the way until you press the shortcut")
        on = cfg.get("start_hidden", True)
        new = ui.toggle(surf, (x + w - ui.px(44), y + ui.px(12), ui.px(44), ui.px(24)), on, "start-hidden")
        if new != on:
            cfg["start_hidden"] = new
            save_config(cfg)
        y += ui.px(64)
        self.setting_row(surf, x, y, w - ui.px(170), "Start menu and desktop shortcut",
                         "Adds Porthole with its icon, so you can pin it")
        if ui.button(surf, (x + w - ui.px(150), y + ui.px(6), ui.px(150), ui.px(36)), "Create shortcuts",
                     "shortcuts", icon="pin"):
            try:
                create_shortcuts()
                self.toast("Added Porthole to the Start menu and the desktop", "check", C.GOOD)
            except Exception as exc:  # noqa: BLE001
                self.toast(f"Couldn't create shortcuts: {exc}", "warning", C.BAD)
        y += ui.px(64) + ui.px(20)

        # -- Devices
        y += self.section(surf, x, y, "Devices")
        if not devices:
            ui.text(surf, "No devices yet.", (x, y + ui.px(10)), 13, C.TEXT_2)
            y += ui.px(40)
        for m in devices:
            row = pg.Rect(x, y, w, ui.px(52))
            ui.rrect(surf, row, C.FIELD, 10, C.BORDER_SOFT)
            ui.icon(surf, kind_icon(m), (row.x + ui.px(24), row.centery), 18, C.TEXT_2)
            ui.text(surf, m["name"], (row.x + ui.px(48), row.y + ui.px(9)), 14, C.TEXT, "semibold",
                    max_w=row.w - ui.px(170))
            sub = " · ".join(filter(None, [m.get("host") or "no address", describe_video(m),
                                           {"clicks": "click-through", False: "keyboard only"}.get(m.get("mouse"))]))
            ui.text(surf, sub, (row.x + ui.px(48), row.y + ui.px(29)), 12, C.TEXT_2, max_w=row.w - ui.px(170))
            b = pg.Rect(0, 0, ui.px(32), ui.px(32))
            b.midright = (row.right - ui.px(10), row.centery)
            if ui.icon_button(surf, b, "delete", ("rm", m["name"]), "Remove"):
                self.open_remove_dialog(m["name"])
                return
            b.right = b.x - ui.px(4)
            if ui.icon_button(surf, b, "edit", ("ed", m["name"]), "Edit"):
                self.open_device_dialog(m["name"])
                return
            y += ui.px(60)
        if ui.button(surf, (x, y + ui.px(4), ui.px(150), ui.px(36)), "Add device", "set-add", icon="add"):
            self.open_device_dialog(None)
            return
        self.end_scroll(surf, body)

        done = pg.Rect(0, 0, ui.px(96), ui.px(36))
        done.bottomright = (card.right - ui.px(24), card.bottom - ui.px(16))
        version = "Porthole · " + (f"{self.panel_key[3]} shows and hides" if self.panel_key else "tray mode")
        ui.text(surf, version, (card.x + ui.px(24), done.centery), 12, C.TEXT_3, anchor="midleft")
        if ui.button(surf, done, "Done", "set-done", "primary") or closed:
            self.close_overlays()

    def capture_hotkey_key(self, ev):
        """Settings is waiting for a new shortcut: take this key press."""
        pg = self.pg
        if ev.key == pg.K_ESCAPE:
            self.capture_hotkey = False
            return
        if ev.key in (pg.K_LCTRL, pg.K_RCTRL, pg.K_LALT, pg.K_RALT, pg.K_LSHIFT, pg.K_RSHIFT, pg.K_LGUI, pg.K_RGUI):
            return                                      # wait for the actual key
        mods = [n for flag, n in ((pg.KMOD_CTRL, "ctrl"), (pg.KMOD_ALT, "alt"), (pg.KMOD_SHIFT, "shift"),
                                  (pg.KMOD_GUI, "win")) if ev.mod & flag]
        name = pg.key.name(ev.key).lower().replace(" ", "")
        name = {"`": "backquote", "scrolllock": "scrolllock"}.get(name, name)
        spec = "+".join(mods + [name])
        key = parse_hotkey(spec)
        if key is None or (not key[0] and name not in ("scrolllock", "pause") and not name.startswith("f")):
            self.toast(f"{spec.title()} can't be a shortcut: add Ctrl, Alt or Shift", "warning", C.WARN)
            return
        if not self.hotkeys:
            self.toast("Shortcuts need Windows", "warning", C.WARN)
            self.capture_hotkey = False
            return
        failed = self.hotkeys.rebind(self._bindings(key))
        if any(p["action"] == "toggle" for p in failed):
            self.hotkeys.rebind(self._bindings(self.panel_key)) if self.panel_key else None
            self.toast(f"{key[3]} is taken by another app. Try another", "warning", C.WARN)
            return
        self.panel_key = key
        self.cfg["panel_hotkey"] = spec
        save_config(self.cfg)
        if self.tray:
            self.tray.hotkey_label = key[3]
        self.capture_hotkey = False
        self.toast(f"The shortcut is now {key[3]}", "check", C.GOOD)

    def draw_device_dialog(self, surf):
        ui, pg = self.ui, self.pg
        d = self.dialog
        if d["added"] is not None:
            return self.draw_added(surf)
        f = d["form"]
        editing = d["edit"] is not None
        n_inputs = len(self.cap.inputs) + 1
        cols = 3 if n_inputs > 2 else n_inputs
        rows = (n_inputs + cols - 1) // cols
        card_w_guess = ui.px(640) - ui.px(48)
        thumb_h = int((card_w_guess - ui.px(12) * (cols - 1)) / cols * 9 / 16) + ui.px(40)
        content_h = ui.px(20 + 76 * 2 + 64 + 76 + 30) + rows * (thumb_h + ui.px(12)) + ui.px(56 + 90 + 20)
        card, body, closed = self.dialog_frame(surf, "Edit device" if editing else "Add a device", ui.px(640),
                                               content_h, "edit" if editing else "add")
        pad = ui.px(24)
        x, w = body.x + pad, body.w - pad * 2
        y = self.scroll_body(surf, body, content_h) + ui.px(20)

        def label(text, hint=None):
            ui.text(surf, text, (x, y), 13, C.TEXT, "semibold")
            if hint:
                ui.text(surf, hint, (x + w, y + ui.px(1)), 12, C.TEXT_3, anchor="topright")

        # Name + type
        label("Name")
        f["name"] = ui.text_field(surf, (x, y + ui.px(24), w, ui.px(40)), f["name"], "dev-name", "e.g. Mac mini")
        y += ui.px(76)
        label("Type")
        f["kind"] = ui.segmented(surf, (x, y + ui.px(24), w, ui.px(38)),
                                 [(k, lbl, ic) for k, lbl, ic in DEVICE_KINDS], f["kind"], "dev-kind")
        y += ui.px(76)
        label("Network address", "for keyboard and mouse")
        f["host"] = ui.text_field(surf, (x, y + ui.px(24), w, ui.px(40)), f["host"], "dev-host",
                                  "hostname or IP, e.g. mac-mini.local or 192.168.1.20")
        y += ui.px(76)

        # Video source cards
        label("Video", "click the one showing your device")
        y += ui.px(26)
        scanner = d["scanner"]
        gap = ui.px(12)
        cw = (w - gap * (cols - 1)) // cols
        th = cw * 9 // 16
        options = [("auto", None, "Find automatically", None)] + [
            ("port", port_of(name), name, idx) for idx, name in self.cap.inputs]
        for i, (kind, value, title, idx) in enumerate(options):
            cx = x + (i % cols) * (cw + gap)
            cy = y + (i // cols) * (th + ui.px(40) + gap)
            cr = pg.Rect(cx, cy, cw, th + ui.px(40))
            sel = f["video"] == (kind, value)
            hot = ui.hovered(cr)
            ui.rrect(surf, cr, C.FIELD, 10, C.ACCENT if sel else ((70, 76, 92) if hot else C.BORDER_SOFT),
                     2 if sel else 1)
            tr = pg.Rect(cx + ui.px(6), cy + ui.px(6), cw - ui.px(12), th - ui.px(6))
            state = None
            if kind == "auto":
                ui.rrect(surf, tr, (24, 27, 33), 6)
                ui.icon(surf, "search", tr.center, 22, C.TEXT_2)
            else:
                thumb, state = scanner.results.get(idx, (None, None))
                if thumb is not None:
                    key = ("thumb", idx, tr.size)
                    img = d["thumbs"].get(key)
                    if img is None:
                        small = cv2.resize(thumb, tr.size, interpolation=cv2.INTER_AREA)
                        img = pg.image.frombuffer(small.tobytes(), tr.size, "RGB").copy()
                        d["thumbs"][key] = img
                    surf.blit(img, tr)
                else:
                    ui.rrect(surf, tr, (24, 27, 33), 6)
                    if state is None:
                        ui.spinner(surf, tr.center, 10)
                    else:
                        ui.icon(surf, "no_signal", tr.center, 20, C.TEXT_3)
            chip = {"signal": ("Picture", C.GOOD), "no signal": ("No signal", C.TEXT_3),
                    "unavailable": ("In use", C.WARN)}.get(state)
            title = title.replace(" (Multiview)", "")
            room = cw - ui.px(24) - (ui.measure(chip[0], 11)[0] + ui.px(22) if chip else 0)
            ui.text(surf, title, (cx + ui.px(12), cr.bottom - ui.px(17)), 13, C.TEXT, "semibold" if sel else "regular",
                    "midleft", max_w=room)
            if chip:
                ui.status_dot(surf, (cr.right - ui.px(14) - ui.measure(chip[0], 11)[0] - ui.px(10),
                                     cr.bottom - ui.px(17)), chip[1], ring=False)
                ui.text(surf, chip[0], (cr.right - ui.px(12), cr.bottom - ui.px(17)), 11, chip[1], anchor="midright")
            if sel:
                ui.icon(surf, "check", (cr.right - ui.px(18), cr.y + ui.px(18)), 12, C.ACCENT_HOVER)
            if ui.clicked(cr):
                f["video"] = (kind, value)
        y += rows * (th + ui.px(40) + gap)
        ui.text(surf, "Or over the network, no cable:", (x, y + ui.px(18)), 12, C.TEXT_2, anchor="midleft")
        net_w = min(ui.px(320), w - ui.px(200))
        net = ui.segmented(surf, (x + w - net_w, y, net_w, ui.px(36)),
                           [("screen", "Screen", "monitor"), ("camera", "Camera", "camera")],
                           f["video"][1] if f["video"][0] == "net" else None, "dev-net")
        if net and f["video"] != ("net", net):
            f["video"] = ("net", net)
        y += ui.px(56)

        # Mouse
        label("Mouse")
        f["mouse"] = ui.segmented(surf, (x, y + ui.px(24), w, ui.px(38)),
                                  [(v, lbl, None) for v, lbl in MOUSE_MODES], f["mouse"], "dev-mouse")
        hint = {True: "Porthole moves the device's pointer and clicks where you click.",
                "clicks": "Deskflow or another KVM shares the device's own mouse; clicks and scrolling still go through.",
                False: "Only what you type is sent; use the device's own mouse."}[f["mouse"]]
        ui.text(surf, hint, (x, y + ui.px(70)), 12, C.TEXT_2, max_w=w)
        self.end_scroll(surf, body)

        # Footer
        name = f["name"].strip()
        taken = any(m["name"] == name for m in self.cfg["machines"] if m["name"] != d["edit"])
        problem = "Give it a name" if not name else f"There's already a device called {name}" if taken else None
        save = pg.Rect(0, 0, ui.button_width("Add device", kind="primary"), ui.px(36))
        save.bottomright = (card.right - ui.px(24), card.bottom - ui.px(16))
        cancel = pg.Rect(0, 0, ui.px(96), ui.px(36))
        cancel.bottomright = (save.x - ui.px(8), save.bottom)
        if problem and name:
            ui.text(surf, problem, (card.x + ui.px(24), save.centery), 12, C.WARN, anchor="midleft",
                    max_w=cancel.x - card.x - ui.px(36))
        if ui.button(surf, cancel, "Cancel", "dev-cancel") or closed:
            self.close_overlays()
            return
        submit = any(k[0] == "enter" for k in ui.keys)
        if ui.button(surf, save, "Save" if editing else "Add device", "dev-save", "primary",
                     enabled=problem is None) or (submit and problem is None):
            entry = self.save_device(d)
            if editing:
                self.toast(f"Saved {entry['name']}", "check", C.GOOD)
                self.close_overlays()
            else:
                d["added"] = entry
                d["form"]["user"] = entry.get("user") or ("pi" if entry["kind"] == "pi" else "")
                self.ui.focus = "dev-user"

    def draw_added(self, surf):
        """After adding: offer to install the agent (keyboard and mouse)."""
        ui, pg = self.ui, self.pg
        d = self.dialog
        entry = d["added"]
        body_h = ui.px(206 if entry.get("host") and entry.get("kind") == "mac" else 186 if entry.get("host") else 120)
        card, body, closed = self.dialog_frame(surf, f"{entry['name']} is added", ui.px(560), body_h, "check")
        pad = ui.px(24)
        x, w = body.x + pad, body.w - pad * 2
        y = body.y + ui.px(20)
        ui.text(surf, "Set up keyboard and mouse", (x, y), 15, C.TEXT, "semibold")
        y += ui.px(28)
        y += ui.paragraph(surf, "Porthole puts a small agent on the device over SSH. A terminal opens and asks "
                               "for the device's password; it's only used for the install.", (x, y), 13, C.TEXT_2, w)
        y += ui.px(14)
        f = d["form"]
        has_host = bool(entry.get("host"))
        if has_host:
            ui.text(surf, "Username on the device", (x, y), 13, C.TEXT, "semibold")
            f["user"] = ui.text_field(surf, (x, y + ui.px(24), w, ui.px(40)), f["user"], "dev-user",
                                      "e.g. pi, or your Mac's short user name")
            y += ui.px(78)
            if entry["kind"] == "mac":
                ui.text(surf, "On the Mac: turn on Remote Login first (System Settings > General > Sharing).",
                        (x, y), 12, C.TEXT_3, max_w=w)
        else:
            ui.text(surf, "Add its network address (Edit) to set up keyboard and mouse.", (x, y), 13, C.WARN, max_w=w)
        install = pg.Rect(0, 0, ui.button_width("Install agent", icon="terminal", kind="primary"), ui.px(36))
        install.bottomright = (card.right - ui.px(24), card.bottom - ui.px(16))
        later = pg.Rect(0, 0, ui.px(100), ui.px(36))
        later.bottomright = (install.x - ui.px(8), install.bottom)
        if ui.button(surf, later, "Later" if has_host else "Done", "added-later") or closed:
            self.close_overlays()
            return
        if has_host and ui.button(surf, install, "Install agent", "added-install", "primary", icon="terminal",
                                  enabled=bool(f["user"].strip())):
            entry["user"] = f["user"].strip()
            save_config(self.cfg)
            self.install_agent_in_terminal(entry)
            self.close_overlays()

    def draw_remove(self, surf):
        ui, pg = self.ui, self.pg
        name = self.dialog["name"]
        card, body, closed = self.dialog_frame(surf, f"Remove {name}?", ui.px(480), ui.px(80), "delete")
        ui.text(surf, "It disappears from Porthole. Its agent stays installed on the device.",
                (body.x + ui.px(24), body.y + ui.px(24)), 13, C.TEXT_2, max_w=body.w - ui.px(48))
        rm = pg.Rect(0, 0, ui.px(110), ui.px(36))
        rm.bottomright = (card.right - ui.px(24), card.bottom - ui.px(16))
        cancel = pg.Rect(0, 0, ui.px(96), ui.px(36))
        cancel.bottomright = (rm.x - ui.px(8), rm.bottom)
        if ui.button(surf, cancel, "Cancel", "rm-cancel") or closed:
            self.dialog = None
            return
        if ui.button(surf, rm, "Remove", "rm-ok", "danger"):
            self.dialog = None
            self.remove_device(name)

    # ------------------------------------------------------------------ main chrome
    def draw_header(self, surf, sw):
        ui, pg = self.ui, self.pg
        hh = ui.px(HEADER_H)
        pg.draw.rect(surf, C.SURFACE, (0, 0, sw, hh))
        pg.draw.line(surf, C.BORDER_SOFT, (0, hh - 1), (sw, hh - 1), 1)
        cy = hh // 2
        # Right-hand tools (drawn first so the device pills know how much room is left)
        bs, gap = ui.px(34), ui.px(4)
        x = sw - ui.px(10) - bs
        tools = []
        if self.panel:
            toward = "chev_right" if self.side() == "right" else "chev_left"
            tools.append(("hide", toward, f"Slide in ({self.panel_key[3]})" if self.panel_key else "Slide in"))
            tools.append(("window", "window", "Open as a window (Right Ctrl+F)"))
        else:
            tools.append(("hide", "hide", f"Hide to the tray ({self.panel_key[3]})" if self.panel_key else "Hide"))
            tools.append(("dock", "dock_left" if self.side() == "left" else "dock_right", "Dock as a side panel"))
        tools.append(("settings", "settings", "Settings (Right Ctrl+,)"))
        tools.append(("help", "help", "Keyboard shortcuts (Right Ctrl+/)"))
        tools.append(("sep", None, None))
        tools.append(("keyboard", "keyboard", "Capture the whole keyboard (Right Ctrl)"))
        tools.append(("paste", "paste", "Type the clipboard (Right Ctrl+V)"))
        tools.append(("source", "video", "Video source (Right Ctrl+I)"))
        for key, icon, tip in tools:
            if key == "sep":
                pg.draw.line(surf, C.BORDER, (x + bs + gap - ui.px(2), cy - ui.px(10)),
                             (x + bs + gap - ui.px(2), cy + ui.px(10)), 1)
                x -= ui.px(8)
                continue
            r = pg.Rect(x, cy - bs // 2, bs, bs)
            if key == "source":
                self._source_btn = r
            active = (key == "keyboard" and self.grabbed) or (key == "source" and self.menu
                                                               and self.menu["kind"] == "source")
            if ui.icon_button(surf, r, icon, ("tool", key), tip, active=active, enabled=bool(self.cfg["machines"])
                              or key in ("hide", "window", "dock", "settings", "help")):
                self.tool(key, r)
            x -= bs + gap
        tools_left = x + bs

        # Brand
        lx = ui.px(14)
        logo = pg.transform.smoothscale(self.logo, (ui.px(22), ui.px(22)))
        surf.blit(logo, logo.get_rect(midleft=(lx, cy)))
        lx += ui.px(32)
        # Device pills
        ph = ui.px(34)
        add_w = bs
        avail = tools_left - ui.px(16) - add_w - lx
        machines = self.cfg["machines"]
        cur = self.machines.current["name"]
        widths = []
        for m in machines:
            weight = "semibold" if m["name"] == cur else "regular"
            widths.append(ui.measure(m["name"], 13, weight)[0] + ui.px(62 if m["name"] == cur else 44))
        shown, used = [], 0
        more_w = ui.px(64)
        for m, wd in zip(machines, widths):
            need = used + wd + (more_w if len(shown) + 1 < len(machines) else 0)
            if need > avail and shown:
                break
            shown.append((m, wd))
            used += wd + gap
        hidden_count = len(machines) - len(shown)
        x = lx
        dot_color, _ = self.link_status()
        for m, wd in shown:
            r = pg.Rect(x, cy - ph // 2, wd, ph)
            current = m["name"] == cur
            hot = ui.hovered(r)
            t = ui.anim(("pill", m["name"]), 1.0 if hot else 0.0)
            if current:
                ui.rrect(surf, r, C.ACCENT_SOFT, 9, (58, 79, 132))
            elif t > 0:
                ui.rrect(surf, r, mix(C.SURFACE, C.HOVER, t), 9)
            fg = C.TEXT if current else mix(C.TEXT_2, C.TEXT, t)
            ui.icon(surf, kind_icon(m), (r.x + ui.px(18), cy), 14, C.ACCENT_HOVER if current else fg)
            ui.text(surf, m["name"], (r.x + ui.px(32), cy), 13, fg, "semibold" if current else "regular", "midleft")
            if current:
                ui.status_dot(surf, (r.right - ui.px(14), cy), dot_color, ring=False)
            if ui.clicked(r):
                self.choose_machine(m["name"])
            if ui.right_release and r.collidepoint(ui.right_release):
                ui.right_release = None
                self.open_menu("device", r, data=m["name"])
            x += wd + gap
        if hidden_count:
            r = pg.Rect(x, cy - ph // 2, more_w, ph)
            if ui.button(surf, r, f"+{hidden_count}", "pill-more", "ghost", icon="chev_down"):
                self.open_menu("devices", r)
            x += more_w + gap
        r = pg.Rect(x, cy - bs // 2, bs, bs)
        if ui.icon_button(surf, r, "add", "add-device", "Add a device (Right Ctrl+N)"):
            self.open_device_dialog(None)
        return hh

    def tool(self, key, rect):
        if key == "hide":
            self.put_away()
        elif key == "window":
            self.show_window()
        elif key == "dock":
            self.dock_as_panel()
        elif key == "help":
            self.open_help()
        elif key == "settings":
            self.open_settings()
        elif key == "keyboard":
            self.set_grab(not self.grabbed)
        elif key == "paste":
            self.paste()
        elif key == "source":
            self.open_menu("source", rect, right=True)

    def draw_status(self, surf, sw, sh):
        ui, pg = self.ui, self.pg
        bh = ui.px(STATUS_H)
        r = pg.Rect(0, sh - bh, sw, bh)
        pg.draw.rect(surf, C.SURFACE, r)
        pg.draw.line(surf, C.BORDER_SOFT, (0, r.y), (sw, r.y), 1)
        cy = r.centery
        x = ui.px(14)
        if not self.cfg["machines"]:
            ui.text(surf, "No devices yet", (x, cy), 12, C.TEXT_3, anchor="midleft")
            return bh
        color, text = self.link_status()
        ui.status_dot(surf, (x + ui.px(4), cy), color, ring=False)
        x = ui.text(surf, text, (x + ui.px(14), cy), 12, C.TEXT_2, anchor="midleft").right
        parts = [self.video_label()]
        if self.cap.fps and (self.frame_surf is not None):
            parts.append(f"{self.cap.fps:.0f} fps")
        if self.agent.mouse == "clicks":
            parts.append("click-through")
        elif self.agent.mouse is False:
            parts.append("keyboard only")
        wifi = self.agent.wifi if self.agent.connected else None
        if wifi:
            parts.append(f"WiFi {wifi.get('signal')}% · router {wifi.get('router_ms')} ms")
        x = ui.text(surf, "   ·   ".join([""] + parts), (x, cy), 12, C.TEXT_3, anchor="midleft").right
        # Right: what the keyboard is doing
        right = sw - ui.px(14)
        if self.grabbed:
            t = ui.text(surf, "Keyboard captured", (right, cy), 12, C.ACCENT_HOVER, "semibold", "midright")
            ui.keycaps(surf, (t.x - ui.px(10), cy), "Right Ctrl", size=10.5, anchor="midright")
        elif right - x > ui.px(260):
            t = ui.text(surf, "captures the keyboard", (right, cy), 12, C.TEXT_3, anchor="midright")
            ui.keycaps(surf, (t.x - ui.px(8), cy), "Right Ctrl", size=10.5, anchor="midright")
        return bh

    def draw_empty(self, surf, area, icon, title, body, actions=(), busy=False, color=C.TEXT_2):
        """A centred message card in the picture area, with buttons. actions: [(label, kind, fn)]."""
        ui, pg = self.ui, self.pg
        w = min(ui.px(460), area.w - ui.px(32))
        body_h = ui.paragraph_height(body, 13, w - ui.px(56))
        h = ui.px(118) + body_h + (ui.px(64) if actions else ui.px(16))
        card = pg.Rect(0, 0, w, h)
        card.center = area.center
        ui.card(surf, card, 14, C.RAISED, C.BORDER_SOFT)
        top = card.y + ui.px(38)
        ui.circle(surf, (card.centerx, top), ui.px(22), (37, 41, 50))
        if busy:
            ui.spinner(surf, (card.centerx, top), 12, C.ACCENT)
        else:
            ui.icon(surf, icon, (card.centerx, top), 20, color)
        ui.text(surf, title, (card.centerx, top + ui.px(36)), 16, C.TEXT, "semibold", "center", max_w=w - ui.px(32))
        ui.paragraph(surf, body, (card.centerx, top + ui.px(54)), 13, C.TEXT_2, w - ui.px(56), align="center")
        if actions:
            widths = [ui.button_width(lbl, kind=kind) for lbl, kind, _ in actions]
            total = sum(widths) + ui.px(8) * (len(actions) - 1)
            bx = card.centerx - total // 2
            for (lbl, kind, fn), bw in zip(actions, widths):
                if ui.button(surf, (bx, card.bottom - ui.px(56), bw, ui.px(36)), lbl, ("empty", lbl), kind):
                    fn()
                bx += bw + ui.px(8)

    def draw_banner(self, surf, area, icon, text, actions):
        """A slim notice across the top of the picture (e.g. No signal), with buttons."""
        ui, pg = self.ui, self.pg
        widths = [ui.button_width(lbl, size=12) - ui.px(8) for lbl, _ in actions]
        tw = ui.measure(text, 13)[0]
        w = min(area.w - ui.px(24), ui.px(48) + tw + sum(widths) + ui.px(8) * len(actions) + ui.px(16))
        r = pg.Rect(0, 0, w, ui.px(44))
        r.midtop = (area.centerx, area.y + ui.px(14))
        ui.card(surf, r, 10, C.OVERLAY, C.BORDER)
        ui.icon(surf, icon, (r.x + ui.px(22), r.centery), 15, C.WARN)
        ui.text(surf, text, (r.x + ui.px(40), r.centery), 13, C.TEXT, anchor="midleft",
                max_w=r.w - ui.px(56) - sum(widths))
        bx = r.right - ui.px(6)
        for (lbl, fn), bw in reversed(list(zip(actions, widths))):
            bx -= bw
            if ui.button(surf, (bx, r.y + ui.px(6), bw, r.h - ui.px(12)), lbl, ("banner", lbl), "secondary", size=12):
                fn()
            bx -= ui.px(6)

    def draw_toast(self, surf, area):
        if not self.toasts:
            return
        text, icon, color, until = self.toasts[-1]
        left = until - time.time()
        if left <= 0:
            self.toasts = []
            return
        ui, pg = self.ui, self.pg
        alpha = min(1.0, left / 0.35, (time.time() - (until - 3.5)) / 0.15 if False else 1.0)
        tw = ui.measure(text, 13)[0]
        w = min(area.w - ui.px(32), tw + ui.px(64))
        r = pg.Rect(0, 0, w, ui.px(42))
        r.midbottom = (area.centerx, area.bottom - ui.px(16))
        layer = pg.Surface(surf.get_size(), pg.SRCALPHA)
        ui.card(layer, r, 10, C.OVERLAY, C.BORDER)
        ui.icon(layer, icon, (r.x + ui.px(22), r.centery), 15, color)
        ui.text(layer, text, (r.x + ui.px(42), r.centery), 13, C.TEXT, anchor="midleft", max_w=r.w - ui.px(56))
        layer.set_alpha(int(255 * max(0.0, alpha)))
        surf.blit(layer, (0, 0))

    def draw_picture(self, surf, area):
        """The device's screen, or the right empty/error state."""
        pg, cap = self.pg, self.cap
        seq, frame = cap.latest()
        if frame is None:
            self.frame_surf = None
        elif seq != self.last_seq or self.frame_key != area.size:
            self.last_seq, self.frame_key = seq, area.size
            fh, fw = frame.shape[:2]
            scale = min(area.w / fw, area.h / fh)
            tw, th = max(1, int(fw * scale)), max(1, int(fh * scale))
            self.video_rect = pg.Rect(area.x + (area.w - tw) // 2, area.y + (area.h - th) // 2, tw, th)
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            img = cv2.cvtColor(cv2.resize(frame, (tw, th), interpolation=interp), cv2.COLOR_BGR2RGB)
            self.frame_surf = pg.image.frombuffer(img.tobytes(), (tw, th), "RGB")
        pg.draw.rect(surf, C.BG, area)
        name = self.machines.name
        if not self.cfg["machines"]:
            self.frame_surf = None
            self.draw_empty(surf, area, "devices", "Add your first device",
                            "Show a computer's screen here and control it with your keyboard and mouse.",
                            [("Add device", "primary", lambda: self.open_device_dialog(None))], color=C.ACCENT_HOVER)
            return
        if self.frame_surf is not None:
            surf.blit(self.frame_surf, self.video_rect)
        if cap.mode == "net" and cap.status not in ("ok", "connecting"):
            self.draw_empty(surf, area, "warning", f"{name}: no picture over the network", cap.status,
                            [("Change source", "secondary", lambda: self.open_menu("source", self.source_rect(), True))],
                            color=C.WARN)
        elif self.frame_surf is None and cap.status.startswith(("cannot open", "capture read failed")):
            self.draw_empty(surf, area, "warning", f"Can't open {cap.input_name()}",
                            "Another app may be using it. Close that app, or choose another input.",
                            [("Change input", "secondary", lambda: self.open_menu("source", self.source_rect(), True)),
                             ("Find it", "secondary", self.choose_auto)], color=C.WARN)
        elif self.frame_surf is None:
            what = ("Connecting to the device…" if cap.mode == "net" else
                    "Looking for the input with a picture…" if cap.status == "scanning inputs" else
                    f"Opening {cap.input_name()}…")
            self.draw_empty(surf, area, "video", "Waiting for the picture", what, busy=True)
        elif cap.status == "no signal":
            self.draw_banner(surf, area, "no_signal", f"No signal on {cap.input_name()}",
                             [("Change input", lambda: self.open_menu("source", self.source_rect(), True)),
                              ("Find it", self.choose_auto)])

    def source_rect(self):
        """Where the video-source button is (the menu opens under it)."""
        if getattr(self, "_source_btn", None) is not None:
            return self._source_btn
        sw = self.window.size[0] if not self.panel else self.panel["W"]
        return self.pg.Rect(sw - self.ui.px(300), self.ui.px(9), self.ui.px(34), self.ui.px(34))

    # ------------------------------------------------------------------ input
    def to_norm(self, pos):
        r = self.video_rect
        x = (pos[0] - r.x) / max(1, r.w)
        y = (pos[1] - r.y) / max(1, r.h)
        return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0), r.collidepoint(pos) and self.frame_surf is not None

    def ui_key(self, ev):
        """A key while a dialog is open: edit the focused field, or close on Esc."""
        pg, ui = self.pg, self.ui
        if self.capture_hotkey:
            self.capture_hotkey_key(ev)
            return
        if ev.key == pg.K_ESCAPE:
            if ui.focus:
                ui.focus = None
            else:
                self.close_overlays()
            return
        ctrl = ev.mod & pg.KMOD_CTRL
        if ev.key == pg.K_BACKSPACE:
            ui.keys.append(("clear" if ctrl else "backspace", None))
        elif ctrl and ev.key == pg.K_v:
            ui.keys.append(("paste", read_clipboard()))
        elif ctrl and ev.key == pg.K_a:
            ui.keys.append(("clear", None))
        elif ev.key in (pg.K_RETURN, pg.K_KP_ENTER):
            ui.keys.append(("enter", None))
        elif ev.key == pg.K_TAB:
            order = ["dev-name", "dev-host"]
            ui.focus = order[(order.index(ui.focus) + 1) % 2] if ui.focus in order else order[0]

    def handle_events(self):
        pg, ui = self.pg, self.ui
        for ev in pg.event.get():
            if ev.type == self.PANEL_EVENT:
                self.on_panel_event(ev)
                continue
            if self.panel and self.panel["state"] != "open" and ev.type in (
                    pg.MOUSEMOTION, pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP, pg.MOUSEWHEEL):
                continue                                # sliding: the layout is moving under the mouse
            if ev.type == pg.KEYDOWN and not self.capture_hotkey and self.sdl_hotkey(ev):
                self.put_away()
                continue
            if ev.type == pg.QUIT:
                if self.tray or self.panel_key:
                    self.hide_window()                  # keeps running in the tray; quit from its menu
                else:
                    self.running = False
            elif ev.type == pg.TEXTINPUT:
                if self.dialog and ui.focus:
                    ui.keys.append(("text", ev.text))
            elif ev.type == pg.MOUSEBUTTONDOWN and ev.button in (1, 3) and (ui.wants(ev.pos) or self.modal):
                if ev.button == 1:
                    ui.press(ev.pos)
                    self.ui_pressing = True
                    if self.dialog and ui.focus and not any(r.collidepoint(ev.pos) for r in ui.regions):
                        ui.focus = None
            elif ev.type == pg.MOUSEBUTTONUP and ev.button in (1, 3) and (
                    self.ui_pressing or ui.wants(ev.pos) or self.modal):
                ui.release(ev.pos, right=ev.button == 3)
                if ev.button == 1:
                    self.ui_pressing = False
            elif ev.type == pg.MOUSEWHEEL and self.dialog:
                d = self.dialog
                d["scroll"] = d.get("scroll", 0) - int(ev.y * ui.px(48))
            elif ev.type == pg.KEYDOWN:
                if ev.scancode == HOST_KEY and not self.dialog:
                    self.host_down, self.host_combo = True, False
                    continue
                if self.dialog:
                    self.ui_key(ev)
                    continue
                if self.menu and not self.host_down:
                    self.menu_key(ev)
                    continue
                if self.host_down:
                    self.host_combo = True
                    self.host_shortcut(ev)
                    continue
                if not self.cfg["machines"]:
                    continue
                code = SDL_TO_CODE.get(ev.scancode) or keycode_fallback(pg, ev.key)
                if code:
                    self.keymap[ev.key] = code          # keyup must release what keydown pressed
                if code and code not in self.held:
                    self.held.add(code)
                    self.agent.send({"t": "k", "c": code, "d": 1})
            elif ev.type == pg.KEYUP:
                if ev.scancode == HOST_KEY and not self.dialog:
                    self.host_down = False
                    if not self.host_combo and self.cfg["machines"]:
                        self.set_grab(not self.grabbed)
                    continue
                code = SDL_TO_CODE.get(ev.scancode) or self.keymap.get(ev.key)
                if code and code in self.held:
                    self.held.discard(code)
                    self.agent.send({"t": "k", "c": code, "d": 0})
            elif self.modal and ev.type in (pg.MOUSEMOTION, pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP, pg.MOUSEWHEEL):
                continue
            elif ev.type == pg.MOUSEMOTION:
                x, y, inside = self.to_norm(ev.pos)
                if inside or ev.buttons[0] or ev.buttons[2]:
                    self.pending_move = (x, y)
            elif ev.type in (pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP):
                btn = {1: 0, 2: 1, 3: 2, 6: 3, 7: 4}.get(ev.button)
                if btn is None:
                    continue
                x, y, inside = self.to_norm(ev.pos)
                down = ev.type == pg.MOUSEBUTTONDOWN
                if down and not inside:
                    continue
                self.agent.send({"t": "m", "x": x, "y": y})
                self.agent.send({"t": "b", "b": btn, "d": int(down)})
            elif ev.type == pg.MOUSEWHEEL:
                if self.video_rect.collidepoint(pg.mouse.get_pos()):
                    self.agent.send({"t": "w", "dx": int(ev.x), "dy": int(ev.y)})
            elif ev.type == pg.WINDOWFOCUSLOST:
                self.release_all()
                self.host_down = False
                if self.grabbed:
                    self.set_grab(False)
            elif ev.type in (pg.WINDOWSIZECHANGED, pg.VIDEORESIZE, pg.WINDOWMOVED):
                self.last_seq = -1
        if self.pending_move:
            self.agent.send({"t": "m", "x": self.pending_move[0], "y": self.pending_move[1]})
            self.pending_move = None

    def host_shortcut(self, ev):
        """Right Ctrl + key."""
        pg = self.pg
        k = ev.key
        if k == pg.K_i:
            self.open_menu("source", self.source_rect(), right=True)
        elif k == pg.K_m:
            self.open_menu("devices", self.pg.Rect(self.ui.px(46), self.ui.px(9), self.ui.px(34), self.ui.px(34)))
        elif k == pg.K_n:
            self.open_device_dialog(None)
        elif k == pg.K_SLASH:
            self.open_help()
        elif k == pg.K_COMMA:
            self.open_settings()
        elif k == pg.K_f and self.panel:
            self.show_window()
        elif k == pg.K_f:
            self.fullscreen = not self.fullscreen
            if self.fullscreen:
                self.window.set_fullscreen(desktop=True)
            else:
                self.window.set_windowed()
            self.last_seq = -1
        elif k == pg.K_q:
            self.running = False
        elif k == pg.K_v:
            self.paste()
        elif k == pg.K_s:
            self.choose_auto()
        elif pg.K_1 <= k <= pg.K_9:
            self.choose_port(k - pg.K_0)

    # ------------------------------------------------------------------ frame
    def draw(self, sliding):
        pg, ui = self.pg, self.ui
        p = self.panel
        screen = pg.Surface((p["W"], p["H"])) if sliding else self.window.get_surface()
        sw, sh = screen.get_size()
        ui.begin(pg.mouse.get_pos() if not sliding else (-1, -1))
        ui.modal = self.modal
        ui.clip = None
        hh = self.draw_header(screen, sw) if not self.dialog else ui.px(HEADER_H)
        if self.dialog:                                  # header still visible under the backdrop
            regions = list(ui.regions)
            self.draw_header(screen, sw)
            ui.regions = regions
        bh = self.draw_status(screen, sw, sh)
        area = pg.Rect(0, hh, sw, sh - hh - bh)
        self.draw_picture(screen, area)
        self.draw_toast(screen, area)
        if self.menu:
            self.draw_menu(screen)
        if self.dialog:
            kind = self.dialog["kind"]
            {"settings": self.draw_settings, "device": self.draw_device_dialog,
             "remove": self.draw_remove, "help": self.draw_help}[kind](screen)
            if self.dialog and self.dialog["kind"] == "device" and self.ui.focus is None:
                pass
        ui.draw_tooltip(screen)
        ui.end()
        if sliding:
            w = p["w"]
            src = pg.Rect(0, 0, w, sh) if p["side"] == "right" else pg.Rect(sw - w, 0, w, sh)
            self.window.get_surface().blit(screen, (0, 0), src)
        self.window.flip()

        # Like a VM viewer: over a live picture, show only the device's own pointer, not the PC's too.
        hide_pointer = (self.frame_surf is not None and not self.modal and not sliding and self.agent.connected
                        and self.agent.mouse is True and self.window.focused
                        and self.video_rect.collidepoint(pg.mouse.get_pos()))
        if hide_pointer == pg.mouse.get_visible():
            pg.mouse.set_visible(not hide_pointer)

    def run(self):
        pg = self.pg
        while self.running:
            self.handle_events()
            if not self.window_visible and not self.panel:
                self.clock.tick(30)                      # hidden until the panel hotkey
                continue
            if time.time() - self.last_dpi_check > 1.0 and not self.panel:
                self.last_dpi_check = time.time()
                self.update_scale()
            sliding = self.panel_step()
            if not self.window_visible and not self.panel:
                continue
            self.draw(sliding)
            caption = f"Porthole · {self.machines.name}" if self.cfg["machines"] else "Porthole"
            if caption != self.last_caption:
                self.window.title = caption
                self.last_caption = caption
            self.clock.tick(120)
        self.release_all()
        if self.tray:
            self.tray.remove()
        time.sleep(0.2)
        pg.quit()


def run_window(cfg, cap, agent, machines, start_in_window=False, start_panel=False):
    PortholeWindow(cfg, cap, agent, machines, start_in_window, start_panel).run()


# --------------------------------------------------------------------------- agent install

def upsert_machine(cfg, name, host=None, port=None, video=None, mouse=None):
    """Add or update a machine in config.json."""
    ms = cfg.setdefault("machines", [])
    m = next((x for x in ms if x["name"] == name), None)
    if m is None:
        m = {"name": name, "host": host or "", "input": None}
        ms.append(m)
    if host:
        m["host"] = host
    if port is not None:
        m["input"] = port
        m["video"] = "hdmi"
    if video:
        m["video"] = video
    if mouse is not None:
        m["mouse"] = mouse
    if not cfg.get("machine"):
        cfg["machine"] = name
    save_config(cfg)
    return m


def describe_video(m):
    v = m.get("video") or "hdmi"
    if v != "hdmi":
        return NET_SOURCES.get(v, v)
    return f"HDMI-{m['input']}" if m.get("input") else "HDMI (auto)"


def parse_port(spec):
    if spec is None:
        return None
    port = port_of(str(spec)) if not str(spec).isdigit() else int(spec)
    if port is None:
        sys.exit(f"--input wants an HDMI port number like 2, got {spec!r}")
    return port


AGENT_FILES = ["porthole_agent.py", "porthole_mac.py", "install.sh", "install_mac.sh"]
MAC_ONLY_FLAGS = ("--ctrl-as-cmd", "--no-ctrl-as-cmd")


def install_agent(cfg, target, name=None, port=None, video=None, extra=(), register=True):
    """scp the agent to a board or Mac and run its installer (prompts for passwords).

    Linux boards: install.sh with sudo (systemd service). Macs: install_mac.sh as the SSH
    user (a LaunchAgent in that user's desktop session; no sudo needed).
    """
    import tempfile

    host = target.split("@", 1)[-1]

    def fail(returncode, is_mac):
        check = ("tail ~/Library/Logs/porthole-agent.log" if is_mac else "systemctl status porthole-agent")
        print(f"\nInstall step failed (exit {returncode}). If the connection dropped near the end, the agent "
              f"may still be installed: on the {'Mac' if is_mac else 'board'} run '{check}'.")
        sys.exit(1)

    # The token travels in a private file (deleted by the installer), never on a command line,
    # so it can't show up in the board's process list or in an error message here.
    with tempfile.TemporaryDirectory() as tmp:
        token_file = pathlib.Path(tmp) / "token"
        token_file.write_text(cfg["agent_token"], encoding="utf-8")
        print("Copying agent to " + target)
        # The password prompt goes to the terminal; only uname's answer comes back on stdout.
        result = subprocess.run(["ssh", target, "uname -s && umask 077 && rm -rf /tmp/porthole-agent && "
                                                "mkdir -p /tmp/porthole-agent"], stdout=subprocess.PIPE, text=True)
        if result.returncode != 0:
            fail(result.returncode, False)
        is_mac = result.stdout.strip().splitlines()[:1] == ["Darwin"]
        result = subprocess.run(["scp", "-q", *(str(HERE / "agent" / f) for f in AGENT_FILES), str(token_file),
                                 f"{target}:/tmp/porthole-agent/"])
        if result.returncode != 0:
            fail(result.returncode, is_mac)
        args = f"--token-file /tmp/porthole-agent/token {cfg['agent_port']}"
        if is_mac:
            print("Installing on the Mac (as your Mac user, no sudo) ...")
            command = f"bash /tmp/porthole-agent/install_mac.sh {args} {' '.join(extra)}"
        else:
            ignored = [f for f in extra if f in MAC_ONLY_FLAGS]
            if ignored:
                print(f"Note: {', '.join(ignored)} only applies to Macs; ignored.")
            print("Installing (sudo will ask for the board's password) ...")
            command = (f"sudo bash /tmp/porthole-agent/install.sh {args} "
                       f"{' '.join(f for f in extra if f not in MAC_ONLY_FLAGS)}")
        result = subprocess.run(["ssh", "-t", target, command.rstrip()])
        if result.returncode != 0:
            fail(result.returncode, is_mac)
    if not register:                    # the app already saved the device (Add device)
        print(f"Done. The agent is installed on {host}. Porthole connects to it by itself.")
        return
    m = upsert_machine(cfg, name or host, host, port, video)
    where = describe_video(m)
    print(f"Done. Machine {m['name']!r}: keyboard/mouse -> {host}:{cfg['agent_port']}, video -> {where}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-window", action="store_true", help="web UI only, no local window")
    ap.add_argument("--window", action="store_true",
                    help="start with the normal window open (by default Porthole starts hidden in the tray: "
                         "panel_hotkey, Ctrl+Alt+K by default, pulls it out as a side panel)")
    ap.add_argument("--panel", action="store_true",
                    help="start with the side panel pulled out (what the Xbox app entry uses). If Porthole is "
                         "already running, any launch just pulls out its panel (or, with --window, its window)")
    ap.add_argument("--install-agent", metavar="USER@HOST", help="install the input agent on a board over SSH "
                                                                 "and add it as a machine")
    ap.add_argument("--add-machine", metavar="NAME", help="add/update a machine (with --host / --input)")
    ap.add_argument("--remove-machine", metavar="NAME")
    ap.add_argument("--name", help="machine name for --install-agent (default: the host)")
    ap.add_argument("--skip-register", action="store_true",
                    help="with --install-agent: only install, don't add the machine to config.json "
                         "(the app's Add device does that itself)")
    ap.add_argument("--host", help="keyboard/mouse host for --add-machine")
    ap.add_argument("--mouse", choices=["on", "off", "clicks"],
                    help="with --add-machine, for when Deskflow or another KVM already drives this machine's "
                         "mouse: clicks = your clicks and scrolling on the picture still reach it (its pointer "
                         "stays with the KVM); off = keys only. on (default) = Porthole drives the pointer")
    ap.add_argument("--input", metavar="PORT", help="HDMI port the machine is on, e.g. 2 for HDMI-2")
    ap.add_argument("--wifi-steer", action="store_true",
                    help="with --install-agent: keep the board on the mesh WiFi node with the fastest link")
    ap.add_argument("--no-wifi-steer", action="store_true", help="with --install-agent: turn WiFi steering off")
    ap.add_argument("--wifi-no-powersave", action="store_true",
                    help="with --install-agent: stop WiFi power saving (it delays input by up to 0.5 s)")
    ap.add_argument("--ctrl-as-cmd", action="store_true",
                    help="with --install-agent on a Mac: PC Ctrl acts as Command (Ctrl+C copies), Win key as Control")
    ap.add_argument("--no-ctrl-as-cmd", action="store_true",
                    help="with --install-agent on a Mac: Ctrl is Control and the Win key is Command (default)")
    ap.add_argument("--video", choices=["hdmi", *NET_SOURCES],
                    help="video source: hdmi (capture card), or screen/camera/test over the network")
    ap.add_argument("--machine", metavar="NAME", help="start on this machine")
    ap.add_argument("--create-shortcut", action="store_true",
                    help="add Porthole (with its icon) to the Start menu and the desktop")
    ap.add_argument("--list-machines", action="store_true")
    ap.add_argument("--list-inputs", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()
    if args.create_shortcut:
        create_shortcuts()
        return
    if args.list_inputs:
        for idx, name in list_inputs(cfg["device_filter"]):
            print(f"{name:22} (capture index {idx})")
        return
    if args.install_agent:
        extra = [flag for flag, on in (("--wifi-steer", args.wifi_steer), ("--no-wifi-steer", args.no_wifi_steer),
                                       ("--wifi-no-powersave", args.wifi_no_powersave),
                                       ("--ctrl-as-cmd", args.ctrl_as_cmd),
                                       ("--no-ctrl-as-cmd", args.no_ctrl_as_cmd)) if on]
        install_agent(cfg, args.install_agent, args.name, parse_port(args.input), args.video, extra,
                      register=not args.skip_register)
        return
    if args.add_machine:
        m = upsert_machine(cfg, args.add_machine, args.host, parse_port(args.input), args.video,
                           {None: None, "on": True, "off": False, "clicks": "clicks"}[args.mouse])
        print("saved:", m)
        return
    if args.remove_machine:
        cfg["machines"] = [m for m in cfg.get("machines", []) if m["name"] != args.remove_machine]
        if cfg.get("machine") == args.remove_machine:
            cfg["machine"] = cfg["machines"][0]["name"] if cfg["machines"] else ""
        save_config(cfg)
        print("removed", args.remove_machine)
        return

    cap = Capture(cfg)
    machines = Machines(cfg, cap)
    if args.list_machines:
        for m in cfg["machines"]:
            mark = "*" if m["name"] == machines.current["name"] else " "
            print(f"{mark} {m['name']:16} host={m.get('host') or '-':20} video={describe_video(m)}"
                  + {False: "  mouse=off", "clicks": "  mouse=clicks"}.get(m.get("mouse"), ""))
        return
    if sys.platform == "win32" and not claim_single_instance():
        # Porthole is already running (web port, hotkey and tray are taken): bring that one forward.
        if hand_off_to_running("window" if args.window else "show"):
            log.info("Porthole is already running: showed it")
        else:
            log.warning("Porthole is already running, without a tray icon (--no-window?)")
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "Porthole is already running, but without its window "
                                             "(it was started with --no-window). Quit it first.",
                                             "Porthole", 0x40)          # MB_ICONINFORMATION
        return
    if sys.platform == "win32":
        keep_on_game_bar(cfg)
    if args.machine:
        if not machines.get(args.machine):
            sys.exit(f"no machine named {args.machine!r}; try --list-machines")
        cfg["machine"] = args.machine
        machines = Machines(cfg, cap)
    cap.start()
    agent = start_backend(cfg, cap, machines)
    if args.no_window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    run_window(cfg, cap, agent, machines, start_in_window=args.window, start_panel=args.panel)


if __name__ == "__main__":
    main()
