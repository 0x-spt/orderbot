import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from orderbot.domain import Order, OrderLine
from orderbot.human_actions import ActionResult, HumanActions
from orderbot.pipeline import Pipeline
from orderbot.store import Store

UTC = UTC


def make_order(oid="1", mp="ozon"):
    return Order(mp, oid, [OrderLine("A1", 1, "СПБ", Decimal("2000"), None)],
                 datetime(2026, 11, 2, 9, tzinfo=UTC), datetime(2026, 11, 5, 23, tzinfo=UTC),
                 "awaiting_packaging")


class GatedMp:
    """Fake marketplace; when `gate` is set, confirm/cancel_by_human wait for it."""
    name = "ozon"

    def __init__(self):
        self.awaiting = True
        self.confirms = 0
        self.human_drops = 0
        self.error = None
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def is_awaiting(self, order):
        return self.awaiting

    async def _work(self):
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error

    async def confirm(self, order):
        self.confirms += 1
        await self._work()

    async def cancel_by_human(self, order):
        self.human_drops += 1
        await self._work()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.upsert_new(make_order())
    s.set_state("ozon", "1", "OBSERVED")
    return s


@pytest.fixture
def mp():
    return GatedMp()


@pytest.fixture
def actions(store, mp):
    pipe = Pipeline(store, [mp], None, None)
    return HumanActions(store, {"ozon": mp}, pipe)


def events(store, name):
    return [e for e in store.events("ozon", "1") if e["event"] == name]


async def release_after_start(mp):
    await asyncio.wait_for(mp.started.wait(), 2)
    await asyncio.sleep(0)
    mp.gate.set()


async def test_collect_confirms_and_records_who(actions, store, mp):
    r = await actions.collect("ozon", "1", "Аня")
    assert r == ActionResult("collected")
    assert store.get_state("ozon", "1") == "CONFIRMED"
    assert mp.confirms == 1
    assert [e["payload"] for e in events(store, "collected_by_human")] == [{"user": "Аня"}]


async def test_collect_twice_concurrently_confirms_once(actions, store, mp):
    mp.gate = asyncio.Event()
    a, b, _ = await asyncio.wait_for(asyncio.gather(
        actions.collect("ozon", "1", "Аня"), actions.collect("ozon", "1", "Боря"),
        release_after_start(mp)), 5)
    assert sorted([a.kind, b.kind]) == ["busy", "collected"]
    assert mp.confirms == 1
    assert store.get_state("ozon", "1") == "CONFIRMED"


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_collect_and_cancel_concurrently_one_wins(actions, store, mp, cancel_first):
    mp.gate = asyncio.Event()
    calls = [actions.collect("ozon", "1", "Аня"), actions.cancel("ozon", "1", "Боря")]
    if cancel_first:
        calls.reverse()
    a, b, _ = await asyncio.wait_for(asyncio.gather(*calls, release_after_start(mp)), 5)
    kinds = {a.kind, b.kind}
    assert kinds & {"collected", "cancelled"} and len(kinds & {"collected", "cancelled"}) == 1
    assert kinds - {"collected", "cancelled"} <= {"busy", "already_done"}
    assert mp.confirms + mp.human_drops == 1
    winner = "cancelled" if cancel_first else "collected"
    assert winner in kinds
    assert store.get_state("ozon", "1") == (
        "CANCELLED_BY_HUMAN" if cancel_first else "CONFIRMED")


async def test_second_action_after_first_finished_is_already_done(actions, store, mp):
    assert (await actions.collect("ozon", "1", "Аня")).kind == "collected"
    assert (await actions.collect("ozon", "1", "Боря")).kind == "already_done"
    assert mp.confirms == 1


async def test_collect_when_marketplace_no_longer_awaiting(actions, store, mp):
    mp.awaiting = False
    r = await actions.collect("ozon", "1", "Аня")
    assert r.kind == "already_done"
    assert store.get_state("ozon", "1") == "HANDLED_EXTERNALLY"
    assert mp.confirms == 0


