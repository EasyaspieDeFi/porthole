"""Porthole's UI kit for pygame: a Windows 11 look and a small immediate-mode widget set.

- Type: Segoe UI Variable (Windows 11's UI font); icons: Segoe Fluent Icons. Falls back to
  Segoe UI / Segoe MDL2 Assets on Windows 10.
- Crisp shapes: rounded rectangles are drawn 4x larger and scaled down (anti-aliased), with
  soft shadows, and cached, so they cost nothing per frame.
- Everything is sized in points and scaled by the monitor's DPI (UI.scale).
- Immediate mode: each frame the app draws widgets and asks "was this clicked?". Clicks are
  fed in from the event loop with press()/release(); wants() tells the event loop whether a
  click belongs to the UI (so it isn't sent to the board).
"""
import math
import os
import time

import pygame

WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def _font_file(*names):
    for n in names:
        p = os.path.join(WIN_FONTS, n)
        if os.path.exists(p):
            return p
    return None


FONT_TEXT = _font_file("SegUIVar.ttf", "segoeui.ttf")
FONT_SEMIBOLD = _font_file("seguisb.ttf", "SegUIVar.ttf", "segoeui.ttf")
FONT_ICONS = _font_file("SegoeIcons.ttf", "segmdl2.ttf")


class C:
    """Colours (dark theme)."""
    BG = (11, 12, 15)                 # behind the video
    SURFACE = (21, 23, 28)            # header, status bar
    RAISED = (28, 31, 38)             # cards, dialogs
    OVERLAY = (34, 37, 46)            # menus, toasts
    FIELD = (16, 18, 22)              # text fields, segmented tracks
    HOVER = (40, 44, 54)
    PRESSED = (48, 53, 65)
    BORDER = (44, 48, 58)
    BORDER_SOFT = (34, 37, 45)
    TEXT = (236, 238, 242)
    TEXT_2 = (168, 174, 186)          # secondary
    TEXT_3 = (118, 124, 137)          # hints, placeholders
    ACCENT = (96, 141, 255)
    ACCENT_HOVER = (122, 160, 255)
    ACCENT_SOFT = (37, 50, 84)        # selected chip background
    ON_ACCENT = (255, 255, 255)
    GOOD = (63, 207, 142)
    WARN = (245, 183, 77)
    BAD = (242, 92, 98)
    SHADOW = (0, 0, 0)


ICON = {
    "settings": 0xE713, "add": 0xE710, "close": 0xE711, "chev_left": 0xE76B, "chev_right": 0xE76C,
    "chev_down": 0xE70D, "keyboard": 0xE765, "paste": 0xE77F, "fullscreen": 0xE740, "window": 0xE8A7,
    "delete": 0xE74D, "edit": 0xE70F, "monitor": 0xE7F4, "laptop": 0xE7F8, "board": 0xEEA1, "devices": 0xE772,
    "video": 0xE714, "camera": 0xE722, "wifi": 0xE701, "check": 0xE73E, "more": 0xE712, "mouse": 0xE962,
    "refresh": 0xE72C, "info": 0xE946, "warning": 0xE7BA, "dock_left": 0xE90C, "dock_right": 0xE90D,
    "search": 0xE721, "hide": 0xED1A, "power": 0xE7E8, "no_signal": 0xEA39, "cast": 0xEC15, "pin": 0xE718,
    "globe": 0xE774, "terminal": 0xE756, "game": 0xE7FC, "tune": 0xE9E9, "help": 0xE897, "link": 0xE71B,
    "sync": 0xE895,
}


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return tuple(int(round(lerp(x, y, t))) for x, y in zip(c1, c2))


