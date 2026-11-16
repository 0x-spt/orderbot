from datetime import UTC, datetime, timezone
from decimal import Decimal

import pytest

from orderbot.domain import Action, Decision, Order, OrderLine, Reason
from orderbot.store import Store

UTC = UTC


def at(h, m=0):
    return datetime(2026, 11, 2, h, m, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_order(oid="1", mp="ozon"):
    return Order(mp, oid, [OrderLine("A1", 2, "СПБ", Decimal("1880.50"), "BrandX"),
                           OrderLine("B2", 1, None, None, None)],
                 at(9), at(18), "awaiting_packaging")


@pytest.fixture
def clock():
    return Clock(at(10))


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "s.db", clock=clock)


def test_upsert_is_once(store):
    assert store.upsert_new(make_order()) is True
    assert store.upsert_new(make_order()) is False


def test_new_state_and_pending(store):
    store.upsert_new(make_order("1"))
    store.upsert_new(make_order("2"))
    store.upsert_new(make_order("3", mp="ym"))
    assert store.get_state("ozon", "1") == "NEW"
    assert store.get_state("ozon", "zzz") is None
    store.set_state("ozon", "2", "OBSERVED")
    assert sorted(store.pending()) == [("ozon", "1"), ("ym", "3")]


def test_set_state_with_decision(store):
    store.upsert_new(make_order())
    d = Decision(Action.ESCALATE, [("A1", Reason.PRICE_LOW)], {"A1": Decimal("1454.00")})
    store.set_state("ozon", "1", "ESCALATED", d)
    assert store.get_state("ozon", "1") == "ESCALATED"
    assert store.get_decision("ozon", "1") == d


def test_get_order_roundtrip(store):
    o = make_order()
    store.upsert_new(o)
    assert store.get_order("ozon", "1") == o


def test_state_survives_reopen(tmp_path, clock):
    p = tmp_path / "x.db"
    s1 = Store(p, clock=clock)
    s1.upsert_new(make_order())
    s1.set_state("ozon", "1", "CONFIRMED")
    s1.set_mode("ozon", "confirm", "u")
    s2 = Store(p)
    assert s2.get_state("ozon", "1") == "CONFIRMED"
    assert s2.get_mode("ozon") == "confirm"
    assert s2.upsert_new(make_order()) is False


def test_default_mode_is_observe(store):
    assert store.get_mode("ozon") == "observe"
    with pytest.raises(ValueError):
        store.set_mode("ozon", "bogus", "u")


def test_default_tolerance_is_50(store):
    s = store.settings_at("ozon", at(11))
    assert s.tolerance == Decimal("50")
    assert s.soinvest is False and s.brand_discounts == {} and s.multi_qty_verified is False


def test_settings_at_uses_history(store, clock):
    clock.t = at(10)
    store.change_setting("ozon", "brand_discount", {"brand": "BrandX", "percent": Decimal("20")}, "ann")
    clock.t = at(12)
    store.change_setting("ozon", "brand_discount", {"brand": "BRANDX", "percent": None}, "ann")
    assert store.settings_at("ozon", at(9)).brand_discounts == {}
    assert store.settings_at("ozon", at(11)).brand_discounts == {"brandx": Decimal("20")}
    assert store.settings_at("ozon", at(13)).brand_discounts == {}


def test_settings_are_per_marketplace_and_typed(store, clock):
    store.change_setting("ozon", "soinvest", True, "u")
    store.change_setting("ozon", "tolerance", Decimal("75.5"), "u")
    store.change_setting("ozon", "multi_qty_verified", True, "u")
    s = store.settings_at("ozon", at(23))
    assert s.soinvest is True and s.tolerance == Decimal("75.5") and s.multi_qty_verified is True
    assert isinstance(s.tolerance, Decimal)
    assert store.settings_at("ym", at(23)).soinvest is False


def test_unknown_setting_rejected(store):
    with pytest.raises(ValueError):
        store.change_setting("ozon", "nope", 1, "u")


