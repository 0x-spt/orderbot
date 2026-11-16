"""Shipment reminders in the «Отгрузочные листы» topic."""
import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

from orderbot import shipments as sh
from orderbot.config import Config, load_config
from orderbot.marketplaces.base import ToShip
from orderbot.marketplaces.ozon import OzonMarketplace
from orderbot.marketplaces.yandex import YandexMarket
from orderbot.shipments import (
    ScheduleError,
    ShipmentReminders,
    build_shipments,
    load_schedule,
    orders_word,
    parse_schedule,
    slot_text,
)
from orderbot.store import Store

CHAT, THREAD = -100500, 4
OZ = "https://api-seller.ozon.ru"
YM = "https://api.partner.market.yandex.ru"
KNOWN = {"Главный склад", "СПБ", "Южная,9", "СЕВЕР", "ВОСТОК 2", "Склад (восто"}

SCHEDULE = {
    "before_minutes": 10,
    "names": {"СЕВЕР": "Север"},
    "weekend_skip": ["Главный склад", "ВОСТОК 2", "Склад (восто"],
    "slots": {
        "10:00": {"ozon": ["Главный склад"], "yandex": ["Главный склад", "СПБ"]},
        "12:00": {"ozon": ["Южная,9"], "yandex": ["Южная,9", "СЕВЕР"]},
        "14:00": {"ozon": ["СПБ", "СЕВЕР", "ВОСТОК 2", "Склад (восто"],
                  "yandex": ["ВОСТОК 2", "Склад (восто"]},
    },
}


def msk(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone(timedelta(hours=3)))


# ------------------------------------------------------------------ plural and schedule
@pytest.mark.parametrize("n,word", [(1, "заказ"), (2, "заказа"), (4, "заказа"),
                                    (5, "заказов"), (11, "заказов"), (12, "заказов"),
                                    (14, "заказов"), (21, "заказ"), (22, "заказа"),
                                    (25, "заказов"), (101, "заказ"), (111, "заказов")])
def test_orders_word_plural_forms(n, word):
    assert orders_word(n) == word


def test_parse_owner_schedule():
    s = parse_schedule(SCHEDULE, KNOWN)
    assert [x.time for x in s.slots] == ["10:00", "12:00", "14:00"]
    assert s.slots[1].warehouses == {"ozon": ["Южная,9"],
                                     "yandex": ["Южная,9", "СЕВЕР"]}
    assert s.before == timedelta(minutes=10)
    assert s.name("СЕВЕР") == "Север" and s.name("СПБ") == "СПБ"


def test_weekend_drops_listed_warehouses_and_empty_slots():
    s = parse_schedule(SCHEDULE, KNOWN)
    sat = date(2026, 11, 7)
    fri = date(2026, 11, 6)
    assert s.for_day(s.slots[0], fri) == {"ozon": ["Главный склад"],
                                          "yandex": ["Главный склад", "СПБ"]}
    assert s.for_day(s.slots[0], sat) == {"ozon": [], "yandex": ["СПБ"]}
    assert s.for_day(s.slots[2], sat) == {"ozon": ["СПБ", "СЕВЕР"], "yandex": []}
    only_main = parse_schedule({"slots": {"10:00": {"ozon": ["Главный склад"]}},
                                "weekend_skip": ["Главный склад"]}, KNOWN)
    assert only_main.for_day(only_main.slots[0], sat) is None
    assert only_main.for_day(only_main.slots[0], date(2026, 11, 8)) is None   # Sunday


def test_unquoted_yaml_time_is_read_as_minutes(tmp_path):
    # YAML 1.1 reads 10:00 without quotes as the number 600
    p = tmp_path / "s.yaml"
    p.write_text("slots:\n  10:00:\n    ozon: [СПБ]\n  9:30:\n    yandex: [СПБ]\n",
                 encoding="utf-8")
    s = load_schedule(p, KNOWN)
    assert [x.time for x in s.slots] == ["09:30", "10:00"]


@pytest.mark.parametrize("raw", [
    None, [], {"slots": {}}, {"slots": []},
    {"slots": {"25:00": {"ozon": ["СПБ"]}}},
    {"slots": {"10:00": {"wb": ["СПБ"]}}},
    {"slots": {"10:00": {"ozon": "СПБ"}}},
    {"slots": {"10:00": {"ozon": ["Нет такого"]}}},
    {"slots": {"10:00": {"ozon": ["СПБ"]}}, "weekend_skip": ["Нет такого"]},
    {"slots": {"10:00": {"ozon": ["СПБ"]}}, "before_minutes": 0},
    {"slots": {"10:00": {"ozon": ["СПБ"]}}, "before_minutes": "десять"},
    {"slots": {"10:00": {"ozon": ["СПБ"]}}, "names": ["СПБ"]},
    {"slots": {"10:00": {"ozon": ["СПБ"]}}, "лишнее": 1},
])
def test_bad_schedule_is_an_error(raw):
    with pytest.raises(ScheduleError):
        parse_schedule(raw, KNOWN)


