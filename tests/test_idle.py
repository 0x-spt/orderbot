"""IdleGate and QuartzTap on fakes: no real event tap, no real event."""
import sys
import threading
import types

import pytest

from orderbot.screen import idle
from orderbot.screen.idle import (
    ROBOT_TAG,
    TAP_DISABLED_BY_TIMEOUT,
    TAP_DISABLED_BY_USER_INPUT,
    IdleGate,
    QuartzTap,
    UserActive,
)
from orderbot.screen.session import SessionLost

KEY_DOWN, MOUSE_MOVED = 10, 5


class T:
    """`now` is the monotonic clock; `hid_idle` what the system idle counter says at start."""

    def __init__(self):
        self.now = 1000.0
        self.hid_idle = 1e6


class FakeTap:
    def __init__(self, on_event, fail=False):
        self.on_event = on_event
        self.fail = fail
        self.started = self.stopped = False
        self.reenabled = 0
        self.is_running = True
        self.is_enabled = True

    def start(self):
        self.started = True
        if self.fail:
            raise SessionLost(idle.NO_TAP)

    def stop(self):
        self.stopped = True

    def reenable(self):
        self.reenabled += 1
        self.is_enabled = True

    def running(self):
        return self.is_running

    def enabled(self):
        return self.is_enabled


def gate(t, min_idle_s=30, fail=False):
    taps = []

    def factory(on_event):
        taps.append(FakeTap(on_event, fail))
        return taps[-1]

    g = IdleGate(min_idle_s, tap_factory=factory, idle_seconds=lambda: t.hid_idle,
                 clock=lambda: t.now)
    g.taps = taps
    return g


def started(t, min_idle_s=30):
    g = gate(t, min_idle_s)
    g.start()
    return g


def robot(g, type_=KEY_DOWN):
    g.taps[0].on_event(type_, ROBOT_TAG)


def human(g, type_=KEY_DOWN):
    g.taps[0].on_event(type_, 0)


def test_idle_gate_threshold():
    t = T()
    g = started(t)
    human(g)
    t.now += 29.9
    with pytest.raises(UserActive):
        g.check()
    t.now += 0.1
    g.check()


def test_default_threshold_is_30_seconds():
    t = T()
    g = IdleGate(tap_factory=FakeTap, idle_seconds=lambda: t.hid_idle, clock=lambda: t.now)
    assert g.min_idle_s == 30.0
    g.start()
    t.now -= 29.0
    g._tap.on_event(KEY_DOWN, 0)
    t.now += 29.0
    with pytest.raises(UserActive):
        g.check()


@pytest.mark.parametrize("value", [9.99, 0, -1])
def test_min_idle_has_a_floor_of_10_seconds(value):
    with pytest.raises(ValueError):
        IdleGate(value)


def test_robot_tagged_events_never_block():
    t = T()
    g = started(t)
    g.check()
    for type_ in (KEY_DOWN, 11, 12, MOUSE_MOVED, 1, 2, 22):
        with g.robot_event():
            robot(g, type_)
        t.now += 0.001
        g.check()


def test_one_untagged_event_blocks_for_min_idle():
    t = T()
    g = started(t, min_idle_s=10)
    human(g, MOUSE_MOVED)
    for dt in (0.0, 0.001, 5.0, 9.99):
        t.now = 1000.0 + dt
        with pytest.raises(UserActive):
            g.check()
    t.now = 1010.0
    g.check()


@pytest.mark.parametrize("data", [0, 1, ROBOT_TAG - 1, -1])
def test_any_other_user_data_is_a_person(data):
    t = T()
    g = started(t)
    g.taps[0].on_event(KEY_DOWN, data)
    with pytest.raises(UserActive):
        g.check()


def test_human_event_right_after_robot_event_is_active():
    """Replaces the old ±5 ms window test: timing no longer matters, only the tag."""
    t = T()
    g = started(t)
    with g.robot_event():
        robot(g)
    t.now += 0.0001
    human(g)              # a person moves the mouse a fraction of a millisecond later
    t.now += 0.0001
    with pytest.raises(UserActive):
        g.check()


