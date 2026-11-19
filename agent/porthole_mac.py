"""macOS keyboard/mouse backend for the Porthole agent (Mac mini, MacBook, iMac, ...).

Injects events through Quartz Event Services (CoreGraphics), loaded with ctypes, so it
needs nothing beyond the Python that comes with the Command Line Tools. macOS only lets
a program inject input once it has been allowed under System Settings -> Privacy &
Security -> Accessibility; trusted() reports that, and request_access() makes macOS
show its "allow" prompt on the Mac's screen.

Keys arrive as W3C KeyboardEvent.code names, the same as on Linux, and are mapped to
the Mac's virtual key codes by position, the way macOS treats a PC keyboard plugged into
a Mac: Alt is Option and the Windows key is Command. PORTHOLE_CTRL_AS_CMD=1 swaps
Ctrl and Command instead, so Ctrl+C / Ctrl+V on the PC copy and paste on the Mac.
"""
import ctypes
import os
import platform
import time

# --------------------------------------------------------------------------- key codes

# W3C code -> macOS virtual key code (kVK_* from HIToolbox/Events.h)
CODE_MAP = {
    "KeyA": 0x00, "KeyS": 0x01, "KeyD": 0x02, "KeyF": 0x03, "KeyH": 0x04, "KeyG": 0x05,
    "KeyZ": 0x06, "KeyX": 0x07, "KeyC": 0x08, "KeyV": 0x09, "KeyB": 0x0B, "KeyQ": 0x0C,
    "KeyW": 0x0D, "KeyE": 0x0E, "KeyR": 0x0F, "KeyY": 0x10, "KeyT": 0x11, "KeyO": 0x1F,
    "KeyU": 0x20, "KeyI": 0x22, "KeyP": 0x23, "KeyL": 0x25, "KeyJ": 0x26, "KeyK": 0x28,
    "KeyN": 0x2D, "KeyM": 0x2E,
    "Digit1": 0x12, "Digit2": 0x13, "Digit3": 0x14, "Digit4": 0x15, "Digit6": 0x16,
    "Digit5": 0x17, "Digit9": 0x19, "Digit7": 0x1A, "Digit8": 0x1C, "Digit0": 0x1D,
    "Equal": 0x18, "Minus": 0x1B, "BracketRight": 0x1E, "BracketLeft": 0x21, "Quote": 0x27,
    "Semicolon": 0x29, "Backslash": 0x2A, "IntlHash": 0x2A, "Comma": 0x2B, "Slash": 0x2C,
    "Period": 0x2F, "Backquote": 0x32, "IntlBackslash": 0x0A, "IntlYen": 0x5D, "IntlRo": 0x5E,
    "Enter": 0x24, "Tab": 0x30, "Space": 0x31, "Backspace": 0x33, "Escape": 0x35,
    "Delete": 0x75, "Insert": 0x72, "Home": 0x73, "End": 0x77, "PageUp": 0x74, "PageDown": 0x79,
    "ArrowLeft": 0x7B, "ArrowRight": 0x7C, "ArrowDown": 0x7D, "ArrowUp": 0x7E,
    "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61, "F7": 0x62,
    "F8": 0x64, "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F, "F13": 0x69, "F14": 0x6B,
    "F15": 0x71, "F16": 0x6A, "F17": 0x40, "F18": 0x4F, "F19": 0x50, "F20": 0x5A,
    # PC keys without a Mac twin: macOS itself maps them like this for PC keyboards.
    "PrintScreen": 0x69, "ScrollLock": 0x6B, "Pause": 0x71, "NumLock": 0x47, "ContextMenu": 0x6E,
    "Numpad0": 0x52, "Numpad1": 0x53, "Numpad2": 0x54, "Numpad3": 0x55, "Numpad4": 0x56,
    "Numpad5": 0x57, "Numpad6": 0x58, "Numpad7": 0x59, "Numpad8": 0x5B, "Numpad9": 0x5C,
    "NumpadDecimal": 0x41, "NumpadMultiply": 0x43, "NumpadAdd": 0x45, "NumpadDivide": 0x4B,
    "NumpadEnter": 0x4C, "NumpadSubtract": 0x4E, "NumpadEqual": 0x51,
    "CapsLock": 0x39,
    "ShiftLeft": 0x38, "ShiftRight": 0x3C,
    "ControlLeft": 0x3B, "ControlRight": 0x3E,
    "AltLeft": 0x3A, "AltRight": 0x3D,
    "MetaLeft": 0x37, "MetaRight": 0x36, "OSLeft": 0x37, "OSRight": 0x36,
}
if os.environ.get("PORTHOLE_CTRL_AS_CMD") == "1":
    CODE_MAP.update({"ControlLeft": 0x37, "ControlRight": 0x36,
                     "MetaLeft": 0x3B, "MetaRight": 0x3E, "OSLeft": 0x3B, "OSRight": 0x3E})

