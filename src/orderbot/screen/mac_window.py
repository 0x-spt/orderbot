"""The operator's Microsoft Remote Desktop window on this Mac.

The operator connects to RDP and opens 1C themselves; the robot never connects or
disconnects RDP. It finds the one window of the app, captures it with the built-in
`screencapture -l <id>`, and brings it to front only right before input (MacInputBackend).

Window facts come from the window server (CGWindowListCopyWindowInfo) and the focused
application from the Accessibility API (AXUIElement, system-wide element); both are current
without a run loop. Bounds are in screen points, top-left origin, the same space as mouse
events; the captured image includes the title bar, so a pixel of the image maps to
`bounds.x + px / scale, bounds.y + py / scale`.
"""
import ctypes
import logging
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from orderbot.screen.session import SessionLost

log = logging.getLogger(__name__)

OWNER = "Microsoft Remote Desktop"
CAPTURE_TIMEOUT = 10

SETTINGS = "(Системные настройки → Конфиденциальность и безопасность)"
NO_CAPTURE = ("нет снимка окна: проверьте разрешение «Запись экрана» для программы, "
              f"в которой запущен робот {SETTINGS}")
BLACK_FRAME = ("снимок окна чёрный: экран Mac спит или заблокирован, либо нет разрешения "
               f"«Запись экрана» для программы, в которой запущен робот {SETTINGS}")

# Windows above the normal layer that do not take keys: the menu bar and its status items
# (layers 24, 25, a strip at most this high) and the Dock (layer 20).
MENU_LAYERS = (24, 25)
MENU_MAX_HEIGHT = 50
DOCK_LAYER = 20
# The Dock owns an invisible full-screen window; only its strip along the screen edge
# can really cover a point. Points within this many points of the Dock window's
# bottom, left or right edge count as covered by it.
DOCK_EDGE_POINTS = 100

Bounds = tuple[float, float, float, float]


def _quartz_windows() -> list[dict]:
    """On-screen windows, front to back."""
    import Quartz

    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    return [dict(w) for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []]


def _quartz_screen_locked() -> bool:
    import Quartz

    session = Quartz.CGSessionCopyCurrentDictionary()
    return bool(session is None or session.get("CGSSessionScreenIsLocked"))


def _quartz_can_capture() -> bool:
    import Quartz

    return bool(Quartz.CGPreflightScreenCaptureAccess())


def _appkit_activate(pid: int) -> None:
    import AppKit

    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        raise SessionLost("программа Microsoft Remote Desktop закрыта")
    app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)


_AX_LIB = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_CF_LIB = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100
_ax = None


def _ax_libs():
    """ApplicationServices and CoreFoundation through ctypes: both are part of macOS, so
    no extra package is needed for the Accessibility API."""
    global _ax
    if _ax is None:
        ax = ctypes.cdll.LoadLibrary(_AX_LIB)
        cf = ctypes.cdll.LoadLibrary(_CF_LIB)
        p = ctypes.c_void_p
        ax.AXUIElementCreateSystemWide.restype = p
        ax.AXUIElementCreateSystemWide.argtypes = []
        ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementCopyAttributeValue.argtypes = [p, p, ctypes.POINTER(p)]
        ax.AXUIElementGetPid.restype = ctypes.c_int32
        ax.AXUIElementGetPid.argtypes = [p, ctypes.POINTER(ctypes.c_int32)]
        cf.CFStringCreateWithCString.restype = p
        cf.CFStringCreateWithCString.argtypes = [p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [p]
        _ax = (ax, cf)
    return _ax


def _ax_focused_pid() -> int | None:
    """Pid of the application that has keyboard focus, or None if AX does not say."""
    ax, cf = _ax_libs()
    system = ax.AXUIElementCreateSystemWide()
    attr = cf.CFStringCreateWithCString(None, b"AXFocusedApplication", _UTF8)
    app = ctypes.c_void_p()
    try:
        if not system or not attr:
            return None
        if ax.AXUIElementCopyAttributeValue(system, attr, ctypes.byref(app)) != 0 or not app:
            return None
        pid = ctypes.c_int32()
        if ax.AXUIElementGetPid(app, ctypes.byref(pid)) != 0:
            return None
        return int(pid.value)
    finally:
        for ref in (app.value, attr, system):
            if ref:
                cf.CFRelease(ref)


def _lsappinfo_front_pid() -> int | None:
    """Fallback when AX cannot be loaded: `lsappinfo` is part of macOS."""
    run = lambda *a: subprocess.run(["lsappinfo", *a], capture_output=True, text=True,
                                    check=True, timeout=5).stdout
    asn = run("front").strip()
    if not asn:
        return None
    m = re.search(r'"pid"\s*=\s*(\d+)', run("info", "-only", "pid", asn))
    return int(m.group(1)) if m else None


def _focused_pid() -> int | None:
    try:
        _ax_libs()
    except OSError:
        log.warning("Accessibility API not loadable, using lsappinfo")
        return _lsappinfo_front_pid()
    return _ax_focused_pid()


def _bounds(w: dict) -> Bounds:
    b = w.get("kCGWindowBounds") or {}
    return (float(b.get("X", 0)), float(b.get("Y", 0)),
            float(b.get("Width", 0)), float(b.get("Height", 0)))


def _contains(b: Bounds, x: float, y: float) -> bool:
    return b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3]


