"""MacInputBackend on fakes: no real event is posted and no real window is activated."""
from contextlib import contextmanager

import pytest

from orderbot.screen.guard import TYPABLE
from orderbot.screen.idle import UserActive
from orderbot.screen.mac_input import KEYCODES, RETURN, ClickGap, MacInputBackend
from orderbot.screen.reader import UnexpectedScreen
from orderbot.screen.session import SessionLost, TransientOverlay

# macOS virtual keycodes of Cmd, Shift, Caps Lock, Option, Control (both sides) and Fn
MODIFIER_CODES = set(range(54, 64))


class FakeWindow:
    def __init__(self, front=True, bounds=(100, 50, 1000, 700), scale=2.0):
        self.front = front
        self._bounds = bounds
        self.scale = scale
        self.captured_bounds = bounds
        self.activations = 0
        self.becomes_front = True
        self.covered = set()
        self.overlay = False

    def bounds(self):
        return self._bounds

    def is_front(self):
        return self.front

    def activate(self):
        self.activations += 1
        if self.becomes_front:
            self.front = True

    def covered_at(self, x, y):
        return (x, y) in self.covered

    def key_overlay(self):
        return self.overlay


class FakeGate:
    def __init__(self):
        self.active = False
        self.checks = 0
        self.robot_events = 0
        self.after = None  # number of checks after which the user becomes active

    def check(self):
        self.checks += 1
        if self.after is not None and self.checks > self.after:
            self.active = True
        if self.active:
            raise UserActive("someone is at the Mac")

    @contextmanager
    def robot_event(self):
        try:
            yield
        finally:
            self.robot_events += 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(window=None, gate=None, can_post=True):
    window = window or FakeWindow()
    gate = gate or FakeGate()
    posted = []
    clock = Clock()
    b = MacInputBackend(window, gate, poster=posted.append, can_post=lambda: can_post,
                        sleep=clock.sleep, clock=clock)
    return b, window, gate, posted


def public(obj):
    return {n for n in dir(obj) if not n.startswith("_")}


def test_backend_has_no_generic_key_or_double_click():
    assert public(MacInputBackend) == {"click", "type_text", "press_return"}


def test_click_converts_pixels_to_points():
    b, w, g, posted = make(FakeWindow(bounds=(100, 50, 1000, 700), scale=2.0))
    b.click(1669, 213)
    x, y = 100 + 834.5, 50 + 106.5
    assert posted == [("mouse", "move", x, y), ("mouse", "down", x, y), ("mouse", "up", x, y)]
    assert g.robot_events == 3
    assert g.checks == 2  # once before activation, once before the click unit


@pytest.mark.parametrize("xy", [(-1, 5), (1.5, 2), ("1", 2), (True, 2)])
def test_click_validates_coordinates(xy):
    b, w, g, posted = make()
    with pytest.raises(ValueError):
        b.click(*xy)
    assert posted == [] and w.activations == 0


def test_click_outside_window_sends_nothing():
    b, w, g, posted = make(FakeWindow(bounds=(100, 50, 1000, 700), scale=2.0))
    with pytest.raises(UnexpectedScreen):
        b.click(2000, 10)  # x = 1100 points: right edge of the window
    assert posted == []


def test_click_on_covered_point_sends_nothing():
    w = FakeWindow(bounds=(100, 50, 1000, 700), scale=2.0)
    w.covered.add((150.0, 100.0))
    b, w, g, posted = make(w)
    with pytest.raises(TransientOverlay):
        b.click(100, 100)
    assert posted == []


def test_click_after_resize_sends_nothing():
    w = FakeWindow(bounds=(100, 50, 1000, 700), scale=2.0)
    w.captured_bounds = (100, 50, 900, 700)  # the frame was taken at another size
    b, w, g, posted = make(w)
    with pytest.raises(UnexpectedScreen):
        b.click(10, 10)
    assert posted == []


def test_click_without_a_frame_sends_nothing():
    w = FakeWindow()
    w.scale = None
    w.captured_bounds = None
    b, w, g, posted = make(w)
    with pytest.raises(UnexpectedScreen):
        b.click(10, 10)
    assert posted == []


def test_activation_when_not_front():
    b, w, g, posted = make(FakeWindow(front=False))
    b.press_return()
    assert w.activations == 1
    assert posted == [("key", 36, True), ("key", 36, False)]


def test_no_activation_when_already_front():
    b, w, g, posted = make(FakeWindow(front=True))
    b.press_return()
    assert w.activations == 0


