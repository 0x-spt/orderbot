"""Lamoda FBS orders with their labels in the group topic «Этикетки Lamoda».

Every Lamoda order in status «Ждёт отгрузки» ("Awaiting shipment") is posted once to the
ALERT_CHAT_ID group, topic LAMODA_THREAD_ID, in the employees' format; its package and item
labels follow as PDF documents replying to that message. No 1C, no buttons, no pause: this
part runs as its own loop beside the order robot and never changes anything at Lamoda (all
calls below only read lists and label addresses).

APIs (checked live):
- api-b2b.lamoda.ru: token (client credentials, ~15 min), the order list (newest first, no
  status filter works: it is paged back LOOKBACK_DAYS) and the order's items.
- seller.lamoda.ru/jsonrpc: packs of an order and the label PDFs' addresses. These are the
  seller cabinet's own methods, not documented: a failure there posts the order anyway with
  a warning line and the labels are tried again on the next polls, MAX_LABEL_ATTEMPTS times.

Exactly once: the posted orders are kept in the Store (table lamoda_orders). First run (no
baseline yet, set only after a successful list read): every order awaiting shipment at that
moment counts as posted, whatever its date (people handled them by hand); only orders that
reach the status later are posted. The list is read LOOKBACK_DAYS back: an order created
earlier that reaches the status only now is not seen.

The order message goes first, the labels after it: while the group cannot be reached no
label is downloaded. Sent label files are remembered by name, so a retry sends only the
missing ones.

Customer data (name, phone, address in the order) is never read into messages or logs:
only the order number, the items' SKU, name and status, and pack numbers are used.
"""
import asyncio
import html
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from aiogram.types import BufferedInputFile, InputMediaDocument, ReplyParameters

from orderbot.alerts import MSK

log = logging.getLogger(__name__)

API_URL = "https://api-b2b.lamoda.ru"
SELLER_URL = "https://seller.lamoda.ru/jsonrpc/v1"
AWAITING = "Awaiting shipment"
POLL_INTERVAL = 300.0
LOOKBACK_DAYS = 14
LIST_LIMIT = 100          # the most Lamoda gives per page (checked: 500 is cut to 100)
MAX_LIST_PAGES = 10
PACK_PAGE = 100
HTTP_TIMEOUT = 20.0
MAX_PDF_BYTES = 10 * 1024 * 1024
TOKEN_MARGIN = 60.0       # seconds before expiry when the token is taken anew
MAX_LABEL_ATTEMPTS = 12   # polls (5 min apart) a label failure is tried: about an hour
ALERT_AFTER_FAILURES = 12  # failed polls in a row before the admins are told, once
GROUP_ALERT_AFTER = 6     # failed sends to the group in a row before the admins are told
LABEL_HOST_SUFFIX = ".yandexcloud.net"   # where Lamoda keeps the label PDFs (checked)
GROUP_MAX = 10            # Telegram: at most 10 documents in one media group
ITEM_LABEL_SIZE = "s"
PACK_LABEL_SIZE = "m"
WARNING = "\n\n⚠️ Этикетки не получены: {reason}"
GAVE_UP = ("⚠️ Этикетки по заказу № {nr} так и не получены. Скачайте их в кабинете "
           "Lamoda.")
GROUP_DOWN = ("Этикетки Lamoda: группа недоступна, сообщения о заказах не уходят. Робот "
              "пробует дальше, подробности в журнале на Mac.")
PLAN_CHANGED = ("⚠️ Состав этикеток заказа № {nr} изменился после отправки части файлов. "
                "Скачайте этикетки в кабинете Lamoda.")
# the ⚠️ line names the step that failed, never the server's own words
REASONS = {"packs": "Lamoda не отдала пакеты заказа",
           "plan": "в заказе есть товар без пакета",
           "label": "Lamoda не дала ссылку на этикетку",
           "download": "этикетка не скачалась",
           "other": "сбой робота"}