def test_change_setting_writes_log_with_user(store):
    store.change_setting("ozon", "soinvest", True, "ann")
    ev = [e for e in store.events("ozon") if e["event"] == "setting_changed"]
    assert len(ev) == 1
    assert ev[0]["payload"]["user"] == "ann"
    assert ev[0]["payload"]["key"] == "soinvest"
    assert ev[0]["payload"]["value"] is True


def test_history_is_appended(store, clock):
    store.change_setting("ozon", "soinvest", True, "a")
    clock.t = at(11)
    store.change_setting("ozon", "soinvest", False, "b")
    assert store.settings_at("ozon", at(10, 30)).soinvest is True
    assert store.settings_at("ozon", at(12)).soinvest is False


def test_pause(store, clock):
    assert store.is_paused() is False
    assert store.paused_since() is None
    clock.t = at(14)
    store.set_paused(True, "ann")
    assert store.is_paused() is True
    assert store.paused_since() == at(14)
    store.set_paused(False, "ann")
    assert store.is_paused() is False
    assert store.paused_since() is None


def test_log(store):
    store.log("ozon", "1", "seen", {"a": Decimal("1.5")})
    ev = store.events("ozon", "1")
    assert ev[0]["event"] == "seen" and ev[0]["payload"] == {"a": "1.5"}
    assert ev[0]["ts"] == at(10)


def test_reserve_approved_state_and_in_state(store):
    store.upsert_new(make_order("1"))
    store.upsert_new(make_order("2"))
    store.set_state("ozon", "1", "RESERVE_APPROVED")
    assert store.get_state("ozon", "1") == "RESERVE_APPROVED"
    assert store.in_state("RESERVE_APPROVED") == [("ozon", "1")]
    assert store.pending() == [("ozon", "2")]


def test_outbox_survives_restart_and_journals_failure(tmp_path):
    s = Store(tmp_path / "o.db")
    i = s.queue_alert("system", {"text": "hello"})
    assert [e["event"] for e in s.events()] == ["alert_failed"]
    s2 = Store(tmp_path / "o.db")
    assert s2.outbox() == [(i, "system", {"text": "hello"})]
    s2.drop_alert(i)
    assert s2.outbox() == []


def test_order_campaign_roundtrip(store):
    import dataclasses

    from orderbot.domain import Order
    base = store.get_order  # noqa: F841
    o = Order("yandex", "77", [], datetime(2026, 11, 3, tzinfo=timezone.utc), None, "x",
              campaign="900000008")
    store.upsert_new(o)
    got = store.get_order("yandex", "77")
    assert got == o and got.campaign == "900000008"
    assert dataclasses.replace(o, campaign=None).campaign is None


def test_order_roundtrip_keeps_express_and_mp_warehouse():
    import json

    from orderbot.store import decode_order, encode_order
    o = Order("ozon", "1", [], datetime(2026, 11, 3, tzinfo=timezone.utc), None, "x",
              express=True, mp_warehouse="СЕВЕР EXPRESS")
    got = decode_order(encode_order(o))
    assert got.express is True and got.mp_warehouse == "СЕВЕР EXPRESS"
    d = json.loads(encode_order(o))
    del d["express"], d["mp_warehouse"]
    old = decode_order(json.dumps(d))
    assert old.express is False and old.mp_warehouse is None


def test_alert_messages_roundtrip(store):
    store.upsert_new(make_order())
    store.save_alert_message("ozon", "1", 100, 11, "photo", "<b>a</b>")
    store.save_alert_message("ozon", "1", 200, 22, "text", "<b>b</b>")
    store.save_alert_message("ozon", "2", 100, 33, "text", "other")
    msgs = store.alert_messages("ozon", "1")
    assert [(m.chat_id, m.message_id, m.kind, m.html) for m in msgs] == [
        (100, 11, "photo", "<b>a</b>"), (200, 22, "text", "<b>b</b>")]
    assert store.alert_messages("ozon", "404") == []


def test_alert_messages_survive_reopen(tmp_path, clock):
    s1 = Store(tmp_path / "r.db", clock=clock)
    s1.save_alert_message("ozon", "1", 100, 11, "text", "x")
    s2 = Store(tmp_path / "r.db", clock=clock)
    assert [m.message_id for m in s2.alert_messages("ozon", "1")] == [11]


