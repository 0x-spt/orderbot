"""Shipment reminders in the group topic «Отгрузочные листы».

Every day at fixed times (Moscow) the warehouses hand orders over to Ozon and Yandex
Market. `before_minutes` (10) ahead of each handover one message goes to the ALERT_CHAT_ID
group, topic SHIPMENTS_THREAD_ID: per company (Альфа, then Бета) and per warehouse of
that slot, how many orders and units are to be handed over today, assembled or not.

What counts (checked live, see the adapters' to_ship): orders not handed over yet
(Ozon awaiting_packaging / awaiting_deliver, Yandex PROCESSING STARTED / READY_TO_SHIP)
whose marketplace handover day (Ozon shipment_date in Moscow, Yandex shipmentDate) is today.
Express warehouses and campaigns are left out: express ships on its own schedule.

The times and warehouses (1C names, as in warehouses.yaml) are in SHIPMENTS_FILE
(shipments.yaml): a bad or missing file turns this part off with a warning, the robot runs.

Its own loop: no 1C, no pause, no buttons, and it only reads lists at the marketplaces. Each
slot is posted once: the last posted slot is kept in the Store, so a restart inside the
window does not post it again; a start after T - before but before T posts at once, a start
after T skips that slot. One read per marketplace and company per slot; a failed send is
retried (same text) while the window lasts, then the admins are told once.
"""
import asyncio
import html
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx
import yaml

from orderbot.alerts import MP_TITLE, MSK
from orderbot.domain import MAIN, SECOND
from orderbot.marketplaces.base import ToShipPartial  # noqa: F401 (re-exported)

log = logging.getLogger(__name__)

TICK_INTERVAL = 20.0
DEFAULT_BEFORE_MIN = 10
MARKETPLACES = ("ozon", "yandex")          # in this order inside a company block
COMPANY_ORDER = (MAIN, SECOND)
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота",
            "воскресенье")
KEYS = {"before_minutes", "names", "weekend_skip", "slots"}
FAILED = ("Отгрузки: сообщение об отгрузке в {time} не собрано из-за сбоя робота. "
          "Проверьте заказы в кабинетах, подробности в журнале на Mac.")
MISSED = ("Отгрузки: сообщение об отгрузке в {time} не дошло до группы. Подробности в "
          "журнале на Mac.")


class ScheduleError(ValueError):
    """The schedule file cannot be used; the message is for the operator."""


# ------------------------------------------------------------------ the schedule
@dataclass(frozen=True)
class Slot:
    time: str                                   # "HH:MM", Moscow
    warehouses: dict[str, list[str]]            # marketplace -> 1C warehouses, in order


@dataclass
class Schedule:
    slots: list[Slot]
    before: timedelta = timedelta(minutes=DEFAULT_BEFORE_MIN)
    weekend_skip: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)

    def name(self, warehouse: str) -> str:
        return self.names.get(warehouse, warehouse)

    def for_day(self, slot: Slot, day: date) -> dict[str, list[str]] | None:
        """The slot's warehouses on that day; None when none is left (no message)."""
        skip = set(self.weekend_skip) if day.weekday() >= 5 else set()
        out = {mp: [w for w in whs if w not in skip] for mp, whs in slot.warehouses.items()}
        return out if any(out.values()) else None


def _time_key(raw) -> str:
    # YAML 1.1 reads 10:00 written without quotes as the number 600 (minutes)
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw < 24 * 60:
        return f"{raw // 60:02d}:{raw % 60:02d}"
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw.strip(), "%H:%M").strftime("%H:%M")
        except ValueError:
            pass
    raise ScheduleError(f"время {raw!r}: нужно ЧЧ:ММ, например \"12:00\"")


def _names(raw, what, known) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ScheduleError(f"{what}: нужен список складов 1С, например [\"СПБ\"]")
    unknown = [x for x in raw if x not in known]
    if unknown:
        raise ScheduleError(f"{what}: склады {', '.join(unknown)} не найдены в файле складов "
                            "(названия как на экране 1С)")
    return list(raw)