@pytest.mark.parametrize("action", [
    lambda b: b.click(10, 10),
    lambda b: b.type_text("den091"),
    lambda b: b.press_return(),
])
def test_foreground_mismatch_sends_nothing(action):
    w = FakeWindow(front=False)
    w.becomes_front = False
    b, w, g, posted = make(w)
    with pytest.raises(TransientOverlay):
        action(b)
    assert posted == []
    assert w.activations == 1


def test_window_leaving_front_mid_typing_stops_between_units():
    class Flaky(FakeWindow):
        calls = 0

        def is_front(self):
            self.calls += 1
            return self.calls < 4  # _begin, "a" unit, "b" unit, then gone

    b, w, g, posted = make(Flaky())
    with pytest.raises(TransientOverlay):
        b.type_text("abcdef")
    assert len(posted) == 2 + 2
    assert held_down(posted) == set()


def test_focus_lost_to_another_app_sends_nothing():
    """The topmost window may be ours while another app (Finder, Spotlight) has focus:
    is_front() is false then, and nothing is sent."""
    w = FakeWindow(front=False)
    w.becomes_front = False
    b, w, g, posted = make(w)
    with pytest.raises(TransientOverlay):
        b.press_return()
    assert posted == []


@pytest.mark.parametrize("action", [
    lambda b: b.type_text("den091"),
    lambda b: b.press_return(),
])
def test_overlay_above_normal_windows_blocks_keys(action):
    w = FakeWindow()
    w.overlay = True
    b, w, g, posted = make(w)
    with pytest.raises(TransientOverlay):
        action(b)
    assert posted == []


@pytest.mark.parametrize("action", [
    lambda b: b.click(10, 10),
    lambda b: b.type_text("den091"),
    lambda b: b.press_return(),
])
def test_user_active_sends_nothing(action):
    g = FakeGate()
    g.active = True
    w = FakeWindow(front=False)
    b, w, g, posted = make(w, g)
    with pytest.raises(UserActive):
        action(b)
    assert posted == []
    assert w.activations == 0  # the window is not even brought to front


def held_down(posted):
    """Keys and the mouse button still down after this event stream."""
    down = set()
    for e in posted:
        if e[0] == "key":
            (down.add if e[2] else down.discard)(("key", e[1]))
        elif e[1] == "down":
            down.add("button")
        elif e[1] == "up":
            down.discard("button")
    return down


def test_user_active_mid_typing_stops_at_once():
    g = FakeGate()
    b, w, g, posted = make(gate=g)
    g.after = 3  # begin, "a", "b"; the person touches the Mac before "c"
    with pytest.raises(UserActive):
        b.type_text("abcdefgh")
    assert len(posted) == 2 + 2
    assert g.robot_events == len(posted)
    assert held_down(posted) == set()


TEXT = "ab-c"


def _all_events(action):
    b, w, g, posted = make()
    action(b)
    return len(posted)


ACTIONS = {
    "type": lambda b: b.type_text(TEXT),
    "click": lambda b: b.click(10, 10),
    "return": lambda b: b.press_return(),
}


@pytest.mark.parametrize("name", ACTIONS)
def test_no_key_or_button_left_down_when_the_gate_stops_at_any_check(name):
    action = ACTIONS[name]
    checks = FakeGate()
    b, w, checks, posted = make(gate=checks)
    action(b)
    for stop_after in range(checks.checks):
        g = FakeGate()
        g.after = stop_after
        b, w, g, posted = make(gate=g)
        with pytest.raises(UserActive):
            action(b)
        assert held_down(posted) == set(), (stop_after, posted)


@pytest.mark.parametrize("name", ACTIONS)
def test_no_key_or_button_left_down_when_posting_fails_at_any_event(name):
    action = ACTIONS[name]
    total = _all_events(action)
    for fail_at in range(total):
        posted, n = [], {"i": 0}

        def poster(event):
            i = n["i"]
            n["i"] += 1
            if i == fail_at:
                raise RuntimeError("pyobjc error")
            posted.append(event)

        clock = Clock()
        b = MacInputBackend(FakeWindow(), FakeGate(), poster=poster, can_post=lambda: True,
                            sleep=clock.sleep, clock=clock)
        with pytest.raises(SessionLost):
            action(b)
        assert held_down(posted) == set(), (fail_at, posted)


@pytest.mark.parametrize("name", ACTIONS)
def test_no_key_or_button_left_down_when_the_window_goes_at_any_check(name):
    action = ACTIONS[name]
    for front_calls in range(1, 12):
        class Going(FakeWindow):
            calls = 0

            def is_front(self):
                self.calls += 1
                return self.calls <= front_calls

        b, w, g, posted = make(Going())
        try:
            action(b)
        except TransientOverlay:
            pass
        assert held_down(posted) == set(), (front_calls, posted)