class UI:
    SS = 4                                            # supersampling for shapes

    def __init__(self, scale=1.0):
        self.scale = scale
        self._fonts = {}
        self._shapes = {}
        self._text = {}
        self.mouse = (-1, -1)
        self.press_pos = None
        self.release_pos = None
        self.right_release = None
        self.regions = []                             # interactive rects drawn this frame
        self._last_regions = []
        self.modal = False                            # a dialog/menu owns all input
        self.hover_t = {}                             # widget id -> 0..1 hover animation
        self.hover_since = {}
        self.focus = None                             # focused text field id
        self.keys = []                                # key / text events for the focused field
        self.now = time.time()
        self.dt = 0.016
        self.tooltip = None
        self.clip = None                              # a scrolled area: clicks outside it don't count

    # -- scale
    def px(self, v):
        return int(round(v * self.scale))

    def set_scale(self, scale):
        scale = max(0.75, min(3.0, scale))
        if abs(scale - self.scale) > 0.01:
            self.scale = scale
            self._fonts.clear()
            self._shapes.clear()
            self._text.clear()

    # -- frame / input plumbing
    def begin(self, mouse):
        now = time.time()
        self.dt, self.now = min(0.1, now - self.now), now
        self.mouse = mouse
        self._last_regions, self.regions = self.regions, []
        self.tooltip = None

    def end(self):
        self.press_pos = self.release_pos = self.right_release = None
        self.keys = []

    def wants(self, pos):
        """Does a click at pos belong to the UI (the latest frame's widgets, or a modal)?

        Called from the event loop, between frames, so self.regions is the frame just drawn."""
        return self.modal or any(r.collidepoint(pos) for r in self.regions)

    def press(self, pos):
        self.press_pos = pos

    def release(self, pos, right=False):
        if right:
            self.right_release = pos
        else:
            self.release_pos = pos

    def region(self, rect):
        self.regions.append(pygame.Rect(rect))

    def clicked(self, rect, wid=None):
        """True on the frame a left click (press and release) happened inside rect."""
        rect = pygame.Rect(rect)
        self.region(rect)
        if self.clip is not None:
            rect = rect.clip(self.clip)
        hit = (self.release_pos is not None and rect.collidepoint(self.release_pos)
               and (self.press_pos is None or rect.collidepoint(self.press_pos)))
        if hit:
            self.release_pos = None                    # one widget per click
        return hit

    def hovered(self, rect):
        return pygame.Rect(rect).collidepoint(self.mouse)

    def anim(self, wid, target, speed=14.0):
        """Smoothly approach target (0/1) for hover/selection fades."""
        v = self.hover_t.get(wid, target)
        v += (target - v) * min(1.0, self.dt * speed)
        if abs(v - target) < 0.01:
            v = target
        self.hover_t[wid] = v
        return v

    # -- fonts and text
    def font(self, size, weight="regular"):
        key = (size, weight, self.scale)
        f = self._fonts.get(key)
        if f is None:
            path = {"regular": FONT_TEXT, "semibold": FONT_SEMIBOLD, "icons": FONT_ICONS}[weight]
            px = max(6, self.px(size))
            f = pygame.font.Font(path, px) if path else pygame.font.SysFont("segoeui", px)
            self._fonts[key] = f
        return f

    def render(self, text, size, color, weight="regular"):
        key = (text, size, color, weight, self.scale)
        s = self._text.get(key)
        if s is None:
            if len(self._text) > 600:
                self._text.clear()
            s = self.font(size, weight).render(text, True, color)
            self._text[key] = s
        return s

    def text(self, surf, text, pos, size=13, color=C.TEXT, weight="regular", anchor="topleft", max_w=None):
        if max_w is not None:
            text = self.ellipsize(text, size, weight, max_w)
        img = self.render(text, size, color, weight)
        r = img.get_rect(**{anchor: (int(pos[0]), int(pos[1]))})
        surf.blit(img, r)
        return r

    def measure(self, text, size=13, weight="regular"):
        return self.font(size, weight).size(text)

    def wrap(self, text, size, max_w, weight="regular"):
        """Split text into lines no wider than max_w."""
        f, lines, line = self.font(size, weight), [], ""
        for word in text.split():
            trial = f"{line} {word}".strip()
            if f.size(trial)[0] <= max_w or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines

    def paragraph(self, surf, text, pos, size=13, color=C.TEXT_2, max_w=400, align="left", line_h=1.45):
        """Wrapped text from pos (top-left, or top-centre with align='center'). Returns its height."""
        lh = int(self.px(size) * line_h)
        lines = self.wrap(text, size, max_w)
        for i, line in enumerate(lines):
            anchor = "midtop" if align == "center" else "topleft"
            self.text(surf, line, (pos[0], pos[1] + i * lh), size, color, anchor=anchor)
        return lh * len(lines)

    def paragraph_height(self, text, size, max_w, line_h=1.45):
        return int(self.px(size) * line_h) * len(self.wrap(text, size, max_w))

    def ellipsize(self, text, size, weight, max_w):
        f = self.font(size, weight)
        if f.size(text)[0] <= max_w:
            return text
        while text and f.size(text + "…")[0] > max_w:
            text = text[:-1]
        return text + "…"

    def icon(self, surf, name, center, size=16, color=C.TEXT):
        img = self.render(chr(ICON[name]), size, color, "icons")
        surf.blit(img, img.get_rect(center=(int(center[0]), int(center[1]))))

    # -- shapes
    def rrect(self, surf, rect, color, radius=8, border=None, width=1, alpha=255):
        """Anti-aliased rounded rectangle, optionally with a border (cached)."""
        rect = pygame.Rect(rect)
        if rect.w <= 0 or rect.h <= 0:
            return
        r = min(self.px(radius), rect.h // 2, rect.w // 2)
        bw = max(1, self.px(width)) if border else 0
        key = ("rr", rect.size, r, color, border, bw, alpha)
        img = self._shapes.get(key)
        if img is None:
            ss = self.SS
            big = pygame.Surface((rect.w * ss, rect.h * ss), pygame.SRCALPHA)
            full = big.get_rect()
            if border:
                pygame.draw.rect(big, (*border, alpha), full, border_radius=r * ss)
                pygame.draw.rect(big, (*color, alpha), full.inflate(-2 * bw * ss, -2 * bw * ss),
                                 border_radius=max(0, (r - bw) * ss))
            else:
                pygame.draw.rect(big, (*color, alpha), full, border_radius=r * ss)
            img = pygame.transform.smoothscale(big, rect.size)
            if len(self._shapes) > 400:
                self._shapes.clear()
            self._shapes[key] = img
        surf.blit(img, rect)

    def shadow(self, surf, rect, radius=12, spread=18, alpha=110, offset=6):
        """Soft drop shadow under a card."""
        rect = pygame.Rect(rect)
        sp = self.px(spread)
        key = ("sh", rect.size, radius, sp, alpha)
        img = self._shapes.get(key)
        if img is None:
            w, h = rect.w + sp * 2, rect.h + sp * 2
            base = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.rect(base, (0, 0, 0, alpha), (sp, sp, rect.w, rect.h), border_radius=self.px(radius))
            k = max(1, sp // 3)
            small = pygame.transform.smoothscale(base, (max(1, w // k), max(1, h // k)))
            img = pygame.transform.smoothscale(small, (w, h))
            self._shapes[key] = img
        surf.blit(img, (rect.x - sp, rect.y - sp + self.px(offset)))

    def circle(self, surf, center, radius, color):
        pygame.draw.aacircle(surf, color, (int(center[0]), int(center[1])), max(1, int(radius)))

    def dim(self, surf, alpha=140):
        shade = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
        shade.fill((5, 6, 9, alpha))
        surf.blit(shade, (0, 0))

    def spinner(self, surf, center, radius, color=C.ACCENT):
        """An arc that turns: the 'working on it' indicator."""
        r = self.px(radius)
        size = r * 2 + 4
        ss = self.SS
        big = pygame.Surface((size * ss, size * ss), pygame.SRCALPHA)
        start = (self.now * 5.0) % (2 * math.pi)
        rect = pygame.Rect(2 * ss, 2 * ss, (size - 4) * ss, (size - 4) * ss)
        pygame.draw.arc(big, (*C.BORDER, 255), rect, 0, 2 * math.pi, max(1, self.px(2.5)) * ss)
        pygame.draw.arc(big, (*color, 255), rect, start, start + 1.6, max(1, self.px(2.5)) * ss)
        img = pygame.transform.smoothscale(big, (size, size))
        surf.blit(img, img.get_rect(center=(int(center[0]), int(center[1]))))

    # -- widgets
    def button(self, surf, rect, label, wid, kind="secondary", icon=None, enabled=True, size=13):
        """kind: primary | secondary | ghost | danger. Returns True when clicked."""
        rect = pygame.Rect(rect)
        hot = enabled and self.hovered(rect)
        t = self.anim(("btn", wid), 1.0 if hot else 0.0)
        if kind == "primary":
            bg, fg, border = mix(C.ACCENT, C.ACCENT_HOVER, t), C.ON_ACCENT, None
        elif kind == "danger":
            bg, fg, border = mix((58, 30, 34), (76, 36, 40), t), (255, 196, 198), None
        elif kind == "ghost":
            bg, fg, border = mix(C.SURFACE, C.HOVER, t), C.TEXT, None
        else:
            bg, fg, border = mix(C.OVERLAY, C.HOVER, t), C.TEXT, C.BORDER
        if not enabled:
            bg, fg = mix(bg, C.SURFACE, 0.6), C.TEXT_3
        if kind != "ghost" or t > 0:
            self.rrect(surf, rect, bg, 8, border)
        weight = "semibold" if kind in ("primary", "danger") else "regular"
        tw = self.measure(label, size, weight)[0] if label else 0
        iw = self.px(16) if icon else 0
        gap = self.px(8) if icon and label else 0
        x = rect.centerx - (tw + iw + gap) // 2
        if icon:
            self.icon(surf, icon, (x + iw // 2, rect.centery), 14, fg)
        if label:
            self.text(surf, label, (x + iw + gap, rect.centery), size, fg, weight, anchor="midleft")
        return enabled and self.clicked(rect)

    def button_width(self, label, icon=None, size=13, kind="secondary"):
        weight = "semibold" if kind in ("primary", "danger") else "regular"
        w = self.measure(label, size, weight)[0] + self.px(32)
        return w + (self.px(24) if icon else 0)

    def icon_button(self, surf, rect, icon, wid, tip=None, active=False, enabled=True, color=None):
        rect = pygame.Rect(rect)
        hot = enabled and self.hovered(rect)
        t = self.anim(("ib", wid), 1.0 if hot else 0.0)
        if active:
            self.rrect(surf, rect, mix(C.ACCENT_SOFT, (48, 64, 106), t), 8)
        elif t > 0:
            self.rrect(surf, rect, mix(C.SURFACE, C.HOVER, t), 8)
        fg = C.ACCENT_HOVER if active else (color or (C.TEXT if enabled else C.TEXT_3))
        self.icon(surf, icon, rect.center, 16, fg)
        if hot and tip:
            since = self.hover_since.setdefault(wid, self.now)
            if self.now - since > 0.45:
                self.tooltip = (tip, rect)
        else:
            self.hover_since.pop(wid, None)
        return enabled and self.clicked(rect)

    def segmented(self, surf, rect, options, value, wid):
        """options: [(value, label, icon or None)]. Returns the (possibly new) value."""
        rect = pygame.Rect(rect)
        self.rrect(surf, rect, C.FIELD, 8, C.BORDER)
        n = len(options)
        pad = self.px(3)
        w = (rect.w - pad * 2) / n
        new = value
        for i, (v, label, icon) in enumerate(options):
            r = pygame.Rect(round(rect.x + pad + i * w), rect.y + pad, round(w), rect.h - pad * 2)
            sel = v == value
            hot = self.hovered(r)
            if sel:
                self.rrect(surf, r, (52, 58, 74), 6)
            elif hot:
                self.rrect(surf, r, C.HOVER, 6)
            fg = C.TEXT if sel else (C.TEXT if hot else C.TEXT_2)
            tw = self.measure(label, 13, "semibold" if sel else "regular")[0]
            iw = self.px(22) if icon else 0
            x = r.centerx - (tw + iw) // 2
            if icon:
                self.icon(surf, icon, (x + self.px(8), r.centery), 14, fg)
            self.text(surf, label, (x + iw, r.centery), 13, fg, "semibold" if sel else "regular", "midleft")
            if self.clicked(r):
                new = v
        return new

    def toggle(self, surf, rect, on, wid):
        rect = pygame.Rect(rect)
        t = self.anim(("tg", wid), 1.0 if on else 0.0, 18)
        self.rrect(surf, rect, mix(C.FIELD, C.ACCENT, t), rect.h, mix(C.BORDER, C.ACCENT, t))
        knob_r = rect.h // 2 - self.px(4)
        cx = lerp(rect.x + rect.h // 2, rect.right - rect.h // 2, t)
        self.circle(surf, (cx, rect.centery), knob_r, mix(C.TEXT_2, C.ON_ACCENT, t))
        return (not on) if self.clicked(rect) else on

    def text_field(self, surf, rect, value, wid, placeholder="", max_len=80):
        """Single-line text input. Click to focus; returns the edited value."""
        rect = pygame.Rect(rect)
        focused = self.focus == wid
        if self.clicked(rect):
            self.focus, focused = wid, True
            self.hover_t[("caret", wid)] = self.now
        border = C.ACCENT if focused else (C.BORDER if not self.hovered(rect) else (64, 70, 84))
        self.rrect(surf, rect, C.FIELD, 8, border, 2 if focused else 1)
        if focused:
            for kind, data in self.keys:
                if kind == "text":
                    value = (value + data)[:max_len]
                elif kind == "backspace":
                    value = value[:-1]
                elif kind == "clear":
                    value = ""
                elif kind == "paste":
                    value = (value + data.replace("\n", " "))[:max_len]
            self.keys = [k for k in self.keys if k[0] in ("tab", "enter", "escape")]
        pad = self.px(12)
        clip = rect.inflate(-pad * 2, 0)
        old = surf.get_clip()
        surf.set_clip(clip)
        if value:
            img = self.render(value, 14, C.TEXT)
            x = clip.x if img.get_width() <= clip.w else clip.right - img.get_width()
            surf.blit(img, img.get_rect(midleft=(x, rect.centery)))
            caret_x = x + img.get_width() + 1
        else:
            self.text(surf, placeholder, (clip.x, rect.centery), 14, C.TEXT_3, anchor="midleft")
            caret_x = clip.x
        surf.set_clip(old)
        if focused and int((self.now - self.hover_t.get(("caret", wid), 0)) * 2) % 2 == 0:
            h = self.px(18)
            pygame.draw.line(surf, C.TEXT, (caret_x, rect.centery - h // 2), (caret_x, rect.centery + h // 2),
                             max(1, self.px(1.5)))
        return value

    def keycap(self, surf, pos, label, anchor="midleft", size=12):
        """A keyboard-key chip, e.g. for shortcuts: [Ctrl] [Alt] [K]."""
        tw, th = self.measure(label, size, "semibold")
        r = pygame.Rect(0, 0, max(tw + self.px(14), self.px(24)), self.px(24))
        setattr(r, anchor, (int(pos[0]), int(pos[1])))
        self.rrect(surf, r, C.FIELD, 6, C.BORDER)
        pygame.draw.line(surf, C.BORDER, (r.x + self.px(4), r.bottom - 2), (r.right - self.px(4), r.bottom - 2), 1)
        self.text(surf, label, r.center, size, C.TEXT, "semibold", "center")
        return r

    def keycaps(self, surf, pos, combo, size=12, anchor="midleft"):
        """Draw 'Ctrl+Alt+K' as keycaps from pos (midleft or midright). Returns the bounding rect."""
        parts = [p.strip().title() if len(p.strip()) > 1 else p.strip().upper() for p in combo.split("+")]
        gap = self.px(4)
        widths = [max(self.measure(p, size, "semibold")[0] + self.px(14), self.px(24)) for p in parts]
        total = sum(widths) + gap * (len(parts) - 1)
        x = pos[0] if anchor == "midleft" else pos[0] - total
        first = None
        for p, w in zip(parts, widths):
            r = self.keycap(surf, (x, pos[1]), p, "midleft", size)
            first = r if first is None else first.union(r)
            x += w + gap
        return first

    def status_dot(self, surf, center, color, ring=True):
        r = self.px(4)
        if ring:
            self.circle(surf, center, r + self.px(3), mix(color, C.SURFACE, 0.75))
        self.circle(surf, center, r, color)

    def card(self, surf, rect, radius=14, color=C.RAISED, border=C.BORDER_SOFT, shadow=True):
        if shadow:
            self.shadow(surf, rect, radius)
        self.rrect(surf, rect, color, radius, border)

    def draw_tooltip(self, surf):
        if not self.tooltip:
            return
        text, anchor = self.tooltip
        tw, th = self.measure(text, 12)
        r = pygame.Rect(0, 0, tw + self.px(16), th + self.px(10))
        r.midtop = (anchor.centerx, anchor.bottom + self.px(6))
        r.clamp_ip(surf.get_rect().inflate(-self.px(8), -self.px(8)))
        self.rrect(surf, r, (48, 52, 63), 6, (64, 69, 82))
        self.text(surf, text, r.center, 12, C.TEXT, anchor="center")