def parse_schedule(raw, known) -> Schedule:
    if not isinstance(raw, dict):
        raise ScheduleError("нужен раздел slots, как в shipments.example.yaml")
    extra = set(map(str, raw)) - KEYS
    if extra:
        raise ScheduleError(f"неизвестные разделы: {', '.join(sorted(extra))}")
    slots_raw = raw.get("slots")
    if not isinstance(slots_raw, dict) or not slots_raw:
        raise ScheduleError("раздел slots пуст или не в виде «время: площадка: [склады]»")
    slots = {}
    for t_raw, table in slots_raw.items():
        t = _time_key(t_raw)
        if t in slots:
            raise ScheduleError(f"время {t} указано дважды")
        if not isinstance(table, dict) or not table:
            raise ScheduleError(f"{t}: нужно «ozon: [склады]» и/или «yandex: [склады]»")
        whs = {}
        for mp, names in table.items():
            if mp not in MARKETPLACES:
                raise ScheduleError(f"{t}: площадка {mp!r} не поддерживается (ozon, yandex)")
            whs[mp] = _names(names, f"{t} {mp}", known)
        slots[t] = Slot(t, whs)
    before = raw.get("before_minutes", DEFAULT_BEFORE_MIN)
    if not isinstance(before, int) or isinstance(before, bool) or not 1 <= before <= 120:
        raise ScheduleError("before_minutes: нужно целое число минут от 1 до 120")
    for t in slots:
        h, m = map(int, t.split(":"))
        if h * 60 + m < before:
            raise ScheduleError(f"{t}: сообщение пришлось бы отправить накануне (до полуночи); "
                                f"время должно быть не раньше чем через {before} мин "
                                "после 00:00")
    skip = _names(raw.get("weekend_skip") or [], "weekend_skip", known)
    names = raw.get("names") or {}
    if not isinstance(names, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in names.items()):
        raise ScheduleError("names: нужно «склад 1С: как показывать»")
    return Schedule([slots[t] for t in sorted(slots)], timedelta(minutes=before), skip,
                    dict(names))


def load_schedule(path, known) -> Schedule:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScheduleError("файл не найден") from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ScheduleError(f"файл не читается ({type(e).__name__})") from None
    return parse_schedule(raw, known)


# ------------------------------------------------------------------ the text
def orders_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "заказ"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "заказа"
    return "заказов"


NONE = (0, 0, 0)


def _counts(value):
    """(today, overdue) triples (assembled, not assembled, units); a single triple is today
    only."""
    if len(value) == 3 and all(isinstance(x, int) for x in value):
        return tuple(value), NONE
    return tuple(value[0]), tuple(value[1])


def _amount(triple, bold) -> str:
    assembled, not_assembled, units = triple
    n = assembled + not_assembled
    parts = ([f"собрано {assembled}"] if assembled else []) + (
        [f"не собрано {not_assembled}"] if not_assembled else [])
    what = f"{n} {orders_word(n)}, {units} шт."
    return (f"<b>{what}</b>" if bold else what) + f" ({', '.join(parts)})"


def _line(mp_title, wh_name, value) -> str:
    where = f"{html.escape(mp_title)} · {html.escape(wh_name)}"
    if isinstance(value, str):
        return f"⚠️ {where} — не удалось узнать ({html.escape(value)})"
    today, overdue = _counts(value)
    parts = []
    if sum(today[:2]):
        parts.append(_amount(today, bold=True))
    if sum(overdue[:2]):
        parts.append("⚠️ просрочено: " + _amount(overdue, bold=False))
    return f"{where} — " + (" · ".join(parts) or "нет отгрузки")


def _empty(value) -> bool:
    if isinstance(value, str):
        return False
    today, overdue = _counts(value)
    return not sum(today[:2]) and not sum(overdue[:2])


def slot_text(slot_time: str, day: date, blocks, name=lambda w: w) -> str:
    """blocks: [(company, [(marketplace title, 1C warehouse, value)])]; value is a failure
    reason, or (today, overdue) triples (assembled, not assembled, units), or one triple
    for today only. HTML, everything escaped."""
    when = f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"
    blocks = [(c, lines) for c, lines in blocks if lines]
    if all(_empty(v) for _, lines in blocks for _, _, v in lines):
        return f"🚚 <b>В {slot_time} отгрузки нет</b> · {when}"
    out = [f"🚚 <b>Отгрузка в {slot_time}</b> · {when}"]
    for company, lines in blocks:
        out.append(f"<b>{html.escape(company)}</b>\n"
                   + "\n".join(_line(mp, name(wh), v) for mp, wh, v in lines))
    return "\n\n".join(out)


