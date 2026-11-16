"""Stock on the other marketplaces after a collected order.

One physical stock in 1C is sold on Ozon, Yandex Market and WB of the main company (Альфа);
1C uploads stocks only on its own day. When an order of the main company becomes CONFIRMED
(a person's «Собрать» or the robot's confirm), the article is decremented, by the line's
quantity, on every OTHER Альфа marketplace warehouse mapped in warehouses.yaml (sections
`ozon`, `yandex`, `wb`) to the line's 1C warehouse, so it is not sold twice. The warehouse
the order came from is skipped (the marketplace reserved it there already); the same
marketplace's other warehouses of that 1C warehouse (e.g. Ozon «СЕВЕР EXPRESS») are
targets. Yandex campaigns of one warehouse group share one stock: written once, through the
group's main campaign, and skipped when the order came from the group. The company
«Бета» is never touched: its orders do not trigger and its sections are never targets.

Per target: read the free stock, write max(0, free - qty) once (no retries); no article or
nothing free -> skipped. Switched by the store flag `stocksync` (/stocksync on|off, off by
default): off reads only and reports what it would do. Yandex writes need also
`/stocksync yandex on` (whether its `count` holds reserves is unverified).
All runs are serialized by one lock, so two orders of one article never lose a decrement.
Runs once per order (journal event `stock_synced`, written before any write). Best effort:
errors are journaled and reported, never raised into the collect.
"""
import asyncio
import html
import inspect
import logging
from dataclasses import dataclass

import httpx

from orderbot.alerts import MP_TITLE
from orderbot.domain import MAIN, Order
from orderbot.marketplaces.base import StockError

log = logging.getLogger(__name__)

SECTIONS = ("ozon", "yandex", "wb")   # Альфа only; the *_second sections never
ERROR_LEN = 60


def _plain(name):
    """A warehouse name without emoji (non-BMP symbols) and edge spaces, as Order.mp_warehouse
    carries Ozon's warehouse name."""
    if not name:
        return None
    return "".join(ch for ch in str(name) if ord(ch) <= 0xFFFF).strip() or None


def _is_source(order: Order, mp: str, key: str) -> bool:
    """The order's own warehouse, or — when it cannot be told which one that is — any
    warehouse of the order's marketplace (decrementing too little is safe, twice is not)."""
    if order.marketplace != mp:
        return False
    if mp == "ozon" and order.mp_warehouse:
        return _plain(key) == _plain(order.mp_warehouse)
    if mp == "yandex" and order.campaign:
        return str(key) == str(order.campaign)
    return True   # WB orders do not keep their warehouseId; one WB warehouse per 1C one


def select_targets(order: Order, warehouse_1c: str, warehouses: dict) -> list[tuple[str, str]]:
    """(marketplace, warehouses.yaml key) of every Альфа warehouse mapped to this 1C
    warehouse, except the order's own."""
    out = []
    for mp in SECTIONS:
        for key, wh in (warehouses.get(mp) or {}).items():
            if wh == warehouse_1c and not _is_source(order, mp, key):
                out.append((mp, str(key)))
    return out


@dataclass
class _Target:
    mp: str
    key: str            # what is read and written
    label: str          # for the report, plain text
    error: str | None = None   # the target cannot be done at all


def _err(e: BaseException) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, StockError):
        text = " ".join(str(e).split())
        return text if len(text) <= ERROR_LEN else text[:ERROR_LEN - 1] + "…"
    return type(e).__name__


