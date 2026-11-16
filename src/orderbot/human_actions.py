"""The human "Собрать" / "Отменить" buttons. The only place where an order is cancelled,
and only because a person pressed the button; the robot (pipeline, rules) never imports this.

Both actions take the order lock shared with the robot's confirm, so one action runs per
order at a time. A pause stops the robot only, not people: it is not checked here."""
import inspect
import logging
import re
from dataclasses import dataclass

from orderbot.domain import MAIN

log = logging.getLogger(__name__)

COLLECT_FROM = frozenset({"OBSERVED", "ESCALATED"})
CANCEL_FROM = frozenset({"OBSERVED", "ESCALATED", "AWAITING_RESERVE", "REJECTED_BY_HUMAN",
                         "RESERVE_APPROVED"})
DONE = frozenset({"CONFIRMED", "CANCELLED_BY_HUMAN", "HANDLED_EXTERNALLY"})

ROBOT_BUSY = "робот уже собирает заказ"
LOCK_BUSY = "уже выполняется"
ERROR_LEN = 80


@dataclass(frozen=True)
class ActionResult:
    # as alerts.result_line, plus "busy" and "not_allowed_state"
    kind: str
    error: str | None = None


_URL_TAIL = re.compile(r"\s+for url '[^']*'")
_MORE_INFO = re.compile(r"\s*For more information check:.*$", re.S)


def _response(e: BaseException | None):
    """The HTTP response carried by the error or by one of its causes, if any."""
    seen = 0
    while e is not None and seen < 5:
        r = getattr(e, "response", None)
        if r is not None and getattr(r, "status_code", None) is not None:
            return r
        e, seen = e.__cause__ or e.__context__, seen + 1
    return None


def _body_message(r) -> str:
    try:
        data = r.json()
    except Exception:
        return " ".join((getattr(r, "text", "") or "").split())
    for _ in range(3):
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            break
        for k in ("message", "error", "errors", "detail", "title", "description"):
            if data.get(k):
                data = data[k]
                break
        else:
            return ""
        if isinstance(data, str):
            return " ".join(data.split())
    return "" if isinstance(data, (dict, list)) else str(data)


# Statuses whose cause a person can fix, said plainly instead of the marketplace's reply.
PLAIN_STATUS = {
    "wb": {401: "WB: токен не подходит (401)",
           403: "WB: нет прав на запись (403) — нужен токен без «Только на чтение»"},
}

_HTTPX_STATUS = re.compile(r"(?:Informational response|Redirect response|Client error|"
                           r"Server error) '(\d{3})[^']*'")


def _short(e: Exception, mp: str | None = None) -> str:
    """The error for the result line: our own context (e.g. the WB supply id), the HTTP status
    and the marketplace's own message, without httpx's url and help-link noise. A token
    problem of `mp` is said plainly, unless the error names a supply left empty."""
    r = _response(e)
    plain = PLAIN_STATUS.get(mp, {}).get(getattr(r, "status_code", None))
    if plain and not getattr(e, "supply", None):
        return plain
    text = _MORE_INFO.sub("", _URL_TAIL.sub("", str(e)))
    text = _HTTPX_STATUS.sub(r"HTTP \1", text)
    text = " ".join(text.split()) or type(e).__name__
    if r is not None:
        if f"HTTP {r.status_code}" not in text:
            text += f" HTTP {r.status_code}"
        detail = _body_message(r)
        if detail and detail not in text:
            text += f": {detail}"
    return text if len(text) <= ERROR_LEN else text[:ERROR_LEN - 1] + "…"


class HumanActions:
    def __init__(self, store, marketplaces: dict, pipeline, on_collected=None):
        """`marketplaces`: {(marketplace, company): adapter}; a plain marketplace name as the
        key means the main company. A button acts through the adapter of the order's own
        company (Order.account). `on_collected(order)` is called once a person's «Собрать»
        went through (stock_sync.StockSync.schedule); its errors are only logged."""
        self._on_collected = on_collected
        self._store = store
        self._mps = {k if isinstance(k, tuple) else (k, MAIN): m
                     for k, m in marketplaces.items()}
        self._pipeline = pipeline

    async def collect(self, mp: str, order_id: str, who: str) -> ActionResult:
        return await self._act(mp, order_id, who, COLLECT_FROM, "CONFIRMED",
                               "collected", "collected_by_human",
                               "collect_failed", "collect_error")

    async def cancel(self, mp: str, order_id: str, who: str) -> ActionResult:
        return await self._act(mp, order_id, who, CANCEL_FROM, "CANCELLED_BY_HUMAN",
                               "cancelled", "cancelled_by_human",
                               "cancel_failed", "cancel_error")

    async def _act(self, mp_name, order_id, who, allowed, new_state, ok_kind, ok_event,
                   fail_kind, fail_event) -> ActionResult:
        st = self._store
        lock = self._pipeline.order_lock(mp_name, order_id)
        if lock.locked():
            busy = ROBOT_BUSY if st.get_state(mp_name, order_id) == "CONFIRMING" else LOCK_BUSY
            return ActionResult("busy", busy)
        async with lock:
            # the locked() check above is only a fast answer: a waiter woken by a release may
            # still get the lock first. Correctness rests on this re-read under the lock.
            state = st.get_state(mp_name, order_id)
            if state == "CONFIRMING":
                return ActionResult("busy", ROBOT_BUSY)
            if state in DONE:
                return ActionResult("already_done")
            order = st.get_order(mp_name, order_id) if state in allowed else None
            mp = self._mps.get((mp_name, order.account)) if order is not None else None
            if state not in allowed or mp is None:
                return ActionResult("not_allowed_state", state)
            try:
                if not await mp.is_awaiting(order):
                    st.set_state_if(mp_name, order_id, set(allowed), "HANDLED_EXTERNALLY")
                    st.log(mp_name, order_id, "handled_externally",
                           {"before": ok_kind, "user": who})
                    return ActionResult("already_done")
                if new_state == "CONFIRMED":
                    await mp.confirm(order)
                else:
                    await mp.cancel_by_human(order)
            except Exception as e:
                log.exception("%s failed for %s/%s", ok_kind, mp_name, order_id)
                try:
                    st.log(mp_name, order_id, fail_event, {"user": who, "error": repr(e)})
                except Exception:
                    log.exception("journal failed")
                if fail_kind == "cancel_failed":
                    # a person wanted this order dropped: the robot must not collect it
                    # after an approved reserve; it goes back to a person
                    try:
                        st.set_state_if(mp_name, order_id, {"RESERVE_APPROVED"}, "ESCALATED")
                    except Exception:
                        log.exception("cannot hand %s/%s back to a person", mp_name, order_id)
                return ActionResult(fail_kind, _short(e, mp_name))
            # done on the marketplace: from here on it is a success whatever the journal says
            try:
                if not st.set_state_if(mp_name, order_id, set(allowed), new_state):
                    log.error("%s/%s: state changed during %s, now %s", mp_name, order_id,
                              ok_kind, st.get_state(mp_name, order_id))
                    st.set_state(mp_name, order_id, new_state)
                st.log(mp_name, order_id, ok_event, {"user": who})
            except Exception:
                log.exception("cannot record %s for %s/%s", ok_kind, mp_name, order_id)
            if new_state == "CONFIRMED" and self._on_collected is not None:
                try:
                    r = self._on_collected(order)
                    if inspect.isawaitable(r):
                        await r
                except Exception:
                    log.exception("on_collected failed for %s/%s", mp_name, order_id)
            return ActionResult(ok_kind)