KVK_RETURN, KVK_TAB, KVK_CAPSLOCK = 0x24, 0x30, 0x39

# Modifier key -> (generic flag, left/right-specific flag) of CGEventFlags.
F_ALPHASHIFT, F_SHIFT, F_CONTROL, F_ALT, F_COMMAND = 0x10000, 0x20000, 0x40000, 0x80000, 0x100000
F_NUMPAD = 0x200000
MODIFIERS = {
    0x38: (F_SHIFT, 0x02), 0x3C: (F_SHIFT, 0x04),
    0x3B: (F_CONTROL, 0x01), 0x3E: (F_CONTROL, 0x2000),
    0x3A: (F_ALT, 0x20), 0x3D: (F_ALT, 0x40),
    0x37: (F_COMMAND, 0x08), 0x36: (F_COMMAND, 0x10),
}
NUMPAD_KEYS = {0x41, 0x43, 0x45, 0x47, 0x4B, 0x4C, 0x4E, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56,
               0x57, 0x58, 0x59, 0x5B, 0x5C, 0x7B, 0x7C, 0x7D, 0x7E}   # arrows carry it too

# Porthole button number (W3C: 0 left, 1 middle, 2 right, 3 back, 4 forward) -> CGMouseButton
BUTTONS = {0: 0, 1: 2, 2: 1, 3: 3, 4: 4}

# CGEventType
LEFT_DOWN, LEFT_UP, RIGHT_DOWN, RIGHT_UP, MOVED, LEFT_DRAGGED, RIGHT_DRAGGED = 1, 2, 3, 4, 5, 6, 7
FLAGS_CHANGED, OTHER_DOWN, OTHER_UP, OTHER_DRAGGED = 12, 25, 26, 27
# CGEventField
FIELD_CLICK_STATE, FIELD_BUTTON_NUMBER = 1, 3

# kCGSessionEventTap. Not kCGHIDEventTap: mouse/keyboard sharing tools (Deskflow, Barrier,
# Synergy, ...) watch the HID level, and while they're sending this Mac's keyboard and mouse to
# another computer they grab every event there, ours included, and forward it to that computer.
# The session level comes after them, and still moves the pointer and reaches every app.
POST_TAP = 1
DOUBLE_CLICK_S = 0.5            # macOS default double-click interval
DOUBLE_CLICK_PT = 5
DRAG_START_PT = 12              # click-through: ignore small pointer movements during a click


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


_cg = None