LIST_DOWN = ("Этикетки Lamoda: не удаётся получить заказы ({reason}). Робот пробует "
             "дальше, подробности в журнале на Mac.")
# outages go to the health topic, HTML
LIST_DOWN_HTML = ("🔴 <b>Lamoda — API не отвечает</b>\n{reason}\n"
                  "Новые заказы и этикетки не приходят")
LIST_UP_HTML = "🟢 <b>Lamoda — API снова отвечает</b>\nЭтикетки снова приходят"


class LamodaError(Exception):
    """A Lamoda call failed; the message is short and holds no key or address."""


@dataclass(frozen=True)
class Item:
    nr: str          # RU000000-000001-001
    sku: str         # MRS181-RIA(MRS181-00918W)
    name: str        # Салатник BRAND-X


@dataclass(frozen=True)
class Label:
    filename: str
    kind: str        # "pack" or "item"
    number: str      # pack number or item number


@dataclass(frozen=True)
class AwaitingOrder:
    nr: str
    partner_id: int | None
    created: date | None


# ------------------------------------------------------------------ texts and files
def article(sku: str) -> str:
    """The seller's article: the SKU before the parentheses."""
    return sku.split("(", 1)[0].strip()


def order_text(order_nr: str, items: list[Item]) -> str:
    """The employees' message (HTML): one numbered line per unit, as Lamoda lists them;
    the heading and the total in bold with a 🚚, so it stands out."""
    e = html.escape
    lines = [f"🚚 <b>Номер заказа: № {e(order_nr)}</b>"]
    lines += [f"{n}. {e(it.sku)}" + (f"({e(it.name)})" if it.name else "") + " x 1"
              for n, it in enumerate(items, 1)]
    lines.append(f"<b>Всего товаров:</b> {len(items)} шт.")
    return "\n\n".join(lines)


def label_plan(items: list[Item], packs: dict[str, list[str]]) -> list[Label]:
    """Per unit: its package label (once per pack), then its item label. Units of an
    article that occurs more than once are numbered 1, 2, … in the order's item order."""
    pack_of = {nr: pn for pn, nrs in packs.items() for nr in nrs}
    total: dict[str, int] = {}
    for it in items:
        total[article(it.sku)] = total.get(article(it.sku), 0) + 1
    seen: dict[str, int] = {}
    done_packs: set[str] = set()
    plan = []
    for it in items:
        art = article(it.sku)
        seen[art] = seen.get(art, 0) + 1
        suffix = f" {seen[art]}" if total[art] > 1 else ""
        pack = pack_of.get(it.nr)
        if pack is None:
            raise LamodaError(f"нет пакета для товара {it.nr}")
        if pack not in done_packs:
            done_packs.add(pack)
            plan.append(Label(f"{art} пакет{suffix}.pdf", "pack", pack))
        plan.append(Label(f"{art} товар{suffix}.pdf", "item", it.nr))
    return plan


def document_chunks(docs: list) -> list[list]:
    return [docs[i:i + GROUP_MAX] for i in range(0, len(docs), GROUP_MAX)]


