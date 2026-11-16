"""Input into the Microsoft Remote Desktop window, posted as Quartz events.

Three operations and nothing else: a single left click at a layout point, a text typed key
by key, and Return. No modifier key is ever pressed and every key event carries empty
modifier flags: Microsoft Remote Desktop drops synthetic Ctrl, Cmd and Shift whatever way
they are posted (seen live). Input is sent in units: one click (move, down,
up) or one key press (down, up). Each operation:

1. asks the idle gate (a person at the Mac means nothing is sent, not even activation);
2. brings the RDP window to front if it is not, and requires that Microsoft Remote Desktop
   has keyboard focus and our window is the topmost normal window, otherwise
   TransientOverlay and no event;
3. before every unit asks the gate again, checks focus and the topmost window again, for
   keys also that no overlay (Spotlight, a notification, a dialog) is on screen, and for a
   click that nothing covers the point. A stop therefore happens between units only.

Focus and overlay problems raise TransientOverlay (a banner or a window in front for a
moment: the pass ends quietly and is tried again on the next tick, like a person at the
Mac). A wrong window size or a click outside the window stays UnexpectedScreen.

A unit is posted whole without checks inside; whatever stops it (an error of the poster),
the key or button it pressed is released in `finally`, so nothing stays held in the RDP
session. Characters map to US virtual keycodes of keys typed without Shift; anything else
is refused before any event. No autorepeat, no double click.
"""
import logging
import time
from collections.abc import Callable

from orderbot.screen.guard import validate_text
from orderbot.screen.idle import ROBOT_TAG
from orderbot.screen.reader import UnexpectedScreen
from orderbot.screen.session import SessionLost, TransientOverlay

log = logging.getLogger(__name__)

NO_POST = ("нельзя отправлять нажатия: проверьте разрешение «Универсальный доступ» для "
           "программы, в которой запущен робот (Системные настройки → Конфиденциальность "
           "и безопасность)")

ACTIVATE_TIMEOUT = 2.0
ACTIVATE_POLL = 0.1
EVENT_DELAY = 0.02
MIN_CLICK_GAP = 0.7  # between any two clicks, so 1C never sees a double click

RETURN = 36

_LETTERS = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
            "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31,
            "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46}
_DIGITS = {"1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28,
           "0": 29}

# character -> US keycode, each typed without any modifier
KEYCODES: dict[str, int] = {**_LETTERS, **_DIGITS, "-": 27, "/": 44, ".": 47}

# abstract events: ("mouse", "move" | "down" | "up", x, y) and ("key", code, down)
Event = tuple


class QuartzPoster:
    """Turns abstract events into CGEvents from an event source on the HID system state.
    Every event carries ROBOT_TAG as its source user data, so the idle gate's tap tells it
    from a person's."""

    def __init__(self):
        import Quartz

        self._q = Quartz
        self._src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        if self._src is None:
            raise SessionLost(NO_POST)
        Quartz.CGEventSourceSetUserData(self._src, ROBOT_TAG)
        self._mouse = {"move": Quartz.kCGEventMouseMoved,
                       "down": Quartz.kCGEventLeftMouseDown,
                       "up": Quartz.kCGEventLeftMouseUp}

    def __call__(self, event: Event) -> None:
        q = self._q
        if event[0] == "key":
            _, code, down = event
            e = q.CGEventCreateKeyboardEvent(self._src, code, down)
            if e is None:
                raise SessionLost(NO_POST)
            q.CGEventSetFlags(e, 0)  # never a modifier, whatever the system state holds
        else:
            _, kind, x, y = event
            e = q.CGEventCreateMouseEvent(self._src, self._mouse[kind], (x, y),
                                          q.kCGMouseButtonLeft)
            if e is None:
                raise SessionLost(NO_POST)
            if kind != "move":
                q.CGEventSetIntegerValueField(e, q.kCGMouseEventClickState, 1)
        q.CGEventSetIntegerValueField(e, q.kCGEventSourceUserData, ROBOT_TAG)
        q.CGEventPost(q.kCGHIDEventTap, e)


def _quartz_can_post() -> bool:
    import Quartz

    return bool(Quartz.CGPreflightPostEventAccess())


def _keys(text: str) -> list[int]:
    validate_text(text)
    try:
        return [KEYCODES[c] for c in text]
    except KeyError as e:
        raise ValueError("text to type has a character without a key") from e


