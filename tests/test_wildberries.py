from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from orderbot.domain import Order, OrderLine
from orderbot.marketplaces.wildberries import WbConfirmError, WbListError, WildberriesMarketplace

BASE = "https://marketplace-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def wb_order(oid=101, article="ART-A", nm=555, wid=9001, price=150000, sale=180000):
    return {"id": oid, "article": article, "nmId": nm, "chrtId": 1, "warehouseId": wid,
            "createdAt": "2026-11-05T09:30:00Z", "price": price, "salePrice": sale,
            "convertedPrice": price, "currencyCode": 643, "skus": ["2000000000001"]}


def make(paid_field="salePrice", wh=None):
    return WildberriesMarketplace("tok", warehouses={"9001": "Склад 1С"} if wh is None else wh,
                                  paid_field=paid_field, retry_delay=0, clock=lambda: NOW)


def mock_status(r, pairs):
    return r.post(f"{BASE}/api/v3/orders/status").mock(return_value=httpx.Response(200, json={
        "orders": [{"id": i, "supplierStatus": s, "wbStatus": w} for i, (s, w) in pairs.items()]}))


def mock_brand(r, nm=555, brand="BrandX"):
    return r.post(f"{CONTENT}/content/v2/get/cards/list").mock(return_value=httpx.Response(
        200, json={"cards": [{"nmID": nm, "brand": brand}]}))


async def test_new_orders_one_line_qty_one_paid_in_rubles():
    with respx.mock() as r:
        new = r.get(f"{BASE}/api/v3/orders/new").mock(
            return_value=httpx.Response(200, json={"orders": [wb_order()]}))
        mock_status(r, {101: ("new", "waiting")})
        mock_brand(r)
        orders = await make().new_orders()
    assert new.calls[0].request.headers["Authorization"] == "tok"
    [o] = orders
    assert o.marketplace == "wb" and o.order_id == "101" and o.deadline is None
    assert o.created_at == datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    [ln] = o.lines
    assert (ln.article, ln.qty, ln.warehouse, ln.paid_total, ln.brand) == (
        "ART-A", 1, "Склад 1С", Decimal("1800"), "BrandX")


@pytest.mark.parametrize("field,expected", [("price", Decimal("1500")),
                                            ("salePrice", Decimal("1800")), (None, None)])
async def test_paid_field_choice(field, expected):
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(
            return_value=httpx.Response(200, json={"orders": [wb_order()]}))
        mock_status(r, {101: ("new", "waiting")})
        mock_brand(r)
        [o] = await make(paid_field=field).new_orders()
    assert o.lines[0].paid_total == expected


def test_unknown_paid_field_refused():
    with pytest.raises(ValueError):
        make(paid_field="buyerPrice")


async def test_new_orders_skip_tasks_not_waiting():
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(return_value=httpx.Response(200, json={
            "orders": [wb_order(101), wb_order(102), wb_order(103)]}))
        mock_status(r, {101: ("new", "waiting"), 102: ("confirm", "waiting"),
                        103: ("new", "declined_by_client")})
        mock_brand(r)
        orders = await make().new_orders()
    assert [o.order_id for o in orders] == ["101"]


async def test_unmapped_warehouse_is_none():
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(
            return_value=httpx.Response(200, json={"orders": [wb_order(wid=1)]}))
        mock_status(r, {101: ("new", "waiting")})
        mock_brand(r)
        [o] = await make().new_orders()
    assert o.lines[0].warehouse is None


async def test_brand_unknown_when_token_lacks_content_scope():
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(return_value=httpx.Response(200, json={
            "orders": [wb_order(101, nm=1), wb_order(102, nm=2)]}))
        mock_status(r, {101: ("new", "waiting"), 102: ("new", "waiting")})
        content = r.post(f"{CONTENT}/content/v2/get/cards/list").mock(
            return_value=httpx.Response(401))
        orders = await make().new_orders()
    assert [o.lines[0].brand for o in orders] == [None, None]
    assert content.call_count == 1


async def test_ready_quantities_count_assembled_tasks():
    with respx.mock() as r:
        lst = r.get(f"{BASE}/api/v3/orders").mock(return_value=httpx.Response(200, json={
            "next": 7, "orders": [wb_order(1), wb_order(2), wb_order(3, article="ART-B"),
                                  wb_order(4), wb_order(5, wid=1)]}))
        mock_status(r, {1: ("confirm", "waiting"), 2: ("confirm", "sorted"),
                        3: ("confirm", "waiting"), 4: ("complete", "sold"),
                        5: ("confirm", "waiting")})
        totals = await make().ready_quantities()
    assert totals == {("ART-A", "Склад 1С"): 2, ("ART-B", "Склад 1С"): 1}
    q = lst.calls[0].request.url.params
    assert q["limit"] == "1000" and q["next"] == "0"
    assert int(q["dateTo"]) - int(q["dateFrom"]) == 30 * 86400