def test_unreadable_file_is_an_error(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("slots: [", encoding="utf-8")
    with pytest.raises(ScheduleError):
        load_schedule(p, KNOWN)


def test_example_schedule_file_is_valid():
    from pathlib import Path
    root = Path(__file__).parent.parent
    known = {w for slot in SCHEDULE["slots"].values() for names in slot.values() for w in names}
    s = load_schedule(root / "shipments.example.yaml", known)
    assert [x.time for x in s.slots] == ["10:00", "12:00", "14:00"]
    assert s.slots[2].warehouses == SCHEDULE["slots"]["14:00"]
    assert set(s.weekend_skip) == set(SCHEDULE["weekend_skip"])


# ------------------------------------------------------------------ message text
def test_message_text_owners_example_exactly():
    s = parse_schedule(SCHEDULE, KNOWN)
    blocks = [
        ("Альфа", [("Озон", "Южная,9", (2, 1, 5)),
                    ("Маркет", "Южная,9", (0, 0, 0)),
                    ("Маркет", "СЕВЕР", (1, 0, 1))]),
        ("Бета", [("Озон", "Южная,9", (0, 0, 0))]),
    ]
    assert slot_text("12:00", date(2026, 11, 6), blocks, s.name) == (
        "🚚 <b>Отгрузка в 12:00</b> · пятница 06.11\n\n"
        "<b>Альфа</b>\n"
        "Озон · Южная,9 — <b>3 заказа, 5 шт.</b> (собрано 2, не собрано 1)\n"
        "Маркет · Южная,9 — нет отгрузки\n"
        "Маркет · Север — <b>1 заказ, 1 шт.</b> (собрано 1)\n\n"
        "<b>Бета</b>\n"
        "Озон · Южная,9 — нет отгрузки")


def test_message_failure_line_and_escaping():
    blocks = [("R&D <x>", [("Озон", "A<b>", "HTTP 503"), ("Маркет", "СПБ", (0, 7, 9))])]
    assert slot_text("14:00", date(2026, 11, 7), blocks, lambda n: n) == (
        "🚚 <b>Отгрузка в 14:00</b> · суббота 07.11\n\n"
        "<b>R&amp;D &lt;x&gt;</b>\n"
        "⚠️ Озон · A&lt;b&gt; — не удалось узнать (HTTP 503)\n"
        "Маркет · СПБ — <b>7 заказов, 9 шт.</b> (не собрано 7)")


def test_whole_slot_empty_is_one_short_line():
    blocks = [("Альфа", [("Озон", "СПБ", (0, 0, 0))]), ("Бета", [])]
    assert slot_text("12:00", date(2026, 11, 6), blocks, lambda n: n) == (
        "🚚 <b>В 12:00 отгрузки нет</b> · пятница 06.11")


# ------------------------------------------------------------------ adapters (read-only)
def ozon_posting(nr, status, wh, ship, qty=(1,)):
    return {"posting_number": nr, "status": status, "shipment_date": ship,
            "delivery_method": {"warehouse": wh},
            "products": [{"offer_id": f"A{i}", "sku": i, "quantity": q}
                         for i, q in enumerate(qty)]}


async def test_ozon_to_ship_reads_both_statuses_maps_and_skips_express():
    m = OzonMarketplace("cid", "key", retry_delay=0,
                        warehouses={"Южная 9": "Южная,9",
                                    "Южная 9 EXPRESS 🚀": "Южная,9", "СПБ": "СПБ"},
                        express={"Южная 9 EXPRESS 🚀"})
    lists = {
        "awaiting_packaging": [
            ozon_posting("1", "awaiting_packaging", "Южная 9", "2026-11-06T07:30:00Z", (2,)),
            ozon_posting("2", "awaiting_packaging", "Южная 9 EXPRESS 🚀",
                         "2026-11-06T07:30:00Z")],
        "awaiting_deliver": [
            # 21:30 UTC on 05.11 is 00:30 on 06.11 in Moscow
            ozon_posting("3", "awaiting_deliver", "СПБ", "2026-11-05T21:30:00Z", (1, 3)),
            ozon_posting("4", "awaiting_deliver", "Неизвестный", "2026-11-06T07:30:00Z")],
    }
    seen = []

    def answer(request):
        import json
        body = json.loads(request.content)
        st = body["filter"]["statuses"][0]
        seen.append(st)
        return httpx.Response(200, json={"postings": lists[st], "has_next": False})

    with respx.mock(assert_all_mocked=True) as r:
        route = r.post(f"{OZ}/v4/posting/fbs/unfulfilled/list").mock(side_effect=answer)
        got = await m.to_ship()
    assert sorted(seen) == ["awaiting_deliver", "awaiting_packaging"]
    assert route.call_count == 2
    assert sorted(got, key=lambda x: x.order_id) == [
        ToShip("1", "Южная,9", date(2026, 11, 6), False, 2),
        ToShip("3", "СПБ", date(2026, 11, 6), True, 4)]
    assert m.shipment_warehouses() == {"Южная,9", "СПБ"}


async def test_yandex_to_ship_reads_started_and_ready_of_wanted_campaigns_only():
    m = YandexMarket("tok", None, retry_delay=0,
                     warehouses={"1": "СЕВЕР", "2": "СЕВЕР", "3": "СПБ", "4": "ВОСТОК 2"},
                     express={"2"})

    def order(oid, d, counts, flag=False):
        o = {"id": oid, "status": "PROCESSING",
             "items": [{"offerId": f"A{i}", "count": c} for i, c in enumerate(counts)],
             "delivery": {"shipments": [{"id": 9, "shipmentDate": d}]}}
        if flag:
            o["cancelRequested"] = True
        return o

    data = {("1", "STARTED"): [order(10, "06-11-2026", [1, 2])],
            ("1", "READY_TO_SHIP"): [order(11, "07-11-2026", [1]),
                                     order(12, "06-11-2026", [5], flag=True)],
            ("3", "STARTED"): [],
            ("3", "READY_TO_SHIP"): [order(13, "06-11-2026", [1])]}
    asked = []

    def answer(request):
        camp = request.url.path.split("/")[3]
        sub = request.url.params["substatus"]
        asked.append((camp, sub))
        assert request.url.params["status"] == "PROCESSING"
        return httpx.Response(200, json={"orders": data[(camp, sub)]})

    with respx.mock(assert_all_mocked=True) as r:
        r.get(url__regex=rf"{YM}/v2/campaigns/\d+/orders").mock(side_effect=answer)
        got = await m.to_ship(only={"СЕВЕР", "СПБ"})
    assert sorted(asked) == [("1", "READY_TO_SHIP"), ("1", "STARTED"),
                             ("3", "READY_TO_SHIP"), ("3", "STARTED")]
    assert sorted(got, key=lambda x: x.order_id) == [
        ToShip("10", "СЕВЕР", date(2026, 11, 6), False, 3),
        ToShip("11", "СЕВЕР", date(2026, 11, 7), True, 1),
        ToShip("13", "СПБ", date(2026, 11, 6), True, 1)]
    assert m.shipment_warehouses() == {"СЕВЕР", "СПБ", "ВОСТОК 2"}


# ------------------------------------------------------------------ the reminder loop
class FakeSource:
    def __init__(self, name, account, warehouses, items=(), error=None):
        self.name, self.account = name, account
        self._wh, self._items, self._error = set(warehouses), list(items), error
        self.calls = 0

    def shipment_warehouses(self):
        return self._wh

    async def to_ship(self, only=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._items)


class FakeBot:
    def __init__(self, fail=0):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat, text, **kw):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("telegram down")
        self.sent.append((chat, text, kw))
        return SimpleNamespace(message_id=len(self.sent))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t.astimezone(UTC)


