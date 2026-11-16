"""The real OneC for the pipeline: the operator's open 1C window on this Mac.

connect() finds the Microsoft Remote Desktop window and builds the gate (starting its input
tap), the input backend and a fresh Guard; it checks that the window shows 1C. It never logs
in and never starts RDP. disconnect() stops the tap and drops all of it; it sends nothing
to RDP. Between connect and disconnect
the window object carries what lookup remembers (last article, last stock crop), so
consecutive reads see each other's state. Runs in a worker thread: it touches no Store.
"""
import functools
import itertools
import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path

from PIL import Image

from orderbot.domain import ItemRead
from orderbot.screen.guard import Guard
from orderbot.screen.idle import IdleGate
from orderbot.screen.lookup import ArticleNotFound, lookup
from orderbot.screen.mac_input import ClickGap, MacInputBackend
from orderbot.screen.mac_window import MacRdpWindow
from orderbot.screen.reader import ReadUncertain, UnexpectedScreen, check_screen, crop_tables, tab_shown
from orderbot.screen.session import SessionLost, ShuttingDown

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


class _GatedBackend:
    """Every input call passes the owner's gate: one at a time, none after a stop request."""

    def __init__(self, inner, owner):
        self._inner = inner
        self._owner = owner

    def click(self, x, y):
        self._owner._act(self._inner.click, x, y)

    def type_text(self, text):
        self._owner._act(self._inner.type_text, text)

    def press_return(self):
        self._owner._act(self._inner.press_return)


