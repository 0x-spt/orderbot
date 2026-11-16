"""Text of the Telegram order message: caption, result lines, reason texts. Pure functions."""
import html
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from orderbot.domain import Action, Decision, ItemRead, Order, Reason

CAPTION_LIMIT = 1024
MSK = ZoneInfo("Europe/Moscow")
MP_TITLE = {"ozon": "Озон", "yandex": "Маркет", "wb": "WB"}

REASON_TEXT = {
    Reason.RESERVE: "свободного не хватает, но есть резерв: нужно решение сотрудника",
    Reason.NO_STOCK: "не хватает остатка даже с учётом резерва",
    Reason.PRICE_LOW: "цена покупателя ниже минимально допустимой",
    Reason.READ_UNCERTAIN: "экран 1С прочитан неуверенно",
    Reason.ARTICLE_NOT_FOUND: "артикул не найден в 1С",
    Reason.WAREHOUSE_NOT_ON_SCREEN: "склад заказа не найден на экране 1С",
    Reason.WAREHOUSE_UNMAPPED: "склад заказа не сопоставлен со складом 1С",
    Reason.QTY_UNVERIFIED: "количество больше 1: проверка цены для него ещё не включена",
    Reason.PAID_MISSING: "сумма, оплаченная покупателем, неизвестна",
    Reason.INVALID_ORDER: "в заказе нет позиций или количество не больше нуля",
    Reason.CONFIRM_UNKNOWN: "подтверждение отправлено, но результат неизвестен: проверьте заказ",
    Reason.INTERNAL_ERROR: "внутренняя ошибка робота: проверьте заказ вручную",
    Reason.READY_UNKNOWN: "не удалось узнать, сколько заказов уже собрано на другой площадке",
    Reason.API_UNAVAILABLE: "API площадки недоступен",
}


def reason_text(reason) -> str:
    try:
        return REASON_TEXT[reason]
    except (KeyError, TypeError):
        return f"причина: {getattr(reason, 'value', reason)}"


def order_link(order: Order) -> str:
    if order.marketplace == "ozon":
        return f"https://seller.ozon.ru/app/postings/fbs?search={order.order_id}"
    if order.marketplace == "yandex":
        return "https://partner.market.yandex.ru/"  # generic cabinet URL
    if order.marketplace == "wb":
        return "https://seller.wildberries.ru/"  # generic cabinet URL
    return ""



_SHORT = {
    Reason.NO_STOCK: "Не хватает остатка",
    Reason.PRICE_LOW: "Цена ниже минимума",
    Reason.READ_UNCERTAIN: "1С прочитана неуверенно",
    Reason.ARTICLE_NOT_FOUND: "Артикул не найден в 1С",
}


def reason_short(reason) -> str:
    if reason in _SHORT:
        return _SHORT[reason]
    t = reason_text(reason)
    return t[:1].upper() + t[1:]


def _money(v) -> str:
    v = Decimal(v).quantize(Decimal("0.01"))
    whole, frac = divmod(abs(v), 1)
    s = f"{int(whole):,}".replace(",", " ")
    if v < 0:
        s = "-" + s
    if frac:
        s += "," + f"{frac:.2f}"[2:]
    return s + " ₽"


def _hm(t: datetime) -> str:
    return t.astimezone(MSK).strftime("%H:%M")


def _article(article: str) -> str:
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", article or "")
    if m and m.group(1):
        return f"<b>{html.escape(m.group(1))}</b> ({html.escape(m.group(2))})"
    return f"<b>{html.escape(article or '-')}</b>"


_HEAD = {Action.CONFIRM: ("✅", "Можно собирать"),
         Action.ASK_RESERVE: ("🔒", "Хватает только из резерва")}


