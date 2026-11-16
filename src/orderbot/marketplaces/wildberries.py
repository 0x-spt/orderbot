"""Wildberries FBS adapter (marketplace API v3).

One WB assembly task (сборочное задание) is one unit of one article, so every Order has a
single line with qty 1. Money fields come in kopecks and are divided by 100.

Statuses are read only as positive lists: an order is awaiting while supplierStatus is
"new" and wbStatus is "waiting"; it counts as assembled stock while supplierStatus is
"confirm". Any other combination is neither awaiting nor ready.
"""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from orderbot.domain import MAIN, Order, OrderLine
from orderbot.fx import RUBLE, Rates, grouped
from orderbot.marketplaces.base import StockAt, StockError, https_url

log = logging.getLogger(__name__)

BASE_URL = "https://marketplace-api.wildberries.ru"
CONTENT_URL = "https://content-api.wildberries.ru"
PAID_FIELDS = ("price", "salePrice", "convertedPrice", "finalPrice")
LIST_DAYS = 30
LIST_LIMIT = 1000
STATUS_CHUNK = 1000
AWAITING_SUPPLIER = "new"
AWAITING_WB = "waiting"
READY_SUPPLIER = {"confirm", "complete"}  # on assembly (in a supply) or handed over
ASSEMBLED_SUPPLIER = "confirm"            # assembled, not yet handed over: holds stock


class WbConfirmError(Exception):
    def __init__(self, message, supply=None):
        super().__init__(message)
        self.supply = supply    # a created supply left empty: its id must reach the person


class WbCancelError(Exception):
    pass


class WbListError(Exception):
    pass


def _retryable(e):
    if isinstance(e, httpx.HTTPStatusError):
        c = e.response.status_code
        return c >= 500 or c == 429
    return True


def _dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