async def test_collect_during_robot_confirming_is_busy(actions, store, mp):
    store.set_state("ozon", "1", "CONFIRMING")
    r = await actions.collect("ozon", "1", "Аня")
    assert r == ActionResult("busy", "робот уже собирает заказ")
    assert mp.confirms == 0
    assert store.get_state("ozon", "1") == "CONFIRMING"


async def test_collect_while_lock_held_is_busy(actions, store, mp):
    async with actions._pipeline.order_lock("ozon", "1"):
        r = await actions.collect("ozon", "1", "Аня")
    assert r.kind == "busy"
    assert mp.confirms == 0


async def test_collect_works_while_paused(actions, store, mp):
    store.set_paused(True, "u")
    r = await actions.collect("ozon", "1", "Аня")
    assert r.kind == "collected"
    assert mp.confirms == 1


async def test_collect_failure_keeps_state(actions, store, mp):
    mp.error = RuntimeError("Ozon 400: " + "очень длинный ответ площадки " * 10)
    r = await actions.collect("ozon", "1", "Аня")
    assert r.kind == "collect_failed"
    assert r.error and len(r.error) <= 80
    assert store.get_state("ozon", "1") == "OBSERVED"
    full = events(store, "collect_error")
    assert len(full) == 1 and "очень длинный" in full[0]["payload"]["error"]
    assert full[0]["payload"]["user"] == "Аня"


async def test_cancel_failure_keeps_state(actions, store, mp):
    mp.error = RuntimeError("нет")
    r = await actions.cancel("ozon", "1", "Аня")
    assert r == ActionResult("cancel_failed", "нет")
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert len(events(store, "cancel_error")) == 1


async def test_cancel_records_who(actions, store, mp):
    store.set_state("ozon", "1", "AWAITING_RESERVE")
    r = await actions.cancel("ozon", "1", "Аня")
    assert r == ActionResult("cancelled")
    assert store.get_state("ozon", "1") == "CANCELLED_BY_HUMAN"
    assert mp.human_drops == 1 and mp.confirms == 0
    assert [e["payload"] for e in events(store, "cancelled_by_human")] == [{"user": "Аня"}]


async def test_cancel_not_allowed_after_collected(actions, store, mp):
    store.set_state("ozon", "1", "CONFIRMED")
    r = await actions.cancel("ozon", "1", "Аня")
    assert r.kind == "already_done"
    assert mp.human_drops == 0


@pytest.mark.parametrize("state", ["AWAITING_RESERVE", "REJECTED_BY_HUMAN", "RESERVE_APPROVED",
                                   "NEW"])
async def test_collect_not_allowed_states(actions, store, mp, state):
    store.set_state("ozon", "1", state)
    r = await actions.collect("ozon", "1", "Аня")
    assert r.kind == "not_allowed_state"
    assert mp.confirms == 0
    assert store.get_state("ozon", "1") == state


@pytest.mark.parametrize("state", ["CONFIRMED", "CANCELLED_BY_HUMAN", "HANDLED_EXTERNALLY"])
async def test_done_states_are_already_done(actions, store, mp, state):
    store.set_state("ozon", "1", state)
    assert (await actions.collect("ozon", "1", "Аня")).kind == "already_done"
    assert (await actions.cancel("ozon", "1", "Аня")).kind == "already_done"
    assert mp.confirms == 0 and mp.human_drops == 0


async def test_unknown_order_is_not_allowed(actions, mp):
    r = await actions.collect("ozon", "404", "Аня")
    assert r.kind == "not_allowed_state"
    assert mp.confirms == 0


async def test_failed_cancel_of_approved_order_goes_to_a_person(actions, store, mp):
    """Ruling B: the robot must not collect an order a person tried to cancel."""
    store.set_state("ozon", "1", "RESERVE_APPROVED")
    mp.error = RuntimeError("нет")
    r = await actions.cancel("ozon", "1", "Аня")
    assert r == ActionResult("cancel_failed", "нет")
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert [e["payload"]["user"] for e in events(store, "cancel_error")] == ["Аня"]


