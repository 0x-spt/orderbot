"""Two seller companies: «Альфа» (account "main", today's keys) and
«Бета» (account "second", SECOND_* keys). Same 1C and warehouses, shared settings."""
import logging
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx

from orderbot.alerts import format_caption
from orderbot.config import Config, load_config
from orderbot.domain import Action, Decision, Reason
from orderbot.human_actions import HumanActions
from orderbot.main import build_marketplaces
from orderbot.marketplaces.ozon import OzonMarketplace
from orderbot.marketplaces.yandex import YandexMarket
from orderbot.pipeline import Pipeline
from orderbot.store import Store, decode_order, encode_order
from tests.test_pipeline import Clock, FakeMp, FakeNotifier, FakeOneC, at, make_order, make_read


def second_order(oid="D1", mp="ozon", **kw):
    o = make_order(oid, mp, **kw)
    o.account = "second"
    return o


def two_ozons(main_orders=(), second_orders=()):
    a = FakeMp("ozon", main_orders)
    b = FakeMp("ozon", second_orders)
    b.account = "second"
    return a, b


# --------------------------------------------------------------------- config
def test_config_reads_second_keys_and_names(tmp_path):
    env = {"WAREHOUSES_FILE": "", "SECOND_OZON_CLIENT_ID": "9", "SECOND_OZON_API_KEY": "k2",
           "SECOND_YM_TOKEN": "t2", "SECOND_YM_BUSINESS_ID": "77"}
    c = load_config(env, warehouses_path=tmp_path / "none.yaml")
    assert (c.second_ozon_client_id, c.second_ozon_api_key) == ("9", "k2")
    assert (c.second_ym_token, c.second_ym_business_id) == ("t2", "77")
    assert c.company_names == {"main": "Альфа", "second": "Бета"}
    c = load_config({"COMPANY_NAME": "Гамма", "SECOND_COMPANY_NAME": "Бэ"},
                    warehouses_path=tmp_path / "none.yaml")
    assert c.company_names == {"main": "Гамма", "second": "Бэ"}


def test_config_reads_second_warehouse_sections(tmp_path):
    p = tmp_path / "w.yaml"
    p.write_text("ozon: {Склад: СПБ}\nozon_second: {Склад Д: СПБ}\n"
                 "yandex_second: {'555': СПБ}\nexpress: {ozon_second: [Склад Д]}\n",
                 encoding="utf-8")
    c = load_config({}, warehouses_path=p)
    assert c.warehouses["ozon_second"] == {"Склад Д": "СПБ"}
    assert c.warehouses["yandex_second"] == {"555": "СПБ"}
    assert c.express["ozon_second"] == {"Склад Д"}


# --------------------------------------------------------------------- order JSON
def test_order_json_keeps_account_and_old_rows_are_main():
    o = second_order()
    assert decode_order(encode_order(o)).account == "second"
    import json
    d = json.loads(encode_order(make_order()))
    d.pop("account")
    assert decode_order(json.dumps(d)).account == "main"
    assert make_order().account == "main"