COMPANIES = {"main": "Альфа", "second": "Бета"}


def reminders(tmp_path, sources, clock, bot=None, store=None, schedule=None, said=None):
    store = store or Store(tmp_path / "db.sqlite")
    bot = bot or FakeBot()

    async def say(text):
        if said is not None:
            said.append(text)

    r = ShipmentReminders(schedule or parse_schedule(SCHEDULE, KNOWN), sources, store, bot,
                          CHAT, THREAD, companies=COMPANIES, clock=clock, say=say)
    return r, store, bot


def all_sources():
    d = date(2026, 11, 6)
    return [
        FakeSource("ozon", "main", KNOWN, [
            ToShip("1", "Южная,9", d, True, 2), ToShip("2", "Южная,9", d, True, 1),
            ToShip("3", "Южная,9", d, False, 2),
            ToShip("4", "Южная,9", d + timedelta(days=1), False, 9),   # tomorrow
            ToShip("5", "СПБ", d, False, 1)]),                             # other slot
        FakeSource("yandex", "main", KNOWN, [ToShip("6", "СЕВЕР", d, True, 1)]),
        FakeSource("ozon", "second", {"Южная,9", "СЕВЕР"}),
        FakeSource("yandex", "second", {"Главный склад"}),   # nothing in this slot
    ]


