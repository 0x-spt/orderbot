import asyncio
import dataclasses
import html
import inspect
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from orderbot.alerts import MP_TITLE, result_line
from orderbot.domain import DEFAULT_COMPANY_NAMES, Action, Decision, ItemRead, Order, Reason, StockRow
from orderbot.marketplaces.base import Marketplace, adapter_key
from orderbot.rules import decide_order
from orderbot.screen.idle import UserActive
from orderbot.screen.lookup import ArticleNotFound
from orderbot.screen.reader import ReadUncertain, UnexpectedScreen
from orderbot.screen.session import SessionLost, ShuttingDown, TransientOverlay
from orderbot.store import Store, decode_decision, decode_order, encode_decision, encode_order

log = logging.getLogger(__name__)

URGENT_WITHIN = timedelta(hours=24)
POLL_FAILURES_BEFORE_ALERT = 3
SNAPSHOT_DAYS = 90
PURGE_EVERY = timedelta(hours=1)
REPEAT_POLL_ALERT = timedelta(minutes=60)
BUSY_ALERT_AFTER = timedelta(minutes=30)
# The marketplace photos of all articles of an order are fetched at the same time and
# waited for at most this long in all; whatever has not come by then is left out.
PHOTOS_WAIT_S = 12.0
# Said with the pause when 1C was left on the picture tab (the screen check fails there
# when the tab is not recognised): the fix is one click by a person.
IMAGE_TAB_HINT = "1С осталась на вкладке «Изображение» — переключите на «Остатки»"