def _as_session_lost(fn, *args):
    """Missing pyobjc or a pyobjc error while sending input means 1C is not usable."""
    try:
        return fn(*args)
    except SessionLost:
        raise
    except Exception as e:
        raise SessionLost(f"{NO_POST} ({type(e).__name__}: {e})") from e


class ClickGap:
    """When the last click was sent. One object is shared by every backend of a run, so a
    reconnect does not reset the gap."""

    def __init__(self):
        self.last: float | None = None


class MacInputBackend:
    """The public surface is exactly click, type_text, press_return; a test pins it."""

    def __init__(self, window, idle_gate, *, poster: Callable[[Event], None] | None = None,
                 can_post: Callable[[], bool] = _quartz_can_post,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 click_gap: ClickGap | None = None):
        self._window = window
        self._gate = idle_gate
        self._poster = poster
        self._can_post = can_post
        self._sleep = sleep
        self._clock = clock
        self._gap = click_gap or ClickGap()

    def _begin(self) -> None:
        self._gate.check()
        if not _as_session_lost(self._can_post):
            raise SessionLost(NO_POST)
        if self._poster is None:
            self._poster = _as_session_lost(QuartzPoster)
        if self._window.is_front():
            return
        self._window.activate()
        deadline = self._clock() + ACTIVATE_TIMEOUT
        while not self._window.is_front():
            if self._clock() >= deadline:
                raise TransientOverlay("окно Microsoft Remote Desktop не вышло на передний план")
            self._sleep(ACTIVATE_POLL)

    def _check_unit(self, unit: list[Event]) -> None:
        self._gate.check()
        if not self._window.is_front():
            raise TransientOverlay("окно Microsoft Remote Desktop ушло с переднего плана")
        if unit[0][0] == "key":
            if self._window.key_overlay():
                raise TransientOverlay("поверх экрана другое окно, нажатия могут уйти в него: "
                                       f"{getattr(self._window, 'last_overlay', None) or '?'}")
        elif self._window.covered_at(unit[0][2], unit[0][3]):
            raise TransientOverlay("точку клика закрывает другое окно")

    def _emit(self, event: Event) -> None:
        with self._gate.robot_event():
            _as_session_lost(self._poster, event)
        self._sleep(EVENT_DELAY)

    @staticmethod
    def _release_event(held: tuple) -> Event:
        if held[0] == "key":
            return ("key", held[1], False)
        return ("mouse", "up", held[1], held[2])

    def _post_unit(self, unit: list[Event]) -> None:
        held: list[tuple] = []  # keys and the button that are down, in press order
        try:
            for event in unit:
                if event[0] == "key":
                    ident = ("key", event[1])
                    down = event[2]
                else:
                    ident = ("mouse", event[2], event[3])
                    down = None if event[1] == "move" else event[1] == "down"
                if down:
                    held.append(ident)  # counted as held even if the post fails half-way
                self._emit(event)
                if down is False and ident in held:
                    held.remove(ident)
        finally:
            for ident in reversed(held):
                try:
                    self._emit(self._release_event(ident))
                except Exception:
                    log.exception("could not release %s", ident)

    def _post(self, units: list[list[Event]]) -> None:
        for unit in units:
            self._check_unit(unit)
            self._post_unit(unit)

    @staticmethod
    def _press(code: int) -> list[Event]:
        return [("key", code, True), ("key", code, False)]

    def click(self, x: int, y: int) -> None:
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (x, y)):
            raise ValueError("click coordinates must be non-negative integers")
        if self._gap.last is not None:   # before _begin: the wait happens before any check
            wait = self._gap.last + MIN_CLICK_GAP - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._begin()
        w = self._window
        bounds, scale = w.bounds(), w.scale
        if scale is None or not scale > 0 or w.captured_bounds is None:
            raise UnexpectedScreen("нет снимка окна перед кликом")
        if tuple(bounds[2:]) != tuple(w.captured_bounds[2:]):
            raise UnexpectedScreen("размер окна изменился после снимка")
        px, py = bounds[0] + x / scale, bounds[1] + y / scale
        if not (bounds[0] <= px < bounds[0] + bounds[2] and bounds[1] <= py < bounds[1] + bounds[3]):
            raise UnexpectedScreen("точка клика вне окна")
        try:
            self._post([[("mouse", "move", px, py), ("mouse", "down", px, py),
                         ("mouse", "up", px, py)]])
        finally:
            self._gap.last = self._clock()

    def type_text(self, text: str) -> None:
        keys = _keys(text)
        self._begin()
        self._post([self._press(code) for code in keys])

    def press_return(self) -> None:
        self._begin()
        self._post([self._press(RETURN)])

