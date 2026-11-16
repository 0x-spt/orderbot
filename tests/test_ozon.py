import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from orderbot.marketplaces.ozon import OzonConfirmError, OzonMarketplace

BASE = "https://api-seller.ozon.ru"
FX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FX / name).read_text())


def make(paid_field="customer_price", wh=None, paid_is_unit=None):
    return OzonMarketplace(
        "cid", "key",
        warehouses={"Склад Тест-1": "Тест 1С"} if wh is None else wh,
        paid_field=paid_field, paid_is_unit=paid_is_unit, retry_delay=0,
    )


def awaiting_deliver_fx():
    data = fx("ozon_list_awaiting.json")
    data["postings"][0]["status"] = "awaiting_deliver"
    return data


def mock_common(r, list_fx="ozon_list_awaiting.json"):
    body = fx(list_fx) if isinstance(list_fx, str) else list_fx
    r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(
        return_value=httpx.Response(200, json=body))
    return r.post(f"{BASE}/v4/product/info/attributes").mock(
        return_value=httpx.Response(200, json=fx("ozon_attributes.json")))


async def test_new_orders_maps_article_qty_warehouse_paid_brand():
    with respx.mock() as r:
        mock_common(r)
        orders = await make().new_orders()
    assert len(orders) == 1
    o = orders[0]
    assert o.marketplace == "ozon" and o.order_id == "00000001-0001-1"
    assert o.status == "awaiting_packaging" and o.deadline is not None
    a, b = o.lines
    assert (a.article, a.qty, a.warehouse, a.paid_total, a.brand) == (
        "ART-A", 1, "Тест 1С", Decimal("1814.29"), "BrandX")
    # quantity 2 and per-unit-vs-line not configured: goes to a human
    assert (b.article, b.qty, b.paid_total, b.brand) == ("ART-B", 2, None, None)


async def test_unmapped_warehouse_is_none():
    with respx.mock() as r:
        mock_common(r)
        o = (await make(wh={}).new_orders())[0]
    assert all(l.warehouse is None for l in o.lines)


async def test_paid_ignores_limit_price():
    with respx.mock() as r:
        mock_common(r)
        o = (await make().new_orders())[0]
    assert o.lines[0].paid_total == Decimal("1814.29")
    assert o.lines[0].paid_total != Decimal("3008")


async def test_missing_paid_field_gives_none():
    with respx.mock() as r:
        mock_common(r)
        o = (await make(paid_field="no_such_field").new_orders())[0]
    assert all(l.paid_total is None for l in o.lines)


async def test_unconfigured_paid_field_gives_none():
    with respx.mock() as r:
        mock_common(r)
        o = (await make(paid_field=None).new_orders())[0]
    assert all(l.paid_total is None for l in o.lines)


async def test_ready_quantities_sums_by_article_and_warehouse():
    data = fx("ozon_list_awaiting.json")
    p = data["postings"][0]
    p2 = json.loads(json.dumps(p)); p2["posting_number"] = "00000002-0001-1"
    p3 = json.loads(json.dumps(p)); p3["posting_number"] = "00000003-0001-1"
    p3["delivery_method"]["warehouse"] = "Неизвестный"
    for q in (p, p2, p3):
        q["status"] = "awaiting_deliver"
    data["postings"] = [p, p2, p3]
    with respx.mock() as r:
        r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(return_value=httpx.Response(200, json=data))
        q = await make().ready_quantities()
    assert q == {("ART-A", "Тест 1С"): 2, ("ART-B", "Тест 1С"): 4}


async def test_ready_quantities_warns_on_unmapped(caplog):
    with respx.mock() as r:
        r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(
            return_value=httpx.Response(200, json=awaiting_deliver_fx()))
        with caplog.at_level("WARNING"):
            q = await make(wh={}).ready_quantities()
    assert q == {} and caplog.records


async def test_confirm_posts_exact_body():
    with respx.mock() as r:
        mock_common(r)
        route = r.post(f"{BASE}/v4/posting/fbs/ship").mock(
            return_value=httpx.Response(200, json={"result": ["00000001-0001-1"]}))
        m = make()
        o = (await m.new_orders())[0]
        await m.confirm(o)
    assert json.loads(route.calls[0].request.content) == {
        "posting_number": "00000001-0001-1",
        "packages": [{"products": [{"product_id": 111, "quantity": 1},
                                   {"product_id": 222, "quantity": 2}]}],
    }
    h = route.calls[0].request.headers
    assert h["Client-Id"] == "cid" and h["Api-Key"] == "key"


