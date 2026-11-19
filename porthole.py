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
    "panel_width": 0.3,            # fraction of the monitor's width
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

    def __init__(self, pygame, event_type, bindings):
        super().__init__(daemon=True, name="hotkeys")
        self.pygame, self.event_type, self.bindings = pygame, event_type, bindings   # [(mods, vk, payload)]
        self.failed = []
        self.ready = threading.Event()

    def run(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        for n, (mods, vk, payload) in enumerate(self.bindings, 1):
            if not user32.RegisterHotKey(None, n, mods | MOD_NOREPEAT, vk):
                self.failed.append(payload)
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
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
    art = write_cover_art()
    print("\nTo reach Porthole from the Xbox button: open the Xbox app, My Library, the + button, "
          "Browse, and add it as an app with:")
    print(f"  Program:    {pythonw}")
    print(f"  Arguments:  \"{HERE / 'porthole.py'}\" --panel")
    print("  Name:       Porthole")
    print(f"  Image:      {art}")
    print("(Choosing it starts Porthole with its side panel out, or pulls the panel out if it's already running.)")


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


PANEL_TAB_H = 44          # machine tabs across the top of the side panel
PANEL_SLIDE_S = 0.18      # slide-in/out time


def run_window(cfg, cap, agent, machines, start_in_window=False, start_panel=False):
    """The local window. Three states: hidden (tray only), the side panel, or a normal window.

    By default Porthole starts hidden: the panel hotkey or the tray icon pulls it out, and
    the same hotkey puts it away. Right Ctrl + F (or the panel's Window button) pops the
    panel out into a normal window; closing that window hides it again.
    """
    os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")
    import pygame

    pygame.init()
    PANEL_EVENT = pygame.event.custom_type()
    on_windows = sys.platform == "win32"

    # Panel hotkey (system-wide, works while a game has focus) and the tray icon.
    panel_key = parse_hotkey(cfg.get("panel_hotkey", "ctrl+alt+k")) if on_windows else None
    hotkey_note = ""
    if panel_key:
        mods, vk, _, label = panel_key
        bindings = [(mods, vk, {"action": "toggle"})]
        if mods:   # never steal the bare number keys
            bindings += [(mods, 0x31 + n, {"action": "machine", "index": n}) for n in range(9)]
        hotkeys = GlobalHotkeys(pygame, PANEL_EVENT, bindings)
        hotkeys.start()
        hotkeys.ready.wait(2)
        if any(p["action"] == "toggle" for p in hotkeys.failed):
            hotkey_note = f"  ({label} is taken by another app: set panel_hotkey in config.json)"
            log.warning("panel hotkey %s is taken by another app; set panel_hotkey in config.json", label)
            panel_key = None
        else:
            log.info("side panel: %s (and %s+1..9 for a machine)", label, label.rsplit("+", 1)[0])
    elif on_windows and cfg.get("panel_hotkey"):
        log.warning("panel_hotkey %r not understood (try e.g. ctrl+alt+k)", cfg.get("panel_hotkey"))
    tray = None
    if on_windows:
        tray = TrayIcon(pygame, PANEL_EVENT, "Porthole" + (f" ({panel_key[3]})" if panel_key else ""),
                        panel_key[3] if panel_key else "")
        tray.start()
        tray.ready.wait(3)
        if not tray.ok:
            tray = None
    # Hidden only when there's a way to bring it back.
    start_hidden = not start_in_window and bool(panel_key or tray)
    if start_panel and start_hidden:
        # --panel (the Xbox app entry): slide out right away, on the monitor of whatever launched us.
        fg = 0
        if on_windows:
            import ctypes
            ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
            fg = ctypes.windll.user32.GetForegroundWindow() or 0
        pygame.event.post(pygame.event.Event(PANEL_EVENT, {"action": "show", "fg": fg}))

    if on_windows:
        # Its own taskbar button and icon, instead of being grouped under Python.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Porthole.App")
        except (OSError, AttributeError):
            pass
    window = pygame.Window("Porthole", (1280, 720), resizable=True, hidden=start_hidden)
    icon_px = porthole_icon_rgba(64)
    window.set_icon(pygame.image.frombuffer(icon_px.tobytes(), (64, 64), "RGBA"))
    if on_windows:
        set_window_icons(window.handle)
    window_visible = not start_hidden            # the normal (non-panel) window is on screen
    normal_geom = None                           # (size, position) of the normal window when it was put away
    told_about_tray = False
    font = pygame.font.SysFont("segoeui", 18)
    clock = pygame.time.Clock()

    # Side panel: None when closed, else its geometry and slide state.
    panel = None
    panel_tabs = []                      # [(rect, machine name)] for clicks
    panel_buttons = []                   # [(rect, action)] in the tab bar
    if start_hidden:
        hint = f"Press {panel_key[3]} to pull it out" if panel_key else "Click the tray icon to show it"
        log.info("Porthole is in the tray. %s.", hint)
        if tray and not start_panel:
            tray.notify("Porthole is running", f"{hint}, even in a game. Right-click the tray icon for more.")

    held = set()
    keymap = {}
    grabbed = False
    fullscreen = False
    host_down = False
    host_combo = False
    last_seq = -1
    video_rect = pygame.Rect(0, 0, 1, 1)
    frame_surf = None
    toast, toast_until = (f"{machines.name}: click to control.  Right Ctrl = capture keyboard.  "
                          "Right Ctrl + M = machine, Right Ctrl + I = input"
                          + (f".  {panel_key[3]} = side panel" if panel_key else hotkey_note)), time.time() + 8
    picker = None                        # None | "input" | "machine"
    picker_rows = []                     # [(rect, action)] for clicks
    small = pygame.font.SysFont("segoeui", 16)
    last_caption = ""
    pending_move = None

    def set_grab(on):
        nonlocal grabbed, toast, toast_until
        grabbed = on
        window.grab_keyboard = on
        toast = f"Keyboard captured (Win, Alt+Tab go to {machines.name}). Right Ctrl to release" if on \
            else "Keyboard released"
        toast_until = time.time() + 3

    def release_all():
        held.clear()
        agent.send({"t": "r"})

    def choose_port(port):
        nonlocal toast, toast_until, picker
        if cap.select_port(port) is None:
            toast, toast_until = f"No HDMI-{port} on the capture card", time.time() + 3
        else:
            toast, toast_until = f"{machines.name} input: HDMI-{port}", time.time() + 3
            picker = None

    def choose_auto():
        nonlocal toast, toast_until, picker
        cap.scan()
        toast, toast_until = "Looking for the input with a picture...", time.time() + 4
        picker = None

    def choose_machine(name):
        nonlocal toast, toast_until, picker, last_seq
        held.clear()
        if machines.switch(name):
            toast, toast_until = f"Switched to {name}", time.time() + 3
            last_seq = -1
        picker = None

    def picker_items():
        if picker == "input":
            auto_on = cfg.get("auto_input", True)
            items = [(str(port_of(n) or "?"), n, ("port", port_of(n)), i == cap.index and not auto_on)
                     for i, n in cap.inputs]
            net = cap.mode == "net"
            items = [(k, label, action, current and not net) for k, label, action, current in items]
            items.append(("S", "Find it automatically", ("auto", None), auto_on and not net))
            for key, src in (("W", "screen"), ("C", "camera"), ("T", "test")):
                items.append((key, NET_SOURCES[src] + "  (over the network, no cable)", ("net", src),
                              net and cap.net_src == src))
            return f"{machines.name}: video source (key or click)", items
        items = []
        for n, m in enumerate(cfg["machines"][:9]):
            where = f"HDMI-{m['input']}" if m.get("input") else "auto input"
            items.append((str(n + 1), f"{m['name']}   ({m.get('host') or 'no host'}, {where})",
                          ("machine", m["name"]), m["name"] == machines.current["name"]))
        if not items:
            items.append(("", "No machines yet: run --install-agent (see README)", ("none", None), False))
        return "Machine (number or click)", items

    def activate(action):
        nonlocal picker, toast, toast_until
        kind, value = action
        if kind == "port":
            choose_port(value)
        elif kind == "auto":
            choose_auto()
        elif kind == "machine":
            choose_machine(value)
        elif kind == "net":
            machines.set_video(value)
            toast, toast_until = f"{machines.name}: {NET_SOURCES[value]}", time.time() + 3
            picker = None
        else:
            picker = None

    def draw_picker(screen):
        picker_rows.clear()
        sw, sh = screen.get_size()
        shade = pygame.Surface((sw, sh), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 170))
        screen.blit(shade, (0, 0))
        heading, lines = picker_items()
        row_h, box_w = 40, min(520, sw - 24)
        box = pygame.Rect(0, 0, box_w, 70 + row_h * len(lines) + 30)
        box.center = (sw // 2, sh // 2)
        pygame.draw.rect(screen, (28, 28, 34), box, border_radius=12)
        title = font.render(heading, True, (255, 255, 255))
        screen.blit(title, (box.x + 20, box.y + 18))
        mouse = pygame.mouse.get_pos()
        for n, (key, label, action, current) in enumerate(lines):
            r = pygame.Rect(box.x + 12, box.y + 58 + n * row_h, box_w - 24, row_h - 6)
            hover = r.collidepoint(mouse)
            pygame.draw.rect(screen, (59, 91, 219) if current else (52, 52, 60) if hover else (38, 38, 46),
                             r, border_radius=8)
            screen.blit(font.render(key, True, (255, 255, 255)), (r.x + 14, r.y + 6))
            screen.blit(font.render(label, True, (230, 230, 236)), (r.x + 50, r.y + 6))
            picker_rows.append((r, action))
        hint = small.render("Esc closes", True, (150, 150, 160))
        screen.blit(hint, (box.x + 20, box.bottom - 28))

    def to_norm(pos):
        x = (pos[0] - video_rect.x) / max(1, video_rect.w)
        y = (pos[1] - video_rect.y) / max(1, video_rect.h)
        return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0), video_rect.collidepoint(pos)

    # -- hidden / side panel / normal window
    def bring_forward():
        win_focus(window.handle)
        window.focus()

    def panel_open(fg):
        """Slide the panel out on the monitor of window `fg` (the game), or the one under the mouse."""
        nonlocal panel, last_seq, picker, toast, toast_until
        toast, toast_until = ({True: "Click to control.  Right Ctrl = capture keyboard",
                               "clicks": f"Clicks, scrolling and typing here go to {machines.name}",
                               }.get(agent.mouse, f"Mouse is off for {machines.name}: move across with your KVM.  "
                                                  "Typing here goes to it"), time.time() + 4)
        if panel:                                       # closing: turn around
            panel.update(state="opening", t0=time.time(), start=panel["w"])
            bring_forward()
            return
        game = fg if fg and fg != window.handle else 0
        left, top, right, bottom = win_monitor_rect(game)
        mw, mh = right - left, bottom - top
        try:
            frac = float(cfg.get("panel_width", 0.3))
        except (TypeError, ValueError):
            frac = 0.3
        w = max(min(480, mw), min(mw, round(mw * frac)))
        h = min(mh, PANEL_TAB_H + round(w * 9 / 16))
        panel = {"state": "opening", "t0": time.time(), "start": 0, "w": 1, "W": w, "H": h,
                 "left": left, "top": top, "right": right, "bottom": bottom, "y": top + (mh - h) // 2,
                 "side": "left" if cfg.get("panel_side") == "left" else "right", "game": game}
        picker = None
        window.borderless = True
        window.resizable = False
        window.always_on_top = True
        panel_place(1)
        window.show()
        bring_forward()
        last_seq = -1

    def panel_place(w):
        panel["w"] = w = max(1, min(panel["W"], w))
        window.size = (w, panel["H"])
        x = panel["right"] - w if panel["side"] == "right" else panel["left"]
        window.position = (x, panel["y"])

    def let_go():
        """Stop sending input before the window goes away."""
        nonlocal picker
        release_all()
        if grabbed:
            set_grab(False)
        picker = None

    def panel_close():
        if not panel or panel["state"] == "closing":
            return
        let_go()
        panel.update(state="closing", t0=time.time(), start=panel["w"])

    def normal_frame():
        window.always_on_top = False
        window.borderless = False
        window.resizable = True
        if on_windows:
            set_window_icons(window.handle)             # the frame change drops the title-bar icon

    def panel_finished_closing():
        nonlocal panel, last_seq
        game = panel["game"]
        panel = None
        window.hide()
        normal_frame()
        last_seq = -1
        win_focus(game)                                 # straight back into the game

    def panel_step():
        """Advance the slide animation. True while it's moving."""
        if not panel or panel["state"] == "open":
            return False
        p = min(1.0, (time.time() - panel["t0"]) / PANEL_SLIDE_S)
        eased = 1 - (1 - p) ** 3
        goal = panel["W"] if panel["state"] == "opening" else 0
        w = round(panel["start"] + (goal - panel["start"]) * eased)
        if p >= 1:
            if panel["state"] == "closing":
                panel_finished_closing()
                return False
            panel["state"] = "open"
            panel_place(panel["W"])
            return False
        panel_place(w)
        return True

    def show_window():
        """Pop out into a normal, resizable window (from the panel or from hidden)."""
        nonlocal panel, window_visible, last_seq, toast, toast_until
        if window_visible:
            bring_forward()
            return
        area = (panel["left"], panel["top"], panel["right"], panel["bottom"]) if panel else win_monitor_rect(0)
        panel = None
        normal_frame()
        if normal_geom:
            window.size, window.position = normal_geom
        else:
            w, h = min(1280, area[2] - area[0] - 80), min(720, area[3] - area[1] - 120)
            window.size = (w, h)
            window.position = (area[0] + (area[2] - area[0] - w) // 2, area[1] + (area[3] - area[1] - h) // 2)
        window.show()
        window_visible = True
        bring_forward()
        last_seq = -1
        put_away = panel_key[3] if panel_key else "closing the window"
        toast, toast_until = f"Window mode.  {put_away} puts it away", time.time() + 4

    def hide_window():
        """Put the normal window away (it keeps running in the tray)."""
        nonlocal window_visible, normal_geom, fullscreen, told_about_tray
        let_go()
        if fullscreen:
            window.set_windowed()
            fullscreen = False
        normal_geom = (window.size, window.position)
        window.hide()
        window_visible = False
        if tray and not told_about_tray:
            told_about_tray = True
            tray.notify("Porthole is still running",
                        (f"{panel_key[3]} brings it back. " if panel_key else "")
                        + "Right-click the tray icon to quit.")

    def on_panel_event(ev):
        nonlocal toast, toast_until, running
        action = ev.action
        if action == "quit":
            running = False
            return
        if action == "window":
            show_window()
            return
        if action == "machine":
            ms = cfg["machines"]
            if ev.index >= len(ms):
                if panel or window_visible:
                    toast, toast_until = f"No machine {ev.index + 1}", time.time() + 2
                return
            if ms[ev.index]["name"] != machines.current["name"]:
                choose_machine(ms[ev.index]["name"])
        focused = ev.fg == window.handle
        if window_visible:
            # Normal window: the hotkey brings it forward, and puts it away when it's in front.
            if focused and action == "toggle":
                hide_window()
            else:
                bring_forward()
        elif not panel or panel["state"] == "closing":
            panel_open(ev.fg)
        elif action == "panel":                         # tray click: open <-> closed
            panel_close()
        elif not focused:
            # Open but the game has focus: hop back into the panel. Pressing again closes it.
            bring_forward()
        elif action == "toggle":
            panel_close()

    def sdl_hotkey(ev):
        """The panel hotkey as seen inside the window (Windows doesn't fire it while the keyboard is captured)."""
        if not panel_key or ev.key != pygame.key.key_code(panel_key[2]):
            return False
        mods = panel_key[0]
        want = {pygame.KMOD_CTRL: mods & 0x2, pygame.KMOD_ALT: mods & 0x1,
                pygame.KMOD_SHIFT: mods & 0x4, pygame.KMOD_GUI: mods & 0x8}
        return all(bool(ev.mod & m) == bool(on) for m, on in want.items())

    def draw_tabs(screen):
        panel_tabs.clear()
        panel_buttons.clear()
        sw = screen.get_width()
        pygame.draw.rect(screen, (22, 22, 28), (0, 0, sw, PANEL_TAB_H))
        # Buttons on the right: pop out to a window, put away.
        right = sw - 8
        for action, text in (("hide", "Hide"), ("window", "Window")):
            label = small.render(text, True, (220, 220, 228))
            r = pygame.Rect(0, 8, label.get_width() + 20, PANEL_TAB_H - 16)
            r.right = right
            hover = r.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(screen, (58, 58, 68) if hover else (36, 36, 44), r, border_radius=6)
            screen.blit(label, label.get_rect(center=r.center))
            panel_buttons.append((r, action))
            right = r.x - 6
        x = 8
        for n, m in enumerate(cfg["machines"][:9]):
            current = m["name"] == machines.current["name"]
            label = font.render(f"{n + 1}  {m['name']}", True, (255, 255, 255) if current else (200, 200, 208))
            r = pygame.Rect(x, 6, label.get_width() + 34, PANEL_TAB_H - 12)
            if r.right > right - 6:
                break
            pygame.draw.rect(screen, (59, 91, 219) if current else (40, 40, 48), r, border_radius=8)
            screen.blit(label, (r.x + 12, r.y + (r.h - label.get_height()) // 2))
            if current:
                dot = (90, 210, 120) if agent.connected else (240, 190, 80)
                pygame.draw.circle(screen, dot, (r.right - 11, r.centery), 4)
            panel_tabs.append((r, m["name"]))
            x = r.right + 6
        if panel_key:
            hint = small.render(panel_key[3], True, (120, 120, 130))
            if hint.get_width() < right - x - 12:
                screen.blit(hint, (right - hint.get_width() - 6, (PANEL_TAB_H - hint.get_height()) // 2))

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == PANEL_EVENT:
                on_panel_event(ev)
                continue
            if panel and panel["state"] != "open" and ev.type in (
                    pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEWHEEL):
                continue                                # sliding: the layout is moving under the mouse
            if ev.type == pygame.KEYDOWN and sdl_hotkey(ev):
                if panel and panel["state"] != "closing":
                    panel_close()
                elif window_visible:
                    hide_window()
                continue
            if ev.type == pygame.QUIT:
                if tray or panel_key:
                    hide_window()                       # keeps running in the tray; quit from its menu
                else:
                    running = False
            elif panel and ev.type == pygame.MOUSEBUTTONDOWN and ev.pos[1] < PANEL_TAB_H and not picker:
                for r, action in panel_buttons:
                    if r.collidepoint(ev.pos):
                        if action == "window":
                            show_window()
                        else:
                            panel_close()
                        break
                else:
                    for r, name in panel_tabs:
                        if r.collidepoint(ev.pos) and name != machines.current["name"]:
                            choose_machine(name)
                continue
            elif ev.type == pygame.KEYDOWN:
                if ev.scancode == HOST_KEY:
                    host_down, host_combo = True, False
                    continue
                if picker and not host_down:
                    pressed = (ev.unicode or "").upper()
                    if ev.key in (pygame.K_ESCAPE, pygame.K_RETURN):
                        picker = None
                    else:
                        for key, _, action, _ in picker_items()[1]:
                            if key and key == pressed:
                                activate(action)
                                break
                    continue
                if host_down:
                    host_combo = True
                    if ev.key in (pygame.K_i, pygame.K_m):
                        want = "input" if ev.key == pygame.K_i else "machine"
                        picker = None if picker == want else want
                        release_all()
                    elif ev.key == pygame.K_f and panel:
                        show_window()                   # panel -> full window
                    elif ev.key == pygame.K_f:
                        fullscreen = not fullscreen
                        if fullscreen:
                            window.set_fullscreen(desktop=True)
                        else:
                            window.set_windowed()
                        last_seq = -1
                    elif ev.key == pygame.K_q:
                        running = False
                    elif ev.key == pygame.K_v:
                        text = read_clipboard()
                        if text:
                            agent.send({"t": "type", "s": text})
                            toast, toast_until = f"Typing {len(text)} chars from clipboard", time.time() + 3
                    elif ev.key == pygame.K_s:
                        choose_auto()
                    elif pygame.K_1 <= ev.key <= pygame.K_9:
                        choose_port(ev.key - pygame.K_0)
                    continue
                code = SDL_TO_CODE.get(ev.scancode) or keycode_fallback(pygame, ev.key)
                if code:
                    keymap[ev.key] = code  # keyup must release what keydown pressed
                if code and code not in held:
                    held.add(code)
                    agent.send({"t": "k", "c": code, "d": 1})
            elif ev.type == pygame.KEYUP:
                if ev.scancode == HOST_KEY:
                    host_down = False
                    if not host_combo:
                        set_grab(not grabbed)
                    continue
                code = SDL_TO_CODE.get(ev.scancode) or keymap.get(ev.key)
                if code and code in held:
                    held.discard(code)
                    agent.send({"t": "k", "c": code, "d": 0})
            elif picker and ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONUP, pygame.MOUSEWHEEL):
                continue
            elif picker and ev.type == pygame.MOUSEBUTTONDOWN:
                for r, action in picker_rows:
                    if r.collidepoint(ev.pos):
                        activate(action)
                        break
                else:
                    picker = None
                continue
            elif ev.type == pygame.MOUSEMOTION:
                x, y, inside = to_norm(ev.pos)
                if inside or ev.buttons[0] or ev.buttons[2]:
                    pending_move = (x, y)
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                btn = {1: 0, 2: 1, 3: 2, 6: 3, 7: 4}.get(ev.button)
                if btn is None:
                    continue
                x, y, inside = to_norm(ev.pos)
                down = ev.type == pygame.MOUSEBUTTONDOWN
                if down and not inside:
                    continue
                agent.send({"t": "m", "x": x, "y": y})
                agent.send({"t": "b", "b": btn, "d": int(down)})
            elif ev.type == pygame.MOUSEWHEEL:
                agent.send({"t": "w", "dx": int(ev.x), "dy": int(ev.y)})
            elif ev.type == pygame.WINDOWFOCUSLOST:
                release_all()
                host_down = False
                if grabbed:
                    set_grab(False)
            elif ev.type in (pygame.WINDOWSIZECHANGED, pygame.VIDEORESIZE):
                last_seq = -1

        if pending_move:
            agent.send({"t": "m", "x": pending_move[0], "y": pending_move[1]})
            pending_move = None

        if not window_visible and not panel:
            clock.tick(30)                              # hidden until the panel hotkey
            continue
        sliding = panel_step()
        seq, frame = cap.latest()
        # While sliding, draw the whole panel off-screen and show the part that's out so far.
        screen = pygame.Surface((panel["W"], panel["H"])) if sliding else window.get_surface()
        sw, sh = screen.get_size()
        top = PANEL_TAB_H if panel else 0
        if sliding:
            last_seq = -1
        if frame is None:
            frame_surf = None
        elif seq != last_seq:
            last_seq = seq
            fh, fw = frame.shape[:2]
            scale = min(sw / fw, (sh - top) / fh)
            tw, th = max(1, int(fw * scale)), max(1, int(fh * scale))
            video_rect = pygame.Rect((sw - tw) // 2, top + (sh - top - th) // 2, tw, th)
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            img = cv2.cvtColor(cv2.resize(frame, (tw, th), interpolation=interp), cv2.COLOR_BGR2RGB)
            frame_surf = pygame.image.frombuffer(img.tobytes(), (tw, th), "RGB")
        screen.fill((12, 12, 14))
        if frame_surf:
            screen.blit(frame_surf, video_rect)
        else:
            msg = font.render(f"Waiting for video... ({cap.status})", True, (200, 200, 200))
            screen.blit(msg, msg.get_rect(center=(sw // 2, sh // 2)))
        if cap.mode == "net" and cap.status != "ok" and not picker:
            warn = font.render(f"{machines.name} {cap.input_name()}: {cap.status}", True, (255, 220, 120))
            pad = warn.get_rect().inflate(20, 10)
            pad.midtop = (sw // 2, top + 16)
            pygame.draw.rect(screen, (0, 0, 0), pad, border_radius=8)
            screen.blit(warn, warn.get_rect(center=pad.center))
        if cap.status == "no signal" and not picker:
            warn = font.render(f"{machines.name} on {cap.input_name()}: no signal.  "
                               "Right Ctrl + I to choose another input",
                               True, (255, 220, 120))
            pad = warn.get_rect().inflate(20, 10)
            pad.midtop = (sw // 2, top + 16)
            pygame.draw.rect(screen, (0, 0, 0), pad, border_radius=8)
            screen.blit(warn, warn.get_rect(center=pad.center))
        if panel:
            draw_tabs(screen)
        if picker:
            draw_picker(screen)
        if not agent.connected or time.time() < toast_until:
            text = toast if time.time() < toast_until else f"Keyboard/mouse: {agent.status_text}"
            surf = font.render(text, True, (255, 255, 255))
            pad = surf.get_rect().inflate(20, 10)
            pad.midbottom = (sw // 2, sh - 16)
            pygame.draw.rect(screen, (0, 0, 0), pad, border_radius=8)
            screen.blit(surf, surf.get_rect(center=pad.center))
        if sliding:
            w = panel["w"]
            area = pygame.Rect(0, 0, w, sh) if panel["side"] == "right" else pygame.Rect(sw - w, 0, w, sh)
            window.get_surface().blit(screen, (0, 0), area)
        window.flip()

        # Like a VM viewer: over a live picture, show only the board's own pointer, not the PC's too.
        hide_pointer = (frame_surf is not None and not picker and not sliding and agent.connected
                        and agent.mouse is True
                        and window.focused and video_rect.collidepoint(pygame.mouse.get_pos()))
        if hide_pointer == pygame.mouse.get_visible():
            pygame.mouse.set_visible(not hide_pointer)

        wifi = agent.wifi if agent.connected else None
        caption = (f"{machines.name} - {cap.input_name()} - {cap.fps:.0f} fps - keyboard/mouse {agent.status_text}"
                   + (f" - WiFi ...{str(wifi.get('ap'))[-5:]} {wifi.get('signal')}%, router {wifi.get('router_ms')} ms"
                      if wifi else "")
                   + (" - KEYBOARD CAPTURED (Right Ctrl releases)" if grabbed else ""))
        if caption != last_caption:
            window.title = caption
            last_caption = caption
        clock.tick(120)

    release_all()
    if tray:
        tray.remove()
    time.sleep(0.2)
    pygame.quit()


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


def install_agent(cfg, target, name=None, port=None, video=None, extra=()):
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
        install_agent(cfg, args.install_agent, args.name, parse_port(args.input), args.video, extra)
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