class WildberriesMarketplace:
    """`warehouses` maps WB warehouseId (as a string) -> 1C warehouse name."""
    name = "wb"
    can_confirm = True

    def __init__(self, token, warehouses=None, paid_field=None, base_url=BASE_URL,
                 content_url=CONTENT_URL, retry_delay=1.0, clock=None, account=MAIN, fx=None):
        self.fx = fx if fx is not None else Rates()   # a sale currency other than rubles
        if paid_field and paid_field not in PAID_FIELDS:
            raise ValueError(f"WB_PAID_FIELD: одно из {', '.join(PAID_FIELDS)}, "
                             f"получено {paid_field!r}")
        self.account = account   # WB is connected for the main company only
        self.warehouses = {str(k): v for k, v in (warehouses or {}).items()}
        self.paid_field = paid_field
        self.retry_delay = retry_delay
        self._clock = clock or (lambda: datetime.now(UTC))
        self._brands: dict[int, str | None] = {}
        self._photos: dict[int, str | None] = {}
        self._nm_by_article: dict[str, int] = {}
        # stock sync: article -> chrtIDs of its content card(s); the card, not an order's
        # chrtId, decides, so an article with several sizes is always refused
        self._card_chrts: dict[str, frozenset[int]] = {}
        self._brands_off = False
        headers = {"Authorization": token}
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30, headers=headers)
        self._content = httpx.AsyncClient(base_url=content_url, timeout=30, headers=headers)

    async def _request(self, client, method, path, retry=True, **kw):
        attempts = 3 if retry else 1
        for i in range(attempts):
            try:
                r = await client.request(method, path, **kw)
                r.raise_for_status()
                return r.json() if r.content else None
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                if i == attempts - 1 or not _retryable(e):
                    raise
                await asyncio.sleep(self.retry_delay * (i + 1))

    # ------------------------------------------------------------ reading
    async def _new_raw(self):
        data = await self._request(self._http, "GET", "/api/v3/orders/new")
        return data.get("orders") or []

    async def _recent_raw(self):
        """Every assembly task created in the last LIST_DAYS days (cursor `next`)."""
        now = self._clock()
        params = {"limit": LIST_LIMIT, "next": 0,
                  "dateFrom": int((now - timedelta(days=LIST_DAYS)).timestamp()),
                  "dateTo": int(now.timestamp())}
        out, seen = [], set()
        while True:
            data = await self._request(self._http, "GET", "/api/v3/orders", params=params)
            page = data.get("orders") or []
            out += page
            nxt = data.get("next")
            if len(page) < LIST_LIMIT:
                return out
            if not nxt or nxt in seen:
                raise WbListError(f"wb list: full page but next is {nxt!r}; refusing to "
                                  "loop or truncate")
            seen.add(nxt)
            params = {**params, "next": nxt}

    async def _statuses(self, ids):
        out = {}
        ids = [int(i) for i in ids]
        for k in range(0, len(ids), STATUS_CHUNK):
            data = await self._request(self._http, "POST", "/api/v3/orders/status",
                                       json={"orders": ids[k:k + STATUS_CHUNK]})
            for s in data.get("orders") or []:
                out[int(s["id"])] = (s.get("supplierStatus"), s.get("wbStatus"))
        return out

    async def _status(self, order_id):
        return (await self._statuses([order_id])).get(int(order_id), (None, None))

    async def _fill_brands(self, nm_ids):
        """Brand per nmId from the content API. A token without the content scope is not
        an error: brands stay unknown (no brand discount) and the API is not asked again."""
        todo = sorted({n for n in nm_ids if n is not None and n not in self._brands})
        if not todo or self._brands_off:
            return
        for nm in todo:
            body = {"settings": {"cursor": {"limit": 100},
                                 "filter": {"textSearch": str(nm), "withPhoto": -1}}}
            try:
                data = await self._request(self._content, "POST", "/content/v2/get/cards/list",
                                           json=body)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    log.warning("wb: токен без доступа к контенту, бренды не известны")
                    self._brands_off = True
                    return
                raise
            for card in data.get("cards") or []:
                if card.get("nmID") is not None:
                    self._brands[int(card["nmID"])] = card.get("brand") or None
                    first = (card.get("photos") or [{}])[0]
                    first = first if isinstance(first, dict) else {}
                    self._photos[int(card["nmID"])] = (https_url(first.get("c516x688"))
                                                       or https_url(first.get("big")))
            self._brands.setdefault(nm, None)

    async def photo_url(self, article):
        """photos[0] of the content card of the nmId this article was ordered under
        (c516x688, else big; read-only). An article never seen in an order gives None."""
        nm = self._nm_by_article.get(article)
        if nm is None:
            return None
        await self._fill_brands([nm])
        return self._photos.get(nm)

    @staticmethod
    def _kopecks(o, field):
        v = o.get(field)
        if v is None or isinstance(v, bool):
            return None
        try:
            amount = Decimal(str(v)) / 100
        except InvalidOperation:
            return None
        return amount if amount.is_finite() else None

    def _paid(self, o):
        if not self.paid_field:
            return None
        return self._kopecks(o, self.paid_field)

    async def _paid_rub(self, o):
        """(paid in rubles, the foreign price as shown). a sale in another
        currency (seen live: currencyCode 51, AMD) is taken in rubles — WB's own ruble value
        (convertedPrice when its currency is the ruble), else the Bank of Russia rate; none
        of them gives no price (a person checks it), never drams taken for rubles."""
        paid = self._paid(o)
        code = o.get("currencyCode")
        if (paid is None or self.paid_field == "convertedPrice" or code is None
                or str(code).lstrip("0") in RUBLE):
            return paid, None
        rub = await self.fx.to_rub(paid, str(code))      # also loads the code's name
        foreign = f"{grouped(paid)} {self.fx.name(code)}"
        conv = self._kopecks(o, "convertedPrice")
        if conv is not None and str(o.get("convertedCurrencyCode", "")).lstrip("0") in RUBLE:
            return conv, foreign
        if rub is None:
            log.warning("no rate for currency %s: WB order %s goes to a person", code, o.get("id"))
        return rub, foreign

    def _warehouse(self, o):
        wid = o.get("warehouseId")
        return self.warehouses.get(str(wid)) if wid is not None else None

    async def _to_order(self, o, status="") -> Order:
        nm = o.get("nmId")
        if isinstance(nm, int) and not isinstance(nm, bool):
            self._nm_by_article[o["article"]] = nm   # for photo_url
        await self._fill_brands([nm])
        paid, foreign = await self._paid_rub(o)
        line = OrderLine(article=o["article"], qty=1, warehouse=self._warehouse(o),
                         paid_total=paid, brand=self._brands.get(nm), paid_foreign=foreign)
        order = Order(marketplace=self.name, order_id=str(o["id"]), lines=[line],
                      created_at=_dt(o.get("createdAt")), deadline=None, status=status,
                      mp_warehouse=line.warehouse, account=self.account)
        return order

    async def new_orders(self):
        raw = await self._new_raw()
        if not raw:
            return []
        st = await self._statuses([o["id"] for o in raw])
        out = []
        for o in raw:
            s = st.get(int(o["id"]), (None, None))
            if s != (AWAITING_SUPPLIER, AWAITING_WB):
                continue
            out.append(await self._to_order(o, f"{s[0]}/{s[1]}"))
        return out

    async def get_order(self, order_id):
        oid = int(order_id)
        for o in await self._new_raw() + await self._recent_raw():
            if int(o["id"]) == oid:
                s = await self._status(oid)
                return await self._to_order(o, f"{s[0]}/{s[1]}")
        raise KeyError(f"wb: order {order_id} not found in the last {LIST_DAYS} days")

    async def ready_quantities(self):
        raw = await self._recent_raw()
        st = await self._statuses([o["id"] for o in raw]) if raw else {}
        totals: dict[tuple[str, str], int] = {}
        for o in raw:
            if st.get(int(o["id"]), (None, None))[0] != ASSEMBLED_SUPPLIER:
                continue
            wh = self._warehouse(o)
            if wh is None:
                log.warning("wb: warehouse %r has no mapping, skipping %s in %s",
                            o.get("warehouseId"), o.get("article"), o.get("id"))
                continue
            key = (o["article"], wh)
            totals[key] = totals.get(key, 0) + 1
        return totals

    async def is_awaiting(self, order):
        return await self._status(order.order_id) == (AWAITING_SUPPLIER, AWAITING_WB)

    async def is_ready(self, order):
        return (await self._status(order.order_id))[0] in READY_SUPPLIER

    async def confirm(self, order):
        # VERIFIED against the official OpenAPI
        # (https://dev.wildberries.ru/api/swagger/yaml/ru/03-orders-fbs.yaml):
        # POST /api/v3/supplies {"name"} -> 201 {"id"};
        # PATCH /api/marketplace/v3/supplies/{id}/orders {"orders": [<int id>]} -> 204.
        # No retries: a retry could create a second supply.
        oid = order.order_id
        try:
            data = await self._request(self._http, "POST", "/api/v3/supplies", retry=False,
                                       json={"name": oid})
        except (httpx.HTTPError, ValueError) as e:
            raise WbConfirmError(f"wb {oid}: supply creation failed, it may still have been "
                                 f"created: check the WB cabinet for a supply named {oid}: {e}") from e
        supply = data.get("id") if isinstance(data, dict) else None
        if not supply:
            raise WbConfirmError(f"wb {oid}: unexpected supply response {data!r}")
        try:
            await self._request(self._http, "PATCH",
                                f"/api/marketplace/v3/supplies/{supply}/orders",
                                retry=False, json={"orders": [int(oid)]})
        except (httpx.HTTPError, ValueError) as e:
            raise WbConfirmError(f"wb {oid}: order not added to supply {supply}: {e}",
                                 supply=supply) from e

    # ------------------------------------------------------------ stock sync
    # VERIFIED against the official OpenAPI
    # (https://dev.wildberries.ru/api/swagger/yaml/ru/02-items.yaml, «Остатки на складах
    # продавца») and live (read-only): stocks are keyed by chrtId (the size id; by barcode
    # the API answers "Uploading stock is not allowed by 'sku'"). Read POST
    # /api/v3/stocks/{warehouseId} {"chrtIds": [...]} -> {"stocks": [{chrtId, amount}]}
    # (an unknown chrtId gives an empty list); write PUT /api/v3/stocks/{warehouseId}
    # {"stocks": [{chrtId, amount}]} -> 204. chrtId: only from the content card with
    # vendorCode == article, exactly one size (sizes[].chrtID; the card answer is cached and
    # the size count checked on every use). The write itself: no live call.
    async def _chrt(self, article):
        if article not in self._card_chrts:
            body = {"settings": {"cursor": {"limit": 100},
                                 "filter": {"textSearch": article, "withPhoto": -1}}}
            data = await self._request(self._content, "POST", "/content/v2/get/cards/list",
                                       json=body)
            self._card_chrts[article] = frozenset(
                int(sz["chrtID"]) for card in (data or {}).get("cards") or []
                if card.get("vendorCode") == article
                for sz in card.get("sizes") or [] if sz.get("chrtID") is not None)
        chrts = self._card_chrts[article]
        if len(chrts) > 1:          # checked on every call, cached or not
            raise StockError("у артикула несколько размеров")
        return next(iter(chrts), None)

    async def stock_read(self, warehouse, article):
        chrt = await self._chrt(article)
        if chrt is None:
            return None
        data = await self._request(self._http, "POST", f"/api/v3/stocks/{warehouse}",
                                   json={"chrtIds": [chrt]})
        for row in (data or {}).get("stocks") or []:
            if row.get("chrtId") == chrt:
                return StockAt(count=int(row.get("amount") or 0), ref=chrt)
        return None

    async def stock_write(self, warehouse, article, ref, count):
        await self._request(self._http, "PUT", f"/api/v3/stocks/{warehouse}", retry=False,
                            json={"stocks": [{"chrtId": int(ref), "amount": int(count)}]})

    async def cancel_by_human(self, order):
        # VERIFIED (see confirm): PATCH /api/v3/orders/{id}/cancel,
        # 204, no body, no reason. One request only.
        try:
            await self._request(self._http, "PATCH", f"/api/v3/orders/{order.order_id}/cancel",
                                retry=False)
        except (httpx.HTTPError, ValueError) as e:
            raise WbCancelError(f"cancel failed for {order.order_id}: {e}") from e