def _visible(w: dict) -> bool:
    return float(w.get("kCGWindowAlpha", 1.0)) > 0


def _in_menu_strip(w: dict) -> bool:
    """Lies entirely inside the menu bar strip (menu extras, the screen-recording
    indicator macOS shows while the robot captures): it cannot take the keys."""
    _, y, _, h = _bounds(w)
    return y + h <= MENU_MAX_HEIGHT


def _is_cursor(w: dict) -> bool:
    """The mouse pointer is drawn as a tiny Window Server window above everything."""
    return w.get("kCGWindowOwnerName") == "Window Server" and w.get("kCGWindowName") == "Cursor"


def _dock_edge(run) -> str:
    """Screen edge the Dock sits on ("bottom" unless the user moved it)."""
    try:
        out = run(["defaults", "read", "com.apple.dock", "orientation"],
                  capture_output=True, text=True, timeout=5)
        edge = (out.stdout or "").strip()
    except Exception:
        return "bottom"
    return edge if edge in ("left", "right", "bottom") else "bottom"


def _menu_or_dock(w: dict) -> bool:
    layer = w.get("kCGWindowLayer", 0)
    if layer in MENU_LAYERS and _bounds(w)[3] <= MENU_MAX_HEIGHT:
        return True
    return layer == DOCK_LAYER and w.get("kCGWindowOwnerName") == "Dock"