def test_type_presses_plain_keys_only():
    b, w, g, posted = make()
    b.type_text("az9-/.")

    def plain(code):
        return [("key", code, True), ("key", code, False)]

    assert posted == plain(0) + plain(6) + plain(25) + plain(27) + plain(44) + plain(47)


def test_keycode_table_covers_exactly_the_typable_charset():
    import string
    assert set(KEYCODES) == TYPABLE
    assert set(string.ascii_lowercase + string.digits + "-./") <= TYPABLE
    assert all(0 <= code <= 50 and code != RETURN for code in KEYCODES.values())
    assert not set(KEYCODES.values()) & MODIFIER_CODES


@pytest.mark.parametrize("name", ACTIONS)
def test_no_modifier_is_ever_posted(name):
    b, w, g, posted = make()
    ACTIONS[name](b)
    b.type_text("".join(sorted(TYPABLE)))
    for e in posted:
        if e[0] == "key":
            assert len(e) == 3 and e[1] not in MODIFIER_CODES, e


@pytest.mark.parametrize("text", ["a\nb", "a\tb", "", "a\x00b", "Ж", "a​b", "a+b",
                                  "a;b", "x" * 500, "A", "DEN091", "a_b", "a(b", "a b",
                                  "a:b"])
def test_type_refuses_other_characters_before_any_event(text):
    b, w, g, posted = make(FakeWindow(front=False))
    with pytest.raises(ValueError):
        b.type_text(text)
    assert posted == [] and w.activations == 0


def test_no_accessibility_permission_is_session_lost():
    b, w, g, posted = make(can_post=False)
    with pytest.raises(SessionLost) as e:
        b.press_return()
    assert "Универсальный доступ" in str(e.value)
    assert posted == []


@pytest.mark.parametrize("exc", [SessionLost("событие не создано"), RuntimeError("objc"),
                                 ImportError("No module named 'Quartz'")])
def test_event_creation_failure_is_session_lost(exc):
    def broken(event):
        raise exc

    w, g, clock = FakeWindow(), FakeGate(), Clock()
    b = MacInputBackend(w, g, poster=broken, can_post=lambda: True, sleep=clock.sleep,
                        clock=clock)
    with pytest.raises(SessionLost):
        b.press_return()


def test_missing_pyobjc_when_checking_permission_is_session_lost():
    def no_quartz():
        raise ImportError("No module named 'Quartz'")

    b, w, g, posted = make()
    b._can_post = no_quartz
    with pytest.raises(SessionLost):
        b.press_return()
    assert posted == []


def test_clicks_are_at_least_700_ms_apart():
    w = FakeWindow()
    gate = FakeGate()
    clock = Clock()
    times = []

    def poster(event):
        if event[0] == "mouse" and event[1] == "down":
            times.append(clock.t)

    b = MacInputBackend(w, gate, poster=poster, can_post=lambda: True,
                        sleep=clock.sleep, clock=clock)
    b.click(10, 10)
    b.click(500, 400)  # another point: the gap holds for any two clicks
    b.click(10, 10)
    assert times[1] - times[0] >= 0.7
    assert times[2] - times[1] >= 0.7


def test_no_extra_wait_when_clicks_are_far_apart():
    w = FakeWindow()
    clock = Clock()
    times = []
    b = MacInputBackend(w, FakeGate(), poster=lambda e: times.append(clock.t) if e[1] == "down" else None,
                        can_post=lambda: True, sleep=clock.sleep, clock=clock)
    b.click(10, 10)
    clock.t += 5
    before = clock.t
    b.click(10, 10)
    assert times[1] - before < 0.2


def test_click_gap_survives_a_new_backend_and_waits_before_any_check():
    clock = Clock()
    gap = ClickGap()
    gate = FakeGate()
    times = []
    def poster(e):
        if e[1] == "down":
            times.append(clock.t)
    b1 = MacInputBackend(FakeWindow(), gate, poster=poster, can_post=lambda: True,
                         sleep=clock.sleep, clock=clock, click_gap=gap)
    b1.click(10, 10)
    checks = gate.checks
    b2 = MacInputBackend(FakeWindow(), gate, poster=poster, can_post=lambda: True,
                         sleep=clock.sleep, clock=clock, click_gap=gap)   # a reconnect
    b2.click(10, 10)
    assert times[1] - times[0] >= 0.7
    assert gate.checks > checks


def test_transient_overlay_is_not_an_unexpected_screen():
    """It must not reach the pipeline's pausing handler for UnexpectedScreen."""
    assert not issubclass(TransientOverlay, UnexpectedScreen)


