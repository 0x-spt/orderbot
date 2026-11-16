import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from orderbot.domain import MAIN, Order, OrderLine
from orderbot.marketplaces.base import StockAt, StockError, ToShip, ToShipPartial, https_url

log = logging.getLogger(__name__)

BASE_URL = "https://api.partner.market.yandex.ru"
PAGE_LIMIT = 50
MSK = timezone(timedelta(hours=3))
# The only status change this code ever sends.
CONFIRM_BODY = {"order": {"status": "PROCESSING", "substatus": "READY_TO_SHIP"}}
READY_PROCESSING_SUBSTATUSES = {"READY_TO_SHIP", "SHIPPED"}
READY_STATUSES = {"DELIVERY", "PICKUP", "DELIVERED"}
# Yandex marks an order the buyer asked to withdraw with this flag. The code only READS it
# and never sends any withdrawal call; the repo test that scans src/ allows exactly that line.
# Flagged orders are never awaiting and never ready, so the robot neither confirms them nor
# counts them as assembled stock.
BUYER_STOP_FLAG = "cancelRequested"  # read-only flag: the buyer asked to drop the order


class YandexConfirmError(Exception):
    pass


class YandexCancelError(Exception):
    pass


def _retryable(e):
    if isinstance(e, httpx.HTTPStatusError):
        c = e.response.status_code
        return c >= 500 or c == 429
    return True


def _dt(s, fmt):
    try:
        return datetime.strptime(s, fmt).replace(tzinfo=MSK) if s else None
    except ValueError:
        return None