async def test_fires_at_t_minus_10_with_the_owners_example(tmp_path):
    clock = Clock(msk(2026, 11, 6, 11, 49, 59))
    srcs = all_sources()
    r, store, bot = reminders(tmp_path, srcs, clock)
    await r.tick()
    assert bot.sent == []
    clock.t = msk(2026, 11, 6, 11, 50, 0)
    await r.tick()
    assert len(bot.sent) == 1
    chat, text, kw = bot.sent[0]
    assert chat == CHAT and kw == {"message_thread_id": THREAD, "parse_mode": "HTML"}
    assert text == (
        "🚚 <b>Отгрузка в 12:00</b> · пятница 06.11\n\n"
        "<b>Альфа</b>\n"
        "Озон · Южная,9 — <b>3 заказа, 5 шт.</b> (собрано 2, не собрано 1)\n"
        "Маркет · Южная,9 — нет отгрузки\n"
        "Маркет · Север — <b>1 заказ, 1 шт.</b> (собрано 1)\n\n"
        "<b>Бета</b>\n"
        "Озон · Южная,9 — нет отгрузки")
    assert [s.calls for s in srcs] == [1, 1, 1, 0]   # one fetch each, none without lines
    clock.t = msk(2026, 11, 6, 11, 55)
    await r.tick()
    assert len(bot.sent) == 1                       # once per slot
    assert store.shipments_last_slot() == "2026-11-06 12:00"


async def test_restart_inside_the_window_does_not_fire_again(tmp_path):
    clock = Clock(msk(2026, 11, 6, 11, 51))
    r, store, bot = reminders(tmp_path, all_sources(), clock)
    await r.tick()
    assert len(bot.sent) == 1
    r2, _, bot2 = reminders(tmp_path, all_sources(), Clock(msk(2026, 11, 6, 11, 58)),
                            store=store)
    await r2.tick()
    assert bot2.sent == []


async def test_start_late_before_t_fires_at_once_after_t_skips(tmp_path):
    r, _, bot = reminders(tmp_path, all_sources(), Clock(msk(2026, 11, 6, 11, 59, 30)))
    await r.tick()
    assert len(bot.sent) == 1
    (tmp_path / "x").mkdir()
    r2, _, bot2 = reminders(tmp_path / "x", all_sources(), Clock(msk(2026, 11, 6, 12, 0)))
    await r2.tick()
    assert bot2.sent == []


async def test_next_day_same_slot_fires_again(tmp_path):
    clock = Clock(msk(2026, 11, 5, 11, 52))     # Thursday
    r, store, bot = reminders(tmp_path, all_sources(), clock)
    await r.tick()
    clock.t = msk(2026, 11, 6, 11, 52)
    await r.tick()
    assert len(bot.sent) == 2
    assert store.shipments_last_slot() == "2026-11-06 12:00"


async def test_weekend_slot_with_empty_list_sends_nothing(tmp_path):
    sched = parse_schedule({"slots": {"10:00": {"ozon": ["Главный склад"]}},
                            "weekend_skip": ["Главный склад"]}, KNOWN)
    r, _, bot = reminders(tmp_path, all_sources(), Clock(msk(2026, 11, 7, 9, 55)),
                          schedule=sched)
    await r.tick()
    assert bot.sent == []


async def test_weekend_keeps_other_warehouses(tmp_path):
    srcs = [FakeSource("ozon", "main", KNOWN), FakeSource("yandex", "main", KNOWN)]
    r, _, bot = reminders(tmp_path, srcs, Clock(msk(2026, 11, 7, 9, 55)))
    await r.tick()
    assert bot.sent[0][1] == "🚚 <b>В 10:00 отгрузки нет</b> · суббота 07.11"
    srcs[0]._items = [ToShip("1", "СПБ", date(2026, 11, 8), True, 1)]
    (tmp_path / "y").mkdir()
    r2, _, bot2 = reminders(tmp_path / "y", srcs, Clock(msk(2026, 11, 8, 13, 50)))
    await r2.tick()
    assert bot2.sent[0][1] == (
        "🚚 <b>Отгрузка в 14:00</b> · воскресенье 08.11\n\n"
        "<b>Альфа</b>\n"
        "Озон · СПБ — <b>1 заказ, 1 шт.</b> (собрано 1)\n"
        "Озон · Север — нет отгрузки")