def test_human_event_just_before_robot_event_is_active():
    t = T()
    g = started(t)
    human(g)
    t.now += 0.001
    robot(g)              # the robot's own later event does not hide the person's
    with pytest.raises(UserActive):
        g.check()


def test_continuous_human_activity_during_typing_is_caught():
    """Robot and person interleave every few ms; the first check after a person's event stops."""
    t = T()
    g = started(t)
    g.check()
    checks = 0
    with pytest.raises(UserActive):
        for i in range(50):
            with g.robot_event():
                robot(g)
            t.now += 0.013
            if i == 3:
                human(g)
            t.now += 0.007
            checks += 1
            g.check()
    assert checks == 4


def test_start_takes_the_last_person_from_the_hid_idle_counter():
    t = T()
    t.hid_idle = 2.0      # someone used the Mac 2 s before the tap started
    g = started(t)
    with pytest.raises(UserActive):
        g.check()
    t.now += 27.9
    with pytest.raises(UserActive):
        g.check()
    t.now += 0.1
    g.check()


def test_start_with_long_hid_idle_lets_input_through():
    t = T()
    t.hid_idle = 30.0
    started(t).check()


def test_hid_counter_failure_at_start_is_session_lost_and_stops_the_tap():
    def broken():
        raise ImportError("No module named 'Quartz'")

    g = IdleGate(tap_factory=FakeTap, idle_seconds=broken)
    with pytest.raises(SessionLost):
        g.start()
    assert g._tap is None


def test_no_tap_is_session_lost():
    t = T()
    g = gate(t, fail=True)
    with pytest.raises(SessionLost, match="Универсальный доступ"):
        g.start()
    assert g.taps[0].stopped
    with pytest.raises(SessionLost):
        g.check()


def test_check_before_start_or_after_stop_is_session_lost():
    t = T()
    g = gate(t)
    with pytest.raises(SessionLost):
        g.check()
    g.start()
    g.check()
    g.stop()
    assert g.taps[0].stopped
    with pytest.raises(SessionLost):
        g.check()


def test_dead_tap_thread_is_session_lost():
    t = T()
    g = started(t)
    g.taps[0].is_running = False
    with pytest.raises(SessionLost):
        g.check()


@pytest.mark.parametrize("notice", [TAP_DISABLED_BY_TIMEOUT, TAP_DISABLED_BY_USER_INPUT])
def test_tap_switched_off_is_switched_on_and_the_gap_counts_as_a_person(notice):
    t = T()
    g = started(t)
    g.taps[0].on_event(notice, 0)
    assert g.taps[0].reenabled == 1
    with pytest.raises(UserActive):
        g.check()
    t.now += 30
    g.check()


def test_tap_found_disabled_at_check_is_switched_on():
    t = T()
    g = started(t)
    g.taps[0].is_enabled = False
    with pytest.raises(UserActive):
        g.check()
    assert g.taps[0].reenabled == 1


def test_start_twice_keeps_one_tap():
    t = T()
    g = started(t)
    g.start()
    assert len(g.taps) == 1


# ------------------------------------------------------------- QuartzTap on a fake Quartz