def failure_reason(e: Exception) -> str:
    """Short and without addresses or keys: the HTTP code, «нет связи» or the type."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.TransportError):
        return "нет связи"
    return type(e).__name__


# ------------------------------------------------------------------ the poster
def _utc_now() -> datetime:
    return datetime.now(UTC)


class ShipmentReminders:
    def __init__(self, schedule: Schedule, sources, store, bot, chat_id: int, thread_id: int,
                 *, companies: dict[str, str], clock=_utc_now, say=None):
        self._schedule = schedule
        self._sources = list(sources)
        self._store = store
        self._bot = bot
        self._chat = chat_id
        self._thread = thread_id
        self._companies = companies
        self._clock = clock
        self._say = say
        self._pending: tuple[str, str, datetime, str] | None = None  # key, time, T, text
        self._broken: str | None = None    # slot whose report() failed: not tried again

    async def run(self, stop: asyncio.Event, interval: float = TICK_INTERVAL) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("shipment reminders tick failed")
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                pass

    def _due(self, now: datetime):
        """(key, slot, T, warehouses) of the slot whose window holds `now`, not yet posted."""
        today = now.date()
        last = self._store.shipments_last_slot() or ""
        for slot in self._schedule.slots:
            t = datetime.combine(today, time.fromisoformat(slot.time), tzinfo=MSK)
            key = f"{today.isoformat()} {slot.time}"
            if not t - self._schedule.before <= now < t or key <= last or key == self._broken:
                continue
            whs = self._schedule.for_day(slot, today)
            if whs is None:
                continue
            return key, slot, t, whs
        return None

    async def tick(self) -> None:
        now = self._clock().astimezone(MSK)
        if self._pending is not None:
            key, slot_time, t, text = self._pending
            if now >= t:
                self._pending = None
                log.warning("shipment message for %s not delivered before the handover", key)
                await self._tell_admins(MISSED.format(time=slot_time))
            else:
                await self._send(key, text)
                return
        due = self._due(now)
        if due is None:
            return
        key, slot, t, whs = due
        try:
            text = await self.report(slot.time, now.date(), whs)
        except Exception:
            # a bug, not a marketplace failure (those are lines of the text): no re-reading
            # every tick, the admins hear once
            self._broken = key
            log.exception("shipment message for %s not built", key)
            await self._tell_admins(FAILED.format(time=slot.time))
            return
        self._pending = (key, slot.time, t, text)
        await self._send(key, text)

    async def _send(self, key: str, text: str) -> None:
        try:
            await self._bot.send_message(self._chat, text, message_thread_id=self._thread,
                                         parse_mode="HTML")
        except Exception:
            log.exception("shipment message for %s not sent, retrying", key)
            return
        self._pending = None
        self._store.set_shipments_last_slot(key)
        log.info("shipment message for %s posted", key)

    async def _tell_admins(self, text: str) -> None:
        if self._say is None:
            return
        try:
            await self._say(text)
        except Exception:
            log.exception("cannot tell the admins: %s", text)

    async def report(self, slot_time: str, day: date, whs: dict[str, list[str]]) -> str:
        blocks = []
        for account in COMPANY_ORDER:
            lines = []
            for mp in MARKETPLACES:
                src = next((s for s in self._sources
                            if s.name == mp and (getattr(s, "account", None) or MAIN)
                            == account), None)
                if src is None:
                    continue
                mine = [w for w in whs.get(mp, []) if w in src.shipment_warehouses()]
                if not mine:
                    continue
                failed = {}
                try:
                    items = await src.to_ship(only=set(mine))
                except ToShipPartial as e:
                    items, failed = e.items, e.failed
                except Exception as e:
                    items, failed = [], {w: e for w in mine}
                for w in mine:
                    if w in failed:
                        why = failure_reason(failed[w])
                        log.warning("shipments: %s %s %s not read: %s", mp, account, w, why)
                        lines.append((MP_TITLE[mp], w, why))
                        continue
                    mine_items = [x for x in items if x.warehouse == w]
                    lines.append((MP_TITLE[mp], w,
                                  (_tally(x for x in mine_items if x.day == day),
                                   _tally(x for x in mine_items if x.day < day))))
            blocks.append((self._companies.get(account, account), lines))
        return slot_text(slot_time, day, blocks, self._schedule.name)


def _tally(items) -> tuple[int, int, int]:
    items = list(items)
    done = sum(1 for x in items if x.assembled)
    return done, len(items) - done, sum(x.units for x in items)


def build_shipments(cfg, mps, store, bot, say):
    """(the part or None, a warning for the admins or None). Off without the topic or the
    group (one info line); a missing or bad schedule file is a warning, the robot runs."""
    if cfg.shipments_thread_id is None or cfg.alert_chat_id is None:
        log.info("Напоминания об отгрузках выключены: не задано %s",
                 "SHIPMENTS_THREAD_ID" if cfg.shipments_thread_id is None else "ALERT_CHAT_ID")
        return None, None
    sources = [m for m in mps if getattr(m, "name", None) in MARKETPLACES]
    if not sources:
        log.info("Напоминания об отгрузках выключены: Озон и Маркет не подключены")
        return None, None
    known = {v for table in cfg.warehouses.values() for v in table.values()}
    try:
        schedule = load_schedule(cfg.shipments_file, known)
    except ScheduleError as e:
        warning = (f"Напоминания об отгрузках выключены: файл {cfg.shipments_file}: {e}. "
                   "Исправьте файл (пример — shipments.example.yaml) и перезапустите робота.")
        return None, warning   # main logs it and tells the admins at start
    return ShipmentReminders(schedule, sources, store, bot, cfg.alert_chat_id,
                             cfg.shipments_thread_id, companies=cfg.company_names,
                             say=say), None