def _created(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _short(text, limit=80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ------------------------------------------------------------------ the API
class LamodaApi:
    def __init__(self, client_id: str, client_secret: str, *, transport=None,
                 clock=time.monotonic, api_url=API_URL, seller_url=SELLER_URL):
        self._id = client_id
        self._secret = client_secret
        self._transport = transport
        self._clock = clock
        self._api = api_url
        self._seller = seller_url
        self._token: str | None = None
        self._token_until = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT), follow_redirects=False,
                                 transport=self._transport)

    async def _get_token(self) -> str:
        if self._token and self._clock() < self._token_until:
            return self._token
        try:
            # the secret goes in the form body (checked live), never in the address
            async with self._client() as c:
                r = await c.post(f"{self._api}/auth/token", data={
                    "client_id": self._id, "client_secret": self._secret,
                    "grant_type": "client_credentials"})
            if r.status_code != 200:
                raise LamodaError(f"токен Lamoda: ответ {r.status_code}")
            data = r.json()
            token = data["access_token"]
            ttl = float(data.get("expires_in") or 900)
        except LamodaError:
            raise
        except httpx.HTTPError as e:
            raise LamodaError(f"токен Lamoda: нет связи ({type(e).__name__})") from None
        except (ValueError, KeyError, TypeError):
            raise LamodaError("токен Lamoda: непонятный ответ") from None
        self._token, self._token_until = token, self._clock() + max(ttl - TOKEN_MARGIN, 30.0)
        return token

    async def _call(self, method: str, url: str, **kw):
        """An authorised request; a 401 takes a new token once. Returns the JSON."""
        for attempt in (1, 2):
            token = await self._get_token()
            try:
                async with self._client() as c:
                    r = await c.request(method, url, headers={"Authorization": f"Bearer {token}"},
                                        **kw)
            except httpx.HTTPError as e:
                raise LamodaError(f"нет связи с Lamoda ({type(e).__name__})") from None
            if r.status_code == 401 and attempt == 1:
                self._token = None
                continue
            if r.status_code != 200:
                raise LamodaError(f"Lamoda ответила {r.status_code}")
            try:
                return r.json()
            except ValueError:
                raise LamodaError("Lamoda прислала не JSON") from None
        raise LamodaError("Lamoda не принимает токен")   # pragma: no cover

    async def _rpc(self, path: str, method: str, params: dict):
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        data = await self._call("POST", f"{self._seller}/{path}", json=body)
        if not isinstance(data, dict) or data.get("error") or "result" not in data:
            err = data.get("error") if isinstance(data, dict) else None
            detail = _short(err.get("message", "")) if isinstance(err, dict) else ""
            raise LamodaError(f"кабинет Lamoda: ошибка {method}" + (f" ({detail})" if detail
                                                                     else ""))
        return data["result"]

    async def awaiting_orders(self, today: date) -> list[AwaitingOrder]:
        """Orders awaiting shipment created in the last LOOKBACK_DAYS, newest first."""
        since = today - timedelta(days=LOOKBACK_DAYS)
        out = []
        for page in range(1, MAX_LIST_PAGES + 1):
            data = await self._call("GET", f"{self._api}/api/v1/orders",
                                    params={"limit": LIST_LIMIT, "page": page})
            try:
                orders = data.get("_embedded", {}).get("orders") or []
                pages = int(data.get("pages") or 1)
            except (AttributeError, TypeError, ValueError):
                raise LamodaError("список заказов Lamoda: непонятный ответ") from None
            older = False
            for o in orders:
                created = _created(o.get("createdAt"))
                if created is not None and created < since:
                    older = True
                    continue
                if o.get("status") != AWAITING or not o.get("orderNr"):
                    continue
                partner = ((o.get("_embedded") or {}).get("partner") or {}).get("partnerId")
                out.append(AwaitingOrder(str(o["orderNr"]),
                                         int(partner) if partner is not None else None,
                                         created))
            if older or page >= pages or not orders:
                break
        return out

    async def items(self, order_nr: str) -> list[Item]:
        """The order's units awaiting shipment (a dropped unit has another status)."""
        data = await self._call("GET", f"{self._api}/api/v1/orders/{order_nr}")
        try:
            raw = (data.get("_embedded") or {}).get("items") or []
            return [Item(str(i["id"]), str(i["sku"]), str(i.get("description") or ""))
                    for i in raw if i.get("status") == AWAITING]
        except (AttributeError, KeyError, TypeError):
            raise LamodaError("заказ Lamoda: непонятный ответ") from None

    async def packs(self, partner_id, order_nr: str) -> dict[str, list[str]]:
        """pack number -> item numbers. `order_nrs` (the singular is ignored: everything)."""
        if partner_id is None:
            raise LamodaError("в заказе нет номера продавца")
        out: dict[str, list[str]] = {}
        page = 1
        while True:
            res = await self._rpc("fbs/order/pack.list", "v1.fbs.order.pack.list", {
                "partner_id": partner_id, "order_nrs": [order_nr],
                "limit": PACK_PAGE, "page": page})
            try:
                data = res.get("data") or []
                total = int((res.get("pagination") or {}).get("total") or 0)
                for p in data:
                    if p.get("order_nr") == order_nr:
                        out[str(p["pack_number"])] = [str(n) for n in p.get("item_nrs") or []]
            except (AttributeError, KeyError, TypeError, ValueError):
                raise LamodaError("пакеты Lamoda: непонятный ответ") from None
            if not data or page * PACK_PAGE >= total or page >= 50:
                return out
            page += 1

    async def label_url(self, kind: str, number: str) -> str:
        if kind == "item":
            res = await self._rpc("label-items.get", "v1.label-items.get",
                                  {"item_nrs": [number], "size": ITEM_LABEL_SIZE})
        else:
            res = await self._rpc("label-packs.get", "v1.label-packs.get",
                                  {"pack_numbers": [number], "size": PACK_LABEL_SIZE})
        url = res.get("url") if isinstance(res, dict) else None
        if not url:
            raise LamodaError("Lamoda не дала ссылку на этикетку")
        return url

    async def download_pdf(self, url: str) -> bytes:
        """The label from the address Lamoda gave: https only, no redirects, at most
        MAX_PDF_BYTES within HTTP_TIMEOUT, and it must be a PDF."""
        if not isinstance(url, str) or not url.startswith("https://"):
            raise LamodaError("ссылка на этикетку не https")
        host = (urlsplit(url).hostname or "").lower()
        if not host.endswith(LABEL_HOST_SUFFIX):
            raise LamodaError(f"этикетка на чужом адресе ({host or '?'})")
        try:
            return await asyncio.wait_for(self._download(url), HTTP_TIMEOUT)
        except LamodaError:
            raise
        except (TimeoutError, httpx.HTTPError) as e:
            raise LamodaError(f"этикетка не скачалась ({type(e).__name__})") from None

    async def _download(self, url: str) -> bytes:
        async with self._client() as c:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    raise LamodaError(f"этикетка не скачалась (ответ {r.status_code})")
                length = r.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_PDF_BYTES:
                    raise LamodaError("этикетка больше допустимого размера")
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_PDF_BYTES:
                        raise LamodaError("этикетка больше допустимого размера")
        if not bytes(data[:5]) == b"%PDF-":
            raise LamodaError("этикетка не PDF")
        return bytes(data)


