import json
from decimal import Decimal

import httpx
import pytest
import respx

from orderbot.marketplaces.yandex import YandexConfirmError, YandexMarket

BASE = "https://api.partner.market.yandex.ru"
C = "777"


def order_json(oid=5001, substatus="STARTED", items=None):
    return {"id": oid, "status": "PROCESSING", "substatus": substatus,
            "creationDate": "02-11-2026 10:00:00",
            "delivery": {"shipments": [{"shipmentDate": "03-11-2026"}]},
            "items": items or [
                {"offerId": "ART-A", "count": 2, "buyerPrice": 1500, "subsidies": []},
                {"offerId": "ART-B", "count": 1, "buyerPrice": 900}]}


def make(tmp_path, paid_field="buyerPrice", wh=None, business_id="55"):
    return YandexMarket("tok", C, warehouses={C: "Склад 1С"} if wh is None else wh,
                        paid_field=paid_field, business_id=business_id,
                        labels_dir=tmp_path / "labels", retry_delay=0)


def mock_orders(r, orders, **kw):
    return r.get(f"{BASE}/v2/campaigns/{C}/orders").mock(
        return_value=httpx.Response(200, json={"orders": orders, **kw}))


def mock_brands(r):
    return r.post(f"{BASE}/v2/businesses/55/offer-mappings").mock(
        return_value=httpx.Response(200, json={"result": {"offerMappings": [
            {"offer": {"offerId": "ART-A", "vendor": "BrandX"}}]}}))


async def test_new_orders_processing_started(tmp_path):
    with respx.mock() as r:
        route = mock_orders(r, [order_json()])
        mock_brands(r)
        orders = await make(tmp_path).new_orders()
    q = route.calls[0].request.url.params
    assert q["status"] == "PROCESSING" and q["substatus"] == "STARTED"
    assert route.calls[0].request.headers["Api-Key"] == "tok"
    o = orders[0]
    assert o.marketplace == "yandex" and o.order_id == "5001"
    assert o.deadline is not None and o.created_at is not None


async def test_paid_total_is_buyer_price_times_count(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
    assert [l.paid_total for l in o.lines] == [Decimal("3000"), Decimal("900")]
    assert [l.brand for l in o.lines] == ["BrandX", None]


async def test_paid_field_unset_is_none(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path, paid_field=None).new_orders())[0]
    assert all(l.paid_total is None for l in o.lines)


