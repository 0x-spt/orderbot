import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from orderbot.domain import Action, ItemRead, Order, OrderLine, Reason, StockRow
from orderbot.pipeline import Pipeline, purge_snapshots
from orderbot.screen.idle import UserActive
from orderbot.screen.lookup import ArticleNotFound


def _real(system):
    """System messages other than the \"do not touch the Mac\" notice."""
    return [t for t in system if not t.startswith("Есть заказы для проверки")]
from orderbot.screen.reader import ReadUncertain, UnexpectedScreen
from orderbot.screen.session import SessionLost, ShuttingDown, TransientOverlay
from orderbot.store import Store

UTC = UTC


def at(h, m=0, d=2):
    return datetime(2026, 11, d, h, m, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_order(oid="1", mp="ozon", article="A1", qty=1, wh="СПБ", deadline=None, created=None,
               paid="2000"):
    return Order(mp, oid, [OrderLine(article, qty, wh, Decimal(paid), None)],
                 created or at(9), deadline or at(23, d=5), "awaiting_packaging")


def make_read(article="A1", on_hand=10, reserve=0, epr="1000", ppr="800", wh="СПБ"):
    return ItemRead(article, {wh: StockRow(wh, on_hand, reserve)}, Decimal(epr),
                    Decimal(ppr), f"/snap/{article}.png")


class FakeMp:
    def __init__(self, name="ozon", orders=()):
        self.name = name
        self.orders = list(orders)
        self.awaiting = {}
        self.ready = {}            # order_id -> bool
        self.ready_qty = {}
        self.confirmed = []
        self.fail_poll = False
        self.confirm_error = None
        self.calls = []

    async def new_orders(self):
        if self.fail_poll:
            raise RuntimeError("api down")
        return list(self.orders)

    async def get_order(self, order_id):
        return next(o for o in self.orders if o.order_id == order_id)

    async def ready_quantities(self):
        return dict(self.ready_qty)

    async def is_awaiting(self, order):
        self.calls.append(("is_awaiting", order.order_id))
        return self.awaiting.get(order.order_id, True)

    async def is_ready(self, order):
        self.calls.append(("is_ready", order.order_id))
        return self.ready.get(order.order_id, False)

    async def confirm(self, order):
        self.calls.append(("confirm", order.order_id))
        if self.confirm_error:
            raise self.confirm_error
        self.confirmed.append(order.order_id)
        self.ready[order.order_id] = True


class FakeOneC:
    def __init__(self, items=None):
        self.items = items if items is not None else {"A1": make_read()}
        self.connects = 0
        self.disconnects = 0
        self.reads = []
        self.connect_error = None

    def connect(self):
        self.connects += 1
        if self.connect_error:
            raise self.connect_error

    def disconnect(self):
        self.disconnects += 1

    def read(self, article):
        self.reads.append(article)
        v = self.items[article]
        if isinstance(v, Exception):
            raise v
        return v


class FakeNotifier:
    def __init__(self):
        self.alerts = []
        self.reserve = []
        self.system = []
        self.done = []          # (mp, order_id, line) of order_done
        self.refreshed = []     # (mp, order_id) of refresh_buttons
        self.stored = 1         # stored copies refresh_buttons reports

    async def order_done(self, mp, order_id, line_html):
        self.done.append((mp, order_id, line_html))
        return self.stored

    async def refresh_buttons(self, mp, order_id):
        self.refreshed.append((mp, order_id))
        return self.stored

    async def order_alert(self, order, decision, snapshot, urgent, observed, reads=None):
        self.alerts.append((order, decision, snapshot, urgent, observed))

    async def reserve_request(self, order, decision, snapshot, urgent, reads=None):
        self.reserve.append((order, decision, snapshot, urgent))

    async def system_alert(self, text):
        self.system.append(text)

    async def health_alert(self, text):
        self.system.append(text)


@pytest.fixture
def clock():
    return Clock(at(10))


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "s.db", clock=clock)


@pytest.fixture
def mp():
    return FakeMp(orders=[make_order()])


@pytest.fixture
def onec():
    return FakeOneC()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def pipe(store, mp, onec, notifier, clock):
    return Pipeline(store, [mp], onec, notifier, clock=clock)


def confirm_mode(store):
    store.set_mode("ozon", "confirm", "t")


async def test_observe_never_confirms(pipe, store, mp, notifier):
    await pipe.tick()
    assert mp.confirmed == []
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert notifier.alerts[0][4] is True
    assert store.get_decision("ozon", "1").action == Action.CONFIRM


async def test_confirm_mode_confirms_once(pipe, store, mp, notifier):
    confirm_mode(store)
    await pipe.tick()
    await pipe.tick()
    assert mp.confirmed == ["1"]
    assert store.get_state("ozon", "1") == "CONFIRMED"
    assert len(notifier.alerts) + len(notifier.reserve) + len(notifier.system) <= 1


async def test_escalate_sends_alert_no_confirm(pipe, store, mp, onec, notifier):
    confirm_mode(store)
    onec.items["A1"] = make_read(on_hand=0)
    await pipe.tick()
    assert mp.confirmed == []
    assert store.get_state("ozon", "1") == "ESCALATED"
    order, decision, snap, urgent, observed = notifier.alerts[0]
    assert decision.action == Action.ESCALATE and observed is False
    assert snap == ["/snap/A1.png"]


async def reserve_setup(pipe, store, onec):
    confirm_mode(store)
    onec.items["A1"] = make_read(on_hand=1, reserve=1)
    await pipe.tick()
    assert store.get_state("ozon", "1") == "AWAITING_RESERVE"


async def test_ask_reserve_then_yes_rereads_and_confirms(pipe, store, mp, onec, notifier):
    await reserve_setup(pipe, store, onec)
    assert len(notifier.reserve) == 1
    reads_before = len(onec.reads)
    await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert len(onec.reads) == reads_before + 1
    assert mp.confirmed == ["1"]
    assert store.get_state("ozon", "1") == "CONFIRMED"


async def test_reserve_yes_but_stock_gone_escalates(pipe, store, mp, onec, notifier):
    await reserve_setup(pipe, store, onec)
    onec.items["A1"] = make_read(on_hand=0)
    await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert mp.confirmed == []
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert notifier.alerts[-1][1].action == Action.ESCALATE


async def test_reserve_yes_but_order_gone_no_confirm(pipe, store, mp, onec, notifier):
    await reserve_setup(pipe, store, onec)
    mp.awaiting["1"] = False
    await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert mp.confirmed == []
    assert store.get_state("ozon", "1") == "HANDLED_EXTERNALLY"
    assert len(notifier.system) == 1


async def test_reserve_no_does_nothing_on_marketplace(pipe, store, mp, onec):
    await reserve_setup(pipe, store, onec)
    mp.calls.clear()
    await pipe.resolve_reserve("ozon", "1", False, "boss")
    assert store.get_state("ozon", "1") == "REJECTED_BY_HUMAN"
    assert mp.calls == [] and mp.confirmed == []


async def test_resolve_reserve_wrong_state_is_noop(pipe, store, mp):
    await pipe.tick()  # observe -> OBSERVED
    msg = await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert msg
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert mp.confirmed == []