# ------------------------------------------------------------------ the poster
def _utc_now() -> datetime:
    return datetime.now(UTC)


class LamodaLabels:
    def __init__(self, api: LamodaApi, store, bot, chat_id: int, thread_id: int, *,
                 clock=_utc_now, say=None, health=None):
        self._api = api
        self._store = store
        self._bot = bot
        self._chat = chat_id
        self._thread = thread_id
        self._clock = clock
        self._say = say                 # admins' system message, for a long outage
        self._health = health           # the health topic (HTML), for the API outage
        self._failures = 0              # polls in a row whose order list failed
        self._down_said = False
        self._group_failures = 0        # sends to the group in a row that failed
        self._group_said = False
        self._empty_said: set[str] = set()

    async def run(self, stop: asyncio.Event, interval: float = POLL_INTERVAL) -> None:
        while not stop.is_set():
            try:
                await self.poll()
            except Exception:
                log.exception("Lamoda labels poll failed")
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                pass

    async def poll(self) -> None:
        today = _aware(self._clock()).astimezone(MSK).date()
        try:
            orders = await self._api.awaiting_orders(today)
        except Exception as e:
            await self._list_failed(e)
            return
        if self._down_said and self._health is not None:
            await self._tell(self._health, LIST_UP_HTML)
        self._failures, self._down_said = 0, False
        if not self._store.lamoda_baseline_done():
            self._store.lamoda_set_baseline([o.nr for o in orders])
            log.info("Lamoda labels: first run, %d orders awaiting shipment are taken as "
                     "already posted; only orders reaching the status later are posted",
                     len(orders))
            return
        for row in self._store.lamoda_pending():
            try:
                await self._retry(row)
            except Exception:
                log.exception("Lamoda order %s: labels retry failed", row.order_nr)
        for o in reversed(orders):        # Lamoda lists newest first: post the oldest first
            if self._store.lamoda_known(o.nr):
                continue
            try:
                await self._post(o)
            except Exception:
                log.exception("Lamoda order %s: not posted", o.nr)

    async def _tell_admins(self, text) -> bool:
        return await self._tell(self._say, text)

    @staticmethod
    async def _tell(fn, text) -> bool:
        if fn is None:
            return False
        try:
            await fn(text)
            return True
        except Exception:
            log.exception("cannot tell: %s", text)
            return False

    async def _list_failed(self, e: Exception) -> None:
        reason = str(e) if isinstance(e, LamodaError) else type(e).__name__
        log.warning("Lamoda labels: orders not read: %s", reason)
        self._failures += 1
        if self._failures >= ALERT_AFTER_FAILURES and not self._down_said:
            if self._health is not None:
                self._down_said = await self._tell(
                    self._health, LIST_DOWN_HTML.format(reason=html.escape(reason)))
            else:
                self._down_said = await self._tell_admins(LIST_DOWN.format(reason=reason))

    async def _group_failed(self, nr, what) -> None:
        # the labels are for the group only: no private copies; tried again next poll
        log.exception("Lamoda order %s: %s to the group failed", nr, what)
        self._group_failures += 1
        if self._group_failures >= GROUP_ALERT_AFTER and not self._group_said:
            self._group_said = await self._tell_admins(GROUP_DOWN)

    def _group_ok(self) -> None:
        self._group_failures, self._group_said = 0, False

    def _topic(self, reply_to=None) -> dict:
        kw = {"message_thread_id": self._thread}
        if reply_to is not None:
            kw["reply_parameters"] = ReplyParameters(message_id=reply_to)
        return kw

    async def _items(self, nr: str) -> list[Item]:
        items = await self._api.items(nr)
        if not items and nr not in self._empty_said:
            self._empty_said.add(nr)
            log.info("Lamoda order %s: no items awaiting shipment", nr)
        return items

    async def _labels(self, partner_id, nr: str, items: list[Item]):
        """(documents, None) or (None, a fixed short reason naming the failed step)."""
        step = "packs"
        try:
            packs = await self._api.packs(partner_id, nr)
            step = "plan"
            plan = label_plan(items, packs)
            docs = []
            for label in plan:
                step = "label"
                url = await self._api.label_url(label.kind, label.number)
                step = "download"
                docs.append((label.filename, await self._api.download_pdf(url)))
            return docs, None
        except LamodaError as e:
            log.warning("Lamoda order %s: labels not received (%s): %s", nr, step, e)
            return None, REASONS[step]
        except Exception:
            log.exception("Lamoda order %s: labels not received (%s)", nr, step)
            return None, REASONS["other"]

    async def _post(self, o: AwaitingOrder) -> None:
        items = await self._items(o.nr)
        if not items:
            return
        text = order_text(o.nr, items)
        # the message first: while the group is unreachable no label is downloaded
        try:
            msg = await self._bot.send_message(self._chat, text, parse_mode="HTML",
                                               **self._topic())
        except Exception:
            await self._group_failed(o.nr, "message")
            return
        self._group_ok()
        self._store.lamoda_add(o.nr, "pending", o.partner_id, msg.message_id, text)
        await self._attempt(o.nr, o.partner_id, msg.message_id, text, items, [],
                            warned=False, attempts=0)

    async def _retry(self, row) -> None:
        items = await self._items(row.order_nr)
        if not items:
            # shipped or dropped meanwhile: no labels are needed any more
            self._store.lamoda_update(row.order_nr, state="done")
            return
        await self._attempt(row.order_nr, row.partner_id, row.message_id, row.text or "",
                            items, list(row.sent_files), warned=row.warned,
                            attempts=row.attempts)

    async def _edit(self, nr, message_id, text) -> bool:
        try:
            await self._bot.edit_message_text(chat_id=self._chat, message_id=message_id,
                                              text=text, parse_mode="HTML")
            return True
        except Exception:
            log.exception("Lamoda order %s: message not edited", nr)
            return False

    async def _attempt(self, nr, partner_id, message_id, text, items, sent, *, warned,
                       attempts) -> None:
        docs, reason = await self._labels(partner_id, nr, items)
        if reason:
            attempts += 1
            self._store.lamoda_update(nr, attempts=attempts)
            if not warned and await self._edit(nr, message_id,
                                               text + WARNING.format(reason=reason)):
                self._store.lamoda_update(nr, warned=True)
            await self._maybe_give_up(nr, message_id, attempts)
            return
        names = [name for name, _ in docs]
        if set(sent) - set(names):
            log.warning("Lamoda order %s: label files changed after some were sent", nr)
            await self._give_up(nr, message_id, PLAN_CHANGED.format(nr=nr))
            return

        def progress(chunk_names):
            sent.extend(chunk_names)
            self._store.lamoda_update(nr, sent_files=sent)

        try:
            await self._send_documents(message_id, [d for d in docs if d[0] not in sent],
                                       progress)
        except Exception:
            await self._group_failed(nr, "labels")
            self._store.lamoda_update(nr, attempts=attempts + 1)
            await self._maybe_give_up(nr, message_id, attempts + 1)
            return
        self._group_ok()
        self._store.lamoda_update(nr, state="done")
        if warned and await self._edit(nr, message_id, text):
            self._store.lamoda_update(nr, warned=False)

    async def _maybe_give_up(self, nr, message_id, attempts) -> None:
        if attempts >= MAX_LABEL_ATTEMPTS:
            await self._give_up(nr, message_id, GAVE_UP.format(nr=nr))

    async def _give_up(self, nr, message_id, text) -> None:
        self._store.lamoda_update(nr, state="gave_up")
        try:
            await self._bot.send_message(self._chat, text, **self._topic(message_id))
        except Exception:
            log.exception("Lamoda order %s: «labels not received» not delivered", nr)

    async def _send_documents(self, message_id, docs, progress=None) -> None:
        """The labels as replies to the order message: media groups of up to 10, a single
        last one as a plain document (a group needs at least two)."""
        for chunk in document_chunks(docs):
            files = [BufferedInputFile(data, filename=name) for name, data in chunk]
            if len(files) == 1:
                await self._bot.send_document(self._chat, files[0], **self._topic(message_id))
            else:
                await self._bot.send_media_group(
                    self._chat, [InputMediaDocument(media=f) for f in files],
                    **self._topic(message_id))
            if progress is not None:
                progress([name for name, _ in chunk])


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def build_lamoda(cfg, store, bot, say, health=None) -> LamodaLabels | None:
    """The labels part, or None (one info line) when a setting is missing."""
    missing = [name for name, value in (
        ("LAMODA_CLIENT_ID", cfg.lamoda_client_id),
        ("LAMODA_CLIENT_SECRET", cfg.lamoda_client_secret),
        ("LAMODA_THREAD_ID", cfg.lamoda_thread_id),
        ("ALERT_CHAT_ID", cfg.alert_chat_id)) if value in (None, "")]
    if missing:
        log.info("Этикетки Lamoda выключены: не задано %s", ", ".join(missing))
        return None
    return LamodaLabels(LamodaApi(cfg.lamoda_client_id, cfg.lamoda_client_secret), store,
                        bot, cfg.alert_chat_id, cfg.lamoda_thread_id, say=say,
                        health=health)