async def test_failed_collect_of_observed_order_keeps_state(actions, store, mp):
    mp.error = RuntimeError("нет")
    await actions.collect("ozon", "1", "Аня")
    assert store.get_state("ozon", "1") == "OBSERVED"


def _http_error(status, body):
    import httpx
    req = httpx.Request("POST", "https://api-seller.ozon.ru/v2/posting/fbs/cancel")
    resp = httpx.Response(status, json=body, request=req)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e


def test_short_keeps_status_and_marketplace_message():
    from orderbot.human_actions import _short
    inner = _http_error(400, {"code": 3, "message": "Posting is already cancelled"})
    try:
        raise RuntimeError(f"cancel failed for 1: {inner}") from inner
    except RuntimeError as e:
        text = _short(e)
    assert "400" in text and "Posting is already cancelled" in text
    assert "url" not in text and "For more information" not in text
    assert len(text) <= 80


def test_short_strips_httpx_noise_without_cause():
    from orderbot.human_actions import _short
    inner = _http_error(404, {})
    text = _short(RuntimeError(f"cancel failed for 1: {inner}"))
    assert "404" in text and "url" not in text and "For more information" not in text
    assert len(text) <= 80


async def test_wb_read_only_token_reaches_the_result_line_plainly(tmp_path):
    import httpx
    s = Store(tmp_path / "w.db")
    s.upsert_new(make_order("9", mp="wb"))
    s.set_state("wb", "9", "OBSERVED")
    wb = GatedMp()
    wb.name = "wb"
    req = httpx.Request("POST", "https://marketplace-api.wildberries.ru/api/v3/supplies")
    try:
        httpx.Response(403, request=req).raise_for_status()
    except httpx.HTTPStatusError as inner:
        try:
            raise RuntimeError(f"wb 9: supply creation failed: {inner}") from inner
        except RuntimeError as e:
            wb.error = e
    acts = HumanActions(s, {"wb": wb}, Pipeline(s, [wb], None, None))
    r = await acts.collect("wb", "9", "Аня")
    assert r == ActionResult("collect_failed",
                             "WB: нет прав на запись (403) — нужен токен без «Только на чтение»")


def test_403_of_other_marketplaces_is_shown_as_before():
    from orderbot.human_actions import _short
    text = _short(_http_error(403, {"message": "Forbidden"}), "ozon")
    assert "403" in text and "Forbidden" in text and "Только на чтение" not in text


# ------------------------------------------------- stock sync trigger
def with_sync(store, mp):
    seen = []
    pipe = Pipeline(store, [mp], None, None)
    return HumanActions(store, {"ozon": mp}, pipe, on_collected=seen.append), seen


async def test_collect_triggers_stock_sync_once(store, mp):
    actions, seen = with_sync(store, mp)
    assert (await actions.collect("ozon", "1", "u")).kind == "collected"
    assert (await actions.collect("ozon", "1", "u")).kind == "already_done"
    assert [o.order_id for o in seen] == ["1"]


async def test_failed_collect_or_external_no_stock_sync(store, mp):
    actions, seen = with_sync(store, mp)
    mp.error = RuntimeError("x")
    assert (await actions.collect("ozon", "1", "u")).kind == "collect_failed"
    mp.error, mp.awaiting = None, False
    assert (await actions.collect("ozon", "1", "u")).kind == "already_done"
    assert seen == []


async def test_human_drop_never_syncs(store, mp):
    actions, seen = with_sync(store, mp)
    await actions.cancel("ozon", "1", "u")
    assert seen == []


async def test_stock_sync_error_keeps_collected(store, mp):
    def boom(order):
        raise RuntimeError("sync broke")
    actions = HumanActions(store, {"ozon": mp}, Pipeline(store, [mp], None, None),
                           on_collected=boom)
    assert (await actions.collect("ozon", "1", "u")).kind == "collected"
    assert store.get_state("ozon", "1") == "CONFIRMED"