async def test_list_paginates_and_refuses_stuck_cursor(monkeypatch):
    import orderbot.marketplaces.wildberries as wbmod
    monkeypatch.setattr(wbmod, "LIST_LIMIT", 2)
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders").mock(side_effect=[
            httpx.Response(200, json={"next": 10, "orders": [wb_order(1), wb_order(2)]}),
            httpx.Response(200, json={"next": 11, "orders": [wb_order(3)]})])
        assert [o["id"] for o in await make()._recent_raw()] == [1, 2, 3]
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders").mock(side_effect=[
            httpx.Response(200, json={"next": 10, "orders": [wb_order(1), wb_order(2)]}),
            httpx.Response(200, json={"next": 10, "orders": [wb_order(3), wb_order(4)]})])
        with pytest.raises(WbListError):
            await make()._recent_raw()


def order(oid="101"):
    return Order("wb", oid, [OrderLine("ART-A", 1, "Склад 1С", Decimal("1800"), None)],
                 NOW, None, "new/waiting")


@pytest.mark.parametrize("st,awaiting,ready", [
    (("new", "waiting"), True, False), (("confirm", "waiting"), False, True),
    (("complete", "sold"), False, True), (("new", "declined_by_client"), False, False)])
async def test_awaiting_and_ready(st, awaiting, ready):
    with respx.mock() as r:
        mock_status(r, {101: st})
        m = make()
        assert await m.is_awaiting(order()) is awaiting
        assert await m.is_ready(order()) is ready


async def test_confirm_creates_supply_named_after_order():
    import json
    with respx.mock() as r:
        sup = r.post(f"{BASE}/api/v3/supplies").mock(
            return_value=httpx.Response(201, json={"id": "WB-GI-1234"}))
        add = r.patch(f"{BASE}/api/marketplace/v3/supplies/WB-GI-1234/orders").mock(
            return_value=httpx.Response(204))
        await make().confirm(order("5000000001"))
    assert json.loads(sup.calls[0].request.content) == {"name": "5000000001"}
    assert json.loads(add.calls[0].request.content) == {"orders": [5000000001]}
    assert sup.call_count == 1 and add.call_count == 1


async def test_confirm_failure_on_add_mentions_supply_and_does_not_retry():
    with respx.mock() as r:
        r.post(f"{BASE}/api/v3/supplies").mock(
            return_value=httpx.Response(201, json={"id": "WB-GI-1234"}))
        add = r.patch(f"{BASE}/api/marketplace/v3/supplies/WB-GI-1234/orders").mock(
            return_value=httpx.Response(500))
        with pytest.raises(WbConfirmError, match="WB-GI-1234"):
            await make().confirm(order())
    assert add.call_count == 1


async def test_confirm_failure_on_create_does_not_retry():
    with respx.mock() as r:
        sup = r.post(f"{BASE}/api/v3/supplies").mock(return_value=httpx.Response(500))
        with pytest.raises(WbConfirmError, match="may still have been created"):
            await make().confirm(order())
    assert sup.call_count == 1


async def test_cancel_by_human():
    from orderbot.marketplaces.wildberries import WbCancelError
    with respx.mock() as r:
        c = r.patch(f"{BASE}/api/v3/orders/101/cancel").mock(return_value=httpx.Response(204))
        await make().cancel_by_human(order())
        assert c.call_count == 1
        c.mock(return_value=httpx.Response(500))
        with pytest.raises(WbCancelError):
            await make().cancel_by_human(order())
        assert c.call_count == 2


async def test_reads_retry_on_5xx():
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(side_effect=[
            httpx.Response(500), httpx.Response(200, json={"orders": []})])
        assert await make().new_orders() == []


async def test_confirm_failure_keeps_supply_id_in_the_short_telegram_text():
    """The Telegram line shows the error cut to ERROR_LEN: the empty supply must stay in it."""
    from orderbot.human_actions import _short
    with respx.mock() as r:
        r.post(f"{BASE}/api/v3/supplies").mock(
            return_value=httpx.Response(201, json={"id": "WB-GI-123456789"}))
        r.patch(f"{BASE}/api/marketplace/v3/supplies/WB-GI-123456789/orders").mock(
            return_value=httpx.Response(500, text="x" * 300))
        with pytest.raises(WbConfirmError) as e:
            await make().confirm(order("5000000001"))
    assert "WB-GI-123456789" in _short(e.value)