@pytest.mark.parametrize("error,why", [
    (httpx.HTTPStatusError("x", request=httpx.Request("POST", "https://h/"),
                           response=httpx.Response(503)), "HTTP 503"),
    (httpx.ConnectError("x"), "нет связи"),
    (KeyError("x"), "KeyError"),
])
async def test_api_failure_is_shown_never_silenced(tmp_path, error, why):
    srcs = [FakeSource("ozon", "main", KNOWN, error=error),
            FakeSource("yandex", "main", KNOWN)]
    r, _, bot = reminders(tmp_path, srcs, Clock(msk(2026, 11, 6, 11, 50)))
    await r.tick()
    assert bot.sent[0][1] == (
        "🚚 <b>Отгрузка в 12:00</b> · пятница 06.11\n\n"
        "<b>Альфа</b>\n"
        f"⚠️ Озон · Южная,9 — не удалось узнать ({why})\n"
        "Маркет · Южная,9 — нет отгрузки\n"
        "Маркет · Север — нет отгрузки")


async def test_send_failure_retries_without_refetch_and_tells_admins_if_slot_missed(tmp_path):
    said = []
    clock = Clock(msk(2026, 11, 6, 11, 50))
    srcs = all_sources()
    r, store, bot = reminders(tmp_path, srcs, clock, bot=FakeBot(fail=2), said=said)
    await r.tick()
    clock.t = msk(2026, 11, 6, 11, 51)
    await r.tick()
    assert bot.sent == [] and store.shipments_last_slot() is None
    clock.t = msk(2026, 11, 6, 11, 52)
    await r.tick()
    assert len(bot.sent) == 1 and srcs[0].calls == 1
    # a slot that never reached the group: the admins hear once
    clock.t = msk(2026, 11, 6, 13, 50)
    bot.fail = 10**6
    await r.tick()
    assert said == []
    clock.t = msk(2026, 11, 6, 14, 0)
    await r.tick()
    await r.tick()
    assert len(said) == 1 and "14:00" in said[0]


