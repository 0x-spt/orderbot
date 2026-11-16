from datetime import UTC, datetime
from decimal import Decimal

import pytest

from orderbot.domain import Action, ItemRead, Order, OrderLine, PriceSettings, Reason, StockRow
from orderbot.rules import decide_order, price_floor

EPR, PPR = Decimal("1880"), Decimal("1340")
NOW = datetime(2026, 11, 2, 12, 0)


def s(**k):
    return PriceSettings(**{"soinvest": False, "brand_discounts": {}, "tolerance": Decimal("50"),
                            "multi_qty_verified": True, **k})


def test_floor_normal(): assert price_floor(EPR, PPR, s(), "BrandX") == Decimal("1880")
def test_floor_soinvest_is_ppr_minus_15_percent():
    """with co-investment a price up to 15 % below PPR is fine; lower — a person."""
    assert price_floor(EPR, PPR, s(soinvest=True), None) == Decimal("1139.00")


def test_soinvest_price_between_85_percent_and_ppr_confirms():
    assert run(order(line(paid="1140")), settings=s(soinvest=True)).action == Action.CONFIRM


def test_soinvest_price_more_than_15_percent_below_ppr_goes_to_person():
    d = run(order(line(paid="1138")), settings=s(soinvest=True))
    assert d.action == Action.ESCALATE and d.reasons[0][1] == Reason.PRICE_LOW
def test_floor_discount_20(): assert price_floor(EPR, PPR, s(brand_discounts={"brandx": Decimal(20)}), "BrandX") == Decimal("1454")
def test_floor_discount_30_stops_at_ppr(): assert price_floor(EPR, PPR, s(brand_discounts={"brandx": Decimal(30)}), "BrandX") == Decimal("1340")
def test_floor_other_brand_has_no_discount(): assert price_floor(EPR, PPR, s(brand_discounts={"x": Decimal(20)}), "BrandX") == EPR
def test_floor_unknown_brand_has_no_discount(): assert price_floor(EPR, PPR, s(brand_discounts={"x": Decimal(20)}), None) == EPR
def test_floor_never_below_ppr_when_epr_lower(): assert price_floor(Decimal(100), PPR, s(), None) == PPR


def line(qty=1, paid="1880", wh="СПБ", art="A1", brand=None):
    return OrderLine(art, qty, wh, None if paid is None else Decimal(paid), brand)


def order(*lines):
    return Order("ozon", "1", list(lines), NOW, None, "awaiting")


def read(on_hand=10, reserve=7, art="A1", wh="СПБ"):
    return ItemRead(art, {wh: StockRow(wh, on_hand, reserve)}, EPR, PPR, None)


def run(o, r=None, settings=None, ready=None, approved=False):
    r = {"A1": read()} if r is None else r
    return decide_order(o, r, settings or s(), ready or {}, approved)


@pytest.mark.parametrize("on_hand,reserve,ready,need,action,reason", [
    (10, 7, 0, 3, Action.CONFIRM, None),
    (10, 7, 0, 5, Action.ASK_RESERVE, Reason.RESERVE),
    (10, 7, 2, 3, Action.ASK_RESERVE, Reason.RESERVE),
    (6, 2, 0, 4, Action.CONFIRM, None),
    (6, 2, 0, 7, Action.ESCALATE, Reason.NO_STOCK),
])
def test_stock_table(on_hand, reserve, ready, need, action, reason):
    d = run(order(line(qty=need, paid=str(1880 * need))), {"A1": read(on_hand, reserve)},
            ready={("A1", "СПБ"): ready})
    assert d.action == action
    assert d.reasons == ([] if reason is None else [("A1", reason)])


def test_reserve_approved_confirms():
    d = run(order(line(qty=5, paid="9400")), ready={}, approved=True)
    assert d.action == Action.CONFIRM


def test_price_1814_soinvest_confirms():
    assert run(order(line(paid="1814")), settings=s(soinvest=True)).action == Action.CONFIRM


def test_price_1814_normal_escalates_price_low():
    d = run(order(line(paid="1814")))
    assert d.action == Action.ESCALATE and ("A1", Reason.PRICE_LOW) in d.reasons
    assert d.floors == {"A1": Decimal("1880")}


def test_price_equal_floor_confirms():
    assert run(order(line(paid="1880"))).action == Action.CONFIRM


def test_paid_is_divided_by_qty():
    d = run(order(line(qty=2, paid="3628")), settings=s(soinvest=True))
    assert d.action == Action.CONFIRM


def test_multi_qty_unverified_escalates():
    d = run(order(line(qty=2, paid="3760")), settings=s(multi_qty_verified=False))
    assert d.action == Action.ESCALATE and ("A1", Reason.QTY_UNVERIFIED) in d.reasons