def fake_quartz(create_ok=True):
    q = types.SimpleNamespace(
        kCGEventKeyDown=10, kCGEventKeyUp=11, kCGEventFlagsChanged=12, kCGEventMouseMoved=5,
        kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2, kCGEventLeftMouseDragged=6,
        kCGEventRightMouseDown=3, kCGEventRightMouseUp=4, kCGEventRightMouseDragged=7,
        kCGEventOtherMouseDown=25, kCGEventOtherMouseUp=26, kCGEventOtherMouseDragged=27,
        kCGEventScrollWheel=22, kCGSessionEventTap=1, kCGHeadInsertEventTap=0,
        kCGEventTapOptionListenOnly=1, kCFRunLoopCommonModes="common",
        kCGEventSourceUserData=42)
    q.calls = []
    q.stop_loop = threading.Event()

    def create(location, place, options, mask, callback, refcon):
        q.calls.append(("create", location, options, mask))
        q.callback = callback
        return "TAP" if create_ok else None

    q.CGEventTapCreate = create
    q.CFMachPortCreateRunLoopSource = lambda a, tap, o: "SRC"
    q.CFRunLoopGetCurrent = lambda: "LOOP"
    q.CFRunLoopAddSource = lambda loop, src, mode: q.calls.append(("add", loop, src, mode))
    q.CGEventTapEnable = lambda tap, on: q.calls.append(("enable", tap, on))
    q.CGEventTapIsEnabled = lambda tap: True
    q.CFRunLoopRun = lambda: q.stop_loop.wait(5)
    q.CFRunLoopStop = lambda loop: (q.calls.append(("stop", loop)), q.stop_loop.set())
    q.CGEventGetIntegerValueField = lambda e, field: e[field]
    return q


def test_quartz_tap_is_listen_only_on_input_events_and_stops(monkeypatch):
    q = fake_quartz()
    monkeypatch.setitem(sys.modules, "Quartz", q)
    seen = []
    tap = QuartzTap(lambda type_, data: seen.append((type_, data)))
    tap.start()
    try:
        _, location, options, mask = q.calls[0]
        assert (location, options) == (1, 1)
        for t_ in (10, 11, 12, 5, 1, 2, 6, 3, 4, 7, 25, 26, 27, 22):
            assert mask & (1 << t_)
        assert ("enable", "TAP", True) in q.calls
        assert tap.running() and tap.enabled()
        event = {42: ROBOT_TAG}
        assert q.callback(None, 10, event, None) is event
        q.callback(None, 5, {42: 0}, None)
        q.callback(None, TAP_DISABLED_BY_TIMEOUT, None, None)
        assert seen == [(10, ROBOT_TAG), (5, 0), (TAP_DISABLED_BY_TIMEOUT, 0)]
        tap.reenable()
        assert q.calls[-1] == ("enable", "TAP", True)
    finally:
        tap.stop()
    assert ("enable", "TAP", False) in q.calls and ("stop", "LOOP") in q.calls
    assert not tap.running()


def test_quartz_tap_unreadable_user_data_counts_as_a_person(monkeypatch):
    q = fake_quartz()
    monkeypatch.setitem(sys.modules, "Quartz", q)
    seen = []
    tap = QuartzTap(lambda type_, data: seen.append((type_, data)))
    tap.start()
    try:
        q.callback(None, 10, {}, None)    # no field: KeyError inside the callback
    finally:
        tap.stop()
    assert seen == [(10, 0)]


def test_quartz_tap_not_created_is_session_lost(monkeypatch):
    q = fake_quartz(create_ok=False)
    monkeypatch.setitem(sys.modules, "Quartz", q)
    tap = QuartzTap(lambda type_, data: None)
    with pytest.raises(SessionLost, match="Универсальный доступ"):
        tap.start()
    assert not tap.running()


def test_gate_with_quartz_tap_end_to_end_on_fake_quartz(monkeypatch):
    q = fake_quartz()
    monkeypatch.setitem(sys.modules, "Quartz", q)
    t = T()
    g = IdleGate(30, idle_seconds=lambda: t.hid_idle, clock=lambda: t.now)
    g.start()
    try:
        q.callback(None, 10, {42: ROBOT_TAG}, None)
        g.check()
        q.callback(None, 10, {42: 0}, None)
        with pytest.raises(UserActive):
            g.check()
        q.callback(None, TAP_DISABLED_BY_TIMEOUT, None, None)
        assert q.calls[-1] == ("enable", "TAP", True)
    finally:
        g.stop()