def test_set_state_if(store, clock):
    store.upsert_new(make_order())
    store.set_state("ozon", "1", "OBSERVED")
    clock.t = at(11)
    assert store.set_state_if("ozon", "1", {"OBSERVED"}, "CONFIRMED") is True
    assert store.set_state_if("ozon", "1", {"OBSERVED"}, "CONFIRMED") is False
    assert store.get_state("ozon", "1") == "CONFIRMED"
    assert store.set_state_if("ozon", "nope", {"OBSERVED"}, "CONFIRMED") is False
    with pytest.raises(ValueError):
        store.set_state_if("ozon", "1", {"CONFIRMED"}, "BOGUS")
    with pytest.raises(ValueError):
        store.set_state_if("ozon", "1", {"BOGUS"}, "CONFIRMED")


def test_cancelled_by_human_is_a_state(store):
    store.upsert_new(make_order())
    store.set_state("ozon", "1", "CANCELLED_BY_HUMAN")
    assert store.get_state("ozon", "1") == "CANCELLED_BY_HUMAN"


# ------------------------------------------------------------------- managers
def test_managers_are_added_listed_removed_and_survive_restart(tmp_path, clock):
    s = Store(tmp_path / "m.db", clock=clock)
    assert s.managers() == []
    assert s.add_manager(222, "Мария К @mk", "Анна") is True
    assert s.add_manager(222, "Мария К @mk", "Олег") is False      # already a manager
    m, = s.managers()
    assert (m.user_id, m.name, m.granted_by, m.ts) == (222, "Мария К @mk", "Анна", at(10))
    s2 = Store(tmp_path / "m.db", clock=clock)                     # after a restart
    assert [x.user_id for x in s2.managers()] == [222]
    assert s2.remove_manager(222) is True
    assert s2.remove_manager(222) is False
    assert s2.managers() == []


def test_role_request_is_open_until_granted_or_denied(store):
    assert store.open_role_request(5) is None
    store.log(None, None, "role_requested", {"user_id": 5, "name": "Пётр"})
    store.log(None, None, "role_requested", {"user_id": 6, "name": "Иван"})
    assert store.open_role_request(5) == {"user_id": 5, "name": "Пётр"}
    store.log(None, None, "role_denied", {"user_id": 5, "name": "Пётр", "by": "111 Анна"})
    assert store.open_role_request(5) is None
    assert store.open_role_request(6) == {"user_id": 6, "name": "Иван"}
    store.log(None, None, "role_requested", {"user_id": 5, "name": "Пётр"})   # asked again
    assert store.open_role_request(5) == {"user_id": 5, "name": "Пётр"}
    store.log(None, None, "role_granted", {"user_id": 5, "name": "Пётр", "by": "111 Анна"})
    assert store.open_role_request(5) is None


def test_order_roundtrip_keeps_promos():
    from datetime import datetime, timezone

    from orderbot.domain import Order, OrderLine
    from orderbot.store import decode_order, encode_order
    o = Order("ozon", "1", [OrderLine("A", 1, "СПБ", None, None, promos=["Промокод 5%"])],
              datetime(2026, 11, 6, tzinfo=timezone.utc), None, "x")
    assert decode_order(encode_order(o)).lines[0].promos == ["Промокод 5%"]


# ------------------------------------------------- stock sync
def test_stocksync_flag_default_off_and_journaled(tmp_path):
    s = Store(tmp_path / "x.db")
    assert s.get_stocksync() is False
    s.set_stocksync(True, "admin")
    assert s.get_stocksync() is True
    s.set_stocksync(False, "admin")
    assert s.get_stocksync() is False
    ev = [e for e in s.events() if e["event"] == "stocksync_changed"]
    assert [e["payload"] for e in ev] == [{"on": True, "user": "admin"},
                                          {"on": False, "user": "admin"}]