class YandexMarket:
    """All FBS shops (campaigns) of one business. `warehouses` maps campaign id -> 1C
    warehouse; `campaign_id`, if given, restricts the robot to that single campaign."""
    name = "yandex"

    def __init__(self, token, campaign_id=None, warehouses=None, paid_field=None,
                 business_id=None, labels_dir="data/labels", base_url=BASE_URL,
                 retry_delay=1.0, express=(), account=MAIN):
        self.account = account   # the seller company of this token; its orders carry it
        self.express = {str(c) for c in (express or ())}
        table = {str(k): v for k, v in (warehouses or {}).items()}
        if campaign_id:
            table = {str(campaign_id): table.get(str(campaign_id))}
        self.warehouses = table
        self.campaigns = list(table)
        self.paid_field = paid_field
        self.business_id = str(business_id) if business_id else None
        self.labels_dir = Path(labels_dir)
        self.retry_delay = retry_delay
        self._brands: dict[str, str | None] = {}
        self._photos: dict[str, str | None] = {}
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30,
                                       headers={"Api-Key": token})

    async def _request(self, method, path, retry=True, **kw):
        attempts = 3 if retry else 1
        for i in range(attempts):
            try:
                r = await self._http.request(method, path, **kw)
                r.raise_for_status()
                return r
            except (httpx.HTTPStatusError, httpx.TransportError) as e:
                if i == attempts - 1 or not _retryable(e):
                    raise
                await asyncio.sleep(self.retry_delay)

    async def _list(self, campaign, substatus):
        out, token = [], None
        while True:
            params = {"status": "PROCESSING", "substatus": substatus, "limit": PAGE_LIMIT}
            if token:
                params["pageToken"] = token
            data = (await self._request(
                "GET", f"/v2/campaigns/{campaign}/orders", params=params)).json()
            out += data.get("orders", [])
            token = (data.get("paging") or {}).get("nextPageToken")
            if not token:
                return out

    async def _list_all(self, substatus):
        """[(campaign, raw order)] over every campaign; any campaign failing raises."""
        out = []
        for c in self.campaigns:
            out += [(c, o) for o in await self._list(c, substatus)
                    if not o.get(BUYER_STOP_FLAG)]
        return out

    async def _fetch_in(self, campaign, order_id):
        r = await self._request("GET", f"/v2/campaigns/{campaign}/orders/{order_id}")
        return r.json()["order"]

    async def _find(self, order_id, campaign=None):
        """(campaign, raw order); without a known campaign looks through all of them."""
        if campaign:
            return str(campaign), await self._fetch_in(campaign, order_id)
        for c in self.campaigns:
            try:
                return c, await self._fetch_in(c, order_id)
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (400, 404):
                    raise
        raise KeyError(f"yandex: order {order_id} not found in campaigns {self.campaigns}")

    async def _fetch(self, order_id, campaign=None):
        return (await self._find(order_id, campaign))[1]

    async def _fill_brands(self, offer_ids):
        todo = sorted({o for o in offer_ids if o not in self._brands})
        if not todo:
            return
        if self.business_id:
            data = (await self._request(
                "POST", f"/v2/businesses/{self.business_id}/offer-mappings",
                json={"offerIds": todo})).json()
            for m in (data.get("result") or {}).get("offerMappings", []):
                offer = m.get("offer") or {}
                if offer.get("offerId"):
                    self._brands[offer["offerId"]] = offer.get("vendor") or None
                    self._photos[offer["offerId"]] = https_url(offer.get("pictures"))
        for o in todo:
            self._brands.setdefault(o, None)

    async def photo_url(self, article):
        """offer.pictures[0] of this offerId, from the brand request (read-only)."""
        await self._fill_brands([article])
        return self._photos.get(article)

    @staticmethod
    def _points(item):
        """Line total paid with Yandex Plus points: subsidies[] of type YANDEX_CASHBACK.
        The amount is per unit (verified live: buyerPrice + YANDEX_CASHBACK +
        SUBSIDY == buyerPriceBeforeDiscount, also for count 2..5). None when there are none."""
        try:
            per_unit = sum((Decimal(str(s.get("amount") or 0))
                            for s in item.get("subsidies") or []
                            if s.get("type") == "YANDEX_CASHBACK"), Decimal(0))
            return per_unit * item["count"] if per_unit else None
        except (InvalidOperation, KeyError, TypeError, AttributeError):
            return None

    @staticmethod
    def _promos(item):
        """only the Market promo code, like Ozon's «🏷» (not every discount)."""
        types = {p.get("type") for p in item.get("promos") or [] if isinstance(p, dict)}
        return ["Промокод Маркета"] if "MARKET_PROMOCODE" in types else []

    def _paid(self, item):
        """Line total paid. Unit price x count: buyerPrice is per unit (verified live
        with count 2). Plus points are added back: buyerPrice is what
        the buyer paid in money, the points the Market pays to the seller."""
        if not self.paid_field or item.get(self.paid_field) is None:
            return None
        try:
            total = Decimal(str(item[self.paid_field])) * item["count"]
        except (InvalidOperation, KeyError, TypeError):
            return None
        return total + (self._points(item) or 0)

    def _warehouse(self, campaign):
        return self.warehouses.get(str(campaign))

    async def _to_order(self, campaign, o) -> Order:
        items = o.get("items", [])
        await self._fill_brands([x["offerId"] for x in items])
        wh = self._warehouse(campaign)
        lines = [OrderLine(article=x["offerId"], qty=x["count"], warehouse=wh,
                           paid_total=self._paid(x), brand=self._brands.get(x["offerId"]),
                           promos=self._promos(x), points=self._points(x))
                 for x in items]
        ships = (o.get("delivery") or {}).get("shipments") or []
        deadline = _dt(ships[0].get("shipmentDate"), "%d-%m-%Y") if ships else None
        express = str(campaign) in self.express
        return Order(marketplace=self.name, order_id=str(o["id"]), lines=lines,
                     created_at=_dt(o.get("creationDate"), "%d-%m-%Y %H:%M:%S"),
                     deadline=deadline,
                     status=f"{o.get('status', '')}/{o.get('substatus', '')}",
                     campaign=str(campaign), express=express,
                     mp_warehouse="СЕВЕР EXPRESS" if express else wh,
                     account=self.account)

    async def new_orders(self):
        return [await self._to_order(c, o) for c, o in await self._list_all("STARTED")]

    async def get_order(self, order_id):
        c, o = await self._find(order_id)
        return await self._to_order(c, o)

    async def ready_quantities(self):
        totals: dict[tuple[str, str], int] = {}
        for c, o in await self._list_all("READY_TO_SHIP"):
            wh = self._warehouse(c)
            for x in o.get("items", []):
                if wh is None:
                    log.warning("yandex: campaign %s has no warehouse mapping, skipping %s in %s",
                                c, x["offerId"], o["id"])
                    continue
                key = (x["offerId"], wh)
                totals[key] = totals.get(key, 0) + x["count"]
        return totals

    # ------------------------------------------------------------ shipment reminders
    # VERIFIED live (read-only): delivery.shipments[].shipmentDate is the day
    # ("DD-MM-YYYY", no time) the order is handed over; one shipment per order was seen.
    def shipment_warehouses(self):
        return {wh for c, wh in self.warehouses.items() if wh and c not in self.express}

    async def to_ship(self, only=None):
        """Raises ToShipPartial when some campaigns failed: their warehouses are marked,
        the others are counted (a warehouse with a failed campaign is failed as a whole)."""
        out, failed = [], {}
        for c in self.campaigns:
            wh = self._warehouse(c)
            if wh is None or c in self.express or (only is not None and wh not in only):
                continue
            try:
                out += await self._campaign_to_ship(c, wh)
            except Exception as e:
                failed.setdefault(wh, e)
        if failed:
            raise ToShipPartial([x for x in out if x.warehouse not in failed], failed)
        return out

    async def _campaign_to_ship(self, c, wh):
        out = []
        for substatus in ("STARTED", "READY_TO_SHIP"):
            for o in await self._list(c, substatus):
                if o.get(BUYER_STOP_FLAG):
                    continue
                ships = (o.get("delivery") or {}).get("shipments") or []
                day = _dt(ships[0].get("shipmentDate"), "%d-%m-%Y") if ships else None
                if day is None:
                    continue
                out.append(ToShip(str(o["id"]), wh, day.date(), substatus != "STARTED",
                                  sum(int(x.get("count") or 0) for x in o.get("items", []))))
        return out

    async def is_awaiting(self, order):
        o = await self._fetch(order.order_id, order.campaign)
        return (not o.get(BUYER_STOP_FLAG) and o.get("status") == "PROCESSING"
                and o.get("substatus") == "STARTED")

    async def is_ready(self, order):
        o = await self._fetch(order.order_id, order.campaign)
        if o.get(BUYER_STOP_FLAG):
            return False
        st = o.get("status")
        if st == "PROCESSING":
            return o.get("substatus") in READY_PROCESSING_SUBSTATUSES
        return st in READY_STATUSES

    async def confirm(self, order):
        oid = order.order_id
        campaign = order.campaign
        if not campaign:
            try:
                campaign = (await self._find(oid))[0]
            except (KeyError, httpx.HTTPError) as e:
                raise YandexConfirmError(f"campaign of {oid} unknown: {e}") from e
        base = f"/v2/campaigns/{campaign}/orders/{oid}"
        try:
            labels = await self._request("GET", f"{base}/delivery/labels")
        except httpx.HTTPError as e:
            raise YandexConfirmError(f"labels failed for {oid}: {e}") from e
        if not labels.content:
            raise YandexConfirmError(f"empty labels for {oid}")
        self.labels_dir.mkdir(parents=True, exist_ok=True)
        (self.labels_dir / f"{oid}.pdf").write_bytes(labels.content)
        try:
            r = await self._request("PUT", f"{base}/status", retry=False, json=CONFIRM_BODY)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise YandexConfirmError(f"status change failed for {oid}: {e}") from e
        o = data.get("order") if isinstance(data, dict) else None
        if not isinstance(o, dict) or o.get("status") != "PROCESSING" \
                or o.get("substatus") != "READY_TO_SHIP":
            raise YandexConfirmError(f"unexpected status response for {oid}: {data!r}")

    # ------------------------------------------------------------ stock sync
    # VERIFIED live (read-only): POST /v2/campaigns/{id}/offers/stocks
    # {"offerIds": [...]} -> result.warehouses[].offers[] {offerId, stocks[{type, count}]},
    # types FIT (free + reserved), FREEZE (reserved), AVAILABLE (free) — the official
    # getStocks page defines them. GET /v2/businesses/{id}/warehouses -> warehouses[] and
    # warehouseGroups[] {mainWarehouse, warehouses[]}: Альфа «Север» and «Север EXPRESS»
    # are ONE group with one shared stock (the read showed the same FIT/FREEZE/updatedAt).
    # Write PUT /v2/campaigns/{id}/offers/stocks {"skus": [{"sku", "items": [{"count"}]}]}:
    # official updateStocks page (count = "количество доступного товара"; for a group send
    # one warehouse, the others follow). UNVERIFIED: whether `count` is FIT or AVAILABLE —
    # the sync writes AVAILABLE - qty, never more than what is free.
    async def stock_read(self, campaign, article):
        data = (await self._request("POST", f"/v2/campaigns/{campaign}/offers/stocks",
                                    json={"offerIds": [article]})).json()
        for wh in ((data or {}).get("result") or {}).get("warehouses") or []:
            for offer in wh.get("offers") or []:
                if offer.get("offerId") != article:
                    continue
                c = {x.get("type"): int(x.get("count") or 0) for x in offer.get("stocks") or []}
                if not c:
                    return None
                freeze = c.get("FREEZE", 0)
                free = c["AVAILABLE"] if "AVAILABLE" in c else c.get("FIT", 0) - freeze
                return StockAt(count=free, ref=None, reserved=freeze)
        return None

    async def stock_write(self, campaign, article, ref, count):
        body = {"skus": [{"sku": article, "items": [{"count": int(count)}]}]}
        r = await self._request("PUT", f"/v2/campaigns/{campaign}/offers/stocks", retry=False,
                                json=body)
        try:
            status = r.json().get("status")
        except Exception:
            status = None
        if status != "OK":
            raise StockError(f"Маркет не ответил OK: {status}")

    async def stock_groups(self):
        """campaign -> (group key, campaign to read and write the group through, name).
        Campaigns of one warehouse group share one stock (VERIFIED live)."""
        if not self.business_id:
            raise StockError("нет YM_BUSINESS_ID: группы складов неизвестны")
        out = {}
        token = None
        while True:
            params = {"pageToken": token} if token else None
            data = (await self._request("GET", f"/v2/businesses/{self.business_id}/warehouses",
                                        params=params)).json()
            res = (data or {}).get("result") or {}
            for w in res.get("warehouses") or []:
                c = str(w.get("campaignId"))
                out[c] = (c, c, w.get("name") or c)
            for g in res.get("warehouseGroups") or []:
                main = str(((g.get("mainWarehouse") or {}).get("campaignId")) or "")
                if not main:
                    raise StockError("у группы складов нет основного склада")
                for w in g.get("warehouses") or []:
                    c = str(w.get("campaignId"))
                    out[c] = (f"g:{main}", main, w.get("name") or c)
            token = (res.get("paging") or {}).get("nextPageToken")
            if not token:
                return out

    async def cancel_by_human(self, order):
        # Verified against yandex.ru/dev/market/partner-api (updateOrderStatus):
        # PUT /v2/campaigns/{id}/orders/{id}/status, order.status required, substatus
        # optional in the schema; the docs name SHOP_FAILED for a seller who cannot fulfil.
        oid = order.order_id
        campaign = order.campaign
        if not campaign:
            try:
                campaign = (await self._find(oid))[0]
            except (KeyError, httpx.HTTPError) as e:
                raise YandexCancelError(f"campaign of {oid} unknown: {e}") from e
        body = {"order": {"status": "CANCELLED", "substatus": "SHOP_FAILED"}}
        try:
            await self._request("PUT", f"/v2/campaigns/{campaign}/orders/{oid}/status",
                                retry=False, json=body)
        except httpx.HTTPError as e:
            raise YandexCancelError(f"cancel failed for {oid}: {e}") from e
