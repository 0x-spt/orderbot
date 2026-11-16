from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from orderbot.alerts import CAPTION_LIMIT, MP_TITLE, _money, format_caption, result_line
from orderbot.domain import Action, Decision, ItemRead, Order, OrderLine, Reason, StockRow

UTC = UTC
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
ART = "DEN091-GRY(DEN091-01114Z)"


def order(mp="ozon", oid="00000001-0002-1", article=ART, qty=1, paid="1814", wh="Южная,9",
          express=False, deadline=None, mp_wh=None):
    return Order(mp, oid, [OrderLine(article, qty, wh, Decimal(paid), None)], NOW, deadline,
                 "awaiting_packaging", express=express, mp_warehouse=mp_wh)


def read(article=ART, wh="Южная,9", on_hand=6, reserve=0, epr="1880", ppr="1340"):
    return ItemRead(article, {wh: StockRow(wh, on_hand, reserve)}, Decimal(epr), Decimal(ppr),
                    None)


def dec(action=Action.CONFIRM, reasons=(), floor="1340", article=ART):
    return Decision(action, list(reasons), {article: Decimal(floor)})


def test_confirm_caption_matches_mockup():
    cap = format_caption(order(), dec(), {ART: read()}, False, NOW)
    for s in ("✅ <b>Можно собирать</b> · Озон", "<b>DEN091-GRY</b>", "× 1",
              "📦 Южная,9 — <b>свободно 6</b>", "💰 <b>1 814 ₽</b> · EPR 1 880 ₽ · PPR 1 340 ₽",
              "(DEN091-01114Z)"):
        assert s in cap
    for s in ("НАБЛЮДЕНИЕ", "Площадка:", "минимум"):
        assert s not in cap
    assert "https://seller.ozon.ru" in cap
    assert len(cap) < CAPTION_LIMIT


def test_price_low_caption():
    o = order("wb", "5000000001", paid="2900")
    cap = format_caption(o, dec(Action.ESCALATE, [(ART, Reason.PRICE_LOW)], "3180"),
                         {ART: read()}, False, NOW)
    assert "⚠️ <b>Цена ниже минимума</b> · WB" in cap
    price = [l for l in cap.split("\n") if "💰" in l][0]
    assert "❌" in price


def test_express_caption_first_line():
    o = order("yandex", express=True, deadline=datetime(2026, 11, 5, 11, 30, tzinfo=UTC),
              mp_wh="СЕВЕР EXPRESS", wh="СЕВЕР")
    cap = format_caption(o, dec(), {ART: read(wh="СЕВЕР")}, False, NOW)
    assert cap.split("\n")[0] == "🚀 <b>ЭКСПРЕСС · собрать до 14:30</b>"
    assert "📦 СЕВЕР EXPRESS — <b>свободно 6</b>" in cap


def test_express_without_deadline():
    cap = format_caption(order(express=True), dec(), None, False, NOW)
    assert cap.split("\n")[0] == "🚀 <b>ЭКСПРЕСС</b>"


def test_reserve_caption():
    cap = format_caption(order("yandex"), dec(Action.ASK_RESERVE, [(ART, Reason.RESERVE)]),
                         {ART: read(on_hand=5, reserve=4)}, False, NOW)
    assert "🔒 <b>Хватает только из резерва</b>" in cap
    assert "свободно 1</b>, в резерве 4" in cap


def test_urgent_prefix():
    cap = format_caption(order(), dec(), {ART: read()}, True, NOW)
    assert cap.split("\n")[0].startswith("⏰")


def test_html_is_escaped():
    a = "A<B&C"
    cap = format_caption(order(article=a), dec(article=a), {a: read(a)}, False, NOW)
    assert "A&lt;B&amp;C" in cap


def test_no_read_shows_no_1c_figures():
    """the marketplace price stays, but no stock or EPR/PPR is made up."""
    cap = format_caption(order(), dec(Action.ESCALATE, [(ART, Reason.READ_UNCERTAIN)]),
                         {ART: None}, False, NOW)
    assert "свободно" not in cap and "EPR" not in cap and "PPR" not in cap
    assert "1С прочитана неуверенно" in cap


def test_extra_reasons_listed():
    cap = format_caption(order(), dec(Action.ESCALATE, [(ART, Reason.NO_STOCK),
                                                        (ART, Reason.PRICE_LOW)]),
                         {ART: read(on_hand=0)}, False, NOW)
    assert "⚠️ <b>Не хватает остатка</b>" in cap
    # no «❌ цена … ниже минимально допустимой» line, the ❌ at 💰 says it
    assert "минимально допустимой" not in cap
    assert "💰" in cap and "❌" in cap.split("💰")[1].split("\n")[0]


def test_empty_line_between_articles():
    o = order()
    second = replace(o.lines[0], article="B2")
    cap = format_caption(replace(o, lines=[o.lines[0], second]), dec(Action.ESCALATE,
                         [(ART, Reason.NO_STOCK)]), {ART: read(on_hand=0)}, False, NOW)
    lines = cap.split("\n")
    j = next(i for i, l in enumerate(lines) if "B2" in l)
    assert lines[j - 1] == "" and lines[j - 2] != ""