def lib():
    """ApplicationServices (CoreGraphics + Accessibility), with the signatures we use."""
    global _cg
    if _cg is not None:
        return _cg
    cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    vp, u32, u16, i32, i64, u64, b = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint16, ctypes.c_int32,
                                      ctypes.c_int64, ctypes.c_uint64, ctypes.c_bool)
    sigs = {
        "CGEventSourceCreate": (vp, [i32]),
        "CGEventCreate": (vp, [vp]),
        "CGEventGetLocation": (CGPoint, [vp]),
        "CGEventCreateMouseEvent": (vp, [vp, u32, CGPoint, u32]),
        "CGEventCreateKeyboardEvent": (vp, [vp, u16, b]),
        "CGEventKeyboardSetUnicodeString": (None, [vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]),
        "CGEventSetFlags": (None, [vp, u64]),
        "CGEventSetType": (None, [vp, u32]),
        "CGEventSetIntegerValueField": (None, [vp, u32, i64]),
        "CGEventPost": (None, [u32, vp]),
        "CFRelease": (None, [vp]),
        "CGMainDisplayID": (u32, []),
        "CGGetActiveDisplayList": (i32, [u32, ctypes.POINTER(u32), ctypes.POINTER(u32)]),
        "CGDisplayBounds": (CGRect, [u32]),
        "AXIsProcessTrusted": (b, []),
        "CGEventSetLocation": (None, [vp, CGPoint]),
    }
    for name, (res, args) in sigs.items():
        fn = getattr(cg, name)
        fn.restype, fn.argtypes = res, args
    # Optional ones: the scroll call without varargs (macOS 13+), the permission prompt (10.15+),
    # and the pointer-visibility calls used by show_pointer().
    for name, res, args in (("CGEventCreateScrollWheelEvent2", vp, [vp, u32, u32, i32, i32, i32]),
                            ("CGRequestPostEventAccess", b, []),
                            ("CGEventSetWindowLocation", None, [vp, CGPoint]),
                            ("CGCursorIsVisible", b, []),
                            ("CGDisplayShowCursor", i32, [u32]),
                            ("_CGSDefaultConnection", i32, []),
                            ("CGSSetConnectionProperty", i32, [i32, i32, vp, vp])):
        try:
            fn = getattr(cg, name)
            fn.restype, fn.argtypes = res, args
        except AttributeError:
            pass
    _cg = cg
    return cg


def trusted():
    """True once the agent may inject input (Accessibility permission)."""
    try:
        return bool(lib().AXIsProcessTrusted())
    except (OSError, AttributeError):
        return False


def trusted_fresh():
    """trusted(), asked in a new process: inside the agent macOS keeps answering "no" after
    the permission is switched on, so the agent can't tell when to restart without this."""
    import subprocess
    import sys
    code = ("import ctypes;l=ctypes.cdll.LoadLibrary('/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices');l.AXIsProcessTrusted.restype=ctypes.c_bool;print(int(l.AXIsProcessTrusted()))")
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def request_access():
    """Show macOS's 'allow this app to control your computer' prompt (once per program)."""
    try:
        lib().CGRequestPostEventAccess()
    except (OSError, AttributeError):
        pass


_pointer_allowed = False