# --------------------------------------------------------------------- adapters
async def test_ozon_orders_carry_their_account():
    m = OzonMarketplace("c", "k", warehouses={}, retry_delay=0, account="second")
    assert m.account == "second" and OzonMarketplace("c", "k").account == "main"
    p = {"posting_number": "P-1", "status": "awaiting_packaging", "products": [
        {"offer_id": "A", "quantity": 1, "sku": 1}], "delivery_method": {"warehouse": "W"}}
    with respx.mock() as r:
        r.post("https://api-seller.ozon.ru/v4/posting/fbs/unfulfilled/list").mock(
            return_value=httpx.Response(200, json={"postings": [p]}))
        r.post("https://api-seller.ozon.ru/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json={"result": []}))
        [o] = await m.new_orders()
    assert o.account == "second"


async def test_yandex_orders_carry_their_account(tmp_path):
    m = YandexMarket("t", None, warehouses={"5": "СПБ"}, labels_dir=tmp_path,
                     retry_delay=0, account="second")
    assert YandexMarket("t", "5", labels_dir=tmp_path).account == "main"
    o = {"id": 1, "status": "PROCESSING", "substatus": "STARTED", "items": [
        {"offerId": "A", "count": 1}]}
    with respx.mock() as r:
        r.get("https://api.partner.market.yandex.ru/v2/campaigns/5/orders").mock(
            return_value=httpx.Response(200, json={"orders": [o]}))
        [order] = await m.new_orders()
    assert order.account == "second"


# --------------------------------------------------------------------- build_marketplaces
def test_build_without_second_keys_is_as_today(caplog):
    cfg = Config(ozon_client_id="1", ozon_api_key="k", ym_token="t",
                 warehouses={"yandex": {"5": "СПБ"}})
    with caplog.at_level(logging.INFO, logger="orderbot"):
        mps = build_marketplaces(cfg)
    assert [(m.name, m.account) for m in mps] == [("ozon", "main"), ("yandex", "main")]
    infos = [r for r in caplog.records if "Бета" in r.getMessage()]
    assert len(infos) == 1 and infos[0].levelno == logging.INFO
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_build_with_second_keys_and_sections():
    cfg = Config(ozon_client_id="1", ozon_api_key="k",
                 second_ozon_client_id="2", second_ozon_api_key="k2",
                 second_ym_token="t2", second_ym_business_id="88", ozon_paid_field="f",
                 ym_paid_field="buyerPrice",
                 warehouses={"ozon": {"W": "СПБ"}, "ozon_second": {"WD": "МСК"},
                             "yandex_second": {"9": "МСК"}},
                 express={"ozon_second": {"WD"}, "yandex_second": {"9"}})
    mps = build_marketplaces(cfg)
    assert [(m.name, m.account) for m in mps] == [
        ("ozon", "main"), ("ozon", "second"), ("yandex", "second")]
    oz, yd = mps[1], mps[2]
    assert oz.warehouses == {"WD": "МСК"} and oz.express == {"WD"} and oz.paid_field == "f"
    assert oz._http.headers["Client-Id"] == "2"
    assert yd.campaigns == ["9"] and yd.business_id == "88" and yd.express == {"9"}
    assert yd._http.headers["Api-Key"] == "t2" and yd.paid_field == "buyerPrice"


def test_build_second_ozon_without_section_runs_unmapped_and_warns(caplog):
    cfg = Config(second_ozon_client_id="2", second_ozon_api_key="k2", second_ym_token="t2",
                 warehouses={"ozon": {"W": "СПБ"}, "yandex": {"5": "СПБ"}})
    with caplog.at_level(logging.INFO, logger="orderbot"):
        mps = build_marketplaces(cfg)
    # Ozon runs with no mapping: its orders go to a person («склад не сопоставлен»);
    # Yandex has no campaigns without its section: not connected, as for Альфа today
    assert [(m.name, m.account, m.warehouses) for m in mps] == [("ozon", "second", {})]
    warn = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "ozon_second" in warn and "yandex_second" in warn


# --------------------------------------------------------------------- pipeline routing
@pytest.fixture
def clock():
    return Clock(at(10))


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "s.db", clock=clock)


async def test_each_order_goes_to_its_own_company_adapter(store, clock):
    a, b = two_ozons([make_order("M1")], [second_order("D1")])
    onec = FakeOneC()
    pipe = Pipeline(store, [a, b], onec, FakeNotifier(), clock=clock)
    store.set_mode("ozon", "confirm", "t")
    await pipe.tick()
    assert a.confirmed == ["M1"] and b.confirmed == ["D1"]
    assert ("is_awaiting", "D1") not in a.calls and ("is_awaiting", "M1") not in b.calls
    assert store.get_order("ozon", "D1").account == "second"


async def test_ready_quantities_are_summed_over_companies(store, clock):
    """Same 1C stock: units assembled for either company are taken out before the check."""
    a, b = two_ozons([make_order("M1", qty=1)])
    a.ready_qty = {("A1", "СПБ"): 4}
    b.ready_qty = {("A1", "СПБ"): 5}
    onec = FakeOneC({"A1": make_read(on_hand=10)})
    n = FakeNotifier()
    pipe = Pipeline(store, [a, b], onec, n, clock=clock)
    store.set_mode("ozon", "confirm", "t")
    await pipe.tick()
    # 10 on hand - 9 assembled = 1 free: enough for one
    assert a.confirmed == ["M1"]
    b.ready_qty = {("A1", "СПБ"): 6}
    a.orders.append(make_order("M2"))
    await pipe.tick()
    # 10 - 4 - 6 = 0 free: not enough
    assert "M2" not in a.confirmed
    assert store.get_decision("ozon", "M2").action == Action.ESCALATE
    assert (("A1", Reason.NO_STOCK) in store.get_decision("ozon", "M2").reasons)