def test_result_lines():
    at = datetime(2026, 11, 5, 11, 5, tzinfo=UTC)
    assert result_line("collected", "Даня", at) == "✅ <b>Собран</b> — Даня, 14:05"
    assert result_line("cancelled", "Даня", at) == "❌ <b>Отменён</b> — Даня, 14:05"
    assert (result_line("collect_failed", "Даня", at, error="HTTP 400")
            == "⚠️ Не удалось собрать: HTTP 400 — Даня, 14:05")
    assert (result_line("cancel_failed", "Даня", at, error="HTTP 400")
            == "⚠️ Не удалось отменить: HTTP 400 — Даня, 14:05")
    assert (result_line("already_done", "Даня", at)
            == "ℹ️ Заказ уже не ждёт сборки (собран или отменён в кабинете) — Даня, 14:05")
    assert result_line("collected_by_robot", "", at) == "✅ <b>Собран роботом</b>, 14:05"


def test_mp_title():
    assert MP_TITLE == {"ozon": "Озон", "yandex": "Маркет", "wb": "WB"}


def test_money_rounds_to_kopecks():
    assert _money(Decimal("999.999")) == "1 000 ₽"
    assert _money(Decimal("1814.5")) == "1 814,50 ₽"


def test_price_line_shows_epr_and_ppr_from_1c():
    """«минимум» replaced by EPR and PPR read in 1C."""
    o = order("wb", "5000000001", paid="2900")
    cap = format_caption(o, dec(Action.ESCALATE, [(ART, Reason.PRICE_LOW)], "3180"),
                         {ART: read(epr="4460", ppr="3180")}, False, NOW)
    price = [l for l in cap.split("\n") if "💰" in l][0]
    assert price == "💰 <b>2 900 ₽</b> ❌ · EPR 4 460 ₽ · PPR 3 180 ₽"


def test_promo_codes_line_under_price():
    o = order()
    o.lines[0].promos = ["Промокод 20% 11.11", "Промокод <b>X</b>"]
    cap = format_caption(o, dec(), {ART: read()}, False, NOW)
    lines = cap.split("\n")
    i = next(n for n, l in enumerate(lines) if l.startswith("💰"))
    assert lines[i + 1] == "🏷 Промокод 20% 11.11, Промокод &lt;b&gt;X&lt;/b&gt;"


def test_no_promo_no_line():
    assert "🏷" not in format_caption(order(), dec(), {ART: read()}, False, NOW)


def test_promo_line_shown_even_without_1c_read():
    o = order()
    o.lines[0].promos = ["Промокод 20% 11.11"]
    cap = format_caption(o, dec(Action.ESCALATE, [(ART, Reason.READ_UNCERTAIN)]), {ART: None},
                         False, NOW)
    assert "🏷 Промокод 20% 11.11" in cap


def test_plus_points_line_under_price():
    o = order()
    o.lines[0].points = Decimal("1476")
    cap = format_caption(o, dec(), {ART: read()}, False, NOW)
    lines = cap.split("\n")
    i = next(n for n, l in enumerate(lines) if l.startswith("💰"))
    assert lines[i + 1] == "💳 Баллами Плюса: 1 476 ₽"


def test_no_points_no_line():
    assert "💳" not in format_caption(order(), dec(), {ART: read()}, False, NOW)


def test_uncertain_read_still_shows_marketplace_data():
    """when 1C was not read, still show the price from the marketplace, the
    warehouse, the promo code and the points."""
    o = order(qty=2, paid="3000", mp="yandex")
    o.lines[0].points = Decimal("400")
    o.lines[0].promos = ["Промокод Маркета"]
    cap = format_caption(o, dec(Action.ESCALATE, [(ART, Reason.READ_UNCERTAIN)]), {ART: None},
                         False, NOW)
    lines = cap.split("\n")
    i = next(n for n, l in enumerate(lines) if l.endswith("× 2"))
    assert lines[i + 1:i + 5] == ["📦 Южная,9 — 1С не прочитана",
                                  "💰 <b>1 500 ₽</b>",
                                  "💳 Баллами Плюса: 400 ₽",
                                  "🏷 Промокод Маркета"]


def test_uncertain_read_without_paid_price_has_no_price_line():
    o = order()
    o.lines[0].paid_total = None
    cap = format_caption(o, dec(Action.ESCALATE, [(ART, Reason.READ_UNCERTAIN)]), {ART: None},
                         False, NOW)
    assert "💰" not in cap and "📦 Южная,9 — 1С не прочитана" in cap


def test_foreign_price_shown_in_rubles_with_the_original():
    o = order()
    o = replace(o, lines=[replace(o.lines[0], paid_total=Decimal("2630.82"),
                                  paid_foreign="14 309 KZT")])
    cap = format_caption(o, dec(Action.ESCALATE, [(ART, Reason.NO_STOCK)]),
                         {ART: read(on_hand=0)}, False, NOW)
    assert "(14 309 KZT)" in cap and "2 630" in cap


def test_foreign_price_without_a_rate_says_so():
    o = order()
    o = replace(o, lines=[replace(o.lines[0], paid_total=None, paid_foreign="14 309 KZT")])
    cap = format_caption(o, dec(Action.ESCALATE, [(ART, Reason.PAID_MISSING)]),
                         {ART: read(on_hand=5)}, False, NOW)
    assert "💰 14 309 KZT — нет курса ЦБ" in cap