def show_pointer():
    """Make the mouse pointer visible if macOS is hiding it.

    After a display change (say the Mac switches from a monitor to the capture card) macOS can
    keep the pointer hidden until a real mouse moves, and posted events don't count. So the
    picture shows no pointer although it moves. Showing it from a background process needs the
    window server's "SetsCursorInBackground" connection property (the same thing Barrier and
    Synergy do), then CGDisplayShowCursor.
    """
    global _pointer_allowed
    cg = lib()
    try:
        if cg.CGCursorIsVisible():
            return
        if not _pointer_allowed:
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            key = cf.CFStringCreateWithCString(None, b"SetsCursorInBackground", 0x08000100)   # UTF-8
            conn = cg._CGSDefaultConnection()
            cg.CGSSetConnectionProperty(conn, conn, key, ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue"))
            _pointer_allowed = True
        cg.CGDisplayShowCursor(cg.CGMainDisplayID())
    except (OSError, AttributeError, ValueError):
        pass                                       # private call missing: nothing worse than before


# --------------------------------------------------------------------------- click-through targets
#
# In click-through mode a click goes straight to the app whose window is under it
# (CGEventPostToPid), so the Mac's pointer never moves: a KVM such as Deskflow notices any
# pointer movement, however brief, and passes it on to the other computer as a jump.

class CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


# Things that track the real pointer while they're clicked (window buttons, menus, the Dock), so
# a posted click can't work them; the accessibility "press" action does, without the pointer.
PRESSABLE = {"AXButton", "AXMenuBarItem", "AXMenuItem", "AXDockItem", "AXMenuButton", "AXPopUpButton",
             "AXCheckBox", "AXRadioButton", "AXDisclosureTriangle", "AXTab"}
PRESS_CHILDREN = {"AXStaticText", "AXImage", "AXGroup", "AXUnknown"}
FIELD_WINDOW_UNDER_POINTER = 91          # kCGMouseEventWindowUnderMousePointer
FIELD_WINDOW_THAT_CAN_HANDLE = 92        # ...ThatCanHandleThisEvent
FIELD_WINDOW_NUMBER = 51                 # AppKit window number for directly posted events
_wl = None


def _window_lib():
    global _wl
    if _wl is None:
        cg = lib()
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        vp = ctypes.c_void_p
        for fn, res, args in ((cg.CGWindowListCopyWindowInfo, vp, [ctypes.c_uint32, ctypes.c_uint32]),
                              (cg.CGRectMakeWithDictionaryRepresentation, ctypes.c_bool, [vp, ctypes.POINTER(CGRect)]),
                              (cg.CGEventPostToPid, None, [ctypes.c_int32, vp]),
                              (cg.AXUIElementCreateApplication, vp, [ctypes.c_int32]),
                              (cg.AXUIElementCreateSystemWide, vp, []),
                              (cg.AXUIElementSetAttributeValue, ctypes.c_int32, [vp, vp, vp]),
                              (cg.AXUIElementCopyElementAtPosition, ctypes.c_int32,
                               [vp, ctypes.c_float, ctypes.c_float, ctypes.POINTER(vp)]),
                              (cg.AXUIElementCopyAttributeValue, ctypes.c_int32, [vp, vp, ctypes.POINTER(vp)]),
                              (cg.AXUIElementPerformAction, ctypes.c_int32, [vp, vp]),
                              (cg.AXUIElementGetPid, ctypes.c_int32, [vp, ctypes.POINTER(ctypes.c_int32)]),
                              (cg.AXUIElementCopyActionNames, ctypes.c_int32, [vp, ctypes.POINTER(vp)]),
                              (cf.CFArrayContainsValue, ctypes.c_bool, [vp, CFRange, vp]),
                              (cf.CFArrayGetCount, ctypes.c_long, [vp]),
                              (cf.CFArrayGetValueAtIndex, vp, [vp, ctypes.c_long]),
                              (cf.CFDictionaryGetValue, vp, [vp, vp]),
                              (cf.CFNumberGetValue, ctypes.c_bool, [vp, ctypes.c_int, vp]),
                              (cf.CFStringGetCString, ctypes.c_bool, [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
                              (cf.CFStringCreateWithCString, vp, [vp, ctypes.c_char_p, ctypes.c_uint32]),
                              (cf.CFRelease, None, [vp])):
            fn.restype, fn.argtypes = res, args
        keys = {n: vp.in_dll(cg, n).value for n in ("kCGWindowLayer", "kCGWindowOwnerPID", "kCGWindowNumber",
                                                     "kCGWindowBounds", "kCGWindowAlpha", "kCGWindowOwnerName")}
        names = {n: cf.CFStringCreateWithCString(None, n.encode(), 0x08000100)
                 for n in ("AXFrontmost", "AXWindow", "AXRaise", "AXRole", "AXParent", "AXPress", "AXShowMenu")}
        _wl = (cg, cf, keys, names, vp.in_dll(cf, "kCFBooleanTrue").value)
    return _wl


def app_window_at(x, y):
    """(pid, window number, origin) of the app window or desktop under (x, y), or None for the menu
    bar and anything else that isn't an ordinary app window. Windows above the normal level
    (the Dock's and Notification Center's full-screen layers, ...) are looked through."""
    try:
        cg, cf, keys, _, _ = _window_lib()
    except (OSError, AttributeError, ValueError):
        return None

    def number(d, key, kind=4, ctype=ctypes.c_int64):
        v, out = cf.CFDictionaryGetValue(d, keys[key]), ctype(0)
        return out.value if v and cf.CFNumberGetValue(v, kind, ctypes.byref(out)) else None

    def text(d, key):
        v, buf = cf.CFDictionaryGetValue(d, keys[key]), ctypes.create_string_buffer(256)
        return buf.value.decode(errors="replace") if v and cf.CFStringGetCString(v, buf, 256, 0x08000100) else ""

    windows = cg.CGWindowListCopyWindowInfo(1, 0)              # on-screen, front to back
    if not windows:
        return None
    try:
        for i in range(cf.CFArrayGetCount(windows)):
            d = cf.CFArrayGetValueAtIndex(windows, i)
            r = CGRect()
            if not cg.CGRectMakeWithDictionaryRepresentation(cf.CFDictionaryGetValue(d, keys["kCGWindowBounds"]),
                                                            ctypes.byref(r)):
                continue
            if not (r.x <= x < r.x + r.w and r.y <= y < r.y + r.h):
                continue
            if (number(d, "kCGWindowAlpha", 13, ctypes.c_double) or 0) <= 0:
                continue
            layer, owner = number(d, "kCGWindowLayer") or 0, text(d, "kCGWindowOwnerName")
            if owner == "Window Server" and r.h <= 40:
                return None                                   # the menu bar: not an app window
            if layer > 0:
                continue                                      # overlays: Dock, Notification Center, ...
            pid, window = number(d, "kCGWindowOwnerPID"), number(d, "kCGWindowNumber")
            if pid and window:
                return pid, window, CGPoint(r.x, r.y)
    finally:
        cf.CFRelease(windows)
    return None


def ax_press(x, y, right=False):
    """Press the button / menu / Dock item at (x, y) through accessibility (right: open its
    context menu). True if that's what was there; the pointer never moves."""
    try:
        cg, cf, _, names, _ = _window_lib()
    except (OSError, AttributeError, ValueError):
        return False
    system, element = cg.AXUIElementCreateSystemWide(), ctypes.c_void_p()
    if not system:
        return False
    try:
        if cg.AXUIElementCopyElementAtPosition(system, x, y, ctypes.byref(element)) != 0 or not element:
            return False
        try:
            # Hit testing can return the label or icon inside a custom button or tab.
            for _ in range(8):
                role, buf = ctypes.c_void_p(), ctypes.create_string_buffer(64)
                if cg.AXUIElementCopyAttributeValue(element, names["AXRole"], ctypes.byref(role)) != 0 or not role:
                    return False
                ok = cf.CFStringGetCString(role, buf, 64, 0x08000100)
                cf.CFRelease(role)
                if not ok:
                    return False
                role_name = buf.value.decode()
                if role_name in PRESSABLE:
                    break
                if role_name not in PRESS_CHILDREN:
                    return False
                parent = ctypes.c_void_p()
                if cg.AXUIElementCopyAttributeValue(element, names["AXParent"], ctypes.byref(parent)) != 0 or not parent:
                    return False
                cf.CFRelease(element)
                element = parent
            else:
                return False
            action = names["AXShowMenu" if right else "AXPress"]
            actions = ctypes.c_void_p()
            if cg.AXUIElementCopyActionNames(element, ctypes.byref(actions)) != 0 or not actions:
                return False
            has = cf.CFArrayContainsValue(actions, CFRange(0, cf.CFArrayGetCount(actions)), action)
            cf.CFRelease(actions)
            if not has:
                return False
            if not right and role_name not in ("AXMenuItem", "AXMenuBarItem", "AXDockItem"):
                pid = ctypes.c_int32()
                if cg.AXUIElementGetPid(element, ctypes.byref(pid)) == 0:
                    bring_forward(pid.value, x, y)
            return cg.AXUIElementPerformAction(element, action) == 0
        finally:
            cf.CFRelease(element)
    finally:
        cf.CFRelease(system)


def bring_forward(pid, x, y):
    """Make the app frontmost and raise the window at (x, y), like a real click would."""
    try:
        cg, cf, _, names, true = _window_lib()
        app = cg.AXUIElementCreateApplication(pid)
        if app:
            cg.AXUIElementSetAttributeValue(app, names["AXFrontmost"], true)
            cf.CFRelease(app)
        system, element, window = cg.AXUIElementCreateSystemWide(), ctypes.c_void_p(), ctypes.c_void_p()
        if system and cg.AXUIElementCopyElementAtPosition(system, x, y, ctypes.byref(element)) == 0 and element:
            if cg.AXUIElementCopyAttributeValue(element, names["AXWindow"], ctypes.byref(window)) == 0 and window:
                cg.AXUIElementPerformAction(window, names["AXRaise"])
                cf.CFRelease(window)
            cf.CFRelease(element)
        if system:
            cf.CFRelease(system)
    except (OSError, AttributeError, ValueError):
        pass


ACCESS_HELP = ("the Mac hasn't allowed Porthole to control it yet: on the Mac open System Settings > "
               "Privacy & Security > Accessibility and turn on python3")


class Devices:
    """Same interface as the Linux uinput Devices: key(), move(), button(), wheel().

    click_through (set per PC connection): another KVM such as Deskflow owns this Mac's pointer
    and passes any movement of it on to the other computer, however brief. So the pointer must
    never move. Plain moves are only remembered. A click is held until the button comes up
    (hands move while clicking), then posted straight to the app whose window is under it
    (CGEventPostToPid), after bringing that window forward. Only the menu bar and the Dock still
    get clicks the ordinary way, which moves the pointer for a moment.
    """

    def __init__(self):
        cg = self.cg = lib()
        self.src = cg.CGEventSourceCreate(1)       # kCGEventSourceStateHIDSystemState
        # Click-through pointer events come from a private source: events from the HID-state
        # source also move macOS's record of where the real mouse is, so the next real movement
        # (which the other KVM reads) would be measured from the click spot and jump.
        self.ct_src = cg.CGEventSourceCreate(-1) or self.src   # kCGEventSourceStatePrivate
        self.click_through = False
        self.mods = set()                          # modifier key codes held
        self.caps = False
        self.buttons = set()                       # CGMouseButtons held
        self.last_click = (None, 0.0, 0.0, 0.0, 0)  # button, time, x, y, count
        here = cg.CGEventCreate(None)
        self.pos = cg.CGEventGetLocation(here) if here else CGPoint(0, 0)
        if here:
            cg.CFRelease(here)
        self.pointer_checked = 0.0                 # last show_pointer() check (monotonic seconds)
        self.ct_press = None                       # click-through: (button, where, click count) not sent yet
        self.ct_park = None                        # click-through: where to put the pointer after a drag
        self.ct_app = None                         # click-through: (pid, window) a drag is going to

    def _where(self):
        here = self.cg.CGEventCreate(None)
        if not here:
            return None
        pt = self.cg.CGEventGetLocation(here)
        self.cg.CFRelease(here)
        return pt

    # -- helpers
    def flags(self):
        f = F_ALPHASHIFT if self.caps else 0
        for code in self.mods:
            generic, side = MODIFIERS[code]
            f |= generic | side
        return f

    def _post(self, ev, flags=None):
        if not ev:
            return
        self.cg.CGEventSetFlags(ev, self.flags() if flags is None else flags)
        self.cg.CGEventPost(POST_TAP, ev)
        self.cg.CFRelease(ev)

    def display_rect(self):
        """Bounds (in points) of the display Porthole shows: PORTHOLE_DISPLAY=N, else the main one."""
        want = os.environ.get("PORTHOLE_DISPLAY", "")
        display = self.cg.CGMainDisplayID()
        if want.isdigit() and int(want) > 0:
            ids = (ctypes.c_uint32 * 16)()
            count = ctypes.c_uint32(0)
            if self.cg.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) == 0 and int(want) <= count.value:
                display = ids[int(want) - 1]
        return self.cg.CGDisplayBounds(display)

    # -- keyboard
    def key(self, code, down):
        if code in MODIFIERS:
            (self.mods.add if down else self.mods.discard)(code)
            ev = self.cg.CGEventCreateKeyboardEvent(self.src, code, down)
            if ev:
                self.cg.CGEventSetType(ev, FLAGS_CHANGED)
            self._post(ev)
            return
        if code == KVK_CAPSLOCK:
            if down:
                self.caps = not self.caps
                ev = self.cg.CGEventCreateKeyboardEvent(self.src, code, True)
                if ev:
                    self.cg.CGEventSetType(ev, FLAGS_CHANGED)
                self._post(ev)
            return
        ev = self.cg.CGEventCreateKeyboardEvent(self.src, code, down)
        self._post(ev, self.flags() | (F_NUMPAD if code in NUMPAD_KEYS else 0))

    def type_char(self, ch):
        """Type one character as itself, whatever the Mac's keyboard layout is."""
        if ch in "\r\n\t":
            code = KVK_TAB if ch == "\t" else KVK_RETURN
            self._post(self.cg.CGEventCreateKeyboardEvent(self.src, code, True), 0)
            self._post(self.cg.CGEventCreateKeyboardEvent(self.src, code, False), 0)
            return
        units = ch.encode("utf-16-le")
        buf = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
        for down in (True, False):
            ev = self.cg.CGEventCreateKeyboardEvent(self.src, 0, down)
            if ev:
                self.cg.CGEventKeyboardSetUnicodeString(ev, len(buf), buf)
            self._post(ev, 0)

    # -- pointer
    def _mouse(self, kind, button, at=None):
        src = self.ct_src if self.click_through else self.src
        return self.cg.CGEventCreateMouseEvent(src, kind, self.pos if at is None else at, button)

    def _send(self, kind, button, at=None, clicks=None):
        ev = self._mouse(kind, button, at)
        if ev:
            if kind in (OTHER_DRAGGED, OTHER_DOWN, OTHER_UP) or clicks is not None:
                self.cg.CGEventSetIntegerValueField(ev, FIELD_BUTTON_NUMBER, button)
            if clicks is not None:
                self.cg.CGEventSetIntegerValueField(ev, FIELD_CLICK_STATE, clicks)
        self._post(ev)

    def _drag_kind(self):
        if 0 in self.buttons:
            return LEFT_DRAGGED, 0
        if 1 in self.buttons:
            return RIGHT_DRAGGED, 1
        if self.buttons:
            return OTHER_DRAGGED, min(self.buttons)
        return MOVED, 0

    @staticmethod
    def _press_kinds(btn):
        return {0: (LEFT_DOWN, LEFT_UP), 1: (RIGHT_DOWN, RIGHT_UP)}.get(btn, (OTHER_DOWN, OTHER_UP))

    def _count_click(self, btn):
        """Click count for a new press, so a quick second click is a double-click on the Mac."""
        last_btn, t, lx, ly, count = self.last_click
        now = time.monotonic()
        near = abs(self.pos.x - lx) <= DOUBLE_CLICK_PT and abs(self.pos.y - ly) <= DOUBLE_CLICK_PT
        count = count + 1 if (last_btn == btn and now - t <= DOUBLE_CLICK_S and near) else 1
        self.last_click = (btn, now, self.pos.x, self.pos.y, count)
        return count

    def move(self, x, y):
        r = self.display_rect()
        nx, ny = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        self.pos = CGPoint(r.x + min(nx * r.w, r.w - 1), r.y + min(ny * r.h, r.h - 1))
        if self.click_through:
            self._ct_move()
            return
        kind, button = self._drag_kind()
        self._send(kind, button)
        now = time.monotonic()
        if now - self.pointer_checked > 1.0:       # cheap, but no need to ask on every move
            self.pointer_checked = now
            show_pointer()

    def button(self, btn, down):
        if self.click_through:
            self._ct_button(btn, down)
            return
        (self.buttons.add if down else self.buttons.discard)(btn)
        count = self._count_click(btn) if down else self.last_click[4]
        self._send(self._press_kinds(btn)[0 if down else 1], btn, clicks=count)

    # -- click-through (see the class docstring)
    def _post_app(self, target, ev, at):
        pid, window, origin = target
        self.cg.CGEventSetIntegerValueField(ev, FIELD_WINDOW_NUMBER, window)
        self.cg.CGEventSetIntegerValueField(ev, FIELD_WINDOW_UNDER_POINTER, window)
        self.cg.CGEventSetIntegerValueField(ev, FIELD_WINDOW_THAT_CAN_HANDLE, window)
        # PID posting skips normal window-server routing. Supply both the screen
        # position and the window-relative position for the receiving app.
        set_window_location = getattr(self.cg, "CGEventSetWindowLocation", None)
        if set_window_location is not None:
            set_window_location(ev, CGPoint(at.x - origin.x, at.y - origin.y))
        self.cg.CGEventSetFlags(ev, self.flags())
        self.cg.CGEventPostToPid(pid, ev)
        self.cg.CFRelease(ev)

    def _send_app(self, target, kind, button, at=None, clicks=None):
        """Post a mouse event straight to the app owning the target window."""
        at = self.pos if at is None else at
        ev = self._mouse(kind, button, at)
        if not ev:
            return
        if kind in (OTHER_DRAGGED, OTHER_DOWN, OTHER_UP) or clicks is not None:
            self.cg.CGEventSetIntegerValueField(ev, FIELD_BUTTON_NUMBER, button)
        if clicks is not None:
            self.cg.CGEventSetIntegerValueField(ev, FIELD_CLICK_STATE, clicks)
        self._post_app(target, ev, at)

    def _park(self, where):
        if where is not None:
            self._send(MOVED, 0, at=where)

    def _ct_move(self):
        if not self.buttons:
            return                                 # just remember where the next click goes
        if self.ct_press:
            btn, at, count = self.ct_press
            still = abs(self.pos.x - at.x) <= DRAG_START_PT and abs(self.pos.y - at.y) <= DRAG_START_PT
            if still:
                return
            # A drag starts: press where the button went down, then follow.
            self.ct_press = None
            self.ct_app = app_window_at(at.x, at.y)
            if self.ct_app:
                bring_forward(self.ct_app[0], at.x, at.y)
                self._send_app(self.ct_app, self._press_kinds(btn)[0], btn, at=at, clicks=count)
            else:
                self.ct_park = self._where()
                self._send(self._press_kinds(btn)[0], btn, at=at, clicks=count)
        kind, button = self._drag_kind()
        if self.ct_app:
            self._send_app(self.ct_app, kind, button)
        else:
            self._send(kind, button)

    def _ct_button(self, btn, down):
        if down:
            self.buttons.add(btn)
            self.ct_press = (btn, CGPoint(self.pos.x, self.pos.y), self._count_click(btn))
            return
        self.buttons.discard(btn)
        press, release = self._press_kinds(btn)
        if self.ct_press and self.ct_press[0] == btn:
            _, at, count = self.ct_press           # a plain click: press and release in one go
            self.ct_press = None
            if btn in (0, 1) and count == 1 and ax_press(at.x, at.y, right=btn == 1):
                return                             # a button, menu or Dock item: pressed directly
            target = app_window_at(at.x, at.y)
            if target:
                bring_forward(target[0], at.x, at.y)
                self._send_app(target, press, btn, at=at, clicks=count)
                self._send_app(target, release, btn, at=at, clicks=count)
                return
            park = self._where()
            self._send(press, btn, at=at, clicks=count)
            self._send(release, btn, at=at, clicks=count)
            self._park(park)
        elif self.ct_app is not None:              # the end of a drag in an app window
            self._send_app(self.ct_app, release, btn, clicks=self.last_click[4])
            if not self.buttons:
                self.ct_app = None
        elif self.ct_park is not None:             # the end of a drag
            self._send(release, btn, clicks=self.last_click[4])
            if not self.buttons:
                self._park(self.ct_park)
                self.ct_park = None

    def cancel_pending(self):
        """The PC went away mid-click: forget a click that was never sent."""
        self.ct_press = None

    def wheel(self, dx, dy):
        # Porthole sends Linux wheel steps: +dy scrolls up, +dx scrolls right.
        # Quartz: +wheel1 scrolls up, +wheel2 scrolls left. Unit 1 = lines.
        park = None
        target = app_window_at(self.pos.x, self.pos.y) if self.click_through else None
        create = getattr(self.cg, "CGEventCreateScrollWheelEvent2", None)
        if target and create is not None:
            ev = create(self.ct_src, 1, 2, int(dy), int(-dx), 0)
            if ev:
                self.cg.CGEventSetLocation(ev, self.pos)
                self._post_app(target, ev, self.pos)
            return
        if self.click_through and not self.buttons:
            # Scrolling goes to the window under the pointer, so it has to be there for a moment.
            park = self._where()
            self._send(MOVED, 0)
        create = getattr(self.cg, "CGEventCreateScrollWheelEvent2", None)
        if create is not None:
            ev = create(self.ct_src if self.click_through else self.src, 1, 2, int(dy), int(-dx), 0)
            if ev and self.click_through:
                self.cg.CGEventSetLocation(ev, self.pos)
            self._post(ev)
            self._park(park)
        elif platform.machine() == "x86_64":
            # macOS 12 and older: only the varargs version, which ctypes can call on Intel Macs.
            old = self.cg.CGEventCreateScrollWheelEvent
            old.restype = ctypes.c_void_p
            old.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32]
            self._post(old(self.src, 1, 2, int(dy), int(-dx)))