async def test_ready_of_both_companies_reach_the_rule(store, clock):
    a, b = two_ozons([make_order("M1")])
    a.ready_qty = {("A1", "СПБ"): 2}
    b.ready_qty = {("A1", "СПБ"): 3, ("B9", "МСК"): 1}
    pipe = Pipeline(store, [a, b], FakeOneC(), FakeNotifier(), clock=clock)
    ready, healthy = await pipe._poll()
    assert ready == {("A1", "СПБ"): 5, ("B9", "МСК"): 1}
    assert healthy == {("ozon", "main"), ("ozon", "second")}


async def test_failed_company_holds_only_its_orders_and_blocks_confirm(store, clock):
    a, b = two_ozons([make_order("M1")], [second_order("D1", deadline=at(12))])
    b.fail_poll = True
    n = FakeNotifier()
    names = {"main": "Альфа", "second": "Бета"}
    pipe = Pipeline(store, [a, b], FakeOneC(), n, clock=clock, companies=names)
    store.upsert_new(second_order("D1", deadline=at(12)))
    store.set_mode("ozon", "confirm", "t")
    for _ in range(3):
        await pipe.tick()
    # Бета's list is unknown: Альфа is not confirmed either (shared stock)
    assert a.confirmed == [] and b.confirmed == []
    assert store.get_state("ozon", "D1") == "ESCALATED"
    held = store.get_decision("ozon", "D1").reasons[0][1]
    assert held == Reason.API_UNAVAILABLE
    [down] = [t for t in n.system if "API не отвечает" in t]
    assert "Бета" in down and "Альфа" not in down
    b.fail_poll = False
    await pipe.tick()
    assert a.confirmed == ["M1"]


async def test_api_down_and_up_texts_name_marketplace_and_company(store, clock):
    a = FakeMp("ozon", [])
    a.fail_poll = True
    n = FakeNotifier()
    pipe = Pipeline(store, [a], FakeOneC(), n, clock=clock,
                    companies={"main": "Альфа", "second": "Бета"})
    for _ in range(3):
        await pipe.tick()
    [down] = [t for t in n.system if "API не отвечает" in t]
    assert down.startswith("🔴 <b>Озон · Альфа — API не отвечает</b>\n")
    assert "Новые заказы этой площадки не проверяются" in down
    a.fail_poll = False
    await pipe.tick()
    assert n.system[-1].startswith("🟢 <b>Озон · Альфа — API снова отвечает</b>\nНе отвечал ")


async def test_order_of_a_company_without_adapter_waits(store, clock):
    a = FakeMp("ozon", [])
    store.upsert_new(second_order("D1"))
    pipe = Pipeline(store, [a], FakeOneC(), FakeNotifier(), clock=clock)
    await pipe.tick()
    assert store.get_state("ozon", "D1") == "NEW" and a.calls == []


async def test_recover_asks_the_company_adapter(store, clock):
    a, b = two_ozons()
    store.upsert_new(second_order("D1"))
    store.set_state("ozon", "D1", "CONFIRMING")
    b.ready["D1"] = True
    pipe = Pipeline(store, [b, a], FakeOneC(), FakeNotifier(), clock=clock)
    await pipe.recover()
    assert store.get_state("ozon", "D1") == "CONFIRMED"
    assert a.calls == [] and b.calls == [("is_ready", "D1")]


async def test_photo_url_comes_from_the_company_adapter(store, clock, tmp_path):
    a, b = two_ozons([], [second_order("D1")])
    asked = []

    async def url_a(article):
        asked.append("main")

    async def url_b(article):
        asked.append("second")

    a.photo_url, b.photo_url = url_a, url_b

    class Photos:
        async def get(self, mp, article, url_fn):
            await url_fn(article)
            return None

        def purge(self):
            pass

    pipe = Pipeline(store, [b, a], FakeOneC(), FakeNotifier(), clock=clock,
                    product_photos=Photos())
    await pipe.tick()
    assert asked == ["second"]