async def test_warehouse_from_campaign_mapping(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
        assert {l.warehouse for l in o.lines} == {"Склад 1С"}
        o2 = (await make(tmp_path, wh={}).new_orders())[0]
    assert {l.warehouse for l in o2.lines} == {None}


async def test_no_business_id_brand_none(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        o = (await make(tmp_path, business_id=None).new_orders())[0]
    assert all(l.brand is None for l in o.lines)


async def test_ready_quantities_counts_ready_to_ship(tmp_path):
    with respx.mock() as r:
        route = mock_orders(r, [order_json(substatus="READY_TO_SHIP"),
                                order_json(5002, "READY_TO_SHIP")])
        got = await make(tmp_path).ready_quantities()
    assert route.calls[0].request.url.params["substatus"] == "READY_TO_SHIP"
    assert got == {("ART-A", "Склад 1С"): 4, ("ART-B", "Склад 1С"): 2}


async def test_confirm_saves_labels_then_sets_ready_to_ship(tmp_path):
    calls = []
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
        r.get(f"{BASE}/v2/campaigns/{C}/orders/5001/delivery/labels").mock(
            side_effect=lambda req: calls.append("labels") or httpx.Response(
                200, content=b"%PDF-1"))
        put = r.put(f"{BASE}/v2/campaigns/{C}/orders/5001/status").mock(
            side_effect=lambda req: calls.append("put") or httpx.Response(
                200, json={"order": order_json(substatus="READY_TO_SHIP")}))
        await make(tmp_path).confirm(o)
    assert calls == ["labels", "put"]
    assert (tmp_path / "labels" / "5001.pdf").read_bytes() == b"%PDF-1"
    import json
    assert json.loads(put.calls[0].request.content) == {
        "order": {"status": "PROCESSING", "substatus": "READY_TO_SHIP"}}


async def test_labels_failure_no_status_change(tmp_path):
    with respx.mock(assert_all_called=False) as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
        r.get(f"{BASE}/v2/campaigns/{C}/orders/5001/delivery/labels").mock(
            return_value=httpx.Response(400, json={}))
        put = r.put(f"{BASE}/v2/campaigns/{C}/orders/5001/status").mock(
            return_value=httpx.Response(200, json={"order": {}}))
        with pytest.raises(Exception):
            await make(tmp_path).confirm(o)
    assert put.call_count == 0


async def test_status_failure_raises_and_is_sent_once(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
        r.get(f"{BASE}/v2/campaigns/{C}/orders/5001/delivery/labels").mock(
            return_value=httpx.Response(200, content=b"%PDF"))
        put = r.put(f"{BASE}/v2/campaigns/{C}/orders/5001/status").mock(
            return_value=httpx.Response(503))
        with pytest.raises(YandexConfirmError):
            await make(tmp_path).confirm(o)
    assert put.call_count == 1


async def test_unexpected_body_raises(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
        r.get(f"{BASE}/v2/campaigns/{C}/orders/5001/delivery/labels").mock(
            return_value=httpx.Response(200, content=b"%PDF"))
        r.put(f"{BASE}/v2/campaigns/{C}/orders/5001/status").mock(
            return_value=httpx.Response(200, json={"order": {"status": "PROCESSING",
                                                              "substatus": "STARTED"}}))
        with pytest.raises(YandexConfirmError):
            await make(tmp_path).confirm(o)


async def test_status_payload_only_ready_to_ship():
    import inspect

    from orderbot.marketplaces import yandex
    src = inspect.getsource(yandex)
    assert src.count("READY_TO_SHIP") >= 1
    assert yandex.CONFIRM_BODY == {"order": {"status": "PROCESSING",
                                             "substatus": "READY_TO_SHIP"}}


async def test_is_awaiting_and_ready_unknown_is_neither(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
        m = make(tmp_path)
        for status, sub, aw, rd in [("PROCESSING", "STARTED", True, False),
                                    ("PROCESSING", "READY_TO_SHIP", False, True),
                                    ("DELIVERY", "", False, True),
                                    ("WEIRD", "X", False, False)]:
            r.get(f"{BASE}/v2/campaigns/{C}/orders/5001").mock(
                return_value=httpx.Response(200, json={"order": {
                    **order_json(), "status": status, "substatus": sub}}))
            assert await m.is_awaiting(o) is aw
            assert await m.is_ready(o) is rd


async def test_reads_retry_on_5xx(tmp_path):
    with respx.mock() as r:
        r.get(f"{BASE}/v2/campaigns/{C}/orders").mock(side_effect=[
            httpx.Response(503), httpx.Response(200, json={"orders": []})])
        assert await make(tmp_path).new_orders() == []


async def test_pagination_by_token(tmp_path):
    with respx.mock() as r:
        r.get(f"{BASE}/v2/campaigns/{C}/orders").mock(side_effect=[
            httpx.Response(200, json={"orders": [order_json(1)],
                                      "paging": {"nextPageToken": "T"}}),
            httpx.Response(200, json={"orders": [order_json(2)]})])
        mock_brands(r)
        orders = await make(tmp_path).new_orders()
    assert [o.order_id for o in orders] == ["1", "2"]


async def test_recon_yandex_prints_money_fields(tmp_path):
    from orderbot.recon import recon_yandex
    with respx.mock() as r:
        r.get(f"{BASE}/v2/campaigns/{C}/orders/5001").mock(
            return_value=httpx.Response(200, json={"order": order_json()}))
        mock_brands(r)
        text = await recon_yandex(make(tmp_path), ["5001"])
    assert "items[0].buyerPrice = 1500" in text and "brand='BrandX'" in text


# ---- several campaigns ----
C2 = "888"
STOP = bytes.fromhex("63616e63656c526571756573746564").decode()


def make_multi(tmp_path, campaign_id=None, paid_field="buyerPrice"):
    return YandexMarket("tok", campaign_id, warehouses={C: "Склад 1", C2: "Склад 2"},
                        paid_field=paid_field, business_id=None,
                        labels_dir=tmp_path / "labels", retry_delay=0)


def mock_c(r, c, orders, **kw):
    return r.get(f"{BASE}/v2/campaigns/{c}/orders").mock(
        return_value=httpx.Response(200, json={"orders": orders, **kw}))


async def test_new_orders_all_campaigns_remember_campaign(tmp_path):
    with respx.mock() as r:
        mock_c(r, C, [order_json(1)])
        mock_c(r, C2, [order_json(2)])
        orders = await make_multi(tmp_path).new_orders()
    assert [(o.order_id, o.campaign) for o in orders] == [("1", C), ("2", C2)]
    assert {l.warehouse for l in orders[1].lines} == {"Склад 2"}


async def test_campaign_id_restricts_to_one(tmp_path):
    with respx.mock(assert_all_called=False) as r:
        a = mock_c(r, C, [order_json(1)])
        b = mock_c(r, C2, [order_json(2)])
        orders = await make_multi(tmp_path, campaign_id=C2).new_orders()
    assert [o.order_id for o in orders] == ["2"] and a.call_count == 0 and b.call_count == 1


async def test_ready_quantities_keyed_by_campaign_warehouse(tmp_path):
    with respx.mock() as r:
        mock_c(r, C, [order_json(1, "READY_TO_SHIP")])
        mock_c(r, C2, [order_json(2, "READY_TO_SHIP")])
        got = await make_multi(tmp_path).ready_quantities()
    assert got == {("ART-A", "Склад 1"): 2, ("ART-B", "Склад 1"): 1,
                   ("ART-A", "Склад 2"): 2, ("ART-B", "Склад 2"): 1}


async def test_pagination_per_campaign(tmp_path):
    with respx.mock() as r:
        r.get(f"{BASE}/v2/campaigns/{C}/orders").mock(side_effect=[
            httpx.Response(200, json={"orders": [order_json(1)], "paging": {"nextPageToken": "T"}}),
            httpx.Response(200, json={"orders": [order_json(2)]})])
        mock_c(r, C2, [order_json(3)])
        orders = await make_multi(tmp_path).new_orders()
    assert [(o.order_id, o.campaign) for o in orders] == [("1", C), ("2", C), ("3", C2)]


async def test_one_failing_campaign_fails_whole_poll(tmp_path):
    with respx.mock() as r:
        mock_c(r, C, [order_json(1)])
        r.get(f"{BASE}/v2/campaigns/{C2}/orders").mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            await make_multi(tmp_path).new_orders()
        with pytest.raises(httpx.HTTPStatusError):
            await make_multi(tmp_path).ready_quantities()


async def test_flagged_orders_not_listed_not_awaiting_not_ready(tmp_path):
    flagged = {**order_json(9, "READY_TO_SHIP"), STOP: True}
    with respx.mock() as r:
        mock_c(r, C, [{**order_json(2), STOP: True}])
        mock_c(r, C2, [flagged])
        m = make_multi(tmp_path)
        assert await m.new_orders() == []
        assert await m.ready_quantities() == {}
        for sub, aw, rd in [("STARTED", True, False), ("READY_TO_SHIP", False, True)]:
            r.get(f"{BASE}/v2/campaigns/{C}/orders/7").mock(return_value=httpx.Response(
                200, json={"order": order_json(7, sub)}))
            o = await m.get_order("7")
            assert (await m.is_awaiting(o), await m.is_ready(o)) == (aw, rd)
            r.get(f"{BASE}/v2/campaigns/{C}/orders/7").mock(return_value=httpx.Response(
                200, json={"order": {**order_json(7, sub), STOP: True}}))
            assert await m.is_awaiting(o) is False and await m.is_ready(o) is False


async def test_get_order_searches_campaigns_and_confirm_uses_campaign(tmp_path):
    with respx.mock() as r:
        r.get(f"{BASE}/v2/campaigns/{C}/orders/5").mock(return_value=httpx.Response(404))
        r.get(f"{BASE}/v2/campaigns/{C2}/orders/5").mock(
            return_value=httpx.Response(200, json={"order": order_json(5)}))
        m = make_multi(tmp_path)
        o = await m.get_order("5")
        assert o.campaign == C2 and o.order_id == "5"
        r.get(f"{BASE}/v2/campaigns/{C2}/orders/5/delivery/labels").mock(
            return_value=httpx.Response(200, content=b"%PDF"))
        put = r.put(f"{BASE}/v2/campaigns/{C2}/orders/5/status").mock(
            return_value=httpx.Response(200, json={"order": order_json(5, "READY_TO_SHIP")}))
        await m.confirm(o)
    assert put.call_count == 1


async def test_get_order_unknown_raises(tmp_path):
    with respx.mock() as r:
        for c in (C, C2):
            r.get(f"{BASE}/v2/campaigns/{c}/orders/5").mock(return_value=httpx.Response(404))
        with pytest.raises(KeyError):
            await make_multi(tmp_path).get_order("5")


async def test_buyer_price_is_per_unit_real_sample(tmp_path):
    items = [{"offerId": "ART-0002", "count": 2, "buyerPrice": 4344.0}]
    with respx.mock() as r:
        mock_c(r, C, [order_json(1, items=items)])
        mock_c(r, C2, [])
        o = (await make_multi(tmp_path).new_orders())[0]
    assert o.lines[0].paid_total == Decimal("8688.0")


async def test_recon_finds_campaign(tmp_path):
    from orderbot.recon import recon_yandex
    with respx.mock() as r:
        r.get(f"{BASE}/v2/campaigns/{C}/orders/5").mock(return_value=httpx.Response(404))
        r.get(f"{BASE}/v2/campaigns/{C2}/orders/5").mock(
            return_value=httpx.Response(200, json={"order": order_json(5)}))
        text = await recon_yandex(make_multi(tmp_path), ["5"])
    assert f"campaign={C2}" in text and "warehouse='Склад 2'" in text


async def test_express_campaign_marks_order(tmp_path):
    mp = YandexMarket("tok", None, warehouses={"900000009": "СЕВЕР", "900000008": "СЕВЕР"},
                      express=["900000009"], paid_field=None, business_id=None,
                      labels_dir=tmp_path / "labels", retry_delay=0)
    with respx.mock() as r:
        mock_c(r, "900000009", [order_json(1)])
        mock_c(r, "900000008", [order_json(2)])
        a, b = await mp.new_orders()
    assert a.express is True and a.mp_warehouse == "СЕВЕР EXPRESS"
    assert b.express is False and b.mp_warehouse == "СЕВЕР"


async def test_cancel_by_human_sets_cancelled(tmp_path):
    from orderbot.marketplaces.yandex import YandexCancelError
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
        put = r.put(f"{BASE}/v2/campaigns/{C}/orders/5001/status").mock(
            return_value=httpx.Response(200, json={"order": order_json(substatus="SHOP_FAILED")}))
        await make(tmp_path).cancel_by_human(o)
        assert put.call_count == 1
        assert json.loads(put.calls[0].request.content) == {
            "order": {"status": "CANCELLED", "substatus": "SHOP_FAILED"}}
        put.mock(return_value=httpx.Response(500))
        with pytest.raises(YandexCancelError):
            await make(tmp_path).cancel_by_human(o)
        assert put.call_count == 2


# --- product photo for the order message ---------------------------


async def test_photo_url_from_offer_pictures(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        route = r.post(f"{BASE}/v2/businesses/55/offer-mappings").mock(
            return_value=httpx.Response(200, json={"result": {"offerMappings": [
                {"offer": {"offerId": "ART-A", "vendor": "BrandX",
                           "pictures": ["https://avatars.mds.yandex.net/a/1.jpg",
                                        "https://avatars.mds.yandex.net/a/2.jpg"]}},
                {"offer": {"offerId": "ART-B", "pictures": ["http://insecure/b.jpg"]}}]}}))
        m = make(tmp_path)
        await m.new_orders()
        assert await m.photo_url("ART-A") == "https://avatars.mds.yandex.net/a/1.jpg"
        assert await m.photo_url("ART-B") is None
    assert route.call_count == 1


async def test_photo_url_without_business_id_is_none(tmp_path):
    with respx.mock() as r:
        assert await make(tmp_path, business_id=None).photo_url("ART-A") is None
    assert not r.calls


# ------------------------------------------------- stock sync
from orderbot.marketplaces.base import StockAt, StockError  # noqa: E402


def ym_stocks(offer="ART-A", **counts):
    return {"status": "OK", "result": {"warehouses": [{"warehouseId": 1, "offers": [
        {"offerId": offer, "stocks": [{"type": t, "count": c} for t, c in counts.items()]}]}]}}


async def test_stock_read_available_and_freeze(tmp_path):
    with respx.mock() as r:
        route = r.post(f"{BASE}/v2/campaigns/{C}/offers/stocks").mock(return_value=httpx.Response(
            200, json=ym_stocks(FIT=13, FREEZE=1, AVAILABLE=12)))
        got = await make(tmp_path).stock_read(C, "ART-A")
    assert json.loads(route.calls[0].request.content) == {"offerIds": ["ART-A"]}
    assert got == StockAt(count=12, ref=None, reserved=1)


async def test_stock_read_fit_minus_freeze_and_missing(tmp_path):
    with respx.mock() as r:
        r.post(f"{BASE}/v2/campaigns/{C}/offers/stocks").mock(return_value=httpx.Response(
            200, json=ym_stocks(FIT=6, FREEZE=2)))
        assert (await make(tmp_path).stock_read(C, "ART-A")).count == 4
        r.post(f"{BASE}/v2/campaigns/{C}/offers/stocks").mock(return_value=httpx.Response(
            200, json=ym_stocks(offer="OTHER", FIT=6)))
        assert await make(tmp_path).stock_read(C, "ART-A") is None


async def test_stock_write_exact_body_once(tmp_path):
    with respx.mock() as r:
        route = r.put(f"{BASE}/v2/campaigns/{C}/offers/stocks").mock(
            return_value=httpx.Response(200, json={"status": "OK"}))
        await make(tmp_path).stock_write(C, "ART-A", None, 4)
        r.put(f"{BASE}/v2/campaigns/{C}/offers/stocks").mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await make(tmp_path).stock_write(C, "ART-A", None, 4)
    assert route.call_count == 2   # one per call: no retry
    assert json.loads(route.calls[0].request.content) == {
        "skus": [{"sku": "ART-A", "items": [{"count": 4}]}]}


async def test_stock_groups_from_business_warehouses(tmp_path):
    body = {"status": "OK", "result": {
        "warehouses": [{"id": 3, "name": "Южная 9", "campaignId": 900000010}],
        "warehouseGroups": [{"name": "G", "mainWarehouse": {"id": 1, "campaignId": 900000008},
                             "warehouses": [{"id": 1, "name": "Север", "campaignId": 900000008},
                                            {"id": 2, "name": "Север EXPRESS",
                                             "campaignId": 900000009}]}]}}
    with respx.mock() as r:
        r.get(f"{BASE}/v2/businesses/55/warehouses").mock(
            return_value=httpx.Response(200, json=body))
        groups = await make(tmp_path).stock_groups()
    assert groups == {"900000010": ("900000010", "900000010", "Южная 9"),
                      "900000008": ("g:900000008", "900000008", "Север"),
                      "900000009": ("g:900000008", "900000008", "Север EXPRESS")}


async def test_stock_groups_need_business_id(tmp_path):
    with pytest.raises(StockError):
        await make(tmp_path, business_id=None).stock_groups()


async def test_stock_write_requires_status_ok(tmp_path):
    with respx.mock() as r:
        r.put(f"{BASE}/v2/campaigns/{C}/offers/stocks").mock(
            return_value=httpx.Response(200, json={}))
        with pytest.raises(StockError):
            await make(tmp_path).stock_write(C, "ART-A", None, 4)


# ------------------------------------------- Plus points and promo codes
# Live: subsidies[].amount is per unit — buyerPrice + YANDEX_CASHBACK + SUBSIDY ==
# buyerPriceBeforeDiscount (count 3: 906.67 + 72.33 + 1061 = 2040).
async def test_plus_points_added_to_paid_and_kept_apart(tmp_path):
    items = [{"offerId": "ART-A", "count": 3, "buyerPrice": 906.67,
              "subsidies": [{"type": "YANDEX_CASHBACK", "amount": 72.33},
                            {"type": "SUBSIDY", "amount": 1061.0}]}]
    with respx.mock() as r:
        mock_orders(r, [order_json(items=items)])
        mock_brands(r)
        ln = (await make(tmp_path).new_orders())[0].lines[0]
    assert ln.points == Decimal("216.99")
    assert ln.paid_total == Decimal("2937.00")  # (906.67 + 72.33) x 3, SUBSIDY not added


async def test_no_points_line_unchanged(tmp_path):
    with respx.mock() as r:
        mock_orders(r, [order_json()])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
    assert [l.points for l in o.lines] == [None, None]
    assert [l.paid_total for l in o.lines] == [Decimal("3000"), Decimal("900")]


async def test_market_promocode_marked(tmp_path):
    items = [{"offerId": "ART-A", "count": 1, "buyerPrice": 100,
              "promos": [{"type": "MARKET_PROMOCODE"}, {"type": "DCO_EXTRA_DISCOUNT"}]},
             {"offerId": "ART-B", "count": 1, "buyerPrice": 100,
              "promos": [{"type": "DISCOUNT_BY_PAYMENT_TYPE"}]}]
    with respx.mock() as r:
        mock_orders(r, [order_json(items=items)])
        mock_brands(r)
        o = (await make(tmp_path).new_orders())[0]
    assert [l.promos for l in o.lines] == [["Промокод Маркета"], []]