class MacRdpWindow:
    def __init__(self, owner: str = OWNER, title_contains: str = "", *,
                 list_windows: Callable[[], list[dict]] = _quartz_windows,
                 run=subprocess.run,
                 screen_locked: Callable[[], bool] = _quartz_screen_locked,
                 can_capture: Callable[[], bool] = _quartz_can_capture,
                 activate_app: Callable[[int], None] = _appkit_activate,
                 focused_pid: Callable[[], int | None] = _focused_pid,
                 dock_edge: str | None = None):
        self.owner = owner
        self.dock_edge = dock_edge  # read from macOS settings on first use
        self.last_overlay = None
        self.title_contains = title_contains or ""
        self._list = list_windows
        self._run = run
        self._locked = screen_locked
        self._can_capture = can_capture
        self._activate_app = activate_app
        self._focused_pid = focused_pid
        self.window_id: int | None = None
        self.pid: int | None = None
        self.scale: float | None = None
        self.captured_bounds: Bounds | None = None
        self.last_article: str | None = None
        self.last_stock_crop: Image.Image | None = None
        self.last_stock_frame: Image.Image | None = None

    @staticmethod
    def _sys(fn, *args):
        """A failing system call (missing pyobjc, a pyobjc error) means 1C is not usable."""
        try:
            return fn(*args)
        except SessionLost:
            raise
        except Exception as e:
            raise SessionLost(f"системный вызов не удался: {type(e).__name__}: {e}") from e

    # ------------------------------------------------------------ lookup
    def _candidates(self, windows: list[dict]) -> list[dict]:
        found = []
        for w in windows:
            if w.get("kCGWindowOwnerName") != self.owner or w.get("kCGWindowLayer", 0) != 0:
                continue
            if self.title_contains and self.title_contains not in (w.get("kCGWindowName") or ""):
                continue
            found.append(w)
        return found

    def _windows(self) -> list[dict]:
        return self._sys(self._list)

    def _find(self, windows: list[dict] | None = None) -> dict:
        if self._sys(self._locked):
            raise SessionLost("экран Mac заблокирован")
        windows = self._windows() if windows is None else windows
        found = self._candidates(windows)
        if not found:
            raise SessionLost(f"окно {self.owner} не найдено: оно закрыто, свёрнуто "
                              "или на другом рабочем столе")
        if len(found) > 1:
            raise SessionLost(f"окон {self.owner} найдено {len(found)}, нужно одно: закройте "
                              "лишние или задайте RDP_WINDOW_TITLE_CONTAINS")
        w = found[0]
        if self.window_id is not None and w.get("kCGWindowNumber") != self.window_id:
            raise SessionLost("окно Microsoft Remote Desktop сменилось после подключения")
        return w

    def _forget(self) -> None:
        self.scale = None
        self.captured_bounds = None
        self.last_article = None
        self.last_stock_crop = None
        self.last_stock_frame = None

    # ------------------------------------------------------------ session
    def connect(self) -> None:
        """Finds the one window. Touches nothing: no activation, no input."""
        if not self._sys(self._can_capture):
            raise SessionLost(NO_CAPTURE)
        self.window_id = None
        self.pid = None
        self._forget()  # a person may have used 1C meanwhile
        w = self._find()
        self.window_id = int(w["kCGWindowNumber"])
        self.pid = int(w["kCGWindowOwnerPID"])
        log.info("RDP window found: id %s", self.window_id)

    def disconnect(self) -> None:
        """RDP stays as it is; only what the robot remembered about the screen is dropped."""
        self._forget()

    def _require(self) -> None:
        if self.window_id is None:
            raise SessionLost("окно Microsoft Remote Desktop не выбрано")

    def bounds(self) -> Bounds:
        self._require()
        return _bounds(self._find())

    # ------------------------------------------------------------ capture
    def screenshot(self) -> Image.Image:
        self._require()
        bounds = _bounds(self._find())
        with tempfile.TemporaryDirectory(prefix="orderbot-") as tmp:
            path = str(Path(tmp) / "window.png")
            try:
                self._run(["screencapture", "-l", str(self.window_id), "-o", "-x", path],
                          check=True, timeout=CAPTURE_TIMEOUT, capture_output=True)
                with Image.open(path) as raw:
                    img = raw.convert("RGB")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
                    SyntaxError, ValueError, Image.DecompressionBombError) as e:
                raise SessionLost(f"{NO_CAPTURE} ({type(e).__name__})") from e
        if img.width == 0 or img.height == 0 or bounds[2] <= 0:
            raise SessionLost(f"{NO_CAPTURE} (пустой снимок)")
        if img.getbbox() is None:
            raise SessionLost(BLACK_FRAME)
        self.scale = img.width / bounds[2]
        self.captured_bounds = bounds
        return img

    # ------------------------------------------------------------ foreground
    def activate(self) -> None:
        self._require()
        self._sys(self._activate_app, self.pid)

    def is_front(self) -> bool:
        """The application with keyboard focus is Microsoft Remote Desktop (from AX) and the
        topmost normal window on screen is this one, so key events go to it."""
        self._require()
        windows = self._windows()
        self._find(windows)
        top = next((w for w in windows if w.get("kCGWindowLayer", 0) == 0), None)
        if top is None or top.get("kCGWindowNumber") != self.window_id:
            return False
        return self._sys(self._focused_pid) == self.pid

    def key_overlay(self) -> bool:
        """A visible window above the normal layer that is not the menu bar or the Dock is
        on screen (Spotlight, a notification, a system dialog): it may take the keys."""
        for w in self._windows():
            if (w.get("kCGWindowLayer", 0) > 0 and _visible(w) and not _menu_or_dock(w)
                    and not _is_cursor(w) and not _in_menu_strip(w)):
                self.last_overlay = (f"{w.get('kCGWindowOwnerName')!s} «{w.get('kCGWindowName') or ''}»"
                                     f" слой {w.get('kCGWindowLayer')}")
                return True
        return False

    def covered_at(self, x: float, y: float) -> bool:
        """Some visible window, of any layer, lies over this point in front of ours."""
        self._require()
        for w in self._windows():
            if w.get("kCGWindowNumber") == self.window_id:
                return False
            if not _visible(w) or not _contains(_bounds(w), x, y):
                continue
            if w.get("kCGWindowOwnerName") == "Dock" and w.get("kCGWindowLayer", 0) == DOCK_LAYER:
                bx, by, bw, bh = _bounds(w)
                if self.dock_edge is None:
                    self.dock_edge = _dock_edge(self._run)
                gap = {"left": x - bx, "right": bx + bw - x}.get(self.dock_edge, by + bh - y)
                if gap <= DOCK_EDGE_POINTS:
                    return True
                continue
            if _is_cursor(w):
                continue
            return True
        raise SessionLost("окно Microsoft Remote Desktop пропало с экрана")