async def test_resolve_reserve_checks_health_of_the_order_company(store, clock):
    a, b = two_ozons()
    store.upsert_new(second_order("D1"))
    store.set_state("ozon", "D1", "AWAITING_RESERVE")
    store.set_mode("ozon", "confirm", "t")
    b.fail_poll = True          # the order's own company is down: not touched at all
    pipe = Pipeline(store, [a, b], FakeOneC(), FakeNotifier(), clock=clock)
    await pipe.resolve_reserve("ozon", "D1", True, "u")
    assert b.calls == [] and store.get_state("ozon", "D1") == "RESERVE_APPROVED"
    b.fail_poll, a.fail_poll = False, True   # Альфа down: no confirm either (shared stock)
    await pipe.tick()
    assert b.confirmed == [] and a.calls == []
    a.fail_poll = False
    await pipe.tick()
    assert b.confirmed == ["D1"] and a.calls == []


async def test_outbox_alert_keeps_the_company(store, clock):
    a, b = two_ozons()
    n = FakeNotifier()
    pipe = Pipeline(store, [a, b], FakeOneC(), n, clock=clock)
    o = second_order("D1")
    store.upsert_new(o)
    pipe._queue("order", o, Decision(Action.ESCALATE, [("A1", Reason.NO_STOCK)], {}), [],
                False, True, {})
    await pipe._flush_outbox()
    assert n.alerts[0][0].account == "second"


# --------------------------------------------------------------------- human buttons
class Btn:
    name = "ozon"

    def __init__(self, account):
        self.account = account
        self.confirmed = []

    async def is_awaiting(self, order):
        return True

    async def confirm(self, order):
        self.confirmed.append(order.order_id)

    async def cancel_by_human(self, order):
        self.confirmed.append(("cancel", order.order_id))


async def test_buttons_use_the_order_company_adapter(store, clock):
    a, b = Btn("main"), Btn("second")
    store.upsert_new(second_order("D1"))
    store.set_state("ozon", "D1", "OBSERVED")
    store.upsert_new(make_order("M1"))
    store.set_state("ozon", "M1", "OBSERVED")
    pipe = Pipeline(store, [a, b], None, None, clock=clock)
    acts = HumanActions(store, {("ozon", "main"): a, ("ozon", "second"): b}, pipe)
    assert (await acts.collect("ozon", "D1", "u")).kind == "collected"
    assert (await acts.cancel("ozon", "M1", "u")).kind == "cancelled"
    assert b.confirmed == ["D1"] and a.confirmed == [("cancel", "M1")]


async def test_buttons_without_the_company_adapter_are_refused(store, clock):
    a = Btn("main")
    store.upsert_new(second_order("D1"))
    store.set_state("ozon", "D1", "OBSERVED")
    acts = HumanActions(store, {("ozon", "main"): a}, Pipeline(store, [a], None, None))
    assert (await acts.collect("ozon", "D1", "u")).kind == "not_allowed_state"
    assert a.confirmed == []


# --------------------------------------------------------------------- alert & status
def _dec():
    return Decision(Action.CONFIRM, [], {"A1": Decimal("1")})


def test_caption_names_the_company_after_the_marketplace():
    cap = format_caption(make_order(), _dec(), None, False, at(10), company="Альфа")
    assert "✅ <b>Можно собирать</b> · Озон · Альфа" in cap
    cap = format_caption(second_order(), _dec(), None, False, at(10), company="Бета")
    assert "· Озон · Бета" in cap


async def test_notifier_caption_always_shows_the_company(tmp_path):
    from orderbot.bot import TelegramNotifier

    sent = []

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append(text)
            return SimpleNamespace(message_id=1)

    s = Store(tmp_path / "n.db")
    n = TelegramNotifier(Bot(), [1], s, companies={"main": "Альфа", "second": "Diff"})
    await n.order_alert(make_order(), _dec(), [], False, True)
    await n.order_alert(second_order(), _dec(), [], False, True)
    assert "· Озон · Альфа" in sent[0] and "· Озон · Diff" in sent[1]
    n2 = TelegramNotifier(Bot(), [1], s)
    await n2.order_alert(make_order(), _dec(), [], False, True)
    assert "· Озон · Альфа" in sent[2]


