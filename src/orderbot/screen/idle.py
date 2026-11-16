"""Whether a person is using this Mac right now.

The robot works on the operator's Mac, in the same RDP window the operator uses. Before
every unit of input (a click, a key press) it asks the gate; while a
person has touched the mouse or keyboard within `min_idle_s`, it sends nothing.

Every event the robot posts carries ROBOT_TAG in its source user data (mac_input.py). A
listen-only event tap, running in its own thread with its own run loop, sees every key,
modifier, mouse and scroll event of the session and records the time of the last one that
does not carry the tag: that is a person. The robot's own events never count, whatever
their timing; any untagged event always counts, even one in the middle of the robot's
typing.

When the tap starts it has seen nothing yet, so the last person's event is first taken from
the system idle counter (any input, HID system state): a person who used the Mac a moment
before the start is respected. If the system switches the tap off (a slow callback, user
input), the tap is switched on again and the gap is counted as a person's input: events in
it were not seen.
"""
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from orderbot.screen.session import SessionLost

log = logging.getLogger(__name__)

MIN_IDLE_S = 30.0
MIN_ALLOWED_S = 10.0

# "ROBOT1C1": fixed, non-zero, fits a signed 64-bit field
ROBOT_TAG = 0x524F424F54314331

# CGEventType values the system sends to a tap it has switched off
TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFF
TAP_DISABLED = (TAP_DISABLED_BY_TIMEOUT, TAP_DISABLED_BY_USER_INPUT)

NO_TAP = ("нельзя следить за мышью и клавиатурой: проверьте разрешения «Универсальный доступ» "
          "и «Мониторинг ввода» для программы, в которой запущен робот (Системные настройки → "
          "Конфиденциальность и безопасность)")

TAP_START_TIMEOUT = 5.0


class UserActive(Exception):
    """A person is using the Mac: send nothing, the order waits for the next tick."""


def hid_idle_seconds() -> float:
    import Quartz

    return float(Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType))


def _event_types(q) -> list[int]:
    return [q.kCGEventKeyDown, q.kCGEventKeyUp, q.kCGEventFlagsChanged,
            q.kCGEventMouseMoved, q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp,
            q.kCGEventLeftMouseDragged, q.kCGEventRightMouseDown, q.kCGEventRightMouseUp,
            q.kCGEventRightMouseDragged, q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp,
            q.kCGEventOtherMouseDragged, q.kCGEventScrollWheel]


class QuartzTap:
    """A listen-only session event tap in a daemon thread with its own CFRunLoop. Calls
    on_event(type, source_user_data) for every event; for a switched-off notice the user
    data is 0."""

    def __init__(self, on_event: Callable[[int, int], None]):
        self._on_event = on_event
        self._q = None
        self._tap = None
        self._loop = None
        self._thread: threading.Thread | None = None

    def _callback(self, proxy, type_, event, refcon):
        type_ = int(type_)
        data = 0
        try:
            if type_ not in TAP_DISABLED:
                data = int(self._q.CGEventGetIntegerValueField(event, self._q.kCGEventSourceUserData))
        except Exception:
            log.exception("event tap: no user data, counted as a person")
        try:
            self._on_event(type_, data)
        except Exception:
            log.exception("event tap callback")
        return event

    def start(self) -> None:
        try:
            import Quartz
        except Exception as e:
            raise SessionLost(f"{NO_TAP} ({type(e).__name__}: {e})") from e
        q = self._q = Quartz
        mask = 0
        for t in _event_types(q):
            mask |= 1 << int(t)
        ready = threading.Event()
        failure: list[BaseException | None] = []

        def run():
            try:
                tap = q.CGEventTapCreate(q.kCGSessionEventTap, q.kCGHeadInsertEventTap,
                                         q.kCGEventTapOptionListenOnly, mask, self._callback,
                                         None)
                if tap is None:
                    failure.append(None)
                    return
                source = q.CFMachPortCreateRunLoopSource(None, tap, 0)
                loop = q.CFRunLoopGetCurrent()
                q.CFRunLoopAddSource(loop, source, q.kCFRunLoopCommonModes)
                q.CGEventTapEnable(tap, True)
                self._tap, self._loop = tap, loop
            except Exception as e:
                failure.append(e)
                return
            finally:
                ready.set()
            q.CFRunLoopRun()

        self._thread = threading.Thread(target=run, name="input-tap", daemon=True)
        self._thread.start()
        if not ready.wait(TAP_START_TIMEOUT) or failure or self._tap is None:
            err = failure[0] if failure else None
            self.stop()
            detail = f" ({type(err).__name__}: {err})" if err is not None else ""
            raise SessionLost(NO_TAP + detail)

    def reenable(self) -> None:
        if self._tap is not None:
            self._q.CGEventTapEnable(self._tap, True)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._tap is not None

    def enabled(self) -> bool:
        return self._tap is not None and bool(self._q.CGEventTapIsEnabled(self._tap))

    def stop(self) -> None:
        tap, loop, thread = self._tap, self._loop, self._thread
        self._tap = self._loop = self._thread = None
        try:
            if tap is not None:
                self._q.CGEventTapEnable(tap, False)
            if loop is not None:
                self._q.CFRunLoopStop(loop)
        except Exception:
            log.exception("event tap stop")
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)


class IdleGate:
    """start() once per connect, stop() on disconnect; check() before every unit of input."""

    def __init__(self, min_idle_s: float = MIN_IDLE_S, *,
                 tap_factory: Callable[[Callable[[int, int], None]], object] = QuartzTap,
                 idle_seconds: Callable[[], float] = hid_idle_seconds,
                 clock: Callable[[], float] = time.monotonic):
        if not float(min_idle_s) >= MIN_ALLOWED_S:
            raise ValueError(f"min_idle_s must be at least {MIN_ALLOWED_S:g} s")
        self._min = float(min_idle_s)
        self._tap_factory = tap_factory
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._tap = None
        self._last_human = float("-inf")

    @property
    def min_idle_s(self) -> float:
        return self._min

    def _human(self) -> None:
        self._last_human = max(self._last_human, self._clock())

    def on_event(self, type_: int, user_data: int) -> None:
        """Called from the tap thread for every event it sees."""
        if type_ in TAP_DISABLED:
            log.warning("the system switched the input tap off (%#x), switching it on", type_)
            self._human()             # what happened while it was off was not seen
            tap = self._tap
            if tap is not None:
                tap.reenable()
            return
        if user_data != ROBOT_TAG:
            self._human()

    def start(self) -> None:
        if self._tap is not None:
            return
        tap = self._tap_factory(self.on_event)
        self._tap = tap
        try:
            tap.start()
            idle = float(self._idle_seconds())
        except SessionLost:
            self.stop()
            raise
        except Exception as e:
            self.stop()
            raise SessionLost(f"не удалось узнать время простоя Mac: {type(e).__name__}") from e
        self._last_human = max(self._last_human, self._clock() - idle)

    def stop(self) -> None:
        tap, self._tap = self._tap, None
        if tap is not None:
            try:
                tap.stop()
            except Exception:
                log.exception("input tap stop")

    @contextmanager
    def robot_event(self):
        """Wraps posting one event. The event carries ROBOT_TAG; nothing to record here."""
        yield

    def check(self) -> None:
        tap = self._tap
        if tap is None or not tap.running():
            raise SessionLost("наблюдение за мышью и клавиатурой не запущено")
        if not tap.enabled():
            self._human()
            tap.reenable()
        ago = self._clock() - self._last_human
        if ago < self._min:
            raise UserActive(f"ввод на Mac {ago:.1f} с назад, нужно не меньше {self._min:g} с")