async def test_api_500_raises_and_confirm_not_retried():
    with respx.mock() as r:
        mock_common(r)
        route = r.post(f"{BASE}/v4/posting/fbs/ship").mock(return_value=httpx.Response(500))
        m = make()
        o = (await m.new_orders())[0]
        with pytest.raises(OzonConfirmError):
            await m.confirm(o)
    assert route.call_count == 1


async def test_read_retried_up_to_3_then_raises():
    with respx.mock() as r:
        route = r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await make().new_orders()
    assert route.call_count == 3


async def test_get_order_and_status_checks():
    def get(status):
        d = fx("ozon_get_awaiting.json"); d["result"]["status"] = status
        return d
    m = make()
    for status, aw, rd in [("awaiting_packaging", True, False),
                           ("awaiting_deliver", False, True),
                           ("delivering", False, True),
                           ("something_new", False, False)]:
        with respx.mock(assert_all_called=False) as r:
            r.post(f"{BASE}/v3/posting/fbs/get").mock(return_value=httpx.Response(200, json=get(status)))
            r.post(f"{BASE}/v4/product/info/attributes").mock(
                return_value=httpx.Response(200, json=fx("ozon_attributes.json")))
            o = await m.get_order("00000001-0001-1")
            assert o.lines[0].paid_total == Decimal("1814.29")
            assert (await m.is_awaiting(o), await m.is_ready(o)) == (aw, rd), status


async def test_brand_cached_per_offer():
    with respx.mock() as r:
        route = mock_common(r)
        m = make()
        await m.new_orders()
        await m.new_orders()
    assert route.call_count == 1


def test_load_config(tmp_path):
    from orderbot.config import load_config
    y = tmp_path / "w.yaml"
    y.write_text('ozon:\n  "A": "B"\n')
    c = load_config({"OZON_CLIENT_ID": "1", "ALLOWED_USER_IDS": "5, 6",
                     "OZON_PAID_FIELD": "x"}, y)
    assert c.warehouses == {"ozon": {"A": "B"}} and c.allowed_user_ids == [5, 6]
    assert c.ozon_paid_field == "x"
    assert load_config({}, tmp_path / "none.yaml").ozon_paid_field is None


@pytest.mark.parametrize("raw,expected", [("", None), ("true", True), ("1", True), ("Yes", True),
                                          ("false", False), ("0", False), ("NO", False)])
def test_load_config_paid_is_unit(tmp_path, raw, expected):
    from orderbot.config import load_config
    c = load_config({"OZON_PAID_IS_UNIT": raw}, tmp_path / "none.yaml")
    assert c.ozon_paid_is_unit is expected
    assert load_config({}, tmp_path / "none.yaml").ozon_paid_is_unit is None


def test_load_config_paid_is_unit_garbage(tmp_path):
    from orderbot.config import load_config
    with pytest.raises(ValueError, match="OZON_PAID_IS_UNIT"):
        load_config({"OZON_PAID_IS_UNIT": "maybe"}, tmp_path / "none.yaml")