async def test_reserve_approval_in_observe_mode_is_recorded(pipe, store, mp, onec):
    await reserve_setup(pipe, store, onec)
    store.set_mode("ozon", "observe", "t")
    mp.calls.clear()
    msg = await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert msg == "Резерв разрешён. Робот в режиме наблюдения: соберите заказ кнопкой."
    assert mp.confirmed == [] and mp.calls == []
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert store.get_decision("ozon", "1").action == Action.ASK_RESERVE
    ev = [e for e in store.events("ozon", "1") if e["event"] == "reserve_approved"]
    assert [e["payload"]["user"] for e in ev] == ["boss"]


async def test_robot_confirm_skips_order_cancelled_by_human(pipe, store, mp, onec, notifier):
    await reserve_setup(pipe, store, onec)
    store.set_paused(True, "u")
    await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert store.get_state("ozon", "1") == "RESERVE_APPROVED"
    store.set_paused(False, "u")
    lock = pipe.order_lock("ozon", "1")
    requested = asyncio.Event()
    real = pipe.order_lock

    def spy(m, o):
        requested.set()
        return real(m, o)
    pipe.order_lock = spy
    await lock.acquire()               # a person's action holds the order
    tick = asyncio.create_task(pipe.tick())
    await asyncio.wait_for(requested.wait(), 5)
    await asyncio.sleep(0)
    assert store.set_state_if("ozon", "1", {"RESERVE_APPROVED"}, "CANCELLED_BY_HUMAN")
    lock.release()
    await asyncio.wait_for(tick, 5)
    assert ("confirm", "1") not in mp.calls and mp.confirmed == []
    assert store.get_state("ozon", "1") == "CANCELLED_BY_HUMAN"
    assert [e["event"] for e in store.events("ozon", "1")].count("skipped_after_human") == 1


async def test_order_lock_is_one_per_order(pipe):
    assert pipe.order_lock("ozon", "1") is pipe.order_lock("ozon", "1")
    assert pipe.order_lock("ozon", "1") is not pipe.order_lock("ozon", "2")
    assert pipe.order_lock("ozon", "1") is not pipe.order_lock("wb", "1")


async def test_reserve_yes_while_paused_is_recorded_and_done_on_resume(pipe, store, mp, onec):
    await reserve_setup(pipe, store, onec)
    store.set_paused(True, "u")
    await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert store.get_state("ozon", "1") == "RESERVE_APPROVED"
    assert mp.confirmed == []
    await pipe.tick()
    assert mp.confirmed == []
    store.set_paused(False, "u")
    await pipe.tick()
    assert mp.confirmed == ["1"]
    assert store.get_state("ozon", "1") == "CONFIRMED"


async def test_reserve_yes_when_1c_unavailable_keeps_approval(pipe, store, mp, onec, notifier):
    await reserve_setup(pipe, store, onec)
    onec.connect_error = SessionLost("x")
    await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert store.get_state("ozon", "1") == "RESERVE_APPROVED"
    assert mp.confirmed == []


async def test_employee_already_handled_no_action(pipe, store, mp, notifier, onec):
    confirm_mode(store)
    mp.awaiting["1"] = False
    await pipe.tick()
    assert mp.confirmed == []
    assert store.get_state("ozon", "1") == "HANDLED_EXTERNALLY"
    assert notifier.alerts == [] and notifier.reserve == []


async def test_employee_handled_between_read_and_confirm(store, onec, notifier, clock):
    class Flip(FakeMp):
        n = 0

        async def is_awaiting(self, order):
            self.n += 1
            return self.n == 1
    m = Flip(orders=[make_order()])
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    confirm_mode(store)
    await p.tick()
    assert m.confirmed == []
    assert store.get_state("ozon", "1") == "HANDLED_EXTERNALLY"


async def test_paused_no_connect_orders_wait(pipe, store, onec):
    store.set_paused(True, "u")
    await pipe.tick()
    assert onec.connects == 0
    assert store.get_state("ozon", "1") == "NEW"


async def test_session_lost_sets_pause_alerts_once_keeps_new(store, notifier, clock):
    m = FakeMp(orders=[make_order("1"), make_order("2")])
    c = FakeOneC()
    c.items["A1"] = SessionLost("gone")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    await p.tick()
    assert store.is_paused()
    assert len(notifier.system) == 1
    assert store.get_state("ozon", "1") == "NEW"
    assert store.get_state("ozon", "2") == "NEW"
    assert c.connects == 1 and c.disconnects == 1


async def test_connect_failure_pauses(store, notifier, clock, mp):
    c = FakeOneC()
    c.connect_error = SessionLost("no rdp")
    p = Pipeline(store, [mp], c, notifier, clock=clock)
    await p.tick()
    assert store.is_paused() and len(notifier.system) == 1
    assert store.get_state("ozon", "1") == "NEW"


async def test_person_at_the_mac_during_connect_does_not_pause(store, notifier, clock, mp):
    """Connect may click the x of 1C's task reminder; a person at the Mac then only
    postpones the order to the next tick."""
    c = FakeOneC()
    c.connect_error = UserActive("input 1 s ago")
    p = Pipeline(store, [mp], c, notifier, clock=clock)
    await p.tick()
    assert not store.is_paused() and _real(notifier.system) == []
    assert store.get_state("ozon", "1") == "NEW"


async def test_unexpected_screen_alerts_once_keeps_new(store, notifier, clock):
    m = FakeMp(orders=[make_order("1"), make_order("2"), make_order("3")])
    c = FakeOneC()
    c.items["A1"] = UnexpectedScreen("foreign window")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    await p.tick()
    assert len(notifier.system) == 1
    assert [store.get_state("ozon", str(i)) for i in (1, 2, 3)] == ["NEW"] * 3
    assert len(c.reads) == 1
    assert store.is_paused()
    assert m.confirmed == []


async def test_pipeline_user_active_keeps_new_without_alert(store, notifier, clock):
    """A person is at the Mac: the pass stops, nothing is said, the order waits for the
    next tick, and the pause is not touched."""
    m = FakeMp(orders=[make_order("1"), make_order("2")])
    c = FakeOneC()
    c.items["A1"] = UserActive("input 3 s ago")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    assert [store.get_state("ozon", i) for i in ("1", "2")] == ["NEW", "NEW"]
    assert _real(notifier.system) == [] and notifier.alerts == [] and notifier.reserve == []
    assert not store.is_paused()
    assert len(c.reads) == 1 and c.disconnects == 1
    assert m.confirmed == []
    c.items["A1"] = make_read()
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"


async def test_mac_busy_for_30_minutes_alerts_once_then_resets(store, notifier, clock):
    m = FakeMp(orders=[make_order("1")])
    c = FakeOneC()
    c.items["A1"] = UserActive("x")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    start = clock.t
    for minutes in (0, 10, 29):
        clock.t = start + timedelta(minutes=minutes)
        await p.tick()
    assert _real(notifier.system) == []
    clock.t = start + timedelta(minutes=30)
    await p.tick()
    assert len(_real(notifier.system)) == 1 and "Mac занят" in _real(notifier.system)[0]
    clock.t = start + timedelta(minutes=45)
    await p.tick()
    assert len(_real(notifier.system)) == 1  # once per unbroken wait
    assert store.get_state("ozon", "1") == "NEW" and not store.is_paused()

    # a read gets through (an earlier order, read first); order 1 still waits, and its
    # wait counts from scratch
    m.orders.append(make_order("2", article="A2", deadline=at(20, d=5)))
    c.items["A2"] = make_read("A2")
    clock.t = start + timedelta(minutes=46)
    await p.tick()
    assert store.get_state("ozon", "2") == "OBSERVED"
    clock.t = start + timedelta(minutes=75)
    await p.tick()
    assert len([t for t in notifier.system if "Mac занят" in t]) == 1
    clock.t = start + timedelta(minutes=76)
    await p.tick()
    assert len([t for t in notifier.system if "Mac занят" in t]) == 2