async def test_status_lists_adapters_per_company(store):
    from orderbot.bot import BotController

    ctl = BotController(store, None, {1}, ["ozon", "yandex", "wb"],
                        companies={"Альфа": ["ozon", "yandex", "wb"], "Бета": ["ozon"]})
    text = await ctl.on_text(1, "/status")
    assert "Альфа: ozon, yandex, wb" in text and "Бета: ozon" in text


# --------------------------------------------------------------------- recon
async def test_recon_lists_second_ozon_warehouses_and_yandex_campaigns(monkeypatch):
    from orderbot import recon

    cfg = Config(second_ozon_client_id="2", second_ozon_api_key="k2", second_ym_token="t2",
                 warehouses={"ozon_second": {"WD": "МСК"}, "yandex_second": {"9": "МСК"}})
    monkeypatch.setattr(recon, "load_config", lambda: cfg)
    with respx.mock() as r:
        wl = r.post("https://api-seller.ozon.ru/v1/warehouse/list").mock(
            return_value=httpx.Response(200, json={"result": [
                {"warehouse_id": 11, "name": "WD"}, {"warehouse_id": 12, "name": "Другой"}]}))
        cl = r.get("https://api.partner.market.yandex.ru/v2/campaigns").mock(
            return_value=httpx.Response(200, json={"campaigns": [
                {"id": 9, "domain": "shop-d", "placementType": "FBS"},
                {"id": 10, "domain": "shop-e", "placementType": "FBS"}],
                "pager": {"pagesCount": 1}}))
        out = await recon.warehouses_text(cfg, "second")
    assert wl.calls[0].request.headers["Client-Id"] == "2"
    assert cl.calls[0].request.headers["Api-Key"] == "t2"
    assert "'WD'" in out and "'МСК'" in out and "'Другой'" in out
    assert "id=9" in out and "id=10" in out and "shop-e" in out
    assert "ozon_second" in out and "yandex_second" in out


async def test_recon_ozon_warehouses_fall_back_to_names_in_orders():
    """The key may lack the warehouse-list role (seen live, HTTP 403): the names of
    the warehouses in the current orders are shown instead."""
    from orderbot import recon

    cfg = Config(ozon_client_id="1", ozon_api_key="k", warehouses={"ozon": {"W": "СПБ"}})
    p = {"posting_number": "P-1", "status": "awaiting_packaging", "products": [],
         "delivery_method": {"warehouse": "W2"}}
    with respx.mock() as r:
        r.post("https://api-seller.ozon.ru/v1/warehouse/list").mock(
            return_value=httpx.Response(403, json={"code": 7}))
        r.post("https://api-seller.ozon.ru/v4/posting/fbs/unfulfilled/list").mock(
            return_value=httpx.Response(200, json={"postings": [p]}))
        out = await recon.warehouses_text(cfg, "main")
    assert "'W2'" in out and "HTTP 403" in out


async def test_recon_without_keys_of_the_company_says_so():
    from orderbot import recon

    out = await recon.warehouses_text(Config(), "second")
    assert "SECOND_OZON_CLIENT_ID" in out and "SECOND_YM_TOKEN" in out


# ------------------------------------------------- stock sync
def test_stock_sync_gets_only_main_adapters_and_sections(tmp_path):
    from orderbot.main import build_stock_sync
    cfg = Config(ozon_client_id="1", ozon_api_key="k",
                 second_ozon_client_id="2", second_ozon_api_key="k2",
                 warehouses={"ozon": {"W": "СПБ"}, "ozon_second": {"WD": "СПБ"}})
    mps = build_marketplaces(cfg)
    sync = build_stock_sync(cfg, mps, Store(tmp_path / "s.db"), None)
    assert {n: m.account for n, m in sync._mps.items()} == {"ozon": "main"}
    assert sync._mps["ozon"] is mps[0]