async def test_recon_output_format():
    from orderbot.recon import recon_ozon
    with respx.mock() as r:
        r.post(f"{BASE}/v3/posting/fbs/get").mock(
            return_value=httpx.Response(200, json=fx("ozon_get_awaiting.json")))
        r.post(f"{BASE}/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json=fx("ozon_attributes.json")))
        text = await recon_ozon(make(), ["00000001-0001-1"])
    assert "== 00000001-0001-1" in text and "brand='BrandX'" in text
    assert "financial_data.products[0].customer_price = 1814.29" in text
    assert "financial_data.products[0].old_price = 3008" in text


async def test_list_body_has_cutoffs_and_status():
    with respx.mock() as r:
        route = mock_common(r)
        lst = r.routes[0]
        await make().new_orders()
    f = json.loads(lst.calls[0].request.content)["filter"]
    assert f["statuses"] == ["awaiting_packaging"] and "status" not in f
    assert f["cutoff_from"] < f["cutoff_to"]


def _pages(n, start):
    base = fx("ozon_list_awaiting.json")["postings"][0]
    out = []
    for i in range(n):
        p = json.loads(json.dumps(base)); p["posting_number"] = f"P-{start+i}"
        out.append(p)
    return out


async def test_list_uses_v4_path_and_body():
    with respx.mock() as r:
        mock_common(r)
        lst = r.routes[0]
        await make().new_orders()
    assert lst.calls[0].request.url.path == "/v4/posting/fbs/unfulfilled/list"
    b = json.loads(lst.calls[0].request.content)
    assert b["dir"] == "ASC" and b["limit"] == 100 and b["with"] == {"financial_data": True}
    assert "offset" not in b and "cursor" not in b


async def test_list_paginates_by_cursor():
    with respx.mock() as r:
        route = r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(side_effect=[
            httpx.Response(200, json={"postings": _pages(100, 0), "count": 150,
                                      "has_next": True, "cursor": "c1"}),
            httpx.Response(200, json={"postings": _pages(50, 100), "count": 150,
                                      "has_next": False, "cursor": ""}),
        ])
        r.post(f"{BASE}/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json=fx("ozon_attributes.json")))
        orders = await make().new_orders()
    assert len(orders) == 150 and route.call_count == 2
    b0, b1 = [json.loads(c.request.content) for c in route.calls]
    assert "cursor" not in b0 and b1["cursor"] == "c1"
    assert {k: v for k, v in b1.items() if k != "cursor"} == b0


@pytest.mark.parametrize("second", [
    {"has_next": True, "cursor": "c1"},   # repeated
    {"has_next": True, "cursor": ""},     # empty
])
async def test_cursor_repeat_or_empty_raises(second):
    with respx.mock(assert_all_called=False) as r:
        route = r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(side_effect=[
            httpx.Response(200, json={"postings": _pages(100, 0), "count": 300,
                                      "has_next": True, "cursor": "c1"}),
            httpx.Response(200, json={"postings": _pages(100, 100), "count": 300, **second}),
            httpx.Response(200, json={"postings": [], "count": 300, "has_next": False}),
        ])
        r.post(f"{BASE}/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json=fx("ozon_attributes.json")))
        with pytest.raises(Exception, match="cursor"):
            await make().new_orders()
    assert route.call_count <= 2


async def test_empty_cursor_on_first_page_with_has_next_raises():
    with respx.mock() as r:
        r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(return_value=httpx.Response(
            200, json={"postings": _pages(1, 0), "count": 9, "has_next": True, "cursor": ""}))
        with pytest.raises(Exception, match="cursor"):
            await make().new_orders()


async def test_ignored_server_filter_is_filtered_client_side():
    data = fx("ozon_list_awaiting.json")
    leak = _pages(1, 50)[0]; leak["status"] = "delivering"
    data["postings"].append(leak)
    with respx.mock() as r:
        mock_common(r, data)
        orders = await make().new_orders()
    assert [o.order_id for o in orders] == ["00000001-0001-1"]


async def test_ready_quantities_ignores_other_statuses():
    data = awaiting_deliver_fx()
    leak = _pages(1, 50)[0]; leak["status"] = "delivering"
    data["postings"].append(leak)
    with respx.mock() as r:
        r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(return_value=httpx.Response(200, json=data))
        q = await make().ready_quantities()
    assert q == {("ART-A", "Тест 1С"): 1, ("ART-B", "Тест 1С"): 2}


async def test_ready_quantities_requests_awaiting_deliver():
    with respx.mock() as r:
        route = r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(
            return_value=httpx.Response(200, json=awaiting_deliver_fx()))
        await make().ready_quantities()
    assert json.loads(route.calls[0].request.content)["filter"]["statuses"] == ["awaiting_deliver"]


@pytest.mark.parametrize("unit,expected", [(None, None), (True, Decimal("8756.2")),
                                           (False, Decimal("4378.1"))])
async def test_paid_for_quantity_above_one(unit, expected):
    with respx.mock() as r:
        mock_common(r)
        o = (await make(paid_is_unit=unit).new_orders())[0]
    assert o.lines[0].paid_total == Decimal("1814.29")
    assert o.lines[1].paid_total == expected


async def test_paid_unit_flag_does_not_change_quantity_one():
    for unit in (True, False):
        with respx.mock() as r:
            mock_common(r)
            o = (await make(paid_is_unit=unit).new_orders())[0]
        assert o.lines[0].paid_total == Decimal("1814.29")


async def test_paid_accepts_number_and_flat_field():
    data = fx("ozon_list_awaiting.json")
    fin = data["postings"][0]["financial_data"]["products"]
    fin[0]["customer_price"]["amount"] = 1814.29
    with respx.mock() as r:
        mock_common(r, data)
        o = (await make().new_orders())[0]
        assert o.lines[0].paid_total == Decimal("1814.29")
    with respx.mock() as r:
        mock_common(r, data)
        o = (await make(paid_field="price").new_orders())[0]
    assert o.lines[0].paid_total == Decimal("1814")


@pytest.mark.parametrize("field", ["customer_price", "customer_price.amount"])
async def test_paid_v4_object_shape_with_both_paths(field):
    with respx.mock() as r:
        mock_common(r)
        o = (await make(paid_field=field).new_orders())[0]
    assert o.lines[0].paid_total == Decimal("1814.29")


async def test_paid_v3_get_plain_number_shape():
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{BASE}/v3/posting/fbs/get").mock(
            return_value=httpx.Response(200, json=fx("ozon_get_awaiting.json")))
        r.post(f"{BASE}/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json=fx("ozon_attributes.json")))
        o = await make(paid_field="customer_price").get_order("00000001-0001-1")
    assert o.lines[0].paid_total == Decimal("1814.29")


async def test_paid_amount_less_dict_gives_none():
    data = fx("ozon_list_awaiting.json")
    data["postings"][0]["financial_data"]["products"][0]["customer_price"] = {"currency": "RUB"}
    with respx.mock() as r:
        mock_common(r, data)
        o = (await make(paid_field="customer_price").new_orders())[0]
    assert o.lines[0].paid_total is None


async def test_paid_bad_path_or_value_gives_none():
    data = fx("ozon_list_awaiting.json")
    data["postings"][0]["financial_data"]["products"][0]["customer_price"]["amount"] = "n/a"
    with respx.mock() as r:
        mock_common(r, data)
        o = (await make().new_orders())[0]
        assert o.lines[0].paid_total is None
    with respx.mock() as r:
        mock_common(r, data)
        o = (await make(paid_field="commission.amount.x").new_orders())[0]
    assert all(l.paid_total is None for l in o.lines)


async def _confirm_with(resp):
    with respx.mock() as r:
        mock_common(r)
        route = r.post(f"{BASE}/v4/posting/fbs/ship").mock(return_value=resp)
        m = make()
        o = (await m.new_orders())[0]
        with pytest.raises(OzonConfirmError):
            await m.confirm(o)
    assert route.call_count == 1


async def test_confirm_400_raises():
    await _confirm_with(httpx.Response(400, json={"message": "bad"}))


@pytest.mark.parametrize("body", [{}, {"result": []}, {"foo": 1}, []])
async def test_confirm_200_unexpected_body_raises(body):
    await _confirm_with(httpx.Response(200, json=body))


async def test_read_403_not_retried():
    with respx.mock() as r:
        route = r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(
            return_value=httpx.Response(403))
        with pytest.raises(httpx.HTTPStatusError):
            await make().new_orders()
    assert route.call_count == 1


async def test_paid_is_none_when_no_financial_entry_has_the_sku():
    """No fallback by position: an entry of another product must not price this line."""
    data = fx("ozon_list_awaiting.json")
    for f in data["postings"][0]["financial_data"]["products"]:
        f["product_id"] += 1000
    with respx.mock() as r:
        r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(
            return_value=httpx.Response(200, json=data))
        r.post(f"{BASE}/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json=fx("ozon_attributes.json")))
        o = (await make().new_orders())[0]
    assert [l.paid_total for l in o.lines] == [None, None]


async def test_express_warehouse_marks_order():
    body = fx("ozon_list_awaiting.json")
    body["postings"][0]["delivery_method"]["warehouse"] = "СЕВЕР EXPRESS 🚀"
    wh = {"СЕВЕР EXPRESS 🚀": "СЕВЕР"}
    with respx.mock() as r:
        mock_common(r, body)
        mp = OzonMarketplace("cid", "key", warehouses=wh, express={"СЕВЕР EXPRESS 🚀"},
                             paid_field=None, retry_delay=0)
        o = (await mp.new_orders())[0]
    assert o.express is True and o.mp_warehouse == "СЕВЕР EXPRESS"
    assert o.lines[0].warehouse == "СЕВЕР"
    with respx.mock() as r:
        mock_common(r)
        o = (await make().new_orders())[0]
    assert o.express is False and o.mp_warehouse == "Склад Тест-1"


async def test_cancel_by_human_sends_one_request():
    from orderbot.marketplaces.ozon import OzonCancelError
    with respx.mock() as r:
        mock_common(r)
        route = r.post(f"{BASE}/v2/posting/fbs/cancel").mock(
            return_value=httpx.Response(200, json={"result": True}))
        m = make()
        o = (await m.new_orders())[0]
        await m.cancel_by_human(o)
        assert route.call_count == 1
        assert json.loads(route.calls[0].request.content) == {
            "posting_number": "00000001-0001-1", "cancel_reason_id": 352}
        route.mock(return_value=httpx.Response(500))
        with pytest.raises(OzonCancelError):
            await m.cancel_by_human(o)
        assert route.call_count == 2  # no retries on 500


async def test_cancel_by_human_200_with_false_result_raises():
    from orderbot.marketplaces.ozon import OzonCancelError
    with respx.mock() as r:
        mock_common(r)
        r.post(f"{BASE}/v2/posting/fbs/cancel").mock(
            return_value=httpx.Response(200, json={"result": False}))
        m = make()
        o = (await m.new_orders())[0]
        with pytest.raises(OzonCancelError):
            await m.cancel_by_human(o)


# --- product photo for the order message ---------------------------


def attributes_with_images(**fields):
    data = fx("ozon_attributes.json")
    data["result"][0].update(fields)
    return data


async def test_photo_url_comes_with_the_brand_request():
    with respx.mock() as r:
        r.post(f"{BASE}/v4/posting/fbs/unfulfilled/list").mock(
            return_value=httpx.Response(200, json=fx("ozon_list_awaiting.json")))
        route = r.post(f"{BASE}/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json=attributes_with_images(
                primary_image="https://ir.ozone.ru/s3/a/main.jpg",
                images=["https://ir.ozone.ru/s3/a/other.jpg"])))
        m = make()
        await m.new_orders()
        assert await m.photo_url("ART-A") == "https://ir.ozone.ru/s3/a/main.jpg"
        assert await m.photo_url("ART-B") is None
    assert route.call_count == 1


@pytest.mark.parametrize("fields, expected", [
    ({"primary_image": ["https://x.ru/p.jpg"]}, "https://x.ru/p.jpg"),
    ({"primary_image": "", "images": ["https://x.ru/i0.jpg", "https://x.ru/i1.jpg"]},
     "https://x.ru/i0.jpg"),
    ({"primary_image": "http://x.ru/p.jpg"}, None),       # https only
    ({"images": []}, None),
])
async def test_photo_url_fields(fields, expected):
    with respx.mock() as r:
        route = r.post(f"{BASE}/v4/product/info/attributes").mock(
            return_value=httpx.Response(200, json=attributes_with_images(**fields)))
        m = make()
        assert await m.photo_url("ART-A") == expected   # asked for an article not seen yet
        assert await m.photo_url("ART-A") == expected
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["filter"]["offer_id"] == ["ART-A"]


async def test_promo_codes_are_taken_from_actions():
    """show promo codes in the alert. Ozon lists them among the line's
    financial actions (seen live: «Промокод 20% 11.11»); other actions are ignored."""
    data = fx("ozon_list_awaiting.json")
    data["postings"][0]["financial_data"]["products"][0]["actions"] = [
        "Промокод 20% 11.11", "Скидка (за счет Озон) - DD by AI benefit system", "Округление"]
    with respx.mock() as r:
        mock_common(r, data)
        a, b = (await make().new_orders())[0].lines
    assert a.promos == ["Промокод 20% 11.11"]
    assert b.promos == []


# ------------------------------------------------- stock sync
from orderbot.marketplaces.base import StockAt, StockError  # noqa: E402


def stock_rows(*rows):
    return {"products": [{"offer_id": "ART-A", "warehouse_name": n, "warehouse_id": i,
                          "present": p, "reserved": r, "free_stock": p - r}
                         for n, i, p, r in rows]}


async def test_stock_read_by_warehouse_name_gives_free_and_id():
    with respx.mock() as r:
        route = r.post(f"{BASE}/v2/product/info/stocks-by-warehouse/fbs").mock(
            return_value=httpx.Response(200, json=stock_rows(
                ("СЕВЕР", 11, 8, 0), ("СЕВЕР EXPRESS 🚀", 22, 5, 1))))
        got = await make().stock_read("СЕВЕР EXPRESS 🚀", "ART-A")
    assert json.loads(route.calls[0].request.content) == {"offer_id": ["ART-A"], "limit": 100}
    assert got == StockAt(count=4, ref=22, reserved=1)


async def test_stock_read_missing_warehouse_is_none():
    with respx.mock() as r:
        r.post(f"{BASE}/v2/product/info/stocks-by-warehouse/fbs").mock(
            return_value=httpx.Response(200, json=stock_rows(("СПБ", 1, 3, 0))))
        assert await make().stock_read("СЕВЕР", "ART-A") is None


async def test_stock_read_without_warehouse_id_refuses():
    body = {"products": [{"offer_id": "ART-A", "warehouse_name": "СПБ", "present": 3,
                          "reserved": 0, "free_stock": 3}]}
    with respx.mock() as r:
        r.post(f"{BASE}/v2/product/info/stocks-by-warehouse/fbs").mock(
            return_value=httpx.Response(200, json=body))
        with pytest.raises(StockError, match="id склада неизвестен"):
            await make().stock_read("СПБ", "ART-A")


async def test_stock_write_exact_body_once():
    with respx.mock() as r:
        route = r.post(f"{BASE}/v2/products/stocks").mock(return_value=httpx.Response(
            200, json={"result": [{"offer_id": "ART-A", "warehouse_id": 22, "updated": True,
                                   "errors": []}]}))
        await make().stock_write("СЕВЕР", "ART-A", 22, 4)
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content) == {
        "stocks": [{"offer_id": "ART-A", "stock": 4, "warehouse_id": 22}]}


async def test_stock_write_not_updated_raises_without_retry():
    with respx.mock() as r:
        route = r.post(f"{BASE}/v2/products/stocks").mock(return_value=httpx.Response(
            200, json={"result": [{"offer_id": "ART-A", "updated": False,
                                   "errors": [{"code": "X", "message": "too many"}]}]}))
        with pytest.raises(StockError, match="too many"):
            await make().stock_write("СЕВЕР", "ART-A", 22, 4)
        r.post(f"{BASE}/v2/products/stocks").mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await make().stock_write("СЕВЕР", "ART-A", 22, 4)
    assert route.call_count == 2


class FakeFx:
    def __init__(self, rate):
        self.rate, self.calls = rate, []

    async def to_rub(self, amount, code):
        self.calls.append((amount, code))
        return None if self.rate is None else (amount * self.rate).quantize(Decimal("0.01"))


def kzt_list():
    data = fx("ozon_list_awaiting.json")
    fin = data["postings"][0]["financial_data"]["products"]
    fin[0]["customer_price"] = {"amount": "14309", "currency": "KZT"}
    return data


@pytest.mark.parametrize("field", ["customer_price", "customer_price.amount"])
async def test_paid_in_tenge_is_converted_to_rubles(field):
    """Kazakhstan buyers pay in KZT; the check is in rubles."""
    m = make(paid_field=field)
    m.fx = FakeFx(Decimal("0.183858"))
    with respx.mock() as r:
        mock_common(r, kzt_list())
        o = (await m.new_orders())[0]
    assert o.lines[0].paid_total == Decimal("2630.82")
    assert o.lines[0].paid_foreign == "14 309 KZT"
    assert o.lines[1].paid_foreign is None       # rubles: nothing to convert
    assert m.fx.calls == [(Decimal("14309"), "KZT")]


async def test_no_rate_leaves_no_price_never_tenge_as_rubles():
    m = make()
    m.fx = FakeFx(None)
    with respx.mock() as r:
        mock_common(r, kzt_list())
        o = (await m.new_orders())[0]
    assert o.lines[0].paid_total is None and o.lines[0].paid_foreign == "14 309 KZT"