WB_403 = "WB: нет прав на запись (403) — нужен токен без «Только на чтение»"
WB_401 = "WB: токен не подходит (401)"


@pytest.mark.parametrize("status, text", [(403, WB_403), (401, WB_401)])
async def test_confirm_with_read_only_token_says_so_plainly(status, text):
    """The live case: POST /api/v3/supplies -> 403 because the token is read-only."""
    from orderbot.human_actions import ERROR_LEN, _short
    with respx.mock() as r:
        r.post(f"{BASE}/api/v3/supplies").mock(return_value=httpx.Response(
            status, json={"title": "access denied", "detail": "read-only token"}))
        with pytest.raises(WbConfirmError) as e:
            await make().confirm(order("5000000001"))
    assert _short(e.value, "wb") == text and len(text) <= ERROR_LEN


@pytest.mark.parametrize("status, text", [(403, WB_403), (401, WB_401)])
async def test_human_drop_with_bad_token_says_so_plainly(status, text):
    from orderbot.human_actions import _short
    with respx.mock() as r:
        r.patch(f"{BASE}/api/v3/orders/5000000001/cancel").mock(
            return_value=httpx.Response(status))
        with pytest.raises(Exception) as e:
            await make().cancel_by_human(order("5000000001"))
    assert _short(e.value, "wb") == text


async def test_supply_id_stays_in_other_wb_errors_with_marketplace_given():
    from orderbot.human_actions import _short
    with respx.mock() as r:
        r.post(f"{BASE}/api/v3/supplies").mock(
            return_value=httpx.Response(201, json={"id": "WB-GI-123456789"}))
        r.patch(f"{BASE}/api/marketplace/v3/supplies/WB-GI-123456789/orders").mock(
            return_value=httpx.Response(403))
        with pytest.raises(WbConfirmError) as e:
            await make().confirm(order("5000000001"))
    # the supply exists and is empty: the person must see its id, whatever the status
    assert "WB-GI-123456789" in _short(e.value, "wb")


# --- product photo for the order message ---------------------------


@pytest.mark.parametrize("photos, expected", [
    ([{"big": "https://basket-1.wbbasket.ru/big/1.webp",
       "c516x688": "https://basket-1.wbbasket.ru/c516x688/1.webp"}],
     "https://basket-1.wbbasket.ru/c516x688/1.webp"),
    ([{"big": "https://basket-1.wbbasket.ru/big/1.webp"}], "https://basket-1.wbbasket.ru/big/1.webp"),
    ([], None),
])
async def test_photo_url_from_the_content_card(photos, expected):
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(
            return_value=httpx.Response(200, json={"orders": [wb_order()]}))
        mock_status(r, {101: ("new", "waiting")})
        content = r.post(f"{CONTENT}/content/v2/get/cards/list").mock(
            return_value=httpx.Response(200, json={"cards": [
                {"nmID": 555, "brand": "BrandX", "vendorCode": "ART-A", "photos": photos}]}))
        m = make()
        await m.new_orders()
        assert await m.photo_url("ART-A") == expected
        assert await m.photo_url("ART-UNSEEN") is None    # no nmId known: nothing asked
    assert content.call_count == 1


async def test_photo_url_when_token_lacks_content_scope():
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(
            return_value=httpx.Response(200, json={"orders": [wb_order()]}))
        mock_status(r, {101: ("new", "waiting")})
        r.post(f"{CONTENT}/content/v2/get/cards/list").mock(return_value=httpx.Response(403))
        m = make()
        await m.new_orders()
        assert await m.photo_url("ART-A") is None


# ------------------------------------------------- stock sync
import json  # noqa: E402

from orderbot.marketplaces.base import StockAt, StockError  # noqa: E402


def cards(*sizes, vendor="ART-A"):
    return {"cards": [{"nmID": 555, "vendorCode": vendor,
                       "sizes": [{"chrtID": c, "skus": ["200"]} for c in sizes]}]}


async def test_stock_read_finds_chrt_id_by_article_and_reads_amount():
    with respx.mock() as r:
        card = r.post(f"{CONTENT}/content/v2/get/cards/list").mock(
            return_value=httpx.Response(200, json=cards(777)))
        st = r.post(f"{BASE}/api/v3/stocks/9001").mock(return_value=httpx.Response(
            200, json={"stocks": [{"sku": "200", "chrtId": 777, "amount": 5}]}))
        got = await make().stock_read("9001", "ART-A")
    assert json.loads(card.calls[0].request.content)["settings"]["filter"]["textSearch"] == "ART-A"
    assert json.loads(st.calls[0].request.content) == {"chrtIds": [777]}
    assert got == StockAt(count=5, ref=777, reserved=0)