# ------------------------------------------------ health messages, HTML
def _duration(d: timedelta) -> str:
    minutes = max(1, int(d.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин"
    h, m = divmod(minutes, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def _api_error(e: Exception) -> str:
    """HTTP status when the marketplace answered, otherwise «нет связи» / the error type;
    never the server's own words (they may carry anything)."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    name = type(e).__name__
    if any(w in name for w in ("Connect", "Timeout", "Network", "Read", "Remote")):
        return f"нет связи ({name})"
    return name


def api_down_text(title: str, error: str, down_for: timedelta, again: bool = False) -> str:
    head = "всё ещё не отвечает" if again else "не отвечает"
    return (f"🔴 <b>{html.escape(title)} — API {head}</b>\n"
            f"{html.escape(error)} · уже {_duration(down_for)}\n"
            "Новые заказы этой площадки не проверяются")


def api_up_text(title: str, down_for: timedelta) -> str:
    return (f"🟢 <b>{html.escape(title)} — API снова отвечает</b>\n"
            f"Не отвечал {_duration(down_for)}, проверка продолжена")


ONEC_HINTS = (
    ("anchor", "на экране не окно 1С: сессия Remote Desktop отключилась или окно чем-то "
               "закрыто"),
    ("SessionLost", "окно Remote Desktop «1c» не найдено"),
    ("rows are not contiguous", "таблица 1С выглядит необычно (перерисовка или чужое окно)"),
)
ONEC_TODO = ("Откройте окно «1c» в Remote Desktop (если просит — Reconnect), "
             "затем «Я закончил» в боте")


def onec_down_text(reason: str, error: Exception | None = None) -> str:
    seen = f"{type(error).__name__} {error}" if error is not None else reason
    hint = next((h for key, h in ONEC_HINTS if key in seen), None)
    lines = ["🔴 <b>1С не работает — проверка заказов на паузе</b>"]
    if hint:
        lines.append(html.escape(hint[0].upper() + hint[1:]))
    lines.append(f"Причина: {html.escape(reason)}")
    lines.append(f"Что сделать: {ONEC_TODO}")
    return "\n".join(lines)


def onec_up_text(down_for: timedelta) -> str:
    return (f"🟢 <b>1С снова работает</b>\n"
            f"Простой {_duration(down_for)}, проверка заказов продолжена")


def _image_hint(e: Exception) -> str:
    return f". {IMAGE_TAB_HINT}" if getattr(e, "image_tab", False) else ""


class OneC(Protocol):
    """Wrapper over the 1C screen. A fresh Guard per connection is its job."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read(self, article: str) -> ItemRead: ...


class Notifier(Protocol):
    """`snapshot` is the list of screenshot paths of this order's reads."""

    async def order_alert(self, order: Order, decision: Decision, snapshot: list[str],
                          urgent: bool, observed: bool, reads: dict | None = None) -> None: ...

    async def reserve_request(self, order: Order, decision: Decision, snapshot: list[str],
                              urgent: bool, reads: dict | None = None) -> None: ...

    async def system_alert(self, text: str) -> None: ...

    async def health_alert(self, text_html: str) -> None:
        """An API or 1C outage and its end: the group's health topic."""
        ...

    async def order_done(self, mp: str, order_id: str, line_html: str) -> int:
        """Adds the result line to every stored copy of the order message and removes its
        buttons; returns the number of copies."""
        ...

    async def refresh_buttons(self, mp: str, order_id: str) -> int:
        """Puts the buttons of the order's current state under every stored copy of its
        message; returns the number of copies (0: the order has no stored message)."""
        ...


def _utc_now():
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def purge_snapshots(directory, days: int = SNAPSHOT_DAYS, now: float | None = None) -> int:
    """Delete files older than `days` from the snapshot directory; returns how many."""
    root = Path(directory)
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    for p in root.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            log.exception("cannot purge %s", p)
    return removed


def _read_json(r):
    if r is None:
        return None
    if isinstance(r, Reason):
        return r.value
    return {"stock": {w: {"on_hand": s.on_hand, "reserve": s.reserve} for w, s in r.stock.items()},
            "epr": r.epr, "ppr": r.ppr, "snapshot_path": r.snapshot_path,
            "photo_1c": r.photo_1c, "photo_mp": r.photo_mp}


def _read_from_json(article: str, v):
    """The inverse of _read_json, for alerts sent again from the outbox."""
    if v is None:
        return None
    if isinstance(v, str):
        return Reason(v)
    return ItemRead(article, {w: StockRow(w, s["on_hand"], s["reserve"])
                              for w, s in v["stock"].items()},
                    Decimal(str(v["epr"])), Decimal(str(v["ppr"])), v["snapshot_path"],
                    photo_1c=v.get("photo_1c"), photo_mp=v.get("photo_mp"))


_STATE_TEXT = {
    "CONFIRMED": "подтверждён, переведён в «готов к отгрузке»",
    "ESCALATED": "передан человеку, подтверждение не выполнено",
    "HANDLED_EXTERNALLY": "уже обработан сотрудником, робот ничего не делал",
    "OBSERVED": "режим наблюдения, подтверждение не выполнялось",
    "AWAITING_RESERVE": "всё ещё ждёт ответа по резерву",
    "REJECTED_BY_HUMAN": "резерв не разрешён",
    "CANCELLED_BY_HUMAN": "отменён человеком",
}

# final states: nothing more is done or said about the order
FINAL = ("CONFIRMED", "CANCELLED_BY_HUMAN", "HANDLED_EXTERNALLY")


class Pipeline:
    def __init__(self, store: Store, marketplaces: list[Marketplace], onec: OneC,
                 notifier: Notifier, *, clock=_utc_now, snapshots_dir=None,
                 fixed_price: dict | None = None, product_photos=None,
                 companies: dict[str, str] | None = None, on_confirmed=None):
        self._store = store
        # called with an order the robot moved to CONFIRMED (stock_sync.StockSync.schedule:
        # the other marketplaces' stock); its errors never reach the confirm
        self._on_confirmed = on_confirmed
        self._fixed_price = {a.lower(): p for a, p in (fixed_price or {}).items()}
        # (marketplace, company) -> adapter: an order is always handled by the adapter of
        # its own company (Order.account); orders stay keyed by (marketplace, order_id)
        self._mps = {adapter_key(m): m for m in marketplaces}
        self._companies = dict(companies or DEFAULT_COMPANY_NAMES)  # account -> name
        self._onec = onec
        self._notifier = notifier
        self._clock = clock
        self._snapshots_dir = snapshots_dir
        self._photos = product_photos   # orderbot.product_photos.ProductPhotos, or None
        self._last_purge: datetime | None = None
        # per adapter, keyed (marketplace, company)
        self._failures: dict[tuple[str, str], int] = {}
        self._last_ready: dict[tuple[str, str], dict[tuple[str, str], int]] = {}
        self._poll_alerted: dict[tuple[str, str], datetime] = {}
        self._down_since: dict[tuple[str, str], datetime] = {}  # first failed poll in a row
        self._onec_down_since: datetime | None = None  # 1C stopped the robot (this run)
        self._connected = False
        self._busy_since: datetime | None = None   # first UserActive of an unbroken run
        self._busy_alerted = False
        self._asked_to_wait = False   # "do not touch the Mac" was sent for the current wait
        self.waiting_for_mac = False  # the last tick stopped because the Mac was in use
        self._wait_resolved = False   # a read got through after that message
        self._lock = asyncio.Lock()
        # «Проверить сейчас»: wakes the tick loop before its interval ends
        self.wake = asyncio.Event()
        # one action per order at a time, the robot's confirm or a person's button
        self._order_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def _confirmed_hook(self, order: Order) -> None:
        if self._on_confirmed is None:
            return
        try:
            r = self._on_confirmed(order)
            if inspect.isawaitable(r):
                await r
        except Exception:
            log.exception("on_confirmed failed for %s/%s", order.marketplace, order.order_id)

    def _mp(self, order: Order):
        """The adapter of the order's company, or None when that company is not connected."""
        return self._mps.get((order.marketplace, order.account))

    def _title(self, key: tuple[str, str]) -> str:
        """«Озон · Альфа» for health messages: always with the company."""
        return (f"{MP_TITLE.get(key[0], key[0])} · "
                f"{self._companies.get(key[1], key[1])}")

    def _label(self, key: tuple[str, str]) -> str:
        """An adapter in system messages: the marketplace, plus the company when the robot
        works for more than one."""
        if len({acc for _, acc in self._mps}) <= 1:
            return key[0]
        return f"{key[0]} ({self._companies.get(key[1], key[1])})"

    def order_lock(self, mp: str, order_id: str) -> asyncio.Lock:
        """The lock shared by the robot's confirm and the human buttons of one order."""
        return self._order_locks.setdefault((mp, order_id), asyncio.Lock())

    # ---------------------------------------------------------------- tick
    def check_now(self) -> str:
        """A person asked for a check right away: wakes the tick loop. Returns "running" when
        a check is going on (another one follows right after it), else "started"."""
        self.wake.set()
        return "running" if self._lock.locked() else "started"

    async def tick(self) -> None:
        async with self._lock:
            self.waiting_for_mac = False
            self._purge()
            await self._flush_outbox()
            ready, healthy = await self._poll()
            if self._store.is_paused():
                return
            work = self._work()
            if not work:
                return
            try:
                await self._process(work, ready, healthy)
            finally:
                await self._disconnect()
                if self._wait_resolved:
                    self._asked_to_wait = self._wait_resolved = False
                    await self._system("Проверка в 1С закончена, Mac можно использовать.")

    def _purge(self) -> None:
        if not self._snapshots_dir and self._photos is None:
            return
        now = self._clock()
        if self._last_purge and now - self._last_purge < PURGE_EVERY:
            return
        self._last_purge = now
        try:
            if self._snapshots_dir:
                purge_snapshots(self._snapshots_dir)   # tables, 1C pictures, joined sheets
        except Exception:
            log.exception("snapshot purge failed")
        try:
            if self._photos is not None:
                self._photos.purge()                   # its own MAX_AGE_DAYS
        except Exception:
            log.exception("product photo purge failed")

    async def _attach_photos(self, order: Order, reads: dict) -> None:
        """The marketplace's product photo next to each successful read, for the person who
        gets the order. Best effort and bounded in time: never part of the decision."""
        url_fn = getattr(self._mp(order), "photo_url", None)
        if self._photos is None or url_fn is None:
            return
        articles = [a for a, r in reads.items() if isinstance(r, ItemRead)]
        if not articles:
            return

        async def one(article):
            # all start together, each under the same cap: the order waits PHOTOS_WAIT_S at most
            try:
                return await asyncio.wait_for(
                    self._photos.get(order.marketplace, article, url_fn), PHOTOS_WAIT_S)
            except Exception as e:
                log.info("no %s photo for %s: %s: %s", order.marketplace, article,
                         type(e).__name__, e)
                return None

        paths = await asyncio.gather(*(one(a) for a in articles))
        for article, path in zip(articles, paths):
            if path:
                reads[article] = dataclasses.replace(reads[article], photo_mp=path)

    async def _poll(self) -> tuple[dict[tuple[str, str], int], set[tuple[str, str]]]:
        """Fetch new orders and ready quantities from every adapter of every company. Returns
        the ready quantities summed over all of them (one 1C stock for both companies) and the
        (marketplace, company) adapters that answered. For a failed one the last known ready
        quantities are used and its orders wait for the next tick."""
        healthy: set[tuple[str, str]] = set()
        for key, mp in self._mps.items():
            name = key[0]
            try:
                for order in await mp.new_orders():
                    self._store.upsert_new(order)
                self._last_ready[key] = dict(await mp.ready_quantities())
                healthy.add(key)
            except Exception as e:
                n = self._failures.get(key, 0) + 1
                self._failures[key] = n
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None:
                    # the marketplace answered with an error (seen live: Yandex 500 for
                    # hours): one line, the traceback says nothing new
                    log.warning("poll failed for %s/%s: HTTP %s (%d in a row)", name, key[1],
                                status, n)
                else:
                    log.exception("poll failed for %s/%s", name, key[1])
                self._down_since.setdefault(key, self._clock())
                self._store.log(name, None, "poll_failed",
                                {"error": repr(e), "count": n, "account": key[1]})
                last = self._poll_alerted.get(key)
                if n >= POLL_FAILURES_BEFORE_ALERT and (
                        last is None or self._clock() - last >= REPEAT_POLL_ALERT):
                    self._poll_alerted[key] = self._clock()
                    await self._health(api_down_text(
                        self._title(key), _api_error(e), self._clock() - self._down_since[key],
                        again=last is not None))
            else:
                self._failures[key] = 0
                since = self._down_since.pop(key, None)
                if self._poll_alerted.pop(key, None) is not None and since is not None:
                    await self._health(api_up_text(self._title(key), self._clock() - since))
        ready: dict[tuple[str, str], int] = {}
        for qtys in self._last_ready.values():
            for key, qty in qtys.items():
                ready[key] = ready.get(key, 0) + qty
        return ready, healthy

    def _work(self) -> list[tuple[Order, bool]]:
        items = [(self._store.get_order(m, o), False) for m, o in self._store.pending()]
        items += [(self._store.get_order(m, o), True)
                  for m, o in self._store.in_state("RESERVE_APPROVED")]
        far = datetime.max.replace(tzinfo=UTC)
        items.sort(key=lambda it: _aware(it[0].deadline) if it[0].deadline else far)
        return items

    def _human_done(self, key: tuple[str, str], where: str) -> bool:
        """The order became final while the robot was awaiting something (a person pressed a
        button, or it is already confirmed): the robot must not write its state or alert.
        Checked right before each state write that follows an await."""
        state = self._store.get_state(*key)
        if state not in FINAL:
            return False
        try:
            self._store.log(*key, "skipped_after_human", {"state": state, "at": where})
        except Exception:
            log.exception("journal failed")
        return True

    async def _hold(self, order: Order, reason: Reason, confirm: bool,
                    approved: bool = False) -> None:
        """The order cannot be decided safely now. It waits for the next tick, unless its
        deadline is close: then a person gets it."""
        key = (order.marketplace, order.order_id)
        if not self._urgent(order):
            return
        if self._human_done(key, "hold"):
            return
        decision = Decision(Action.ESCALATE, [(ln.article, reason) for ln in order.lines], {})
        self._store.set_state(*key, "ESCALATED" if confirm else "OBSERVED", decision)
        self._store.log(*key, "held_urgent", {"reason": reason.value})
        await self._alert(order, decision, [], True, not confirm)
        if approved:
            await self._system(
                f"{order.marketplace} {order.order_id}: разрешение по резерву записано, "
                "но не выполнено (проверка сейчас невозможна, срок близко). Решите вручную.")

    async def _process(self, work: list[tuple[Order, bool]], ready: dict,
                       healthy: set[tuple[str, str]]) -> None:
        fresh = healthy >= set(self._mps)
        wanted = {True: "RESERVE_APPROVED", False: "NEW"}
        for order, approved in work:
            if self._store.is_paused():
                break
            if self._store.get_state(order.marketplace, order.order_id) != wanted[approved]:
                continue
            try:
                confirm = self._store.get_mode(order.marketplace) == "confirm"
                if (order.marketplace, order.account) not in healthy:
                    # its company's adapter failed, or that company is not connected
                    await self._hold(order, Reason.API_UNAVAILABLE, confirm, approved)
                    continue
                if confirm and not fresh:
                    # a ready list of another marketplace or company is unknown: confirming
                    # could take the last unit that is already promised there
                    await self._hold(order, Reason.READY_UNKNOWN, confirm, approved)
                    continue
                if await self._handle(order, approved, ready, fresh):
                    break
            except Exception as e:
                # an error outside 1C (API, Store): this order goes to a person, the pass
                # goes on with the next one
                log.exception("order %s/%s failed", order.marketplace, order.order_id)
                await self._internal_error(order, e)

    async def _internal_error(self, order: Order, e: Exception) -> None:
        try:
            self._store.log(order.marketplace, order.order_id, "order_error",
                            {"error": repr(e)})
        except Exception:
            log.exception("journal failed")
        try:
            await self._give_to_person(order, Reason.INTERNAL_ERROR)
        except Exception:
            log.exception("cannot escalate %s/%s", order.marketplace, order.order_id)

    async def _give_to_person(self, order: Order, reason: Reason) -> None:
        """The order cannot be checked: a person gets it. In observe mode it is recorded and
        shown as an observation, like every other observe-mode alert."""
        key = (order.marketplace, order.order_id)
        if self._human_done(key, "give_to_person"):
            return
        state = self._store.get_state(*key)
        if state == "HANDLED_EXTERNALLY":
            return
        # CONFIRMING: a confirm may have been sent, so this is never "only observed"
        observed = self._store.get_mode(order.marketplace) != "confirm" and state != "CONFIRMING"
        decision = Decision(Action.ESCALATE,
                            [(ln.article, reason) for ln in order.lines] or [("", reason)], {})
        self._store.set_state(*key, "OBSERVED" if observed else "ESCALATED", decision)
        await self._alert(order, decision, [], self._urgent(order), observed)

    # ------------------------------------------------------------ one order
    async def _handle(self, order: Order, approved: bool, ready: dict, fresh: bool) -> bool:
        """Returns True if the whole pass has to stop."""
        mp = self._mp(order)   # in `healthy`, so connected
        st = self._store
        awaiting = await mp.is_awaiting(order)
        if self._human_done((order.marketplace, order.order_id), "handle"):
            return False
        if not awaiting:
            st.set_state(order.marketplace, order.order_id, "HANDLED_EXTERNALLY")
            st.log(order.marketplace, order.order_id, "handled_externally", {})
            if approved:
                await self._system(
                    f"{order.marketplace} {order.order_id}: заказ уже обработан сотрудником, "
                    "подтверждение резерва не выполнено.")
            return False
        if approved and st.get_mode(order.marketplace) != "confirm":
            # as resolve_reserve in observe mode: the decision is kept, a person collects
            # the order with the button
            st.set_state(order.marketplace, order.order_id, "OBSERVED")
            st.log(order.marketplace, order.order_id, "approved_observed", {"why": "observe mode"})
            await self._observed_buttons(order)
            return False
        if order.created_at is None:
            # the settings in force at order time cannot be chosen: nothing to read 1C for
            st.log(order.marketplace, order.order_id, "invalid_order", {"why": "no created_at"})
            await self._give_to_person(order, Reason.INVALID_ORDER)
            return False

        if st.is_paused():
            return True
        if not self._connected:
            try:
                await asyncio.to_thread(self._onec.connect)
            except ShuttingDown:
                log.info("1C connect skipped: the robot is stopping")
                return True
            except UserActive as e:
                # closing 1C's task reminder on connect met a person at the Mac: no pause,
                # the order is tried again on the next tick
                log.info("1C connect stopped, the Mac is in use: %s", e)
                await self._mac_busy(overlay=False)
                return True
            except Exception as e:
                log.error("1C connect failed: %r", e, exc_info=True)
                try:
                    await asyncio.to_thread(self._onec.disconnect)
                except Exception:
                    log.exception("disconnect after failed connect")
                await self._terminal(f"не удалось подключиться к 1С ({type(e).__name__})"
                                     f"{_image_hint(e)}", e)
                return True
            self._connected = True
            if self._onec_down_since is not None:
                since, self._onec_down_since = self._onec_down_since, None
                await self._health(onec_up_text(self._clock() - since))

        reads: dict[str, ItemRead | Reason | None] = {}
        failure_snaps: list[str] = []   # screenshots the 1C side attached to failed reads
        for article in dict.fromkeys(ln.article for ln in order.lines):
            try:
                reads[article] = await asyncio.to_thread(self._onec.read, article)
                self._busy_since, self._busy_alerted = None, False
                if self._asked_to_wait:
                    self._wait_resolved = True
            except ArticleNotFound as e:
                log.info("1C read of %s: article not found: %s", article, e)
                reads[article] = Reason.ARTICLE_NOT_FOUND
                failure_snaps += [p for p in [getattr(e, "snapshot_path", None)] if p]
            except ReadUncertain as e:
                log.warning("1C read of %s is uncertain: %s", article, e)
                reads[article] = None
                failure_snaps += [p for p in [getattr(e, "snapshot_path", None)] if p]
            except UserActive as e:
                # A person is at the Mac: no alert, no pause, the order keeps its state
                # and is tried again on the next tick.
                log.info("1C pass stopped, the Mac is in use: %s", e)
                await self._mac_busy(overlay=False)
                return True
            except TransientOverlay as e:
                # a notification banner or a window in front for a moment: the same as a
                # person at the Mac, the order waits for the next tick
                log.info("1C pass stopped, something covers the RDP window: %s", e)
                await self._mac_busy(overlay=True)
                return True
            except ShuttingDown:
                # a normal stop by the operator: no alert, no pause, the order keeps its state
                log.info("1C pass stopped: the robot is stopping")
                return True
            except (UnexpectedScreen, SessionLost) as e:
                log.error("1C stopped on order %s/%s: %r", order.marketplace, order.order_id, e,
                          exc_info=True)
                await self._terminal(f"{type(e).__name__} (заказ {order.marketplace} "
                                     f"{order.order_id} остался в очереди){_image_hint(e)}", e)
                return True
            except Exception as e:
                # Unexpected while driving 1C (a refused action, a bug, a library error):
                # nothing more is typed in this pass. The order goes to a person, the robot
                # pauses with one alert, and the window is let go at once.
                log.error("1C read failed unexpectedly on order %s/%s: %r", order.marketplace,
                          order.order_id, e, exc_info=True)
                try:
                    await self._internal_error(order, e)
                    await self._disconnect()
                    await self._terminal(
                        f"внутренняя ошибка при работе с 1С ({type(e).__name__}, заказ "
                        f"{order.marketplace} {order.order_id} передан человеку)", e)
                except Exception:
                    log.exception("cannot finish the pass after a 1C error")
                return True   # whatever failed above, nothing more is typed in this pass

        # The ready list was fetched (in _poll) before is_awaiting returned True above, and an
        # order still awaiting packaging then was not ready earlier either, so this order is
        # not in `ready`: nothing of its own quantity has to be taken out.
        settings = st.settings_at(order.marketplace, order.created_at)
        # Prices follow the discounts in force when the order was placed, but whether the
        # paid sum of several units is understood is knowledge, not a sales mode: the
        # current answer applies to old orders too (an operator may switch it on and re-run one).
        settings = dataclasses.replace(
            settings,
            multi_qty_verified=st.settings_at(order.marketplace, self._clock()).multi_qty_verified,
            fixed_price=self._fixed_price)
        decision = decide_order(order, reads, settings, dict(ready), reserve_approved=approved)
        await self._attach_photos(order, reads)
        snapshots = [r.snapshot_path for r in reads.values()
                     if isinstance(r, ItemRead) and r.snapshot_path] + failure_snaps
        st.log(order.marketplace, order.order_id, "decision", {
            "action": decision.action.value,
            "reasons": [[a, r.value] for a, r in decision.reasons],
            "floors": decision.floors,
            "reads": {a: _read_json(r) for a, r in reads.items()},
            "snapshots": snapshots, "reserve_approved": approved})
        await self._dispatch(order, mp, decision, reads, snapshots, approved, ready, fresh)
        return False

    async def _dispatch(self, order, mp, decision, reads, snapshots, approved, ready,
                        fresh=True) -> None:
        st, n = self._store, self._notifier
        key = (order.marketplace, order.order_id)
        urgent = self._urgent(order)
        if self._human_done(key, "dispatch"):
            return
        confirm = st.get_mode(order.marketplace) == "confirm"
        if confirm and not fresh:
            # the mode may have been switched during the slow 1C read
            await self._hold(order, Reason.READY_UNKNOWN, True, approved)
            return
        if not confirm:
            st.set_state(*key, "OBSERVED", decision)
            await self._alert(order, decision, snapshots, urgent, True, reads)
        elif decision.action == Action.CONFIRM:
            await self._confirm(order, mp, decision, reads, snapshots, urgent, ready, approved,
                                fresh)
        elif decision.action == Action.ASK_RESERVE and not approved:
            st.set_state(*key, "AWAITING_RESERVE", decision)
            try:
                await n.reserve_request(order, decision, snapshots, urgent, reads=reads)
            except Exception:
                log.exception("reserve_request failed")
                self._queue("reserve", order, decision, snapshots, urgent, False, reads)
        else:
            st.set_state(*key, "ESCALATED", decision)
            await self._alert(order, decision, snapshots, urgent, False, reads)

    async def _confirm(self, order, mp, decision, reads, snapshots, urgent, ready,
                       approved=False, fresh=True) -> None:
        key = (order.marketplace, order.order_id)
        if not fresh:
            await self._hold(order, Reason.READY_UNKNOWN, True, approved)
            return
        async with self.order_lock(*key):
            await self._confirm_locked(order, mp, decision, reads, snapshots, urgent, ready,
                                       approved)

    async def _confirm_locked(self, order, mp, decision, reads, snapshots, urgent, ready,
                              approved) -> None:
        st = self._store
        key = (order.marketplace, order.order_id)
        if self._human_done(key, "confirm"):  # a person may have acted while it waited
            return
        if not await mp.is_awaiting(order):
            st.set_state(*key, "HANDLED_EXTERNALLY", decision)
            st.log(*key, "handled_externally", {"before": "confirm"})
            if approved:
                await self._system(
                    f"{order.marketplace} {order.order_id}: заказ уже обработан сотрудником, "
                    "подтверждение резерва не выполнено.")
            return
        if st.get_mode(order.marketplace) != "confirm":  # read last, right before confirm
            st.set_state(*key, "OBSERVED", decision)
            await self._alert(order, decision, snapshots, urgent, True, reads)
            return
        st.set_state(*key, "CONFIRMING", decision)
        try:
            await mp.confirm(order)
        except Exception as e:
            log.exception("confirm failed for %s/%s", *key)
            try:
                st.log(*key, "confirm_error", {"error": repr(e)})
            except Exception:
                log.exception("journal failed")
            try:
                went_through = await mp.is_ready(order)
            except Exception:
                went_through = False
            if not went_through:
                st.set_state(*key, "ESCALATED")
                await self._alert(order, Decision(
                    Action.ESCALATE, [(ln.article, Reason.CONFIRM_UNKNOWN) for ln in order.lines],
                    {}), snapshots, urgent, False, reads)
                return
        st.set_state(*key, "CONFIRMED")
        try:
            st.log(*key, "confirmed", {})
        except Exception:
            log.exception("journal failed")
        for ln in order.lines:  # the next order of this pass must see this one as assembled
            if ln.warehouse is not None:
                k = (ln.article, ln.warehouse)
                ready[k] = ready.get(k, 0) + ln.qty
        try:   # stale buttons must not stay under an order the robot collected
            await self._notifier.order_done(*key, result_line("collected_by_robot", "",
                                                              self._clock()))
        except Exception:
            log.exception("cannot mark the messages of %s/%s as collected", *key)
        await self._confirmed_hook(order)

    async def _observed_buttons(self, order: Order) -> None:
        """The stored order message gets the buttons of its new state; without a stored
        message the alert is sent anew."""
        key = (order.marketplace, order.order_id)
        try:
            stored = await self._notifier.refresh_buttons(*key)
        except Exception:
            log.exception("cannot refresh the buttons of %s/%s", *key)
            stored = 0
        if stored:
            return
        decision = self._store.get_decision(*key)
        if decision is not None:
            await self._alert(order, decision, [], self._urgent(order), True)

    def _urgent(self, order: Order) -> bool:
        return bool(order.deadline and _aware(order.deadline) - _aware(self._clock()) <= URGENT_WITHIN)

    async def _alert(self, order, decision, snapshots, urgent, observed, reads=None) -> None:
        try:
            await self._notifier.order_alert(order, decision, snapshots, urgent, observed,
                                             reads=reads)
        except Exception:
            log.exception("order_alert failed for %s/%s", order.marketplace, order.order_id)
            self._queue("order", order, decision, snapshots, urgent, observed, reads)

    async def _health(self, text: str) -> None:
        try:
            await self._notifier.health_alert(text)
        except Exception:
            log.exception("health_alert failed")
            try:
                self._store.queue_alert("health", {"text": text})
            except Exception:
                log.exception("cannot keep the failed alert")

    async def _system(self, text: str) -> None:
        try:
            await self._notifier.system_alert(text)
        except Exception:
            log.exception("system_alert failed")
            try:
                self._store.queue_alert("system", {"text": text})
            except Exception:
                log.exception("cannot keep the failed alert")

    def _queue(self, kind, order, decision, snapshots, urgent, observed, reads) -> None:
        try:
            self._store.queue_alert(kind, {
                "marketplace": order.marketplace, "order_id": order.order_id,
                "order": encode_order(order), "decision": encode_decision(decision),
                "snapshots": list(snapshots), "urgent": urgent, "observed": observed,
                "reads": {a: _read_json(r) for a, r in (reads or {}).items()}})
        except Exception:
            log.exception("cannot keep the failed alert for %s/%s", order.marketplace,
                          order.order_id)

    async def _flush_outbox(self) -> None:
        """Alerts that failed earlier, oldest first, each tried at most once per tick; the
        first failure ends the attempt until the next tick. Sent ones are dropped."""
        try:
            pending = self._store.outbox()
        except Exception:
            log.exception("cannot read the outbox")
            return
        for alert_id, kind, p in pending:
            try:
                if kind == "system":
                    await self._notifier.system_alert(p["text"])
                elif kind == "health":
                    await self._notifier.health_alert(p["text"])
                else:
                    order = decode_order(p["order"])
                    decision = decode_decision(p["decision"])
                    reads = {a: _read_from_json(a, v) for a, v in p["reads"].items()}
                    if kind == "reserve":
                        # buttons for an order that no longer waits for them would mislead
                        if self._store.get_state(order.marketplace,
                                                 order.order_id) == "AWAITING_RESERVE":
                            await self._notifier.reserve_request(
                                order, decision, p["snapshots"], p["urgent"], reads=reads)
                    elif self._store.get_state(order.marketplace,
                                               order.order_id) not in FINAL:
                        # a finished order needs no alert with buttons
                        await self._notifier.order_alert(order, decision, p["snapshots"],
                                                         p["urgent"], p["observed"], reads=reads)
            except Exception:
                log.exception("resend of alert %s failed, kept for the next tick", alert_id)
                return
            try:
                self._store.drop_alert(alert_id)
                self._store.log(p.get("marketplace"), p.get("order_id"), "alert_resent",
                                {"kind": kind, "outbox_id": alert_id})
            except Exception:
                log.exception("cannot drop the resent alert %s", alert_id)
                return

    async def _mac_busy(self, overlay: bool) -> None:
        """Orders may wait on a person at the Mac, or on a window that keeps covering RDP,
        for long: say so once after BUSY_ALERT_AFTER of unbroken waiting; a read that gets
        through resets it."""
        now = self._clock()
        self.waiting_for_mac = True
        if self._busy_since is None:
            self._busy_since = now
        if not self._asked_to_wait:
            # Said once per wait, right away: otherwise a person at the Mac cannot tell that
            # the robot is waiting for them (by request).
            self._asked_to_wait = True
            if overlay:
                await self._system(
                    "Есть заказы для проверки в 1С, но окно Remote Desktop чем-то закрыто. "
                    "Включите «Не беспокоить», откройте окно «1c» и не трогайте Mac около "
                    "минуты. Робот пробует снова каждые 30 секунд.")
            else:
                await self._system(
                    "Есть заказы для проверки в 1С: не трогайте мышь и клавиатуру Mac около "
                    "минуты. Робот начнёт, когда Mac 30 секунд не используется: он "
                    "проверяет это каждые 30 секунд.")
        if not self._busy_alerted and now - self._busy_since >= BUSY_ALERT_AFTER:
            self._busy_alerted = True
            minutes = int((now - self._busy_since).total_seconds() // 60)
            if overlay:
                await self._system(
                    f"Mac занят, заказы ждут: уже {minutes} мин поверх окна Remote Desktop "
                    "появляется другое окно или уведомление, либо окно не выходит на передний "
                    "план, робот не вводит в 1С. Включите на Mac режим «Не беспокоить» и "
                    "закройте лишние окна; робот продолжит сам.")
            else:
                await self._system(
                    f"Mac занят, заказы ждут: уже {minutes} мин мышь или клавиатура "
                    "используются, робот не вводит в 1С. Он продолжит сам, когда Mac "
                    "освободится.")

    async def _terminal(self, text: str, error: Exception | None = None) -> None:
        """1C is not usable: pause (so nothing reconnects until a person resumes), tell once."""
        self._store.set_paused(True, "system")
        self._store.log(None, None, "onec_stopped", {"text": text})
        if self._onec_down_since is None:
            self._onec_down_since = self._clock()
        await self._health(onec_down_text(text, error))

    async def _disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        try:
            await asyncio.to_thread(self._onec.disconnect)
        except Exception:
            log.exception("disconnect failed")

    # ------------------------------------------------------- human reserve
    async def resolve_reserve(self, mp: str, order_id: str, approved: bool, user) -> str:
        st = self._store
        state = st.get_state(mp, order_id)
        if state != "AWAITING_RESERVE":
            return f"Заказ {order_id}: сейчас не ждёт ответа по резерву ({state or 'не найден'})."
        if not approved:
            st.set_state(mp, order_id, "REJECTED_BY_HUMAN")
            st.log(mp, order_id, "reserve_rejected", {"user": user})
            return f"Заказ {order_id}: резерв не разрешён, робот ничего не делает."
        if st.get_mode(mp) != "confirm":
            # the decision is kept; the order gets the "collect" button
            st.set_state(mp, order_id, "OBSERVED")
            st.log(mp, order_id, "reserve_approved", {"user": user, "mode": "observe"})
            return "Резерв разрешён. Робот в режиме наблюдения: соберите заказ кнопкой."
        st.set_state(mp, order_id, "RESERVE_APPROVED")
        st.log(mp, order_id, "reserve_approved", {"user": user})
        if st.is_paused():
            return f"Заказ {order_id}: разрешение записано, обработаю после снятия паузы."
        if self._lock.locked():
            return f"Заказ {order_id}: разрешение записано, обработаю в ближайшем проходе."
        async with self._lock:
            ready, healthy = await self._poll()
            order = st.get_order(mp, order_id)
            if (mp, order.account) in healthy and not st.is_paused():
                try:
                    await self._process([(order, True)], ready, healthy)
                finally:
                    await self._disconnect()
        final = st.get_state(mp, order_id)
        if final == "RESERVE_APPROVED":
            return f"Заказ {order_id}: разрешение записано, обработаю при следующей возможности."
        return f"Заказ {order_id}: {_STATE_TEXT.get(final, 'состояние изменилось')}."

    # ------------------------------------------------------------- restart
    async def recover(self) -> None:
        st = self._store
        for mp_name, order_id in st.in_state("CONFIRMING"):
            order = st.get_order(mp_name, order_id)
            try:
                # the adapter of the order's company; not connected: escalated below
                went_through = await self._mp(order).is_ready(order)
            except Exception:
                went_through = False
            if went_through:
                st.set_state(mp_name, order_id, "CONFIRMED")
                st.log(mp_name, order_id, "recovered", {"result": "confirmed"})
                await self._confirmed_hook(order)
            else:
                st.set_state(mp_name, order_id, "ESCALATED")
                st.log(mp_name, order_id, "recovered", {"result": "escalated"})
                await self._alert(order, Decision(
                    Action.ESCALATE, [(ln.article, Reason.CONFIRM_UNKNOWN) for ln in order.lines],
                    {}), [], self._urgent(order), False)