async def test_independent_of_robot_pause(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.set_paused(True, "test")
    r, _, bot = reminders(tmp_path, all_sources(), Clock(msk(2026, 11, 6, 11, 50)),
                          store=store)
    await r.tick()
    assert len(bot.sent) == 1


async def test_run_loop_survives_a_failing_tick(tmp_path, monkeypatch):
    r, _, _ = reminders(tmp_path, all_sources(), Clock(msk(2026, 11, 6, 11, 50)))
    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("bug")

    monkeypatch.setattr(r, "tick", boom)
    stop = asyncio.Event()
    task = asyncio.ensure_future(r.run(stop, interval=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert len(calls) >= 2


# ------------------------------------------------------------------ config and build
def test_config_reads_shipments_thread(tmp_path):
    cfg = load_config({"SHIPMENTS_THREAD_ID": "4"}, warehouses_path=tmp_path / "none.yaml")
    assert cfg.shipments_thread_id == 4
    assert cfg.shipments_file == "shipments.yaml"
    cfg = load_config({"SHIPMENTS_FILE": "x.yaml"}, warehouses_path=tmp_path / "none.yaml")
    assert cfg.shipments_thread_id is None and cfg.shipments_file == "x.yaml"


def test_non_numeric_shipments_thread_turns_feature_off_not_robot(tmp_path, caplog):
    cfg = load_config({"SHIPMENTS_THREAD_ID": "тема"}, warehouses_path=tmp_path / "none.yaml")
    assert cfg.shipments_thread_id is None
    assert sum("SHIPMENTS_THREAD_ID" in r.getMessage() for r in caplog.records) == 1


def _cfg(tmp_path, schedule_text=None, **kw):
    p = tmp_path / "shipments.yaml"
    if schedule_text is not None:
        p.write_text(schedule_text, encoding="utf-8")
    base = dict(alert_chat_id=CHAT, shipments_thread_id=THREAD, shipments_file=str(p),
                warehouses={"ozon": {"СПБ": "СПБ"}, "yandex": {"1": "СПБ"}})
    base.update(kw)
    return Config(**base)


GOOD = "slots:\n  '10:00':\n    ozon: [СПБ]\n"


def test_build_off_without_thread_or_chat(tmp_path):
    src = [FakeSource("ozon", "main", {"СПБ"})]
    part, warning = build_shipments(_cfg(tmp_path, GOOD, shipments_thread_id=None), src,
                                    None, None, None)
    assert part is None and warning is None
    part, warning = build_shipments(_cfg(tmp_path, GOOD, alert_chat_id=None), src,
                                    None, None, None)
    assert part is None and warning is None


def test_build_off_with_missing_or_bad_file_gives_a_warning(tmp_path):
    src = [FakeSource("ozon", "main", {"СПБ"})]
    part, warning = build_shipments(_cfg(tmp_path), src, None, None, None)
    assert part is None and "shipments.yaml" in warning
    part, warning = build_shipments(_cfg(tmp_path, "slots:\n  '10:00':\n    ozon: [Х]\n"),
                                    src, None, None, None)
    assert part is None and "Х" in warning and "выключ" in warning


def test_build_uses_only_ozon_and_yandex_sources(tmp_path):
    wb = SimpleNamespace(name="wb", account="main")
    oz = FakeSource("ozon", "main", {"СПБ"})
    part, warning = build_shipments(_cfg(tmp_path, GOOD), [wb, oz], None, None, None)
    assert warning is None and part is not None
    assert part._sources == [oz]


# ------------------------------------------------------------------ wiring
async def test_run_starts_the_shipments_loop_beside_the_robot(tmp_path, monkeypatch):
    import orderbot.main as m

    seen = {}

    class FakeNotifier:
        def __init__(self, *a, **k):
            pass

        async def say(self, text, keyboard=False):
            pass

        async def system_alert(self, text):
            seen.setdefault("alerts", []).append(text)

        async def health_alert(self, text):
            seen.setdefault("alerts", []).append(text)

    class FakeOneC:
        def __init__(self, *a, **k):
            pass

        def request_stop(self):
            pass

    class FakeDp:
        async def start_polling(self, bot, handle_signals=False):
            await asyncio.sleep(0.05)
            raise RuntimeError("stop here")

        async def stop_polling(self):
            pass

    class FakePart:
        async def run(self, stop):
            seen["ran"] = True
            await stop.wait()

    def build(cfg, mps, store, bot, say):
        seen["args"] = (cfg, mps, store, bot, say)
        return FakePart(), "предупреждение об отгрузках"

    monkeypatch.setattr(m, "TelegramNotifier", FakeNotifier)
    monkeypatch.setattr(m, "MacOneC", FakeOneC)
    monkeypatch.setattr(m, "build_dispatcher", lambda controller, notifier: FakeDp())
    monkeypatch.setattr(m, "build_marketplaces", lambda cfg: [])
    monkeypatch.setattr(m, "_caffeinate", lambda: None)
    monkeypatch.setattr(m, "build_lamoda", lambda *a, **k: None)
    monkeypatch.setattr(m, "build_shipments", build)
    cfg = Config(bot_token="123:abc", allowed_user_ids=[1], alert_chat_id=-100500)
    await asyncio.wait_for(m.run(cfg, SimpleNamespace(warehouses={}), tmp_path), 10)
    assert seen.get("ran") is True
    assert seen["args"][0] is cfg and isinstance(seen["args"][2], Store)
    assert "предупреждение об отгрузках" in seen["alerts"]


# ------------------------------------------------------------------ edge cases
def test_overdue_part_of_the_line():
    Z = (0, 0, 0)
    blocks = [("Альфа", [
        ("Озон", "СПБ", ((1, 0, 1), (0, 2, 3))),        # today and overdue
        ("Озон", "СЕВЕР", (Z, (1, 1, 4))),             # only overdue: not «нет отгрузки»
        ("Маркет", "СПБ", (Z, Z)),
        ("Маркет", "СЕВЕР", (Z, (1, 0, 1)))])]
    assert slot_text("14:00", date(2026, 11, 6), blocks, lambda n: n) == (
        "🚚 <b>Отгрузка в 14:00</b> · пятница 06.11\n\n"
        "<b>Альфа</b>\n"
        "Озон · СПБ — <b>1 заказ, 1 шт.</b> (собрано 1) · "
        "⚠️ просрочено: 2 заказа, 3 шт. (не собрано 2)\n"
        "Озон · СЕВЕР — ⚠️ просрочено: 2 заказа, 4 шт. (собрано 1, не собрано 1)\n"
        "Маркет · СПБ — нет отгрузки\n"
        "Маркет · СЕВЕР — ⚠️ просрочено: 1 заказ, 1 шт. (собрано 1)")


def test_only_overdue_is_not_an_empty_slot():
    blocks = [("Альфа", [("Озон", "СПБ", ((0, 0, 0), (0, 1, 1)))])]
    assert slot_text("12:00", date(2026, 11, 6), blocks, lambda n: n) == (
        "🚚 <b>Отгрузка в 12:00</b> · пятница 06.11\n\n"
        "<b>Альфа</b>\n"
        "Озон · СПБ — ⚠️ просрочено: 1 заказ, 1 шт. (не собрано 1)")


async def test_ozon_day_is_moscow_near_midnight_utc():
    m = OzonMarketplace("cid", "key", retry_delay=0, warehouses={"СПБ": "СПБ"})
    posts = [ozon_posting("a", "awaiting_deliver", "СПБ", "2026-11-05T20:59:00Z"),
             ozon_posting("b", "awaiting_deliver", "СПБ", "2026-11-05T21:00:00Z")]

    def answer(request):
        import json
        st = json.loads(request.content)["filter"]["statuses"][0]
        return httpx.Response(200, json={"postings": posts if st == "awaiting_deliver"
                                         else [], "has_next": False})

    with respx.mock() as r:
        r.post(f"{OZ}/v4/posting/fbs/unfulfilled/list").mock(side_effect=answer)
        got = {x.order_id: x.day for x in await m.to_ship()}
    # «a» is 05.11 23:59 in Moscow (overdue on 06.11), «b» is 06.11 00:00 (today)
    assert got == {"a": date(2026, 11, 5), "b": date(2026, 11, 6)}


async def test_loop_counts_overdue_and_today_apart(tmp_path):
    d = date(2026, 11, 6)
    srcs = [FakeSource("ozon", "main", KNOWN, [
                ToShip("a", "СПБ", d - timedelta(days=1), True, 1),     # overdue, assembled
                ToShip("b", "СПБ", d - timedelta(days=3), False, 2),    # overdue
                ToShip("c", "СПБ", d, True, 2),
                ToShip("d", "СЕВЕР", d - timedelta(days=1), False, 1),
                ToShip("e", "СЕВЕР", d + timedelta(days=1), False, 9)]),  # tomorrow
            FakeSource("yandex", "main", KNOWN)]
    r, _, bot = reminders(tmp_path, srcs, Clock(msk(2026, 11, 6, 13, 50)))
    await r.tick()
    assert bot.sent[0][1] == (
        "🚚 <b>Отгрузка в 14:00</b> · пятница 06.11\n\n"
        "<b>Альфа</b>\n"
        "Озон · СПБ — <b>1 заказ, 2 шт.</b> (собрано 1) · "
        "⚠️ просрочено: 2 заказа, 3 шт. (собрано 1, не собрано 1)\n"
        "Озон · Север — ⚠️ просрочено: 1 заказ, 1 шт. (не собрано 1)\n"
        "Озон · ВОСТОК 2 — нет отгрузки\n"
        "Озон · Склад (восто — нет отгрузки\n"
        "Маркет · ВОСТОК 2 — нет отгрузки\n"
        "Маркет · Склад (восто — нет отгрузки")


async def test_report_bug_alerts_admins_once_and_does_not_refetch(tmp_path, monkeypatch):
    said = []
    clock = Clock(msk(2026, 11, 6, 11, 50))
    srcs = all_sources()
    r, store, bot = reminders(tmp_path, srcs, clock, said=said)
    calls = []

    async def boom(*a):
        calls.append(1)
        raise ZeroDivisionError("bug")

    monkeypatch.setattr(r, "report", boom)
    for minute in (50, 51, 52, 59):
        clock.t = msk(2026, 11, 6, 11, minute)
        await r.tick()
    assert len(calls) == 1 and bot.sent == []
    assert len(said) == 1 and "12:00" in said[0]
    clock.t = msk(2026, 11, 6, 13, 50)            # the next slot is tried again
    await r.tick()
    assert len(calls) == 2 and len(said) == 2


async def test_yandex_one_failing_campaign_marks_only_its_warehouses():
    m = YandexMarket("tok", None, retry_delay=0,
                     warehouses={"1": "СЕВЕР", "3": "СПБ", "5": "СЕВЕР"})

    def answer(request):
        camp = request.url.path.split("/")[3]
        if camp == "5":
            return httpx.Response(500)
        o = {"id": int(camp), "items": [{"offerId": "A", "count": 1}],
             "delivery": {"shipments": [{"shipmentDate": "06-11-2026"}]}}
        return httpx.Response(200, json={"orders": [o] if request.url.params["substatus"]
                                         == "STARTED" else []})

    with respx.mock() as r:
        r.get(url__regex=rf"{YM}/v2/campaigns/\d+/orders").mock(side_effect=answer)
        with pytest.raises(sh.ToShipPartial) as e:
            await m.to_ship()
    # campaign 1 was read but shares СЕВЕР with the failed 5: the warehouse is failed whole
    assert sorted(x.order_id for x in e.value.items) == ["3"]
    assert set(e.value.failed) == {"СЕВЕР"}
    assert sh.failure_reason(e.value.failed["СЕВЕР"]) == "HTTP 500"


async def test_partial_failure_in_the_message(tmp_path):
    d = date(2026, 11, 6)
    err = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://h/"),
                                response=httpx.Response(502))

    class Partial(FakeSource):
        async def to_ship(self, only=None):
            self.calls += 1
            raise sh.ToShipPartial([ToShip("1", "Южная,9", d, False, 2)],
                                   {"СЕВЕР": err})

    srcs = [FakeSource("ozon", "main", KNOWN), Partial("yandex", "main", KNOWN)]
    r, _, bot = reminders(tmp_path, srcs, Clock(msk(2026, 11, 6, 11, 50)))
    await r.tick()
    assert bot.sent[0][1] == (
        "🚚 <b>Отгрузка в 12:00</b> · пятница 06.11\n\n"
        "<b>Альфа</b>\n"
        "Озон · Южная,9 — нет отгрузки\n"
        "Маркет · Южная,9 — <b>1 заказ, 2 шт.</b> (не собрано 1)\n"
        "⚠️ Маркет · Север — не удалось узнать (HTTP 502)")


@pytest.mark.parametrize("t,before", [("00:05", 10), ("00:00", 1), (5, 10)])
def test_window_crossing_midnight_is_rejected(t, before):
    with pytest.raises(ScheduleError):
        parse_schedule({"slots": {t: {"ozon": ["СПБ"]}}, "before_minutes": before}, KNOWN)
    parse_schedule({"slots": {"00:10": {"ozon": ["СПБ"]}}, "before_minutes": 10}, KNOWN)


async def test_ozon_to_ship_follows_the_cursor():
    m = OzonMarketplace("cid", "key", retry_delay=0, warehouses={"СПБ": "СПБ"})
    pages = {None: ({"postings": [ozon_posting("1", "awaiting_deliver", "СПБ",
                                               "2026-11-06T07:00:00Z")],
                     "has_next": True, "cursor": "c1"}),
             "c1": {"postings": [ozon_posting("2", "awaiting_deliver", "СПБ",
                                              "2026-11-06T07:00:00Z")], "has_next": False}}

    def answer(request):
        import json
        body = json.loads(request.content)
        if body["filter"]["statuses"][0] != "awaiting_deliver":
            return httpx.Response(200, json={"postings": [], "has_next": False})
        return httpx.Response(200, json=pages[body.get("cursor")])

    with respx.mock() as r:
        r.post(f"{OZ}/v4/posting/fbs/unfulfilled/list").mock(side_effect=answer)
        got = await m.to_ship()
    assert sorted(x.order_id for x in got) == ["1", "2"]


async def test_yandex_to_ship_follows_the_page_token():
    m = YandexMarket("tok", None, retry_delay=0, warehouses={"3": "СПБ"})

    def o(i):
        return {"id": i, "items": [{"offerId": "A", "count": 1}],
                "delivery": {"shipments": [{"shipmentDate": "06-11-2026"}]}}

    def answer(request):
        if request.url.params["substatus"] != "READY_TO_SHIP":
            return httpx.Response(200, json={"orders": []})
        if request.url.params.get("pageToken") == "p2":
            return httpx.Response(200, json={"orders": [o(2)], "paging": {}})
        return httpx.Response(200, json={"orders": [o(1)],
                                         "paging": {"nextPageToken": "p2"}})

    with respx.mock() as r:
        r.get(url__regex=rf"{YM}/v2/campaigns/3/orders").mock(side_effect=answer)
        got = await m.to_ship()
    assert sorted(x.order_id for x in got) == ["1", "2"]


async def test_same_warehouse_in_two_slots_is_counted_in_both(tmp_path):
    sched = parse_schedule({"slots": {"10:00": {"ozon": ["СПБ"]},
                                      "14:00": {"ozon": ["СПБ"]}}}, KNOWN)
    d = date(2026, 11, 6)
    srcs = [FakeSource("ozon", "main", KNOWN, [ToShip("1", "СПБ", d, False, 1)])]
    clock = Clock(msk(2026, 11, 6, 9, 50))
    r, _, bot = reminders(tmp_path, srcs, clock, schedule=sched)
    await r.tick()
    clock.t = msk(2026, 11, 6, 13, 50)
    await r.tick()
    assert [t.splitlines()[-1] for _, t, _ in bot.sent] == [
        "Озон · СПБ — <b>1 заказ, 1 шт.</b> (не собрано 1)"] * 2
