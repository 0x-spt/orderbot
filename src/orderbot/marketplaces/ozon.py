import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from orderbot.domain import MAIN, Order, OrderLine
from orderbot.fx import RUBLE, Rates
from orderbot.fx import grouped as _grouped
from orderbot.marketplaces.base import StockAt, StockError, ToShip, https_url

log = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))

BASE_URL = "https://api-seller.ozon.ru"
BRAND_ATTRIBUTE_ID = 85
CUTOFF_PAST_DAYS = 30
CUTOFF_FUTURE_DAYS = 30
PAGE_LIMIT = 100
LIST_PATH = "/v4/posting/fbs/unfulfilled/list"
AWAITING = "awaiting_packaging"
READY = {"awaiting_deliver", "driver_pickup", "sent_by_seller", "delivering",
         "last_mile", "arbitration", "client_arbitration", "delivered"}


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _plain_name(name):
    """Marketplace warehouse name without emoji (non-BMP symbols) and edge spaces."""
    if not name:
        return None
    return "".join(ch for ch in name if ord(ch) <= 0xFFFF).strip() or None


class OzonConfirmError(Exception):
    pass


class OzonCancelError(Exception):
    pass


class OzonListError(Exception):
    pass


def _retryable(e):
    if isinstance(e, httpx.HTTPStatusError):
        c = e.response.status_code
        return c >= 500 or c == 429
    return True