def test_has_event(tmp_path):
    s = Store(tmp_path / "x.db")
    assert not s.has_event("ozon", "1", "stock_synced")
    s.log("ozon", "1", "stock_synced", {})
    assert s.has_event("ozon", "1", "stock_synced")
    assert not s.has_event("ozon", "2", "stock_synced")


def test_alert_result_and_notes_are_kept(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_alert_message("ozon", "1", 5, 7, "text", "caption")
    m = s.alert_messages("ozon", "1")[0]
    assert (m.result, m.notes) == ("", "")
    s.set_alert_result("ozon", "1", "R")
    s.add_alert_note("ozon", "1", "N1")
    s.add_alert_note("ozon", "1", "N2")
    m = s.alert_messages("ozon", "1")[0]
    assert (m.html, m.result, m.notes) == ("caption", "R", "N1\nN2")


def test_old_alert_table_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(str(path))
    db.executescript("""CREATE TABLE alert_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, marketplace TEXT NOT NULL,
        order_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        kind TEXT NOT NULL, html TEXT NOT NULL, ts TEXT NOT NULL);
        INSERT INTO alert_messages (marketplace, order_id, chat_id, message_id, kind, html, ts)
        VALUES ('wb', '9', 1, 2, 'text', 'old', '2026-11-01T00:00:00+00:00');""")
    db.commit()
    db.close()
    s = Store(path)
    m = s.alert_messages("wb", "9")[0]
    assert (m.html, m.result, m.notes) == ("old", "", "")
    s.add_alert_note("wb", "9", "N")
    assert s.alert_messages("wb", "9")[0].notes == "N"


def test_stocksync_yandex_flag_separate(tmp_path):
    s = Store(tmp_path / "x.db")
    s.set_stocksync(True, "admin")
    assert s.get_stocksync() is True and s.get_stocksync("yandex") is False
    s.set_stocksync(True, "admin", "yandex")
    assert s.get_stocksync("yandex") is True
    ev = [e["payload"] for e in s.events() if e["event"] == "stocksync_changed"]
    assert ev[-1] == {"on": True, "user": "admin", "marketplace": "yandex"}


# ------------------------------------------------------------- Lamoda label orders
def test_lamoda_orders_are_kept_once_and_pending_listed(tmp_path):
    s = Store(tmp_path / "db")
    assert not s.lamoda_baseline_done() and not s.lamoda_known("RU1")
    s.lamoda_set_baseline(["RU0"])
    assert s.lamoda_baseline_done() and s.lamoda_known("RU0")
    s.lamoda_add("RU1", "pending", 990, 11, "text", warned=True, attempts=1)
    s.lamoda_add("RU1", "pending", 990, 12, "other")        # never twice
    [row] = s.lamoda_pending()
    assert (row.order_nr, row.message_id, row.text, row.warned, row.attempts) == \
        ("RU1", 11, "text", True, 1)
    assert s.lamoda_pending()[0].sent_files == []
    s.lamoda_update("RU1", sent_files=["A пакет.pdf", "A товар.pdf"], warned=False)
    assert s.lamoda_pending()[0].sent_files == ["A пакет.pdf", "A товар.pdf"]
    s.lamoda_update("RU1", state="done")
    assert s.lamoda_pending() == []
    s2 = Store(tmp_path / "db")
    assert s2.lamoda_known("RU1") and s2.lamoda_baseline_done()
    with pytest.raises(ValueError):
        s.lamoda_update("RU1", message_id=5)
    with pytest.raises(ValueError):
        s.lamoda_add("RU2", "posted")


def test_order_roundtrip_keeps_points():
    from datetime import datetime, timezone
    from decimal import Decimal

    from orderbot.domain import Order, OrderLine
    from orderbot.store import decode_order, encode_order
    o = Order("yandex", "1", [OrderLine("A", 1, "СПБ", Decimal("2760"), None,
                                        points=Decimal("1476"))],
              datetime(2026, 11, 9, tzinfo=timezone.utc), None, "x")
    assert decode_order(encode_order(o)).lines[0].points == Decimal("1476")
    o.lines[0].points = None
    assert decode_order(encode_order(o)).lines[0].points is None