def test_same_article_two_lines_summed():
    d = run(order(line(qty=2, paid="3760"), line(qty=2, paid="3760")), {"A1": read(10, 7)})
    assert d.action != Action.CONFIRM


def test_one_bad_line_escalates_whole_order():
    reads = {"A1": read(), "B1": read(art="B1")}
    d = run(order(line(), line(art="B1", paid="100")), reads)
    assert d.action == Action.ESCALATE and d.reasons == [("B1", Reason.PRICE_LOW)]


def test_read_none_escalates():
    d = run(order(line()), {"A1": None})
    assert d.action == Action.ESCALATE and d.reasons == [("A1", Reason.READ_UNCERTAIN)]


def test_paid_none_escalates():
    d = run(order(line(paid=None)))
    assert d.action == Action.ESCALATE and ("A1", Reason.PAID_MISSING) in d.reasons


def test_warehouse_none_escalates_unmapped():
    d = run(order(line(wh=None)))
    assert d.action == Action.ESCALATE and ("A1", Reason.WAREHOUSE_UNMAPPED) in d.reasons


def test_warehouse_missing_on_screen_escalates():
    d = run(order(line(wh="МСК")))
    assert d.action == Action.ESCALATE and ("A1", Reason.WAREHOUSE_NOT_ON_SCREEN) in d.reasons


def test_reserve_and_price_low_escalates():
    d = run(order(line(qty=5, paid="5000")))
    assert d.action == Action.ESCALATE
    assert ("A1", Reason.RESERVE) in d.reasons and ("A1", Reason.PRICE_LOW) in d.reasons


def test_empty_order_escalates():
    d = run(order())
    assert d.action == Action.ESCALATE and d.reasons == [("", Reason.INVALID_ORDER)]


def test_zero_qty_escalates():
    d = run(order(line(qty=0, paid="0")))
    assert d.action == Action.ESCALATE and ("A1", Reason.INVALID_ORDER) in d.reasons


def test_reads_value_may_carry_a_reason():
    from datetime import datetime
    from decimal import Decimal

    from orderbot.domain import Action, Order, OrderLine, PriceSettings, Reason
    from orderbot.rules import decide_order
    o = Order("ozon", "1", [OrderLine("A1", 1, "СПБ", Decimal(100), None)],
              datetime(2026, 1, 1), None, "x")
    d = decide_order(o, {"A1": Reason.ARTICLE_NOT_FOUND},
                     PriceSettings(False, {}, Decimal(50), False), {})
    assert d.action == Action.ESCALATE
    assert d.reasons == [("A1", Reason.ARTICLE_NOT_FOUND)]


def test_fixed_price_article_floor_is_price_minus_150_without_soinvest():
    """A fixed-price article sells at 5900; below 5750 it is not let through.
    With soinvest on (Ozon) the price is not checked at all; stock always is."""
    from datetime import datetime
    from decimal import Decimal

    from orderbot.domain import Action, ItemRead, Order, OrderLine, PriceSettings, Reason, StockRow
    from orderbot.rules import decide_order

    fixed = {"fix-0001": Decimal("5900")}
    market = PriceSettings(False, {}, Decimal(50), True, fixed)
    ozon = PriceSettings(True, {}, Decimal(50), True, fixed)
    read = ItemRead("FIX-0001", {"СПБ": StockRow("СПБ", 5, 0)}, Decimal("10500"),
                    Decimal("7500"), None)

    def order(paid, qty=1):
        return Order("x", "1", [OrderLine("FIX-0001", qty, "СПБ", Decimal(paid), None)],
                     datetime(2026, 11, 5, tzinfo=UTC), None, "awaiting_packaging")

    assert decide_order(order("5750"), {"FIX-0001": read}, market, {}).action == Action.CONFIRM
    d = decide_order(order("5749.99"), {"FIX-0001": read}, market, {})
    assert d.reasons == [("FIX-0001", Reason.PRICE_LOW)] and d.floors["FIX-0001"] == Decimal("5750")
    assert decide_order(order("11500", 2), {"FIX-0001": read}, market, {}).action == Action.CONFIRM
    assert decide_order(order("3368.22"), {"FIX-0001": read}, ozon, {}).action == Action.CONFIRM
    assert (decide_order(order("3368.22", 6), {"FIX-0001": read}, ozon, {}).reasons
            == [("FIX-0001", Reason.NO_STOCK)])
    plain = PriceSettings(True, {}, Decimal(50), True)
    assert (decide_order(order("3368.22"), {"FIX-0001": read}, plain, {}).reasons
            == [("FIX-0001", Reason.PRICE_LOW)])