class MacOneC:
    def __init__(self, layout, title_contains: str = "", idle_min_seconds: float = 30.0,
                 snapshots_dir=None, *, window_factory=None, gate_factory=IdleGate,
                 backend_factory=MacInputBackend, guard_factory=Guard, lookup_fn=lookup,
                 check_fn=check_screen, crop_fn=crop_tables, tab_fn=tab_shown):
        self._layout = layout
        self._title = title_contains
        self._idle = idle_min_seconds
        self._snapshots = Path(snapshots_dir) if snapshots_dir else None
        self._window_factory = window_factory or (
            lambda title: MacRdpWindow(title_contains=title))
        self._gate_factory = gate_factory
        self._backend_factory = backend_factory
        self._guard_factory = guard_factory
        self._lookup = lookup_fn
        self._check = check_fn
        # Connecting sends nothing: a window left on «Изображение» is accepted here, and the
        # lookup's first action is the way back to «Остатки» (every other input is checked
        # without the image-tab exemption).
        self._connect_check = (functools.partial(check_screen, image_tab_ok=True)
                               if check_fn is check_screen else check_fn)
        self._crop = crop_fn
        self._tab = tab_fn
        self._window = None
        self._guard = None
        self._gate = None
        self._counter = itertools.count()
        self._gap = ClickGap()               # lives across reconnects
        self._action_lock = threading.Lock()
        self._stopping = False

    # --------------------------------------------------------------- shutdown
    def request_stop(self) -> None:
        """No new input from now on; an input call already running finishes."""
        self._stopping = True

    def stop_and_wait(self, timeout: float = 10.0) -> bool:
        """request_stop, then wait until the running input call (and its release of the key or
        button it pressed) is done."""
        self._stopping = True
        if self._action_lock.acquire(timeout=timeout):
            self._action_lock.release()
            return True
        return False

    def _act(self, fn, *args):
        with self._action_lock:
            if self._stopping:
                raise ShuttingDown("робот останавливается")
            return fn(*args)

    def connect(self) -> None:
        self.disconnect()
        if self._stopping:
            raise ShuttingDown("робот останавливается")
        window = self._window_factory(self._title)
        gate = None
        try:
            window.connect()
            gate = self._gate_factory(self._idle)
            gate.start()               # the input tap lives from connect to disconnect
            backend = self._backend_factory(window, gate, click_gap=self._gap)
            guard = self._guard_factory(_GatedBackend(backend, self), self._layout,
                                        screenshot=window.screenshot)
            guard.dismiss_task_notice()     # 1C's task reminder may sit over the list
            self._connect_check(window.screenshot(), self._layout)  # foreign or empty window
        except UnexpectedScreen as e:
            self._mark_image_tab(e, window)
            self._drop_half_connected(gate, window)
            raise
        except BaseException:
            self._drop_half_connected(gate, window)
            raise
        self._window, self._guard, self._gate = window, guard, gate

    def _mark_image_tab(self, e: Exception, window) -> None:
        """e.image_tab = True when 1C is (or was left) on the «Изображение» tab, so the pause
        alert can say to switch back. Best effort: never hides the original error."""
        try:
            left = getattr(window, "image_tab_left", False) is True
            e.image_tab = left or self._tab(window.screenshot(), self._layout) == "image"
        except Exception:
            log.info("cannot tell whether the image tab is shown", exc_info=True)

    def _drop_half_connected(self, gate, window) -> None:
        try:
            if gate is not None:
                gate.stop()
            window.disconnect()    # nothing half-connected is kept
        except Exception:
            log.exception("disconnect after failed connect")

    def disconnect(self) -> None:
        window, self._window, self._guard = self._window, None, None
        gate, self._gate = self._gate, None
        try:
            if gate is not None:
                gate.stop()
        finally:
            if window is not None:
                window.disconnect()

    def read(self, article: str) -> ItemRead:
        if self._window is None:
            raise SessionLost("подключение к окну 1С не выполнено")
        if self._stopping:
            raise ShuttingDown("робот останавливается")
        window = self._window
        window.last_stock_frame = None
        window.last_prices_frame = None   # nothing of an earlier article is attached
        window.last_image_crop = None
        try:
            item = self._lookup(article, window, self._guard, self._layout)
        except (ArticleNotFound, ReadUncertain) as e:
            # best effort: the person who gets the order sees what the screen showed
            e.snapshot_path = self._save_snapshot(article, [getattr(window, "last_stock_frame", None)])
            raise
        except UnexpectedScreen as e:
            self._mark_image_tab(e, window)
            self._save_refused(article, getattr(e, "frame", None))
            raise
        # After a successful read the screen may show the «Изображение» tab: the snapshot
        # is the stock frame above the prices frame the values were read from.
        prices = getattr(window, "last_prices_frame", None)
        snapshot = self._save_snapshot(
            article, [getattr(window, "last_stock_frame", None), prices],
            current=prices is None)
        return replace(item, snapshot_path=snapshot,
                       photo_1c=self._save_picture(article, getattr(window, "last_image_crop", None)))

    def _name(self, article: str, suffix: str = "") -> Path:
        return self._snapshots / (f"{int(time.time())}-{next(self._counter)}-"
                                  f"{_UNSAFE.sub('_', article)[:40]}{suffix}.png")

    def _save_picture(self, article: str, picture) -> str | None:
        """The item's picture from the «Изображение» tab, next to the snapshots (the same
        purge). Any failure gives no picture, not an error."""
        if self._snapshots is None or picture is None:
            return None
        try:
            self._snapshots.mkdir(parents=True, exist_ok=True)
            path = self._name(article, "-1c")
            picture.save(path)
            return str(path)
        except Exception as e:
            log.info("1C picture of %r not saved: %s", article, e)
            return None

    def _save_refused(self, article: str, frame) -> None:
        """The whole 1C window frame that stopped the robot, kept locally for finding out
        what was on screen (seen once: an unexplained «grid_header not found» before Return).
        Never sent anywhere; purged with the snapshots."""
        if self._snapshots is None or frame is None:
            return
        try:
            self._snapshots.mkdir(parents=True, exist_ok=True)
            path = self._name(article, "-refused")
            frame.save(path)
            log.info("1C frame that stopped the robot: %s", path)
        except Exception as e:
            log.info("refused 1C frame of %r not saved: %s", article, e)

    def _save_snapshot(self, article: str, earlier_frames, current: bool = True) -> str | None:
        """Only crop_tables output is ever written: the frames read earlier (if any) above
        the current screen. Any failure gives no image, not an error: the alert says
        the image is missing."""
        if self._snapshots is None:
            return None
        try:
            frames = [f for f in earlier_frames if f is not None]
            if current or not frames:
                frames.append(self._window.screenshot())
            crops = []
            for frame in frames:
                self._check(frame, self._layout)
                crops.append(self._crop(frame, self._layout))
            if len(crops) == 1:
                image = crops[0]
            else:
                image = Image.new("RGB", (max(c.width for c in crops), sum(c.height for c in crops)),
                                  "white")
                y = 0
                for c in crops:
                    image.paste(c, (0, y))
                    y += c.height
            self._snapshots.mkdir(parents=True, exist_ok=True)
            path = self._name(article)
            image.save(path)
            return str(path)
        except Exception:
            log.exception("snapshot of %r not saved", article)
            return None