class OzonMarketplace:
    name = "ozon"

    def __init__(self, client_id, api_key, warehouses=None, paid_field=None,
                 paid_is_unit=None, base_url=BASE_URL, retry_delay=1.0, express=(),
                 account=MAIN, fx=None):
        self.fx = fx if fx is not None else Rates()   # foreign customer_price -> rubles
        self.account = account   # the seller company of these keys; its orders carry it
        self.warehouses = warehouses or {}
        self.express = set(express or ())
        self.paid_field = paid_field
        self.paid_is_unit = paid_is_unit
        self.retry_delay = retry_delay
        self._brands: dict[str, str | None] = {}
        self._photos: dict[str, str | None] = {}
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=30,
            headers={"Client-Id": client_id, "Api-Key": api_key})

    async def _post(self, path, body, retry=True):
        attempts = 3 if retry else 1
        for i in range(attempts):
            try:
                r = await self._http.post(path, json=body)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                if i == attempts - 1 or not _retryable(e):
                    raise
                await asyncio.sleep(self.retry_delay)

    async def _list(self, status):
        """v4 unfulfilled list: top-level response, cursor pagination. The server-side
        `statuses` filter is repeated client-side so a wrong filter can never leak."""
        now = datetime.now(UTC)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        body = {
            "dir": "ASC",
            "filter": {
                "cutoff_from": (now - timedelta(days=CUTOFF_PAST_DAYS)).strftime(fmt),
                "cutoff_to": (now + timedelta(days=CUTOFF_FUTURE_DAYS)).strftime(fmt),
                "statuses": [status],
            },
            "limit": PAGE_LIMIT,
            "with": {"financial_data": True},
        }
        out, seen = [], set()
        while True:
            data = await self._post(LIST_PATH, body)
            out += [p for p in data.get("postings") or [] if p.get("status") == status]
            if not data.get("has_next"):
                return out
            cursor = data.get("cursor")
            if not cursor or cursor in seen:
                raise OzonListError(
                    f"ozon list: has_next is true but cursor is {cursor!r} "
                    f"({'repeated' if cursor else 'empty'}); refusing to loop or truncate")
            seen.add(cursor)
            body = {**body, "cursor": cursor}

    async def _fill_brands(self, offer_ids):
        todo = sorted({o for o in offer_ids if o not in self._brands})
        if not todo:
            return
        data = await self._post("/v4/product/info/attributes", {
            "filter": {"offer_id": todo, "visibility": "ALL"}, "limit": 1000})
        for item in data.get("result", []):
            brand = None
            for a in item.get("attributes", []):
                if a.get("id") == BRAND_ATTRIBUTE_ID and a.get("values"):
                    brand = a["values"][0].get("value") or None
            self._brands[item["offer_id"]] = brand
            # the same response carries the photos (checked live: primary_image is the
            # main one and differs from images[0])
            self._photos[item["offer_id"]] = (https_url(item.get("primary_image"))
                                              or https_url(item.get("images")))
        for o in todo:
            self._brands.setdefault(o, None)

    async def photo_url(self, article):
        """Main product photo of this offer_id, from the brand request (read-only)."""
        await self._fill_brands([article])
        return self._photos.get(article)

    def _paid(self, fin_products, sku, qty):
        return self._paid_in(fin_products, sku, qty)[0]

    def _paid_in(self, fin_products, sku, qty):
        """(paid total, currency) of this very product (matched by product_id == sku, no
        positional fallback). paid_field may be a dotted path; an object value (v4 list) is
        read via its "amount" and "currency" (KZT for Kazakhstan buyers), a plain
        number (v3 get) is used as is, e.g. customer_price, currency None (rubles). The value
        is a line total only when qty is 1; for qty > 1 it is None unless paid_is_unit says
        whether the value is per unit (True: x qty) or already the line total (False)."""
        if not self.paid_field or sku is None:
            return None, None
        entry = next((f for f in fin_products if f.get("product_id") == sku), None)
        val, parent = entry, None
        for part in self.paid_field.split("."):
            parent, val = val, (val.get(part) if isinstance(val, dict) else None)
        # "customer_price.amount" still has its currency beside it
        currency = parent.get("currency") if isinstance(parent, dict) else None
        currency = currency if isinstance(currency, str) and currency else None
        if isinstance(val, dict):  # v4 list: {"amount": "...", "currency": "RUB"}
            currency = val.get("currency") or None
            val = val.get("amount")
        if val is None or isinstance(val, bool):
            return None, None
        try:
            amount = Decimal(str(val))
        except InvalidOperation:
            return None, None
        if not amount.is_finite():
            return None, None
        if qty == 1:
            return amount, currency
        if self.paid_is_unit is None:
            return None, None
        return (amount * qty if self.paid_is_unit else amount), currency

    @staticmethod
    def _promos(fin_products, sku):
        """Promo codes of this product: Ozon names them «Промокод …» among the line's
        financial actions (seen live); every other action is a discount, not a code."""
        entry = next((f for f in fin_products if f.get("product_id") == sku), None) or {}
        return [a for a in entry.get("actions") or []
                if isinstance(a, str) and a.strip().lower().startswith("промокод")]

    def _warehouse(self, p):
        name = (p.get("delivery_method") or {}).get("warehouse")
        return self.warehouses.get(name)

    def _raw_warehouse(self, p):
        return (p.get("delivery_method") or {}).get("warehouse")

    async def _to_order(self, p) -> Order:
        prods = p.get("products", [])
        await self._fill_brands([x["offer_id"] for x in prods])
        fin = (p.get("financial_data") or {}).get("products", [])
        wh = self._warehouse(p)
        raw_wh = self._raw_warehouse(p)
        lines = []
        for x in prods:
            paid, cur = self._paid_in(fin, x.get("sku"), x["quantity"])
            foreign = None
            if paid is not None and cur and cur.upper() not in RUBLE:
                # per unit, like the 💰 price it stands beside
                foreign = f"{_grouped(paid / x['quantity'])} {cur.upper()}"
                paid = await self.fx.to_rub(paid, cur)
                if paid is None:
                    log.warning("no %s rate: %s paid in %s goes to a person",
                                cur, p["posting_number"], cur)
            lines.append(OrderLine(article=x["offer_id"], qty=x["quantity"], warehouse=wh,
                                   paid_total=paid, brand=self._brands.get(x["offer_id"]),
                                   promos=self._promos(fin, x.get("sku")),
                                   paid_foreign=foreign))
        order = Order(marketplace=self.name, order_id=p["posting_number"], lines=lines,
                      created_at=_dt(p.get("in_process_at")),
                      deadline=_dt(p.get("shipment_date")), status=p.get("status", ""),
                      express=raw_wh in self.express,
                      mp_warehouse=_plain_name(raw_wh), account=self.account)
        order._ship_items = [{"product_id": x["sku"], "quantity": x["quantity"]} for x in prods]
        return order

    async def new_orders(self):
        return [await self._to_order(p) for p in await self._list(AWAITING)]

    async def _fetch(self, order_id):
        data = await self._post("/v3/posting/fbs/get", {
            "posting_number": order_id,
            "with": {"financial_data": True}})
        return data["result"]

    async def get_order(self, order_id):
        return await self._to_order(await self._fetch(order_id))

    async def ready_quantities(self):
        totals: dict[tuple[str, str], int] = {}
        for p in await self._list("awaiting_deliver"):
            wh = self._warehouse(p)
            for x in p.get("products", []):
                if wh is None:
                    log.warning("ozon: warehouse %r has no mapping, skipping %s in %s",
                                (p.get("delivery_method") or {}).get("warehouse"),
                                x["offer_id"], p["posting_number"])
                    continue
                key = (x["offer_id"], wh)
                totals[key] = totals.get(key, 0) + x["quantity"]
        return totals

    # ------------------------------------------------------------ shipment reminders
    # VERIFIED live (read-only): in the v4 list `shipment_date` is a UTC time
    # shortly before the handover (СПБ 10:30Z = 13:30 MSK for the 14:00 car, Южная 9
    # 07:30Z = 10:30 MSK for the 12:00 one); its Moscow date is the handover day.
    def shipment_warehouses(self):
        return {wh for name, wh in self.warehouses.items() if name not in self.express}

    async def to_ship(self, only=None):
        out = []
        for status in (AWAITING, "awaiting_deliver"):
            for p in await self._list(status):
                raw = self._raw_warehouse(p)
                wh = self._warehouse(p)
                when = _dt(p.get("shipment_date"))
                if raw in self.express or wh is None or when is None:
                    continue
                if only is not None and wh not in only:
                    continue
                out.append(ToShip(p["posting_number"], wh, when.astimezone(MSK).date(),
                                  status != AWAITING,
                                  sum(int(x.get("quantity") or 0)
                                      for x in p.get("products", []))))
        return out

    async def is_awaiting(self, order):
        return (await self._fetch(order.order_id)).get("status") == AWAITING

    async def is_ready(self, order):
        return (await self._fetch(order.order_id)).get("status") in READY

    async def confirm(self, order):
        items = getattr(order, "_ship_items", None)
        if items is None:
            items = [{"product_id": x["sku"], "quantity": x["quantity"]}
                     for x in (await self._fetch(order.order_id))["products"]]
        try:
            data = await self._post("/v4/posting/fbs/ship", {
                "posting_number": order.order_id,
                "packages": [{"products": items}],
            }, retry=False)
        except httpx.HTTPError as e:
            raise OzonConfirmError(f"ship failed for {order.order_id}: {e}") from e
        if not isinstance(data, dict) or not data.get("result"):
            raise OzonConfirmError(f"unexpected ship response for {order.order_id}: {data!r}")

    # ------------------------------------------------------------ stock sync
    # VERIFIED live (read-only): POST
    # /v2/product/info/stocks-by-warehouse/fbs {"offer_id": [...], "limit": 100} ->
    # products[] {offer_id, warehouse_id, warehouse_name (as in postings, with emoji),
    # present, reserved, free_stock}; the v1 method answers "obsolete method", and
    # /v1/warehouse/list is 403 for this key, so the warehouse id comes from this read.
    # Write POST /v2/products/stocks {"stocks": [{offer_id, stock, warehouse_id}]} ->
    # result[] {updated, errors}: from the Ozon docs as mirrored (docs.ozon.ru blocks the
    # tooling); `stock` is "available without reserved", so it is compared with free_stock.
    # UNVERIFIED live (no live writes allowed): the write itself.
    async def stock_read(self, warehouse, article):
        data = await self._post("/v2/product/info/stocks-by-warehouse/fbs",
                                {"offer_id": [article], "limit": 100})
        want = _plain_name(warehouse)
        for row in (data or {}).get("products") or []:
            if row.get("offer_id") != article:
                continue
            name = row.get("warehouse_name")
            if name != warehouse and _plain_name(name) != want:
                continue
            wid = row.get("warehouse_id")
            if not wid:
                raise StockError("id склада неизвестен")
            reserved = int(row.get("reserved") or 0)
            free = row.get("free_stock")
            free = int(row.get("present") or 0) - reserved if free is None else int(free)
            return StockAt(count=free, ref=int(wid), reserved=reserved)
        return None

    async def stock_write(self, warehouse, article, ref, count):
        data = await self._post("/v2/products/stocks", {"stocks": [
            {"offer_id": article, "stock": int(count), "warehouse_id": ref}]}, retry=False)
        rows = (data or {}).get("result") if isinstance(data, dict) else None
        row = next((x for x in rows or [] if x.get("offer_id") == article), None)
        if not row or not row.get("updated"):
            errs = (row or {}).get("errors") or []
            msg = "; ".join(str(e.get("message") or e.get("code")) for e in errs
                            if isinstance(e, dict))
            raise StockError(f"Озон не принял остаток: {msg or data!r}"[:120])

    async def cancel_by_human(self, order):
        # Docs (docs.ozon.ru/api/seller) could not be fetched: path and body are
        # from the Ozon API as known (POST /v2/posting/fbs/cancel, cancel_reason_id required);
        # reason 352 "Товар закончился на вашем складе" verified live via
        # /v2/posting/fbs/cancel-reason/list. No retries: one request only.
        try:
            data = await self._post("/v2/posting/fbs/cancel", {
                "posting_number": order.order_id, "cancel_reason_id": 352}, retry=False)
        except (httpx.HTTPError, ValueError) as e:
            raise OzonCancelError(f"cancel failed for {order.order_id}: {e}") from e
        if not isinstance(data, dict) or not data.get("result"):
            raise OzonCancelError(f"unexpected cancel response for {order.order_id}: {data!r}")