async def test_user_active_keeps_reserve_approved(store, notifier, clock):
    confirm_mode(store)
    m = FakeMp(orders=[make_order("1")])
    c = FakeOneC()
    c.items["A1"] = UserActive("x")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "NEW"
    store.set_state("ozon", "1", "RESERVE_APPROVED")
    await p.tick()
    assert store.get_state("ozon", "1") == "RESERVE_APPROVED"
    assert [t for t in notifier.system if "не трогайте" not in t] == []
    assert notifier.alerts == [] and not store.is_paused()
    assert len(c.reads) == 2 and m.confirmed == []


async def test_resumes_after_pause_flip(store, notifier, clock):
    m = FakeMp(orders=[make_order("1")])
    c = FakeOneC()
    c.items["A1"] = UnexpectedScreen("x")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    c.items["A1"] = make_read()
    store.set_paused(False, "u")
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert c.connects == 2


async def test_read_uncertain_escalates_order(pipe, store, onec, notifier, mp):
    confirm_mode(store)
    onec.items["A1"] = ReadUncertain("blurry")
    await pipe.tick()
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert notifier.alerts[0][1].reasons == [("A1", Reason.READ_UNCERTAIN)]
    assert mp.confirmed == []


async def test_snapshot_attached_to_a_failed_read_reaches_the_alert(pipe, store, onec, notifier):
    confirm_mode(store)
    err = ReadUncertain("blurry")
    err.snapshot_path = "/snap/fail.png"
    onec.items["A1"] = err
    await pipe.tick()
    assert notifier.alerts[0][2] == ["/snap/fail.png"]


async def test_article_not_found_carries_reason(pipe, store, onec, notifier):
    confirm_mode(store)
    onec.items["A1"] = ArticleNotFound("none")
    await pipe.tick()
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert notifier.alerts[0][1].reasons == [("A1", Reason.ARTICLE_NOT_FOUND)]


async def test_unexpected_error_on_one_order_does_not_kill_loop(store, notifier, clock):
    """An unexpected error during a 1C read: that order goes to a person, the pass ends with
    one pause alert, the 1C window is let go; the robot itself keeps running."""
    confirm_mode(store)
    m = FakeMp(orders=[make_order("1", article="BAD"), make_order("2")])
    c = FakeOneC({"A1": make_read()})  # BAD -> KeyError
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert store.get_state("ozon", "2") == "NEW"
    assert notifier.alerts[0][0].order_id == "1"
    assert notifier.alerts[0][1].reasons == [("BAD", Reason.INTERNAL_ERROR)]
    assert notifier.alerts[0][4] is False
    assert store.is_paused()
    assert len(notifier.system) == 1 and "KeyError" in notifier.system[0]
    assert c.disconnects == 1
    store.set_paused(False, "u")
    await p.tick()                       # the loop is alive: the next pass handles order 2
    assert store.get_state("ozon", "2") == "CONFIRMED"


async def test_unexpected_1c_error_reads_nothing_more_in_the_pass(store, notifier, clock):
    m = FakeMp(orders=[make_order("1", article="BAD", deadline=at(20)),
                       make_order("2", deadline=at(21)), make_order("3", deadline=at(22))])
    c = FakeOneC({"A1": make_read(), "BAD": ValueError("numpy says no")})
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    assert c.reads == ["BAD"]
    assert [store.get_state("ozon", i) for i in "23"] == ["NEW", "NEW"]
    assert c.connects == 1 and c.disconnects == 1


async def test_unexpected_error_outside_1c_keeps_going(store, notifier, clock, onec):
    class Flaky(FakeMp):
        async def is_awaiting(self, order):
            if order.order_id == "1":
                raise RuntimeError("api glitch")
            return True
    m = Flaky(orders=[make_order("1", deadline=at(20)), make_order("2", deadline=at(21))])
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert store.get_state("ozon", "2") == "OBSERVED"
    assert not store.is_paused() and notifier.system == []


async def test_generic_error_alert_in_observe_mode_is_marked_observed(store, notifier, clock, onec):
    class Flaky(FakeMp):
        async def is_awaiting(self, order):
            raise RuntimeError("api glitch")
    m = Flaky(orders=[make_order("1")])
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await p.tick()
    assert notifier.alerts[0][1].reasons == [("A1", Reason.INTERNAL_ERROR)]
    assert notifier.alerts[0][4] is True


async def test_unexpected_1c_error_in_observe_mode_is_marked_observed(store, notifier, clock):
    m = FakeMp(orders=[make_order("1", article="BAD")])
    p = Pipeline(store, [m], FakeOneC({}), notifier, clock=clock)
    await p.tick()
    assert notifier.alerts[0][4] is True
    assert store.get_state("ozon", "1") == "OBSERVED"