async def test_stock_read_absent_is_none_and_card_is_cached():
    with respx.mock() as r:
        card = r.post(f"{CONTENT}/content/v2/get/cards/list").mock(
            return_value=httpx.Response(200, json=cards(777)))
        st = r.post(f"{BASE}/api/v3/stocks/9001").mock(
            return_value=httpx.Response(200, json={"stocks": []}))
        mp = make()
        assert await mp.stock_read("9001", "ART-A") is None
        assert await mp.stock_read("9001", "ART-A") is None
    assert card.call_count == 1
    assert json.loads(st.calls[0].request.content) == {"chrtIds": [777]}


async def test_seen_order_chrt_does_not_bypass_size_check():
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(return_value=httpx.Response(
            200, json={"orders": [wb_order()]}))
        mock_status(r, {101: ("new", "waiting")})
        mock_brand(r)
        mp = make()
        await mp.new_orders()
        r.post(f"{CONTENT}/content/v2/get/cards/list").mock(
            return_value=httpx.Response(200, json=cards(1, 2)))
        st = r.post(f"{BASE}/api/v3/stocks/9001").mock(
            return_value=httpx.Response(200, json={"stocks": []}))
        for _ in range(2):    # the cached card answer is checked again
            with pytest.raises(StockError, match="размер"):
                await mp.stock_read("9001", "ART-A")
    assert st.call_count == 0


async def test_stock_read_other_vendor_code_or_many_sizes():
    with respx.mock() as r:
        r.post(f"{CONTENT}/content/v2/get/cards/list").mock(
            return_value=httpx.Response(200, json=cards(777, vendor="ART-AB")))
        assert await make().stock_read("9001", "ART-A") is None
        r.post(f"{CONTENT}/content/v2/get/cards/list").mock(
            return_value=httpx.Response(200, json=cards(1, 2)))
        with pytest.raises(StockError, match="размер"):
            await make().stock_read("9001", "ART-A")


async def test_stock_write_exact_body_once():
    with respx.mock() as r:
        route = r.put(f"{BASE}/api/v3/stocks/9001").mock(return_value=httpx.Response(204))
        await make().stock_write("9001", "ART-A", 777, 4)
        r.put(f"{BASE}/api/v3/stocks/9001").mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await make().stock_write("9001", "ART-A", 777, 4)
    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content) == {
        "stocks": [{"chrtId": 777, "amount": 4}]}


class FakeFx:
    names = {"51": "AMD"}

    def __init__(self, rate):
        self.rate, self.calls = rate, []

    async def to_rub(self, amount, code):
        self.calls.append((amount, code))
        return None if self.rate is None else (amount * self.rate).quantize(Decimal("0.01"))

    def name(self, code):
        return self.names.get(str(code), str(code))


def dram_order(converted=514297, conv_code=643):
    o = wb_order(price=2240000, sale=None)
    o.update(currencyCode=51, convertedPrice=converted, convertedCurrencyCode=conv_code)
    return o


async def _one(order, fx, field="price"):
    m = make(paid_field=field)
    m.fx = fx
    with respx.mock() as r:
        r.get(f"{BASE}/api/v3/orders/new").mock(
            return_value=httpx.Response(200, json={"orders": [order]}))
        mock_status(r, {101: ("new", "waiting")})
        mock_brand(r)
        [o] = await m.new_orders()
    return o.lines[0]


async def test_sale_in_drams_takes_wbs_own_ruble_value():
    """Live: currencyCode 51 (AMD), price 22 400, convertedPrice 5 142.97 in rubles."""
    ln = await _one(dram_order(), FakeFx(Decimal("0.21")))
    assert ln.paid_total == Decimal("5142.97") and ln.paid_foreign == "22 400 AMD"


async def test_sale_in_drams_without_a_ruble_value_uses_the_bank_rate():
    ln = await _one(dram_order(converted=None), FakeFx(Decimal("0.21")))
    assert ln.paid_total == Decimal("4704.00") and ln.paid_foreign == "22 400 AMD"


async def test_sale_in_drams_without_any_rate_has_no_price():
    ln = await _one(dram_order(converted=None), FakeFx(None))
    assert ln.paid_total is None and ln.paid_foreign == "22 400 AMD"


async def test_rubles_are_not_converted():
    fx = FakeFx(Decimal("0.21"))
    ln = await _one(wb_order(), fx)
    assert ln.paid_total == Decimal("1500") and ln.paid_foreign is None and fx.calls == []
