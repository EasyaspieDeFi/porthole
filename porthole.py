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
            self.agent.retarget(m.get("host", ""), self.port_for(m))
        log.info("switched to %s", name)
        return True


# --------------------------------------------------------------------------- agent link

_SWITCH = object()   # queue sentinel: drop the current connection, a new host was chosen


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
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    async def _pause(self, gen, seconds):
        """Sleep, but wake early if the target machine changes."""
        if gen != self.gen:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

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
                writer.write((json.dumps({"t": "hello", "token": self.cfg["agent_token"]}) + "\n").encode())
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
                self.state = "connected"
                log.info("input agent connected (%s)", self.remote_host)
                while True:
                    try:
                        msg = await asyncio.wait_for(self.queue.get(), 5)
                    except asyncio.TimeoutError:
                        msg = {"t": "p"}  # keepalive so a dead link is noticed
                    if msg is _SWITCH or gen != self.gen:
                        break
                    writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
                    await writer.drain()
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
        return {"t": "status", "agent": agent.state, "agent_host": agent.remote_host or agent.host,
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


def read_clipboard():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.replace("\r\n", "\n").rstrip("\n")
    except Exception:  # noqa: BLE001
        return ""


def run_window(cfg, cap, agent, machines):
    os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")
    import pygame

    pygame.init()
    window = pygame.Window("Porthole", (1280, 720), resizable=True)
    font = pygame.font.SysFont("segoeui", 18)
    clock = pygame.time.Clock()

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
                          "Right Ctrl + M = machine, Right Ctrl + I = input"), time.time() + 8
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
        row_h, box_w = 40, 520
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

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
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

        seq, frame = cap.latest()
        screen = window.get_surface()
        sw, sh = screen.get_size()
        if frame is None:
            frame_surf = None
        elif seq != last_seq:
            last_seq = seq
            fh, fw = frame.shape[:2]
            scale = min(sw / fw, sh / fh)
            tw, th = max(1, int(fw * scale)), max(1, int(fh * scale))
            video_rect = pygame.Rect((sw - tw) // 2, (sh - th) // 2, tw, th)
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
            pad.midtop = (sw // 2, 16)
            pygame.draw.rect(screen, (0, 0, 0), pad, border_radius=8)
            screen.blit(warn, warn.get_rect(center=pad.center))
        if cap.status == "no signal" and not picker:
            warn = font.render(f"{machines.name} on {cap.input_name()}: no signal.  "
                               "Right Ctrl + I to choose another input",
                               True, (255, 220, 120))
            pad = warn.get_rect().inflate(20, 10)
            pad.midtop = (sw // 2, 16)
            pygame.draw.rect(screen, (0, 0, 0), pad, border_radius=8)
            screen.blit(warn, warn.get_rect(center=pad.center))
        if picker:
            draw_picker(screen)
        if not agent.connected or time.time() < toast_until:
            text = toast if time.time() < toast_until else f"Keyboard/mouse: {agent.state}"
            surf = font.render(text, True, (255, 255, 255))
            pad = surf.get_rect().inflate(20, 10)
            pad.midbottom = (sw // 2, sh - 16)
            pygame.draw.rect(screen, (0, 0, 0), pad, border_radius=8)
            screen.blit(surf, surf.get_rect(center=pad.center))
        window.flip()

        caption = (f"{machines.name} - {cap.input_name()} - {cap.fps:.0f} fps - keyboard/mouse {agent.state}"
                   + (" - KEYBOARD CAPTURED (Right Ctrl releases)" if grabbed else ""))
        if caption != last_caption:
            window.title = caption
            last_caption = caption
        clock.tick(120)

    release_all()
    time.sleep(0.2)
    pygame.quit()


# --------------------------------------------------------------------------- agent install

def upsert_machine(cfg, name, host=None, port=None, video=None):
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


def install_agent(cfg, target, name=None, port=None, video=None):
    """scp the agent to a board and run install.sh with sudo (prompts for passwords)."""
    import tempfile

    host = target.split("@", 1)[-1]
    # The token travels in a private file (deleted by install.sh), never on a command line,
    # so it can't show up in the board's process list or in an error message here.
    with tempfile.TemporaryDirectory() as tmp:
        token_file = pathlib.Path(tmp) / "token"
        token_file.write_text(cfg["agent_token"], encoding="utf-8")
        steps = [
            ("Copying agent to " + target, ["ssh", target, "umask 077 && rm -rf /tmp/porthole-agent && "
                                                         "mkdir -p /tmp/porthole-agent"]),
            (None, ["scp", "-q", str(HERE / "agent" / "porthole_agent.py"), str(HERE / "agent" / "install.sh"),
                    str(token_file), f"{target}:/tmp/porthole-agent/"]),
            ("Installing (sudo will ask for the board's password) ...",
             ["ssh", "-t", target, f"sudo bash /tmp/porthole-agent/install.sh --token-file "
                                   f"/tmp/porthole-agent/token {cfg['agent_port']}"]),
        ]
        for message, argv in steps:
            if message:
                print(message)
            result = subprocess.run(argv)
            if result.returncode != 0:
                print(f"\nInstall step failed (exit {result.returncode}). If the connection dropped near the "
                      f"end, the agent may still be installed: on the board run "
                      f"'systemctl status porthole-agent'.")
                sys.exit(1)
    m = upsert_machine(cfg, name or host, host, port, video)
    where = describe_video(m)
    print(f"Done. Machine {m['name']!r}: keyboard/mouse -> {host}:{cfg['agent_port']}, video -> {where}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-window", action="store_true", help="web UI only, no local window")
    ap.add_argument("--install-agent", metavar="USER@HOST", help="install the input agent on a board over SSH "
                                                                 "and add it as a machine")
    ap.add_argument("--add-machine", metavar="NAME", help="add/update a machine (with --host / --input)")
    ap.add_argument("--remove-machine", metavar="NAME")
    ap.add_argument("--name", help="machine name for --install-agent (default: the host)")
    ap.add_argument("--host", help="keyboard/mouse host for --add-machine")
    ap.add_argument("--input", metavar="PORT", help="HDMI port the machine is on, e.g. 2 for HDMI-2")
    ap.add_argument("--video", choices=["hdmi", *NET_SOURCES],
                    help="video source: hdmi (capture card), or screen/camera/test over the network")
    ap.add_argument("--machine", metavar="NAME", help="start on this machine")
    ap.add_argument("--list-machines", action="store_true")
    ap.add_argument("--list-inputs", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()
    if args.list_inputs:
        for idx, name in list_inputs(cfg["device_filter"]):
            print(f"{name:22} (capture index {idx})")
        return
    if args.install_agent:
        install_agent(cfg, args.install_agent, args.name, parse_port(args.input), args.video)
        return
    if args.add_machine:
        m = upsert_machine(cfg, args.add_machine, args.host, parse_port(args.input), args.video)
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
            print(f"{mark} {m['name']:16} host={m.get('host') or '-':20} video={describe_video(m)}")
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
    run_window(cfg, cap, agent, machines)


if __name__ == "__main__":
    main()