async def test_hold_in_observe_mode_is_marked_observed(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1", deadline=at(20))])
    store.upsert_new(a.orders[0])
    a.fail_poll = True
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert notifier.alerts[0][1].reasons == [("A1", Reason.API_UNAVAILABLE)]
    assert notifier.alerts[0][4] is True


async def test_order_without_creation_time_goes_to_person_before_1c(store, onec, notifier, clock):
    o = make_order("1")
    o.created_at = None
    m = FakeMp(orders=[o])
    confirm_mode(store)
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await p.tick()
    assert onec.connects == 0 and onec.reads == []
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert notifier.alerts[0][1].reasons == [("A1", Reason.INVALID_ORDER)]
    assert notifier.alerts[0][4] is False
    assert m.confirmed == [] and not store.is_paused()


async def test_transient_overlay_waits_without_alert_or_pause(store, notifier, clock):
    m = FakeMp(orders=[make_order("1"), make_order("2")])
    c = FakeOneC()
    c.items["A1"] = TransientOverlay("banner")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    assert [store.get_state("ozon", i) for i in "12"] == ["NEW", "NEW"]
    assert _real(notifier.system) == [] and notifier.alerts == []
    assert not store.is_paused()
    assert len(c.reads) == 1 and c.disconnects == 1
    c.items["A1"] = make_read()
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"


async def test_transient_overlay_for_30_minutes_alerts_once(store, notifier, clock):
    m = FakeMp(orders=[make_order("1")])
    c = FakeOneC()
    c.items["A1"] = TransientOverlay("banner")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    start = clock.t
    for minutes in (0, 15, 29):
        clock.t = start + timedelta(minutes=minutes)
        await p.tick()
    assert _real(notifier.system) == []
    clock.t = start + timedelta(minutes=30)
    await p.tick()
    clock.t = start + timedelta(minutes=40)
    await p.tick()
    assert len(_real(notifier.system)) == 1 and "Не беспокоить" in _real(notifier.system)[0]
    assert not store.is_paused() and store.get_state("ozon", "1") == "NEW"


async def test_recover_confirming_advanced_marks_confirmed(pipe, store, mp):
    store.upsert_new(make_order())
    store.set_state("ozon", "1", "CONFIRMING")
    mp.ready["1"] = True
    await pipe.recover()
    assert store.get_state("ozon", "1") == "CONFIRMED"


async def test_recover_confirming_not_advanced_escalates_no_second_call(pipe, store, mp, notifier):
    store.upsert_new(make_order())
    store.set_state("ozon", "1", "CONFIRMING")
    await pipe.recover()
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert ("confirm", "1") not in mp.calls
    assert notifier.alerts[0][0].order_id == "1"
    assert notifier.alerts[0][1].reasons == [("A1", Reason.CONFIRM_UNKNOWN)]


async def test_confirm_error_escalates_no_retry(pipe, store, mp, notifier):
    confirm_mode(store)
    mp.confirm_error = RuntimeError("timeout")
    await pipe.tick()
    await pipe.tick()
    assert mp.calls.count(("confirm", "1")) == 1
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert [a[1].reasons for a in notifier.alerts] == [[("A1", Reason.CONFIRM_UNKNOWN)]]


async def test_confirm_error_but_order_is_ready_marks_confirmed(pipe, store, mp, notifier):
    confirm_mode(store)
    mp.confirm_error = RuntimeError("timeout")
    mp.ready["1"] = True
    await pipe.tick()
    assert store.get_state("ozon", "1") == "CONFIRMED"
    assert notifier.system == []


async def test_uses_settings_at_order_time(store, notifier, onec, clock):
    # order created while a 10% brand discount applied; the discount was switched off later
    clock.t = at(8)
    store.change_setting("ozon", "brand_discount", {"brand": "Nova", "percent": 10}, "u")
    o = make_order(created=at(9), paid="900")
    o.lines[0].brand = "Nova"
    clock.t = at(9, 30)
    store.change_setting("ozon", "brand_discount", {"brand": "Nova", "percent": None}, "u")
    clock.t = at(10)
    onec.items["A1"] = make_read(epr="1000", ppr="800")
    m = FakeMp(orders=[o])
    confirm_mode(store)
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await p.tick()
    # with discount: floor = max(800, 1000*0.9-50) = 850 -> 900 passes
    assert m.confirmed == ["1"]


async def test_ready_quantities_summed_across_marketplaces(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1")])
    b = FakeMp("ym", [])
    a.ready_qty = {("A1", "СПБ"): 5}
    b.ready_qty = {("A1", "СПБ"): 5}
    onec.items["A1"] = make_read(on_hand=10)
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [a, b], onec, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "ESCALATED"  # 10 - 10 ready = 0 free


async def test_awaiting_order_is_not_asked_whether_it_is_ready(store, onec, notifier, clock):
    """The ready list is fetched before is_awaiting; an order still awaiting packaging at
    the later call cannot be in it, so no own quantity is taken out and no is_ready call
    is made on the normal path."""
    a = FakeMp("ozon", [make_order("1", qty=1)])
    a.ready_qty = {("A1", "СПБ"): 1}     # another order already assembled
    onec.items["A1"] = make_read(on_hand=1)
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    await p.tick()
    assert ("is_ready", "1") not in a.calls
    assert store.get_decision("ozon", "1").action == Action.ESCALATE


async def test_confirmed_order_counts_as_ready_for_next_in_same_tick(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1", deadline=at(12, d=3)), make_order("2", deadline=at(13, d=3))])
    onec.items["A1"] = make_read(on_hand=1)
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    await p.tick()
    assert a.confirmed == ["1"]
    assert store.get_state("ozon", "2") == "ESCALATED"


async def test_urgent_when_deadline_within_24h(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1", deadline=at(20)), make_order("2", deadline=at(20, d=5))])
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    await p.tick()
    urgent = {al[0].order_id: al[3] for al in notifier.alerts}
    assert urgent == {"1": True, "2": False}


async def test_processed_in_deadline_order(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("late", deadline=at(20, d=5)), make_order("soon", deadline=at(20))])
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    await p.tick()
    assert [al[0].order_id for al in notifier.alerts] == ["soon", "late"]


async def test_disconnects_when_queue_empty(store, onec, notifier, clock, mp):
    p = Pipeline(store, [mp], onec, notifier, clock=clock)
    await p.tick()
    assert (onec.connects, onec.disconnects) == (1, 1)
    await p.tick()  # nothing new
    assert onec.connects == 1


async def test_no_connect_when_nothing_needs_1c(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1")])
    a.awaiting["1"] = False
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    await p.tick()
    assert onec.connects == 0


async def test_pause_mid_pass_finishes_current_order_only(store, notifier, clock):
    a = FakeMp("ozon", [make_order("1", deadline=at(20)), make_order("2", deadline=at(21))])

    c = FakeOneC()

    class PausingNotifier(FakeNotifier):
        async def order_alert(self, *args, **kw):
            store.set_paused(True, "u")
            await super().order_alert(*args, **kw)
    notifier = PausingNotifier()
    p = Pipeline(store, [a], c, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert store.get_state("ozon", "2") == "NEW"
    assert c.disconnects == 1


async def test_poll_failures_alert_after_3(store, onec, notifier, clock):
    a = FakeMp("ozon", [])
    a.fail_poll = True
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    for _ in range(5):
        await p.tick()
    assert len(notifier.system) == 1


async def test_poll_failure_counter_resets_on_success(store, onec, notifier, clock):
    a = FakeMp("ozon", [])
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    for ok in (False, False, True, False, False):
        a.fail_poll = not ok
        await p.tick()
    assert notifier.system == []


async def test_one_marketplace_failure_does_not_stop_other(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1")])
    a.fail_poll = True
    b = FakeMp("ym", [make_order("2", mp="ym")])
    p = Pipeline(store, [a, b], onec, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ym", "2") == "OBSERVED"


async def test_decisions_and_reads_go_to_journal(pipe, store):
    await pipe.tick()
    ev = [e for e in store.events("ozon", "1") if e["event"] == "decision"]
    assert len(ev) == 1
    p = ev[0]["payload"]
    assert p["action"] == "confirm"
    assert p["reads"]["A1"]["epr"] == "1000"
    assert p["reads"]["A1"]["snapshot_path"] == "/snap/A1.png"


def test_purge_snapshots_removes_only_old(tmp_path):
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    t = time.time() - 91 * 86400
    os.utime(old, (t, t))
    removed = purge_snapshots(tmp_path, days=90)
    assert removed == 1
    assert not old.exists() and new.exists()


async def test_no_confirm_when_other_marketplace_ready_unknown(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1")])
    b = FakeMp("ym", [])
    b.fail_poll = True
    onec.items["A1"] = make_read(on_hand=1)   # enough only if ym's ready list is ignored
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [a, b], onec, notifier, clock=clock)
    await p.tick()
    assert a.confirmed == []
    assert store.get_state("ozon", "1") == "NEW"
    b.fail_poll = False
    await p.tick()
    assert a.confirmed == ["1"]


async def test_urgent_order_escalated_when_ready_unknown(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1", deadline=at(20))])
    b = FakeMp("ym", [])
    b.fail_poll = True
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [a, b], onec, notifier, clock=clock)
    await p.tick()
    assert a.confirmed == []
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert notifier.alerts[0][1].reasons == [("A1", Reason.READY_UNKNOWN)]
    assert notifier.alerts[0][3] is True


async def test_observe_still_records_when_other_marketplace_failing(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1")])
    b = FakeMp("ym", [])
    b.fail_poll = True
    p = Pipeline(store, [a, b], onec, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"


async def test_urgent_order_of_failing_marketplace_is_escalated(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1", deadline=at(20))])
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    a.orders = [a.orders[0]]
    # stored while healthy, then the API starts failing
    store.upsert_new(a.orders[0])
    a.fail_poll = True
    store.set_mode("ozon", "confirm", "t")
    await p.tick()
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert notifier.alerts[0][1].reasons == [("A1", Reason.API_UNAVAILABLE)]


async def test_failed_connect_disconnects(store, notifier, clock, mp):
    c = FakeOneC()
    c.connect_error = SessionLost("no rdp")
    p = Pipeline(store, [mp], c, notifier, clock=clock)
    await p.tick()
    assert c.disconnects == 1


async def test_paused_just_before_connect_no_connect(store, onec, notifier, clock):
    class Pausing(FakeMp):
        async def is_awaiting(self, order):
            store.set_paused(True, "u")
            return True
    m = Pausing(orders=[make_order("1")])
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await p.tick()
    assert onec.connects == 0
    assert store.get_state("ozon", "1") == "NEW"


async def test_mode_switched_to_observe_before_confirm(store, onec, notifier, clock):
    class Switch(FakeMp):
        n = 0

        async def is_awaiting(self, order):
            self.n += 1
            if self.n == 2:
                store.set_mode("ozon", "observe", "u")
            return True
    m = Switch(orders=[make_order("1")])
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await p.tick()
    assert m.confirmed == []
    assert store.get_state("ozon", "1") == "OBSERVED"


async def test_poll_failure_alert_repeats_hourly(store, onec, notifier, clock):
    a = FakeMp("ozon", [])
    a.fail_poll = True
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    for _ in range(4):
        await p.tick()
    assert len(notifier.system) == 1
    clock.t = clock.t + timedelta(minutes=61)
    await p.tick()
    assert len(notifier.system) == 2


async def test_later_failure_after_confirmed_keeps_confirmed(store, onec, notifier, clock, mp):
    store.set_mode("ozon", "confirm", "t")
    real = store.log

    def flaky(m, o, event, payload):
        if event == "confirmed":
            raise RuntimeError("disk full")
        return real(m, o, event, payload)
    store.log = flaky
    p = Pipeline(store, [mp], onec, notifier, clock=clock)
    await p.tick()
    assert mp.confirmed == ["1"]
    assert store.get_state("ozon", "1") == "CONFIRMED"


async def test_mode_flip_to_confirm_during_read_with_stale_ready_no_confirm(store, notifier, clock):
    a = FakeMp("ozon", [make_order("1")])
    b = FakeMp("ym", [])
    b.fail_poll = True

    # flip the mode from the main thread, in settings_at, which runs after the 1C read
    real = store.settings_at

    def flip(mp, when):
        store.set_mode("ozon", "confirm", "u")
        return real(mp, when)
    store.settings_at = flip
    p = Pipeline(store, [a, b], FakeOneC(), notifier, clock=clock)
    await p.tick()
    assert a.confirmed == []
    assert store.get_state("ozon", "1") == "NEW"


async def test_reserve_approval_with_other_marketplace_failing_no_confirm(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1")])
    b = FakeMp("ym", [])
    onec.items["A1"] = make_read(on_hand=1, reserve=1)
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [a, b], onec, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "AWAITING_RESERVE"
    b.fail_poll = True
    await p.resolve_reserve("ozon", "1", True, "boss")
    assert a.confirmed == []
    assert store.get_state("ozon", "1") == "RESERVE_APPROVED"


async def test_urgent_approved_held_in_observe_mode_says_approval_not_acted(store, onec, notifier, clock):
    a = FakeMp("ozon", [make_order("1", deadline=at(20))])
    store.upsert_new(a.orders[0])
    store.set_state("ozon", "1", "RESERVE_APPROVED")
    a.fail_poll = True
    p = Pipeline(store, [a], onec, notifier, clock=clock)
    await p.tick()
    assert any("разрешение по резерву записано" in t for t in notifier.system)


async def test_alert_failure_does_not_stop_pass(store, onec, clock):
    class Boom(FakeNotifier):
        async def order_alert(self, *a, **k):
            raise RuntimeError("telegram down")
    m = FakeMp(orders=[make_order("1"), make_order("2")])
    p = Pipeline(store, [m], onec, Boom(), clock=clock)
    await p.tick()
    assert [store.get_state("ozon", i) for i in "12"] == ["OBSERVED", "OBSERVED"]


async def test_resolve_reserve_message_is_human_text(pipe, store, onec):
    await reserve_setup(pipe, store, onec)
    msg = await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert "CONFIRMED" not in msg and "подтверждён" in msg


async def test_approved_handled_externally_before_confirm_notifies(store, onec, notifier, clock):
    class Flip(FakeMp):
        n = 0

        async def is_awaiting(self, order):
            self.n += 1
            return self.n != 2
    m = Flip(orders=[make_order("1")])
    onec.items["A1"] = make_read(on_hand=1, reserve=1)
    store.set_mode("ozon", "confirm", "t")
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "AWAITING_RESERVE"
    m.n = 0   # in resolve: 1st call (start) true, 2nd (right before confirm) false
    await p.resolve_reserve("ozon", "1", True, "boss")
    assert m.confirmed == []
    assert store.get_state("ozon", "1") == "HANDLED_EXTERNALLY"
    assert len(notifier.system) == 1


async def test_shutdown_during_read_sends_no_alert_and_does_not_pause(pipe, store, onec, notifier):
    confirm_mode(store)
    onec.items["A1"] = ShuttingDown("stop")
    await pipe.tick()
    assert not store.is_paused()
    assert notifier.system == [] and notifier.alerts == []
    assert store.get_state("ozon", "1") == "NEW"
    assert onec.disconnects == 1


async def test_shutdown_during_connect_is_quiet(pipe, store, onec, notifier):
    confirm_mode(store)
    onec.connect_error = ShuttingDown("stop")
    await pipe.tick()
    assert not store.is_paused() and notifier.system == []
    assert store.get_state("ozon", "1") == "NEW"


async def test_terminal_alert_has_class_name_not_exception_text(pipe, store, onec, notifier):
    confirm_mode(store)
    onec.items["A1"] = UnexpectedScreen("secret screen detail")
    await pipe.tick()
    assert store.is_paused()
    text = notifier.system[0]
    assert "UnexpectedScreen" in text and "secret screen detail" not in text
    store.set_paused(False, "t")
    onec.connect_error = RuntimeError("secret connect detail")
    await pipe.tick()
    assert "RuntimeError" in notifier.system[-1] and "secret connect detail" not in notifier.system[-1]


# ------------------------------------------------------------- alert outbox
class Flaky(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.down = True
        self.tries = 0

    def _gate(self):
        self.tries += 1
        if self.down:
            raise RuntimeError("telegram down")

    async def order_alert(self, *a, **k):
        self._gate()
        await super().order_alert(*a, **k)

    async def reserve_request(self, *a, **k):
        self._gate()
        await super().reserve_request(*a, **k)

    async def system_alert(self, text):
        self._gate()
        await super().system_alert(text)


async def test_failed_order_alert_is_kept_and_sent_on_the_next_tick(store, onec, clock):
    n = Flaky()
    m = FakeMp(orders=[make_order("1")])
    onec.items["A1"] = make_read(on_hand=0)
    confirm_mode(store)
    p = Pipeline(store, [m], onec, n, clock=clock)
    await p.tick()
    assert n.alerts == [] and len(store.outbox()) == 1
    assert [e["event"] for e in store.events() if e["event"] == "alert_failed"] == ["alert_failed"]
    n.down = False
    await p.tick()
    assert len(n.alerts) == 1
    order, decision, snap, urgent, observed = n.alerts[0]
    assert order.order_id == "1" and decision.reasons == [("A1", Reason.NO_STOCK)]
    assert snap == ["/snap/A1.png"] and observed is False
    assert store.outbox() == []
    await p.tick()
    assert len(n.alerts) == 1                          # dropped after success


async def test_outbox_tries_each_alert_at_most_once_per_tick(store, onec, clock):
    n = Flaky()
    m = FakeMp(orders=[make_order("1"), make_order("2")])
    p = Pipeline(store, [m], onec, n, clock=clock)
    await p.tick()                                      # two observe alerts fail
    assert len(store.outbox()) == 2
    n.tries = 0
    await p.tick()                                      # still down: one try, then wait
    assert n.tries == 1 and len(store.outbox()) == 2
    n.down = False
    await p.tick()
    assert [a[0].order_id for a in n.alerts] == ["1", "2"] and store.outbox() == []


async def test_failed_system_and_reserve_alerts_are_resent(store, onec, clock):
    n = Flaky()
    m = FakeMp(orders=[make_order("1")])
    onec.items["A1"] = make_read(on_hand=1, reserve=1)
    confirm_mode(store)
    p = Pipeline(store, [m], onec, n, clock=clock)
    await p.tick()
    assert store.get_state("ozon", "1") == "AWAITING_RESERVE"
    await p._system("hello")
    n.down = False
    await p.tick()
    assert len(n.reserve) == 1 and n.reserve[0][0].order_id == "1"
    assert n.system == ["hello"]
    assert store.outbox() == []


async def test_stale_reserve_request_is_dropped_not_sent(store, onec, clock):
    n = Flaky()
    m = FakeMp(orders=[make_order("1")])
    onec.items["A1"] = make_read(on_hand=1, reserve=1)
    confirm_mode(store)
    p = Pipeline(store, [m], onec, n, clock=clock)
    await p.tick()
    store.set_state("ozon", "1", "REJECTED_BY_HUMAN")
    n.down = False
    await p.tick()
    assert n.reserve == [] and store.outbox() == []


async def test_unexpected_1c_error_ends_the_pass_even_if_pausing_fails(store, notifier, clock):
    m = FakeMp(orders=[make_order("1", article="BAD", deadline=at(20)),
                       make_order("2", deadline=at(21))])
    c = FakeOneC({"A1": make_read()})
    real = store.set_paused

    def broken(paused, user):
        if user == "system":
            raise RuntimeError("disk full")
        return real(paused, user)
    store.set_paused = broken
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    assert c.reads == ["BAD"] and c.connects == 1 and c.disconnects == 1
    assert store.get_state("ozon", "2") == "NEW"


async def test_owner_is_asked_not_to_touch_the_mac_once_then_told_when_done(store, notifier, clock):
    """Say right away that the robot waits for the Mac, and when the
    check is over. Once per wait, not every tick."""
    m = FakeMp(orders=[make_order("1")])
    c = FakeOneC()
    c.items["A1"] = UserActive("x")
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    await p.tick()
    assert [t for t in notifier.system if "не трогайте" in t] and len(notifier.system) == 1
    c.items["A1"] = make_read()
    await p.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert len([t for t in notifier.system if "закончена" in t]) == 1


async def test_multi_qty_switched_on_later_applies_to_an_older_order(store, notifier, clock):
    """An operator switched on /multiqty after the order arrived and re-ran it: the current
    answer counts, unlike discounts, which stay as at order time."""
    order = make_order("1")
    order.lines[0].qty = 2
    order.lines[0].paid_total = order.lines[0].paid_total * 2
    store.change_setting("ozon", "multi_qty_verified", True, "admin")
    m = FakeMp(orders=[order])
    c = FakeOneC()
    c.items["A1"] = make_read()
    p = Pipeline(store, [m], c, notifier, clock=clock)
    await p.tick()
    d = store.get_decision("ozon", "1")
    assert all(r != Reason.QTY_UNVERIFIED for _, r in d.reasons)


async def test_fixed_price_list_reaches_the_rules(store, notifier, clock):
    # EPR 1000 / PPR 800 would refuse 760; the fixed price 900 − 150 lets it through
    m = FakeMp(orders=[make_order("1", paid="760")])
    c = FakeOneC()
    c.items["A1"] = make_read()
    p = Pipeline(store, [m], c, notifier, clock=clock, fixed_price={"A1": Decimal("900")})
    await p.tick()
    d = store.get_decision("ozon", "1")
    assert d.reasons == [] and store.get_state("ozon", "1") == "OBSERVED"


async def approved_setup(pipe, store, onec):
    """RESERVE_APPROVED and unpaused, waiting for the next tick."""
    await reserve_setup(pipe, store, onec)
    store.set_paused(True, "u")
    await pipe.resolve_reserve("ozon", "1", True, "boss")
    assert store.get_state("ozon", "1") == "RESERVE_APPROVED"
    store.set_paused(False, "u")


async def test_person_cancels_during_1c_read_robot_keeps_off(pipe, store, mp, onec, notifier):
    import threading
    await approved_setup(pipe, store, onec)
    started, go = threading.Event(), threading.Event()

    class SlowOneC(FakeOneC):
        def read(self, article):
            started.set()
            go.wait(5)
            return make_read(on_hand=0)     # the decision escalates
    pipe._onec = SlowOneC()
    alerts_before = len(notifier.alerts)
    tick = asyncio.create_task(pipe.tick())
    while not started.is_set():
        await asyncio.sleep(0.01)
    assert store.set_state_if("ozon", "1", {"RESERVE_APPROVED"}, "CANCELLED_BY_HUMAN")
    go.set()
    await asyncio.wait_for(tick, 5)
    assert store.get_state("ozon", "1") == "CANCELLED_BY_HUMAN"
    assert len(notifier.alerts) == alerts_before
    assert mp.confirmed == []
    assert [e["event"] for e in store.events("ozon", "1")].count("skipped_after_human") == 1


async def test_person_cancels_then_not_awaiting_keeps_human_state(store, onec, notifier, clock):
    class CancelledMp(FakeMp):
        async def is_awaiting(self, order):
            if store.get_state("ozon", "1") == "RESERVE_APPROVED":
                store.set_state_if("ozon", "1", {"RESERVE_APPROVED"}, "CANCELLED_BY_HUMAN")
                return False
            return True
    m = CancelledMp(orders=[make_order()])
    p = Pipeline(store, [m], onec, notifier, clock=clock)
    await approved_setup(p, store, onec)
    sys_before = len(notifier.system)
    await p.tick()
    assert store.get_state("ozon", "1") == "CANCELLED_BY_HUMAN"
    assert len(notifier.system) == sys_before and m.confirmed == []
    assert [e["event"] for e in store.events("ozon", "1")].count("skipped_after_human") == 1


@pytest.mark.parametrize("state", ["CONFIRMED", "CANCELLED_BY_HUMAN", "HANDLED_EXTERNALLY"])
async def test_give_to_person_and_hold_skip_human_final_state(pipe, store, notifier, clock, state):
    store.upsert_new(make_order("9", deadline=at(10, 30)))   # urgent: _hold would escalate
    store.set_state("ozon", "9", state)
    order = store.get_order("ozon", "9")
    await pipe._give_to_person(order, Reason.INTERNAL_ERROR)
    await pipe._hold(order, Reason.READY_UNKNOWN, True, True)
    assert store.get_state("ozon", "9") == state
    assert notifier.alerts == [] and notifier.system == []


# ------------------------------------------------- final review fixes
async def test_robot_confirm_marks_stored_messages_collected(pipe, store, mp, notifier, clock):
    from orderbot.alerts import result_line
    confirm_mode(store)
    await pipe.tick()
    assert store.get_state("ozon", "1") == "CONFIRMED"
    assert notifier.done == [("ozon", "1", result_line("collected_by_robot", "", clock()))]
    assert "cancel" not in notifier.done[0][2].lower()


async def test_order_done_failure_is_logged_not_raised(store, mp, onec, clock):
    class Boom(FakeNotifier):
        async def order_done(self, *a, **k):
            raise RuntimeError("telegram down")
    n = Boom()
    p = Pipeline(store, [mp], onec, n, clock=clock)
    confirm_mode(store)
    await p.tick()
    assert store.get_state("ozon", "1") == "CONFIRMED" and mp.confirmed == ["1"]


async def test_order_done_missing_on_notifier_is_harmless(store, mp, onec, clock):
    class Old:
        async def order_alert(self, *a, **k):
            pass

        async def reserve_request(self, *a, **k):
            pass

        async def system_alert(self, text):
            pass
    p = Pipeline(store, [mp], onec, Old(), clock=clock)
    confirm_mode(store)
    await p.tick()
    assert store.get_state("ozon", "1") == "CONFIRMED"


async def test_approved_order_in_observe_mode_gets_collect_buttons(pipe, store, mp, onec,
                                                                    notifier):
    await approved_setup(pipe, store, onec)
    store.set_mode("ozon", "observe", "t")
    alerts_before, sys_before = len(notifier.alerts), len(notifier.system)
    await pipe.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert store.get_decision("ozon", "1").action == Action.ASK_RESERVE
    assert notifier.refreshed == [("ozon", "1")]
    assert len(notifier.alerts) == alerts_before          # the stored message got its buttons
    assert len(_real(notifier.system)) == sys_before
    assert mp.confirmed == []
    ev = [e for e in store.events("ozon", "1") if e["event"] == "approved_observed"]
    assert len(ev) == 1


async def test_approved_order_in_observe_mode_without_message_is_alerted(pipe, store, mp, onec,
                                                                         notifier):
    await approved_setup(pipe, store, onec)
    store.set_mode("ozon", "observe", "t")
    notifier.stored = 0
    alerts_before = len(notifier.alerts)
    await pipe.tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert len(notifier.alerts) == alerts_before + 1
    order, decision, _, _, observed = notifier.alerts[-1]
    assert order.order_id == "1" and observed is True
    assert decision.action == Action.ASK_RESERVE


@pytest.mark.parametrize("state", ["CONFIRMED", "CANCELLED_BY_HUMAN", "HANDLED_EXTERNALLY"])
async def test_stale_order_alert_in_outbox_is_dropped_not_sent(store, onec, clock, state):
    n = Flaky()
    m = FakeMp(orders=[make_order("1")])
    p = Pipeline(store, [m], onec, n, clock=clock)
    await p.tick()                                     # the observe alert fails
    assert len(store.outbox()) == 1
    store.set_state("ozon", "1", state)
    n.down = False
    await p.tick()
    assert n.alerts == [] and store.outbox() == []


def test_state_text_names_cancel_by_person():
    from orderbot.pipeline import _STATE_TEXT
    assert _STATE_TEXT["CANCELLED_BY_HUMAN"] == "отменён человеком"


# ------------------------------------------------- product photos
class ReadsNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.reads = []

    async def order_alert(self, order, decision, snapshot, urgent, observed, reads=None):
        self.reads.append(reads)
        await super().order_alert(order, decision, snapshot, urgent, observed, reads)


class FakePhotos:
    def __init__(self, result="/pp/A1.jpg", error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.calls = []
        self.purged = 0

    async def get(self, marketplace, article, url_fn):
        self.calls.append((marketplace, article, await url_fn(article)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result

    def purge(self):
        self.purged += 1
        return 0


class PhotoMp(FakeMp):
    async def photo_url(self, article):
        return f"https://cdn/{article}.jpg"


async def test_marketplace_photo_goes_with_the_read(store, onec, clock):
    n, photos = ReadsNotifier(), FakePhotos()
    p = Pipeline(store, [PhotoMp(orders=[make_order()])], onec, n, clock=clock,
                 product_photos=photos)
    await p.tick()
    assert photos.calls == [("ozon", "A1", "https://cdn/A1.jpg")]
    assert n.reads[0]["A1"].photo_mp == "/pp/A1.jpg"
    assert n.reads[0]["A1"].snapshot_path == "/snap/A1.png"
    ev = [e for e in store.events() if e["event"] == "decision"]
    assert ev[0]["payload"]["reads"]["A1"]["photo_mp"] == "/pp/A1.jpg"


@pytest.mark.parametrize("photos", [FakePhotos(result=None), FakePhotos(error=RuntimeError("x"))])
async def test_no_marketplace_photo_changes_nothing_else(store, onec, clock, photos):
    n = ReadsNotifier()
    p = Pipeline(store, [PhotoMp(orders=[make_order()])], onec, n, clock=clock,
                 product_photos=photos)
    await p.tick()
    assert n.reads[0]["A1"].photo_mp is None
    assert n.alerts[0][1].action == Action.CONFIRM and n.alerts[0][2] == ["/snap/A1.png"]


async def test_slow_marketplace_photo_is_not_waited_for(store, onec, clock, monkeypatch):
    from orderbot import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "PHOTOS_WAIT_S", 0.01)
    n = ReadsNotifier()
    p = Pipeline(store, [PhotoMp(orders=[make_order()])], onec, n, clock=clock,
                 product_photos=FakePhotos(delay=5))
    await asyncio.wait_for(p.tick(), 2)
    assert n.reads[0]["A1"].photo_mp is None and len(n.alerts) == 1


async def test_failed_reads_and_adapters_without_photos_ask_nothing(store, clock):
    n, photos = ReadsNotifier(), FakePhotos()
    onec = FakeOneC({"A1": ReadUncertain("x")})
    p = Pipeline(store, [PhotoMp(orders=[make_order()])], onec, n, clock=clock,
                 product_photos=photos)
    await p.tick()
    assert photos.calls == []
    p2 = Pipeline(store, [FakeMp(orders=[make_order("2")])], FakeOneC(), n, clock=clock,
                  product_photos=photos)
    await p2.tick()
    assert photos.calls == []


async def test_photos_survive_the_outbox(store, clock):
    n = Flaky()
    n.reads = []

    async def order_alert(order, decision, snapshot, urgent, observed, reads=None):
        n._gate()
        n.reads.append(reads)
    n.order_alert = order_alert
    onec = FakeOneC({"A1": ItemRead("A1", {"СПБ": StockRow("СПБ", 0, 0)}, Decimal(1000),
                                    Decimal(800), "/snap/A1.png", photo_1c="/snap/A1-1c.png")})
    p = Pipeline(store, [PhotoMp(orders=[make_order()])], onec, n, clock=clock,
                 product_photos=FakePhotos())
    await p.tick()
    n.down = False
    await p.tick()
    r = n.reads[-1]["A1"]
    assert (r.photo_1c, r.photo_mp) == ("/snap/A1-1c.png", "/pp/A1.jpg")


async def test_photo_cache_is_purged_with_the_snapshots(store, mp, onec, notifier, clock, tmp_path):
    photos = FakePhotos()
    p = Pipeline(store, [mp], onec, notifier, clock=clock, snapshots_dir=tmp_path,
                 product_photos=photos)
    await p.tick()
    assert photos.purged == 1


def test_purge_snapshots_removes_old_pictures_and_sheets(tmp_path):
    names = ["1-0-A1.png", "1-1-A1-1c.png", "1-0-A1-all.png", "1-0-A1-all.jpg"]
    t = time.time() - 91 * 86400
    for name in names:
        (tmp_path / name).write_bytes(b"x")
        os.utime(tmp_path / name, (t, t))
    assert purge_snapshots(tmp_path, days=90) == len(names)
    assert list(tmp_path.iterdir()) == []


IMAGE_HINT = "1С осталась на вкладке «Изображение» — переключите на «Остатки»"


async def test_terminal_alert_tells_to_switch_back_from_the_image_tab(pipe, store, onec,
                                                                      notifier):
    e = UnexpectedScreen("tables_tail")
    e.image_tab = True
    onec.items["A1"] = e
    await pipe.tick()
    assert store.is_paused() and IMAGE_HINT in notifier.system[-1]
    store.set_paused(False, "t")
    e2 = UnexpectedScreen("x")
    e2.image_tab = True
    onec.connect_error = e2
    await pipe.tick()
    assert IMAGE_HINT in notifier.system[-1]


async def test_terminal_alert_without_the_image_tab_has_no_hint(pipe, store, onec, notifier):
    onec.items["A1"] = UnexpectedScreen("x")
    await pipe.tick()
    assert "Изображение" not in notifier.system[-1]



def two_article_order():
    return Order("ozon", "1", [OrderLine("A1", 1, "СПБ", Decimal("2000"), None),
                               OrderLine("A2", 1, "СПБ", Decimal("2000"), None)],
                 at(9), at(23, d=5), "awaiting_packaging")


class DelayPhotos(FakePhotos):
    def __init__(self, delays):
        super().__init__()
        self.delays = delays

    async def get(self, marketplace, article, url_fn):
        self.calls.append(article)
        await asyncio.sleep(self.delays[article])
        return f"/pp/{article}.jpg"


async def test_photos_of_an_order_are_fetched_together_under_one_cap(store, clock,
                                                                     monkeypatch):
    """Two articles of 0.3 s each fit a 0.5 s cap only when fetched at the same time."""
    from orderbot import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "PHOTOS_WAIT_S", 0.5)
    n = ReadsNotifier()
    onec = FakeOneC({"A1": make_read("A1"), "A2": make_read("A2")})
    p = Pipeline(store, [PhotoMp(orders=[two_article_order()])], onec, n, clock=clock,
                 product_photos=DelayPhotos({"A1": 0.3, "A2": 0.3}))
    await p.tick()
    assert (n.reads[0]["A1"].photo_mp, n.reads[0]["A2"].photo_mp) == ("/pp/A1.jpg", "/pp/A2.jpg")


async def test_the_cap_is_for_the_whole_order_and_keeps_what_came_in_time(store, clock,
                                                                          monkeypatch):
    from orderbot import pipeline as pipeline_mod
    assert pipeline_mod.PHOTOS_WAIT_S == 12.0
    monkeypatch.setattr(pipeline_mod, "PHOTOS_WAIT_S", 0.2)
    n = ReadsNotifier()
    onec = FakeOneC({"A1": make_read("A1"), "A2": make_read("A2")})
    p = Pipeline(store, [PhotoMp(orders=[two_article_order()])], onec, n, clock=clock,
                 product_photos=DelayPhotos({"A1": 0.01, "A2": 30}))
    started = time.monotonic()
    await asyncio.wait_for(p.tick(), 5)
    assert time.monotonic() - started < 1.5          # dispatched within the cap
    assert n.reads[0]["A1"].photo_mp == "/pp/A1.jpg" and n.reads[0]["A2"].photo_mp is None
    assert len(n.alerts) == 1


# ------------------------------------------------- stock sync trigger
async def test_robot_confirm_triggers_stock_sync_once(store, mp, onec, notifier, clock):
    seen = []
    p = Pipeline(store, [mp], onec, notifier, clock=clock, on_confirmed=seen.append)
    confirm_mode(store)
    await p.tick()
    await p.tick()
    assert [(o.marketplace, o.order_id) for o in seen] == [("ozon", "1")]


async def test_no_stock_sync_when_not_confirmed(store, mp, onec, notifier, clock):
    seen = []
    p = Pipeline(store, [mp], onec, notifier, clock=clock, on_confirmed=seen.append)
    await p.tick()                       # observe mode
    confirm_mode(store)
    mp.confirm_error = RuntimeError("timeout")
    await p.tick()
    assert seen == []


async def test_stock_sync_failure_does_not_touch_confirm(store, mp, onec, notifier, clock):
    def boom(order):
        raise RuntimeError("sync broke")
    p = Pipeline(store, [mp], onec, notifier, clock=clock, on_confirmed=boom)
    confirm_mode(store)
    await p.tick()
    assert store.get_state("ozon", "1") == "CONFIRMED" and mp.confirmed == ["1"]


async def test_recover_confirmed_triggers_stock_sync(store, mp, onec, notifier, clock):
    seen = []
    p = Pipeline(store, [mp], onec, notifier, clock=clock, on_confirmed=seen.append)
    store.upsert_new(make_order())
    store.set_state("ozon", "1", "CONFIRMING")
    mp.ready["1"] = True
    await p.recover()
    assert [o.order_id for o in seen] == ["1"]


# ------------------------------------------------ health messages
async def test_1c_down_then_up_messages(store, notifier, clock, mp):
    from orderbot.screen.reader import UnexpectedScreen
    c = FakeOneC()
    c.connect_error = UnexpectedScreen("anchor menu not found")
    p = Pipeline(store, [mp], c, notifier, clock=clock)
    await p.tick()
    [down] = notifier.system
    assert down.startswith("🔴 <b>1С не работает — проверка заказов на паузе</b>\n"
                           "На экране не окно 1С: сессия Remote Desktop отключилась")
    assert "Что сделать: Откройте окно «1c» в Remote Desktop" in down
    c.connect_error = None
    store.set_paused(False, "u")
    await p.tick()
    assert notifier.system[-1].startswith("🟢 <b>1С снова работает</b>\nПростой ")


def test_health_texts():
    from datetime import timedelta

    from orderbot.pipeline import api_down_text, api_up_text
    assert api_down_text("Маркет · Бета", "HTTP 500", timedelta(minutes=7)) == (
        "🔴 <b>Маркет · Бета — API не отвечает</b>\n"
        "HTTP 500 · уже 7 мин\n"
        "Новые заказы этой площадки не проверяются")
    assert "API всё ещё не отвечает" in api_down_text("a", "b", timedelta(hours=2), again=True)
    assert "уже 2 ч" in api_down_text("a", "b", timedelta(hours=2))
    assert api_up_text("WB · Альфа", timedelta(minutes=75)) == (
        "🟢 <b>WB · Альфа — API снова отвечает</b>\nНе отвечал 1 ч 15 мин, проверка продолжена")