# ------------------------------------------------------------------ QuartzPoster
def _fake_quartz():
    import types

    fake = types.SimpleNamespace(
        kCGEventSourceStateHIDSystemState=1, kCGEventMouseMoved=5, kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2, kCGHIDEventTap=0, kCGMouseButtonLeft=0,
        kCGMouseEventClickState=1, kCGEventSourceUserData=42)
    made, posted, sources = [], [], []

    class Ev(dict):
        pass

    src = object()
    fake.src = src
    fake.CGEventSourceCreate = lambda state: src
    fake.CGEventSourceSetUserData = lambda s, data: sources.append((s, data))

    def make_event(*args):
        e = Ev(args=args)
        made.append(e)
        return e
    fake.CGEventCreateKeyboardEvent = make_event
    fake.CGEventCreateMouseEvent = make_event
    fake.CGEventSetFlags = lambda e, f: e.__setitem__("flags", f)
    fake.CGEventSetIntegerValueField = lambda e, field, value: e.__setitem__(field, value)
    fake.CGEventPost = lambda tap, e: posted.append(e)
    return fake, made, posted, sources


def test_quartz_poster_clears_modifier_flags_on_every_key(monkeypatch):
    """Whatever the system modifier state holds, a key event carries no modifier flag; the
    poster has no way to change an event's type (no flags-changed events)."""
    import sys

    from orderbot.screen import mac_input

    fake, made, posted, _ = _fake_quartz()
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    poster = mac_input.QuartzPoster()
    events = [ev for c in sorted(TYPABLE) for ev in MacInputBackend._press(KEYCODES[c])]
    events += MacInputBackend._press(RETURN)
    for ev in events:
        poster(ev)
    assert len(posted) == len(events)
    assert all(e["flags"] == 0 for e in posted)
    assert not any(e["args"][1] in MODIFIER_CODES for e in posted)


def test_quartz_poster_tags_every_event_and_its_source(monkeypatch):
    """The idle gate's tap tells the robot's events from a person's by this tag."""
    import sys

    from orderbot.screen import mac_input
    from orderbot.screen.idle import ROBOT_TAG

    fake, made, posted, sources = _fake_quartz()
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    poster = mac_input.QuartzPoster()
    assert sources == [(fake.src, ROBOT_TAG)]
    events = MacInputBackend._press(KEYCODES["a"])
    events += MacInputBackend._press(RETURN)
    events += [("mouse", k, 1.0, 2.0) for k in ("move", "down", "up")]
    for ev in events:
        poster(ev)
    assert len(posted) == len(events) == 7
    assert all(e[42] == ROBOT_TAG for e in posted)


class TapFeedingPoster:
    """Stands for the system: every posted event reaches the gate's tap with the robot tag;
    after `human_after` events a person presses a key (untagged)."""

    def __init__(self, gate, human_after):
        from orderbot.screen.idle import ROBOT_TAG

        self.gate, self.human_after, self.tag = gate, human_after, ROBOT_TAG
        self.posted = []

    def __call__(self, event):
        self.posted.append(event)
        self.gate.on_event(10, self.tag)
        if len(self.posted) == self.human_after:
            self.gate.on_event(10, 0)


def real_gate(clock):
    from orderbot.screen.idle import IdleGate

    class Tap:
        def __init__(self, on_event):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def running(self):
            return True

        def enabled(self):
            return True

        def reenable(self):
            pass

    g = IdleGate(30, tap_factory=Tap, idle_seconds=lambda: 1e6, clock=clock)
    g.start()
    return g


def test_robot_typing_alone_passes_the_real_gate():
    clock = Clock()
    g = real_gate(clock)
    poster = TapFeedingPoster(g, human_after=None)
    b = MacInputBackend(FakeWindow(), g, poster=poster, can_post=lambda: True,
                        sleep=clock.sleep, clock=clock)
    b.type_text("den091-01114z")
    b.press_return()
    b.click(10, 10)
    assert len(poster.posted) == 2 * len("den091-01114z") + 2 + 3


def test_untagged_event_mid_typing_stops_input_before_the_next_unit():
    clock = Clock()
    g = real_gate(clock)
    poster = TapFeedingPoster(g, human_after=3)   # "a", then during "b"
    b = MacInputBackend(FakeWindow(), g, poster=poster, can_post=lambda: True,
                        sleep=clock.sleep, clock=clock)
    with pytest.raises(UserActive):
        b.type_text("abcdefgh")
    assert len(poster.posted) == 2 + 2      # the unit in progress finishes, nothing after it
    assert held_down(poster.posted) == set()
