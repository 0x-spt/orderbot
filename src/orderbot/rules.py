from decimal import Decimal

from .domain import Action, Decision, ItemRead, Order, PriceSettings, Reason

SOINVEST_BELOW_PPR_PERCENT = Decimal(15)  # with co-investment up to 15 % under PPR is fine


def price_floor(epr: Decimal, ppr: Decimal, settings: PriceSettings, brand: str | None) -> Decimal:
    if settings.soinvest:
        return (ppr * (Decimal(100) - SOINVEST_BELOW_PPR_PERCENT) / Decimal(100)).quantize(
            Decimal("0.01"))
    base = epr
    if brand is not None and brand.lower() in settings.brand_discounts:
        n = settings.brand_discounts[brand.lower()]
        base = epr * (Decimal(100) - n) / Decimal(100) - settings.tolerance
    return max(ppr, base)


FIXED_PRICE_TOLERANCE = Decimal("150")  # business rule: a fixed-price item may sell up to 150 RUB lower


def decide_order(order: Order, reads: dict[str, ItemRead | Reason | None], settings: PriceSettings,
                 ready_qty: dict[tuple[str, str], int], reserve_approved: bool = False) -> Decision:
    if not order.lines:
        return Decision(Action.ESCALATE, [("", Reason.INVALID_ORDER)], {})
    bad = [(ln.article, Reason.INVALID_ORDER) for ln in order.lines if ln.qty <= 0]
    if bad:
        return Decision(Action.ESCALATE, bad, {})

    groups: dict[tuple[str, str | None], dict] = {}
    for ln in order.lines:
        g = groups.setdefault((ln.article, ln.warehouse),
                              {"qty": 0, "paid": Decimal(0), "paid_ok": True, "brand": ln.brand})
        g["qty"] += ln.qty
        if ln.paid_total is None:
            g["paid_ok"] = False
        else:
            g["paid"] += ln.paid_total
        if g["brand"] is None:
            g["brand"] = ln.brand

    reasons: list[tuple[str, Reason]] = []
    floors: dict[str, Decimal] = {}
    for (article, wh), g in groups.items():
        read = reads.get(article)
        if read is None or isinstance(read, Reason):
            reasons.append((article, Reason.READ_UNCERTAIN if read is None else read))
            continue
        qty = g["qty"]
        fixed = settings.fixed_price.get(article.lower())
        if fixed is None or not settings.soinvest:
            floor = (price_floor(read.epr, read.ppr, settings, g["brand"]) if fixed is None
                     else fixed - FIXED_PRICE_TOLERANCE)
            floors[article] = floor
            if qty > 1 and not settings.multi_qty_verified:
                reasons.append((article, Reason.QTY_UNVERIFIED))
            if not g["paid_ok"]:
                reasons.append((article, Reason.PAID_MISSING))
            elif qty > 0 and g["paid"] / qty < floor:
                reasons.append((article, Reason.PRICE_LOW))
        if wh is None:
            reasons.append((article, Reason.WAREHOUSE_UNMAPPED))
            continue
        row = read.stock.get(wh)
        if row is None:
            reasons.append((article, Reason.WAREHOUSE_NOT_ON_SCREEN))
            continue
        free = row.on_hand - ready_qty.get((article, wh), 0)
        if qty > free:
            reasons.append((article, Reason.NO_STOCK))
        elif not reserve_approved and qty > free - row.reserve:
            reasons.append((article, Reason.RESERVE))

    if not reasons:
        action = Action.CONFIRM
    elif all(r == Reason.RESERVE for _, r in reasons):
        action = Action.ASK_RESERVE
    else:
        action = Action.ESCALATE
    return Decision(action, reasons, floors)