class StockSync:
    def __init__(self, store, adapters: dict, warehouses: dict, notifier=None):
        """`adapters`: marketplace name -> adapter of the MAIN company only."""
        self._store = store
        self._mps = dict(adapters)
        self._wh = warehouses or {}
        self._notifier = notifier
        self._running: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        # every read-modify-write runs alone: two orders of one article read the stock one
        # after the other, so the second sees the first one's write
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- triggers
    def schedule(self, order: Order) -> None:
        """For the confirm paths: runs in the background, so a press or a tick never waits."""
        if getattr(order, "account", MAIN) != MAIN:
            return None
        task = asyncio.ensure_future(self.run(order))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def run(self, order: Order) -> str | None:
        """The report line, or None when nothing was done (not Альфа, or already done)."""
        key = (order.marketplace, order.order_id)
        if getattr(order, "account", MAIN) != MAIN or key in self._running:
            return None
        self._running.add(key)
        try:
            async with self._lock:
                return await self._run(order, key)
        except Exception as e:
            log.exception("stock sync failed for %s/%s", *key)
            try:
                self._store.log(*key, "stock_sync_error", {"error": repr(e)})
            except Exception:
                log.exception("journal failed")
            return None
        finally:
            self._running.discard(key)

    # ---------------------------------------------------------------- work
    async def _run(self, order: Order, key) -> str | None:
        st = self._store
        if st.has_event(*key, "stock_synced"):
            return None
        on = st.get_stocksync()
        # Yandex writes need their own switch too (count semantics FIT/AVAILABLE unverified)
        writes = {mp: on and (mp != "yandex" or st.get_stocksync("yandex")) for mp in SECTIONS}
        st.log(*key, "stock_synced", {"on": on, "writes": writes})   # claimed: never again
        changes, would, empty, failed, results = [], [], [], [], []
        entries: list[tuple[str, str, str]] = []    # (article, kind, text) in order
        groups = None
        for ln in order.lines:
            if not ln.warehouse:
                failed.append("склад 1С не сопоставлен")
                entries.append((ln.article, "failed", "склад 1С не сопоставлен"))
                continue
            if not ln.qty or ln.qty <= 0:
                continue
            raw = select_targets(order, ln.warehouse, self._wh)
            if any(mp == "yandex" for mp, _ in raw) or order.marketplace == "yandex":
                if groups is None:
                    groups = await self._groups()
            for t in self._collapse(order, raw, groups):
                label = t.label
                if t.error is None and t.mp not in self._mps:
                    t.error = "площадка не подключена"
                if t.error is not None:
                    failed.append(f"{label} ({t.error})")
                    entries.append((ln.article, "failed", f"{label} ({t.error})"))
                    results.append({"target": t.label, "article": ln.article,
                                    "error": t.error})
                    continue
                outcome = await self._one(t, ln.article, ln.qty, writes.get(t.mp, False))
                results.append({"target": t.label, "article": ln.article, **outcome})
                if "error" in outcome:
                    failed.append(f"{label} ({outcome['error']})")
                    entries.append((ln.article, "failed", f"{label} ({outcome['error']})"))
                elif outcome.get("new") is None:
                    empty.append(label)
                    entries.append((ln.article, "empty", label))
                else:
                    kind = "changes" if outcome["written"] else "would"
                    (changes if outcome["written"] else would).append(label)
                    entries.append((ln.article, kind,
                                    f"{label} {outcome['old']}→{outcome['new']}"))
        text = self._text(on, entries)
        try:
            st.log(*key, "stock_sync_done", {"on": on, "results": results})
        except Exception:
            log.exception("journal failed")
        if self._notifier is not None:
            try:
                r = self._notifier.add_note(*key, text)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                log.exception("cannot add the stock line to %s/%s", *key)
        return text

    async def _groups(self):
        """Yandex campaign -> (group, campaign to use, name), or the error that stops every
        Yandex target (without it a shared stock could be decremented twice)."""
        mp = self._mps.get("yandex")
        if mp is None:
            return StockError("площадка не подключена")
        try:
            return await mp.stock_groups()
        except Exception as e:
            log.exception("yandex warehouse groups unknown")
            return e

    def _collapse(self, order, raw, groups) -> list[_Target]:
        out, seen, ym_failed = [], set(), False
        src_group = None
        if order.marketplace == "yandex" and isinstance(groups, dict) and order.campaign:
            src_group = (groups.get(str(order.campaign)) or (None,))[0]
        for mp, key in raw:
            if mp == "ozon":
                out.append(_Target(mp, key, f"{MP_TITLE[mp]} {_plain(key) or key}"))
            elif mp == "wb":
                out.append(_Target(mp, key, f"{MP_TITLE[mp]} {self._wh['wb'][key]}"))
            elif not isinstance(groups, dict):
                if not ym_failed:
                    ym_failed = True
                    out.append(_Target(mp, key, MP_TITLE[mp], error=_err(groups)))
            else:
                g = groups.get(key)
                if g is None:
                    out.append(_Target(mp, key, f"{MP_TITLE[mp]} {key}",
                                       error="склада нет в кабинете"))
                    continue
                group, via, _ = g
                if group == src_group or group in seen:
                    continue
                seen.add(group)
                names = [n for c, (gr, _, n) in groups.items()
                         if gr == group and c in (self._wh.get("yandex") or {})]
                out.append(_Target(mp, via, f"{MP_TITLE[mp]} {' + '.join(names)}"))
        return out

    async def _one(self, t: _Target, article: str, qty: int, on: bool) -> dict:
        mp = self._mps[t.mp]
        try:
            cur = await mp.stock_read(t.key, article)
        except Exception as e:
            log.exception("stock read %s %s %s failed", t.mp, t.key, article)
            return {"error": _err(e)}
        if cur is None or cur.count <= 0:
            return {"old": None if cur is None else cur.count, "new": None}
        new = max(0, cur.count - qty)
        if new >= cur.count:     # never a larger value
            return {"old": cur.count, "new": None}
        if on:
            try:
                await mp.stock_write(t.key, article, cur.ref, new)
            except Exception as e:
                log.exception("stock write %s %s %s failed", t.mp, t.key, article)
                return {"old": cur.count, "error": _err(e)}
        return {"old": cur.count, "new": new, "written": on}

    @staticmethod
    def _text(on, entries) -> str:
        """a collapsed quote, so it does not fill the screen. «📉 Остатки» what
        was written, «📉 Уменьшил бы» what a switch kept from being written; then each article
        with its marketplaces, one per line."""
        esc = html.escape
        kinds = {k for _, k, _ in entries}
        if "changes" in kinds:
            head = "📉 <b>Остатки</b>"
        elif "would" in kinds or not on:
            head = "📉 <b>Уменьшил бы</b>"
        else:
            head = "📉 <b>Остатки</b>"
        if not kinds & {"changes", "would"}:
            head += ": нечего уменьшать"
        lines = [head]
        mixed = "changes" in kinds and "would" in kinds
        articles = list(dict.fromkeys(a for a, _, _ in entries))
        for article in articles:
            lines.append(f"<b>SKU:</b> {esc(article)}")
            for a, kind, txt in entries:
                if a != article:
                    continue
                if kind == "changes":
                    lines.append(esc(txt))
                elif kind == "would":
                    lines.append(esc(txt) + (" (не записано)" if mixed else ""))
                elif kind == "empty":
                    lines.append(f"{esc(txt)} — нет товара")
                else:
                    lines.append(f"⚠️ не удалось: {esc(txt)}")
        return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"