def format_caption(order: Order, decision: Decision, reads: dict | None, urgent: bool,
                   now: datetime, company: str | None = None) -> str:
    """`company`: the seller company's name, shown after the marketplace (always,
    so the notifier always passes it)."""
    out = []
    if order.express:
        out.append("🚀 <b>ЭКСПРЕСС"
                   + (f" · собрать до {_hm(order.deadline)}" if order.deadline else "")
                   + "</b>")
    reasons = list(decision.reasons)
    if decision.action in _HEAD:
        icon, title = _HEAD[decision.action]
    else:
        icon = "⚠️"
        title = reason_short(reasons[0][1]) if reasons else "Нужна проверка"
    mp = MP_TITLE.get(order.marketplace, html.escape(order.marketplace))
    head = f"{'⏰ ' if urgent else ''}{icon} <b>{html.escape(title)}</b> · {mp}"
    if company:
        head += f" · {html.escape(company)}"
    out.append(head)
    num = html.escape(str(order.order_id))
    link = order_link(order)
    out.append(f"Заказ <b><a href=\"{html.escape(link)}\">{num}</a></b>" if link
               else f"Заказ <b>{num}</b>")
    out.append("")
    for i, ln in enumerate(order.lines):
        if i:
            out.append("")   # an empty line between the articles
        out.append(f"{_article(ln.article)} × {ln.qty}")
        out += _line_details(order, ln, (reads or {}).get(ln.article), decision, reasons)
        if ln.points:  # Yandex Plus points, already inside the 💰 price
            out.append(f"💳 Баллами Плюса: {_money(ln.points)}")
        if ln.promos:  # promo codes only, not every discount
            out.append("🏷 " + ", ".join(html.escape(p) for p in ln.promos))
    for _, r in reasons[1:]:
        if r == Reason.PRICE_LOW:
            continue   # the ❌ next to the 💰 price already says it
        out.append(f"❌ {html.escape(reason_text(r))}")
    return "\n".join(out)


def _foreign(ln) -> str:
    """a price paid in another currency is shown in rubles, the original beside."""
    return f" ({html.escape(ln.paid_foreign)})" if getattr(ln, "paid_foreign", None) else ""


def _line_details(order, ln, rd, decision, reasons) -> list[str]:
    """The 📦 stock and 💰 price lines of one order line. Without a 1C read
    still the warehouse and the price the buyer paid, from the marketplace."""
    out = []
    if not isinstance(rd, ItemRead):
        wh = order.mp_warehouse or ln.warehouse
        if wh:
            out.append(f"📦 {html.escape(wh)} — 1С не прочитана")
        if ln.paid_total is not None and ln.qty:
            out.append(f"💰 <b>{_money(Decimal(ln.paid_total) / ln.qty)}</b>{_foreign(ln)}")
        elif ln.paid_foreign:
            out.append(f"💰 {html.escape(ln.paid_foreign)} — нет курса ЦБ")
        return out
    bad = {r for a, r in reasons if a == ln.article}
    escalate = decision.action == Action.ESCALATE
    row = rd.stock.get(ln.warehouse)
    if row is not None:
        free = row.on_hand - row.reserve
        s = (f"📦 {html.escape(order.mp_warehouse or ln.warehouse or '')} — "
             f"<b>свободно {free}</b>")
        if row.reserve:
            s += f", в резерве {row.reserve}"
        if Reason.NO_STOCK in bad:
            s += " ❌"
        elif escalate:
            s += " ✅"
        out.append(s)
    if ln.paid_total is not None and ln.qty:
        s = f"💰 <b>{_money(Decimal(ln.paid_total) / ln.qty)}</b>{_foreign(ln)}"
        if Reason.PRICE_LOW in bad:
            s += " ❌"
        elif decision.action != Action.CONFIRM and ln.article in decision.floors:
            s += " ✅"
        s += f" · EPR {_money(rd.epr)} · PPR {_money(rd.ppr)}"  # not «минимум»
        out.append(s)
    elif getattr(ln, "paid_foreign", None):
        out.append(f"💰 {html.escape(ln.paid_foreign)} — нет курса ЦБ")
    return out


_RESULTS = {
    "collected": "✅ <b>Собран</b> — {who}, {t}",
    "cancelled": "❌ <b>Отменён</b> — {who}, {t}",
    "collected_by_robot": "✅ <b>Собран роботом</b>, {t}",
    "collect_failed": "⚠️ Не удалось собрать: {err} — {who}, {t}",
    "cancel_failed": "⚠️ Не удалось отменить: {err} — {who}, {t}",
    "already_done": "ℹ️ Заказ уже не ждёт сборки (собран или отменён в кабинете) — {who}, {t}",
}


def result_line(kind: str, who: str, at: datetime, error: str | None = None) -> str:
    return _RESULTS[kind].format(who=html.escape(who or ""), t=_hm(at),
                                 err=html.escape(error or "ошибка"))
