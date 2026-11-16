"""Telegram side: commands, buttons, message texts.

BotController holds the behaviour and knows nothing of aiogram's transport, so it is tested
on plain calls; `build_dispatcher` is the thin aiogram wiring. Everything runs in the event
loop thread, like the Store.
"""
import asyncio
import html
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from orderbot.alerts import (  # noqa: F401
    CAPTION_LIMIT,
    MP_TITLE,
    MSK,
    REASON_TEXT,
    format_caption,
    reason_text,
    result_line,
)
from orderbot.domain import DEFAULT_COMPANY_NAMES, MAIN
from orderbot.store import Store

log = logging.getLogger(__name__)

BTN_BUSY = "Я работаю в 1С"
BTN_DONE = "Я закончил"
BTN_CHECK = "🔄 Проверить сейчас"
CONFIRM_WORD = "да"
CONFIRM_TTL = timedelta(minutes=5)
# Marketplaces the robot never confirms by itself: /mode … confirm is refused. WB orders are
# collected only by a person's «Собрать» button (the robot's WB confirm was never asked for).
OBSERVE_ONLY = {"wb"}
REMINDER_EVERY = timedelta(minutes=60)
MAX_PHOTOS = 10
TEXT_LIMIT = 4096
GROUP_DOWN = "Группа для заказов недоступна: {err}. Заказы приходят в личку."
GROUP_MOVED = (" Группа стала супергруппой с новым номером {new_id}: впишите его в .env "
               "в ALERT_CHAT_ID и перезапустите робота.")
RESULT_RESERVE = 200   # room left in a caption for the result line added by finish
CANCEL_CONFIRM_TTL = timedelta(minutes=2)
NO_ACCESS = "Нет доступа"
CANCEL_EXPIRED = "Время вышло, нажмите «Отменить» ещё раз"
CANCEL_OTHERS = "Подтвердить может тот, кто нажал «Отменить»"
# A press is answered with its result when that comes within this time (Telegram drops a
# late answer); otherwise it is answered empty and the result follows.
ANSWER_WAIT = 10.0
# Order states whose message gives no visible sign of a reserve answer's outcome.
REPLY_STATES = ("AWAITING_RESERVE", "RESERVE_APPROVED")
ACTIONS = ("col", "cx", "cxy", "back")
# Roles: a press by someone without access asks the admins, at most once per ROLE_ASK_EVERY.
ROLE_ASK_EVERY = timedelta(minutes=10)
ROLE_ASK = "Кнопку под заказом нажал {who} (id {id}) — доступа нет."
ROLE_DECIDED = "Уже решено"
ROLE_FROM_ENV = "Этот менеджер записан в .env — убрать можно только там"
_ROLE_DATA = re.compile(r"role:(grant|deny|revoke):(\d{1,19})")

# ------------------------------------------------------------------ parsing
class CommandError(ValueError):
    """The text is a command the bot understands badly; the message is shown to the user."""


@dataclass
class Command:
    name: str
    marketplace: str | None = None
    brand: str | None = None
    value: object = None


_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"'})


def _decimal(text: str, what: str) -> Decimal:
    try:
        d = Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise CommandError(f"{what}: нужно число, получено «{text}»") from None
    if not d.is_finite():
        raise CommandError(f"{what}: нужно обычное число")
    return d


def _on_off(text: str) -> bool:
    if text not in ("on", "off"):
        raise CommandError("нужно on или off")
    return text == "on"


def parse_command(text: str) -> Command:
    try:
        parts = shlex.split(text.translate(_QUOTES))
    except ValueError:
        raise CommandError("не закрыта кавычка") from None
    if not parts or not parts[0].startswith("/"):
        raise CommandError("это не команда")
    name = parts[0][1:].split("@")[0].lower()
    args = parts[1:]

    def need(n):
        if len(args) != n:
            raise CommandError(f"/{name}: неверное число параметров")

    if name in ("start", "status", "managers"):
        return Command(name)
    if name == "stocksync":
        # value: (None, on) for the whole sync, ("yandex", on) for Yandex's own switch
        if len(args) == 2 and args[0].lower() == "yandex":
            return Command(name, value=("yandex", _on_off(args[1].lower())))
        if len(args) == 2:
            raise CommandError("отдельно включается только yandex: /stocksync yandex on")
        need(1)
        return Command(name, value=(None, _on_off(args[0].lower())))
    if name in ("soinvest", "multiqty"):
        need(2)
        return Command(name, args[0].lower(), value=_on_off(args[1].lower()))
    if name == "tolerance":
        need(2)
        v = _decimal(args[1], "погрешность")
        if v < 0:
            raise CommandError("погрешность не может быть отрицательной")
        return Command(name, args[0].lower(), value=v)
    if name == "mode":
        need(2)
        if args[1].lower() not in ("observe", "confirm"):
            raise CommandError("режим: observe или confirm")
        return Command(name, args[0].lower(), value=args[1].lower())
    if name == "discount":
        need(3)
        v = _decimal(args[2], "скидка")
        if not 0 < v < 100:
            raise CommandError("скидка должна быть больше 0 и меньше 100 процентов")
        return Command(name, args[0].lower(), brand=args[1], value=v)
    if name == "discount_off":
        need(2)
        return Command(name, args[0].lower(), brand=args[1])
    raise CommandError(f"неизвестная команда /{name}")


# ----------------------------------------------------------------- notifier
def _btn(text, data) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def order_markup(mp, order_id, state: str) -> InlineKeyboardMarkup | None:
    key = f"{mp}:{order_id}"
    cancel = _btn("❌ Отменить", f"act:cx:{key}")
    if state in ("OBSERVED", "ESCALATED"):
        rows = [[_btn("✅ Собрать", f"act:col:{key}"), cancel]]
    elif state == "AWAITING_RESERVE":
        rows = [[_btn("Резерв можно", f"rsv:y:{key}"), _btn("Резерв нельзя", f"rsv:n:{key}")],
                [cancel]]
    elif state in ("REJECTED_BY_HUMAN", "RESERVE_APPROVED"):
        rows = [[cancel]]
    else:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_confirm_markup(mp, order_id) -> InlineKeyboardMarkup:
    key = f"{mp}:{order_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("Да, отменить", f"act:cxy:{key}"), _btn("Назад", f"act:back:{key}")]])


def role_request_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Сделать менеджером", f"role:grant:{user_id}"),
        _btn("Отказать", f"role:deny:{user_id}")]])


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BUSY), KeyboardButton(text=BTN_DONE)],
                  [KeyboardButton(text=BTN_CHECK)]],
        resize_keyboard=True)


def photo_rows(snapshots: list[str], reads: dict | None) -> list[list[str]]:
    """One row per article: its 1C tables snapshot, then the 1C picture and the
    marketplace photo when they exist. A snapshot of a failed read is a row of
    its own; photos of a read whose snapshot is missing make a row without it."""
    attached: dict[str, list[str]] = {}
    extra: list[list[str]] = []
    for r in (reads or {}).values():
        pictures = [p for p in (getattr(r, "photo_1c", None), getattr(r, "photo_mp", None))
                    if p and Path(p).is_file()]
        if not pictures:
            continue
        snap = getattr(r, "snapshot_path", None)
        if snap in snapshots and snap not in attached:
            attached[snap] = pictures
        else:
            extra.append(pictures)
    return [[s] + attached.get(s, []) for s in snapshots] + extra


BUTTONS_TEXT = "Заказ <b>{num}</b>"   # the buttons message under an album
ALBUM_MAX = 10   # Telegram: at most 10 photos in one album
# an order of more than 3 articles gets its photos as albums of 3 articles
# (up to 9 photos: tables, 1C picture, marketplace photo each) before the order message.
ALBUM_ARTICLES = 3
ALBUM_PART = "📷 Заказ <b>{num}</b> · артикулы {a}–{b} из {n}"


def order_photos(snapshots: list[str], reads: dict | None) -> list[str]:
    """The order's photos in reading order — per article its 1C tables, the 1C picture and
    the marketplace photo — at most one album (separate photos, not one sheet)."""
    return [p for row in photo_rows(snapshots, reads) for p in row][:ALBUM_MAX]


class TelegramNotifier:
    """Implements the pipeline's Notifier. Order messages go to the alert group when it is
    set, otherwise to every admin; system messages always go to the admins."""

    def __init__(self, bot, admin_chat_ids, store: Store, alert_chat_id=None,
                 alert_thread_id=None, companies: dict[str, str] | None = None,
                 health_thread_id=None):
        self._bot = bot
        self._health_thread = health_thread_id   # topic for API / 1C outages
        # account -> company name for the decision line (always shown)
        self._companies = dict(companies or DEFAULT_COMPANY_NAMES)
        self._thread = alert_thread_id   # forum topic of the group for order messages
        self._chats = list(admin_chat_ids)
        self._store = store
        self._alert_chat = alert_chat_id
        self._group_down_said = False   # «group unreachable» was said; reset by a group send

    async def _each(self, chats, fn) -> None:
        last = None
        sent = False
        for chat in chats:
            try:
                await fn(chat)
                sent = True
            except Exception as e:
                last = e
                log.exception("telegram send to %s failed", chat)
        if chats and not sent:
            raise last

    async def _group_down(self, e: Exception) -> None:
        """Said once until the group works again; the operator learns why orders come privately
        and, when the group became a supergroup, its new id for .env."""
        new_id = getattr(e, "migrate_to_chat_id", None)
        if new_id is not None:
            log.error("the alert group %s moved to %s: set ALERT_CHAT_ID=%s in .env",
                      self._alert_chat, new_id, new_id)
        if self._group_down_said:
            return
        err = " ".join(str(e).split()) or type(e).__name__
        if len(err) > 80:
            err = err[:79] + "…"
        text = GROUP_DOWN.format(err=err)
        if new_id is not None:
            text += GROUP_MOVED.format(new_id=new_id)
        try:
            await self.system_alert(text)
            self._group_down_said = True
        except Exception:
            log.exception("cannot say that the alert group is down")

    async def _each_order_chat(self, one) -> None:
        """Order messages go to the group; when it cannot be reached, to every admin."""
        if self._alert_chat is None:
            await self._each(self._chats, one)
            return
        try:
            await one(self._alert_chat)
        except Exception as e:
            log.exception("telegram send to the alert group %s failed", self._alert_chat)
            await self._group_down(e)
            await self._each(self._chats, one)
            return
        self._group_down_said = False

    async def _albums_first(self, chat, rows, oid, topic) -> None:
        """Albums of ALBUM_ARTICLES articles each, before the order message. A part that
        fails is logged and skipped: the order message still follows."""
        n = len(rows)
        for i in range(0, n, ALBUM_ARTICLES):
            part = [p for row in rows[i:i + ALBUM_ARTICLES] for p in row][:ALBUM_MAX]
            cap = ALBUM_PART.format(num=html.escape(oid), a=i + 1,
                                    b=min(i + ALBUM_ARTICLES, n), n=n)
            try:
                if len(part) == 1:
                    await self._bot.send_photo(chat, FSInputFile(part[0]), caption=cap,
                                               parse_mode="HTML", **topic)
                else:
                    media = [InputMediaPhoto(media=FSInputFile(p)) for p in part]
                    media[0] = InputMediaPhoto(media=FSInputFile(part[0]), caption=cap,
                                               parse_mode="HTML")
                    await self._bot.send_media_group(chat, media, **topic)
            except Exception:
                log.exception("album part %s of order %s to %s failed", i // ALBUM_ARTICLES + 1,
                              oid, chat)

    async def _send_order(self, order, caption, snapshot, markup, reads=None) -> None:
        # snapshot paths come only from crop_tables output (see screen/onec.py); the photos
        # beside them from the 1C picture tab and the marketplace's product photo
        rows = photo_rows([p for p in snapshot if Path(p).is_file()][:MAX_PHOTOS], reads)
        many = len(rows) > ALBUM_ARTICLES
        photos = [] if many else [p for row in rows for p in row][:ALBUM_MAX]
        mp, oid = order.marketplace, order.order_id

        async def one(chat):
            # the topic applies only to the group, never to an admin's private chat
            topic = ({"message_thread_id": self._thread}
                     if chat == self._alert_chat and self._thread is not None else {})
            if many:
                await self._albums_first(chat, rows, oid, topic)
            rest = photos
            sent = None
            text = caption           # what the message with the buttons says
            if len(photos) > 1:
                # Telegram puts no buttons under an album: the order
                # text is the album's caption, the buttons come in a short message under it;
                # a caption too long for an album stays in the buttons message instead
                in_album = len(caption) <= CAPTION_LIMIT
                media = [InputMediaPhoto(media=FSInputFile(p)) for p in photos]
                if in_album:
                    media[0] = InputMediaPhoto(media=FSInputFile(photos[0]), caption=caption,
                                               parse_mode="HTML")
                try:
                    await self._bot.send_media_group(chat, media, **topic)
                    if in_album:
                        text = BUTTONS_TEXT.format(num=html.escape(oid))
                except Exception:
                    log.exception("album to %s failed, sending the order text only", chat)
                photos_left, rest = [], []
            else:
                photos_left = photos
            if photos_left and len(caption) + RESULT_RESERVE <= CAPTION_LIMIT:
                try:
                    sent = await self._bot.send_photo(
                        chat, FSInputFile(photos_left[0]), caption=caption, parse_mode="HTML",
                        reply_markup=markup, **topic)
                    kind, rest = "photo", photos_left[1:]
                except Exception:
                    log.exception("photo with caption to %s failed, sending text", chat)
                    rest = []
            if sent is None:
                sent = await self._bot.send_message(chat, text[:TEXT_LIMIT],
                                                    parse_mode="HTML", reply_markup=markup,
                                                    **topic)
                kind = "text"
            else:
                text = caption
            try:
                self._store.save_alert_message(mp, oid, chat, sent.message_id, kind, text)
            except Exception:
                log.exception("cannot remember the message of order %s", oid)
            for p in rest:
                try:
                    await self._bot.send_photo(chat, FSInputFile(p), **topic)
                except Exception:
                    log.exception("photo to %s failed", chat)

        await self._each_order_chat(one)

    def _caption(self, order, decision, urgent, reads) -> str:
        account = getattr(order, "account", MAIN)
        return format_caption(order, decision, reads, urgent, datetime.now(UTC),
                              company=self._companies.get(account, account))

    async def order_alert(self, order, decision, snapshot, urgent, observed, reads=None) -> None:
        state = self._store.get_state(order.marketplace, order.order_id)
        markup = order_markup(order.marketplace, order.order_id, state) if state else None
        await self._send_order(order, self._caption(order, decision, urgent, reads), snapshot,
                               markup, reads)

    async def reserve_request(self, order, decision, snapshot, urgent, reads=None) -> None:
        await self._send_order(order, self._caption(order, decision, urgent, reads), snapshot,
                               order_markup(order.marketplace, order.order_id,
                                            "AWAITING_RESERVE"), reads)

    def _log_edit_error(self, m, what) -> None:
        import sys
        e = sys.exc_info()[1]
        if "message is not modified" in str(e):
            log.info("message %s of order is already up to date", m.message_id)
        else:
            log.exception("cannot %s message %s", what, m.message_id)

    @staticmethod
    def _full(m) -> str:
        """A stored copy as shown: the order text, its result line, then the notes."""
        out = m.html
        if m.result:
            out += "\n\n" + m.result
        if m.notes:
            out += ("\n" if m.result else "\n\n") + m.notes
        return out

    async def finish(self, mp, order_id, line_html: str, markup) -> int:
        """Returns the number of stored copies; 0 means the order has no stored message.
        The line replaces an earlier result line; notes added later (add_note) stay."""
        try:
            self._store.set_alert_result(mp, order_id, line_html)
        except Exception:
            log.exception("cannot remember the result line of order %s", order_id)
        msgs = self._store.alert_messages(mp, order_id)
        for m in msgs:
            html = self._full(m) if m.result == line_html else m.html + "\n\n" + line_html
            try:
                if m.kind == "photo":
                    await self._bot.edit_message_caption(
                        chat_id=m.chat_id, message_id=m.message_id, caption=html,
                        parse_mode="HTML", reply_markup=markup)
                else:
                    await self._bot.edit_message_text(
                        chat_id=m.chat_id, message_id=m.message_id, text=html[:TEXT_LIMIT],
                        parse_mode="HTML", reply_markup=markup)
            except Exception:
                self._log_edit_error(m, "edit")
                try:    # stale buttons must never stay on a finished order
                    await self._bot.edit_message_reply_markup(
                        chat_id=m.chat_id, message_id=m.message_id, reply_markup=markup)
                except Exception:
                    self._log_edit_error(m, "change buttons of")
        return len(msgs)

    async def add_note(self, mp, order_id, line_html: str) -> int:
        """Adds a line (the stock sync report) under the order's result in every stored copy;
        the buttons of the order's current state stay. A photo caption that would get too
        long gets the line as a message of its own. Returns the number of copies; with none
        stored (an order the robot collected without a message) the line goes, headed by the
        order, to where order messages go: the group and its topic, else every admin."""
        self._store.add_alert_note(mp, order_id, line_html)
        if not self._store.alert_messages(mp, order_id):
            await self._order_line(mp, order_id, line_html)
            return 0
        state = self._store.get_state(mp, order_id)
        markup = order_markup(mp, order_id, state) if state else None
        msgs = self._store.alert_messages(mp, order_id)
        for m in msgs:
            html = self._full(m)
            try:
                if m.kind == "photo" and len(html) > CAPTION_LIMIT:
                    topic = ({"message_thread_id": self._thread}
                             if m.chat_id == self._alert_chat and self._thread is not None
                             else {})
                    await self._bot.send_message(m.chat_id, line_html[:TEXT_LIMIT],
                                                 parse_mode="HTML", **topic)
                elif m.kind == "photo":
                    await self._bot.edit_message_caption(
                        chat_id=m.chat_id, message_id=m.message_id, caption=html,
                        parse_mode="HTML", reply_markup=markup)
                else:
                    await self._bot.edit_message_text(
                        chat_id=m.chat_id, message_id=m.message_id, text=html[:TEXT_LIMIT],
                        parse_mode="HTML", reply_markup=markup)
            except Exception:
                self._log_edit_error(m, "add a note to")
        return len(msgs)

    async def _order_line(self, mp, order_id, line_html: str) -> None:
        try:
            account = self._store.get_order(mp, order_id).account
        except Exception:
            account = MAIN
        head = (f"Заказ {html.escape(str(order_id))} · {MP_TITLE.get(mp, html.escape(mp))} · "
                f"{html.escape(self._companies.get(account, account))}")
        text = f"{head}\n{line_html}"[:TEXT_LIMIT]

        async def one(chat):
            topic = ({"message_thread_id": self._thread}
                     if chat == self._alert_chat and self._thread is not None else {})
            await self._bot.send_message(chat, text, parse_mode="HTML", **topic)

        await self._each_order_chat(one)

    async def order_done(self, mp, order_id, line_html: str) -> int:
        """The robot collected the order: the result line goes in, the buttons go away."""
        return await self.finish(mp, order_id, line_html, None)

    async def refresh_buttons(self, mp, order_id) -> int:
        """Every stored copy gets the buttons of the order's current state."""
        state = self._store.get_state(mp, order_id)
        return await self.set_markup(mp, order_id,
                                     order_markup(mp, order_id, state) if state else None)

    async def set_markup(self, mp, order_id, markup) -> int:
        msgs = self._store.alert_messages(mp, order_id)
        for m in msgs:
            try:
                await self._bot.edit_message_reply_markup(
                    chat_id=m.chat_id, message_id=m.message_id, reply_markup=markup)
            except Exception:
                self._log_edit_error(m, "change buttons of")
        return len(msgs)

    async def system_alert(self, text: str) -> None:
        async def one(chat):
            await self._bot.send_message(chat, text)
        await self._each(self._chats, one)

    async def health_alert(self, text_html: str) -> None:
        """An API or 1C outage and its end: the group's health topic; when the topic is not
        set or the group cannot be reached, every admin privately (never lost)."""
        if self._alert_chat is not None and self._health_thread is not None:
            try:
                await self._bot.send_message(self._alert_chat, text_html, parse_mode="HTML",
                                             message_thread_id=self._health_thread)
                return
            except Exception:
                log.exception("health alert to the group failed, sending to the admins")

        async def one(chat):
            await self._bot.send_message(chat, text_html, parse_mode="HTML")
        await self._each(self._chats, one)

    async def to_admins(self, text: str, markup) -> None:
        """A message with buttons to every admin privately (never to the group)."""
        async def one(chat):
            await self._bot.send_message(chat, text, reply_markup=markup)
        await self._each(self._chats, one)

    async def say(self, text: str, keyboard: bool = False) -> None:
        async def one(chat):
            await self._bot.send_message(chat, text,
                                         reply_markup=main_keyboard() if keyboard else None)
        await self._each(self._chats, one)


# --------------------------------------------------------------- controller
KEEP = object()   # ActionReply.markup: leave the buttons as they are


@dataclass
class ActionReply:
    toast: str | None                  # shown as the button's pop-up
    finish_line: str | None = None     # appended to every copy of the order message
    markup: object = KEEP              # new keyboard, None removes it, KEEP leaves it
    again: tuple[str, str] | None = None   # the order waits for a reserve answer again
    ask: tuple[str, object] | None = None  # (text, buttons) for the admins: give access?


@dataclass
class RoleReply:
    toast: str | None
    text: str | None = None            # the pressed message's new text; None leaves it
    markup: object = None              # its buttons with the new text


@dataclass
class Screen:
    """A reply with its own inline buttons (instead of the main keyboard)."""
    text: str
    markup: object = None


def user_label(user_id, who: str | None = None) -> str:
    return f"{user_id} {who}".strip() if who else str(user_id)


class BotController:
    """Admins (`admin_ids`) give commands and press buttons; managers only press the buttons
    under order messages. Messages from anyone but an admin are ignored. Managers come from
    .env (`manager_ids`) and from admins' answers in the bot (stored); admins only from .env."""

    def __init__(self, store: Store, pipeline, admin_ids, marketplaces, *, manager_ids=(),
                 actions=None, clock=None, companies: dict[str, list[str]] | None = None):
        self._store = store
        # company name -> its connected marketplaces, for /status (settings stay shared)
        self._companies = {c: list(m) for c, m in (companies or {}).items()}
        self._pipeline = pipeline
        self._allowed = set(admin_ids)
        self._env_managers = set(manager_ids)
        self._db_managers = {m.user_id for m in store.managers()}
        self._asked: dict[int, datetime] = {}    # access requests sent to the admins
        self._mps = set(marketplaces)
        self._actions = actions          # HumanActions: the only way to collect or cancel
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending_mode: dict[int, tuple[str, datetime]] = {}
        self._pending_cancel: dict[tuple[int, tuple[str, str]], datetime] = {}
        self._reserve_clicked: set[tuple[str, str]] = set()
        self._reserve_inflight: set[tuple[str, str]] = set()

    def allowed(self, user_id) -> bool:
        """Commands and the pause buttons: admins only."""
        return user_id in self._allowed

    def can_press(self, user_id) -> bool:
        """Buttons under order messages: admins and managers."""
        return (user_id in self._allowed or user_id in self._env_managers
                or user_id in self._db_managers)

    async def on_text(self, user_id: int, text: str,
                      who: str | None = None) -> str | Screen | None:
        """Returns the reply, or None when the message is ignored."""
        if not self.allowed(user_id):
            return None
        text = (text or "").strip()
        pending = self._pending_mode.pop(user_id, None)
        if (pending and pending[0] not in OBSERVE_ONLY and text.lower() == CONFIRM_WORD
                and self._clock() - pending[1] <= CONFIRM_TTL):
            self._store.set_mode(pending[0], "confirm", user_label(user_id, who))
            return f"{pending[0]}: режим подтверждения включён."
        if text == BTN_BUSY:
            self._store.set_paused(True, user_label(user_id, who))
            return "Пауза. Робот не трогает 1С. Нажмите «Я закончил», когда освободитесь."
        if text == BTN_DONE:
            self._store.set_paused(False, user_label(user_id, who))
            return "Пауза снята, робот продолжит со следующего прохода."
        if text == BTN_CHECK:
            return self._check_now()
        if not text.startswith("/"):
            return None
        try:
            cmd = parse_command(text)
            if cmd.marketplace is not None and cmd.marketplace not in self._mps:
                raise CommandError(f"площадка «{cmd.marketplace}» не подключена")
            return self._run(user_id, cmd, who)
        except CommandError as e:
            return f"Не понял: {e}"

    def _check_now(self) -> str:
        check = getattr(self._pipeline, "check_now", None)
        if check is None:
            return "Проверка по кнопке недоступна."
        state = check()
        if self._store.is_paused():
            return ("Проверяю заказы на площадках. Сейчас пауза: в 1С робот не пойдёт, "
                    "пока не нажмёте «Я закончил».")
        if state == "running":
            return "Проверка уже идёт, сразу после неё робот проверит ещё раз."
        return "Проверяю заказы сейчас."

    def _run(self, user_id: int, c: Command, who: str | None = None) -> str | Screen:
        st, user, mp = self._store, user_label(user_id, who), c.marketplace
        if c.name == "start":
            return "Робот проверки заказов. Команды: /status, /managers, /stocksync on|off"
        if c.name == "status":
            return self._status()
        if c.name == "managers":
            return self._managers_screen()
        if c.name == "stocksync":
            which, on = c.value
            st.set_stocksync(on, user, which)
            if which == "yandex":
                return (f"Остатки на Маркете: {'включено' if on else 'выключено'}. "
                        "Пишутся, только когда включено и /stocksync on.")
            if on:
                return ("Остатки на других площадках: включено (Озон, WB). После сборки заказа "
                        "Альфа робот уменьшит остаток этого артикула на других складах. "
                        "Маркет включается отдельно: /stocksync yandex on.")
            return "Остатки на других площадках: выключено. Робот только пишет, что уменьшил бы."
        if c.name == "soinvest":
            st.change_setting(mp, "soinvest", c.value, user)
            return f"{mp}: соинвест {'включён' if c.value else 'выключен'}."
        if c.name == "multiqty":
            st.change_setting(mp, "multi_qty_verified", c.value, user)
            return f"{mp}: количество больше 1 {'проверено' if c.value else 'не проверено'}."
        if c.name == "tolerance":
            st.change_setting(mp, "tolerance", c.value, user)
            return f"{mp}: погрешность {c.value} ₽."
        if c.name == "discount":
            st.change_setting(mp, "brand_discount", {"brand": c.brand, "percent": c.value}, user)
            return f"{mp}: скидка на «{c.brand}» {c.value}%. Действует на заказы, созданные позже."
        if c.name == "discount_off":
            st.change_setting(mp, "brand_discount", {"brand": c.brand, "percent": None}, user)
            return f"{mp}: скидка на «{c.brand}» снята."
        if c.name == "mode":
            if c.value == "observe":
                st.set_mode(mp, "observe", user)
                return f"{mp}: режим наблюдения, подтверждений нет."
            if mp in OBSERVE_ONLY:
                return f"{mp}: подтверждение сборки для этой площадки ещё не сделано, только наблюдение."
            self._pending_mode[user_id] = (mp, self._clock())
            return (f"{mp}: включить подтверждение? Робот начнёт сам переводить заказы в "
                    "«готов к отгрузке». Переходить из наблюдения в подтверждение можно только "
                    "после проверки наблюдения: решения робота совпали с решениями сотрудников. "
                    f"Если да, ответьте «{CONFIRM_WORD}» в течение 5 минут.")
        raise CommandError(f"неизвестная команда /{c.name}")

    def _status(self) -> str:
        st, now = self._store, self._clock()
        lines = []
        since = st.paused_since()
        lines.append(f"Пауза: да, с {since:%d.%m %H:%M}" if since else "Пауза: нет")
        for company, mps in self._companies.items():
            lines.append(f"Компания {company}: {', '.join(mps) or 'площадки не подключены'}")
        lines.append("Остатки на других площадках: "
                     + ("включено (Озон, WB)" if st.get_stocksync() else "выключено")
                     + "; Маркет: " + ("включено" if st.get_stocksync("yandex") else "выключено"))
        for mp in sorted(self._mps):
            s = st.settings_at(mp, now)
            discounts = ", ".join(f"{b} {p}%" for b, p in s.brand_discounts.items()) or "нет"
            lines.append(f"{mp}: режим {st.get_mode(mp)}; соинвест {'да' if s.soinvest else 'нет'}; "
                         f"погрешность {s.tolerance} ₽; скидки: {discounts}; "
                         f"количество больше 1 {'проверено' if s.multi_qty_verified else 'не проверено'}")
        lines.append(f"Очередь: ждут проверки {len(st.pending())}, ждут разрешения резерва "
                     f"{len(st.in_state('AWAITING_RESERVE'))}")
        last = st.last_event_ts("decision")
        lines.append(f"Последняя проверка заказа: {last:%d.%m %H:%M}" if last
                     else "Последняя проверка заказа: проверок нет")
        return "\n".join(lines)

    async def on_callback(self, user_id: int, data: str, who: str | None = None) -> str | None:
        r = await self.on_action(user_id, data, who)
        return r.toast if r else None

    def order_state(self, key) -> str | None:
        return self._store.get_state(*key)

    def _markup_now(self, key):
        state = self._store.get_state(*key)
        return order_markup(key[0], key[1], state) if state else None

    async def on_action(self, user_id: int, data: str, who: str | None = None, *,
                        name: str | None = None) -> ActionReply | None:
        """A press on a button under an order message; None when the data is not ours.
        `who` is the journal label (as for commands), `name` the short name for the result
        line in the message (defaults to `who`)."""
        parts = (data or "").split(":", 3)
        if len(parts) != 4 or parts[2] not in self._mps:
            return None
        if parts[0] == "rsv":
            if parts[1] not in ("y", "n"):
                return None
        elif parts[0] != "act" or parts[1] not in ACTIONS:
            return None
        if not self.can_press(user_id):
            return ActionReply(NO_ACCESS, ask=self._ask(user_id, who))
        key = (parts[2], parts[3])
        if parts[0] == "rsv":
            return await self._reserve(user_id, key, parts[1] == "y", who)
        label = user_label(user_id, who)
        name = name or who or str(user_id)
        now = self._clock()
        what = parts[1]
        if what == "cx":
            self._pending_cancel = {k: t for k, t in self._pending_cancel.items()
                                    if now - t <= CANCEL_CONFIRM_TTL}
            self._pending_cancel[(user_id, key)] = now
            return ActionReply(None, markup=cancel_confirm_markup(*key))
        if what == "back":
            self._pending_cancel.pop((user_id, key), None)
            return ActionReply(None, markup=self._markup_now(key))
        if what == "cxy":
            asked = self._pending_cancel.get((user_id, key))
            mine = asked is not None and now - asked <= CANCEL_CONFIRM_TTL
            if not mine and any(k == key and u != user_id and now - t <= CANCEL_CONFIRM_TTL
                                for (u, k), t in self._pending_cancel.items()):
                return ActionReply(CANCEL_OTHERS)
            self._pending_cancel.pop((user_id, key), None)
            if not mine:
                return ActionReply(CANCEL_EXPIRED, markup=self._markup_now(key))
        if self._actions is None:
            return ActionReply("Кнопки не подключены")
        try:
            if what == "col":
                res = await self._actions.collect(key[0], key[1], label)
            else:
                res = await self._actions.cancel(key[0], key[1], label)
        except Exception:
            log.exception("button %s on %s failed", what, key)
            return ActionReply("Не получилось, нажмите ещё раз")
        now = self._clock()
        if res.kind == "busy":
            return ActionReply(res.error)
        if res.kind in ("collected", "cancelled", "already_done"):
            return ActionReply(None, result_line(res.kind, name, now), None)
        if res.kind in ("collect_failed", "cancel_failed"):
            return ActionReply(None, result_line(res.kind, name, now, res.error),
                               self._markup_now(key))
        # not_allowed_state: the buttons were stale; show the ones for the current state
        return ActionReply("Сейчас это сделать нельзя", markup=self._markup_now(key))

    def _reserve_outcome(self, key, text) -> ActionReply:
        if self._store.get_state(*key) == "CONFIRMED":
            return ActionReply(text, result_line("collected_by_robot", "", self._clock()), None)
        return ActionReply(text, markup=self._markup_now(key))

    async def _reserve(self, user_id, key, approved: bool, who) -> ActionReply:
        if key in self._reserve_inflight:
            return ActionReply("Ответ по этому заказу уже принят и обрабатывается.")
        if key in self._reserve_clicked:
            if self._store.get_state(*key) == "AWAITING_RESERVE":
                self._reserve_clicked.discard(key)    # the order asks again: a new answer counts
            else:
                return self._reserve_outcome(key, "Ответ по этому заказу уже принят.")
        self._reserve_inflight.add(key)   # before the await: a double click cannot pass twice
        try:
            text = await self._pipeline.resolve_reserve(key[0], key[1], approved,
                                                        user_label(user_id, who))
        except Exception:
            log.exception("resolve_reserve failed")
            return ActionReply("Не удалось обработать ответ, кнопки остались: нажмите ещё раз.")
        finally:
            self._reserve_inflight.discard(key)
        if self._store.get_state(*key) == "AWAITING_RESERVE":
            return ActionReply(text, markup=self._markup_now(key),
                               again=key if approved else None)
        self._reserve_clicked.add(key)   # the order left AWAITING_RESERVE: the answer is used
        return self._reserve_outcome(key, text)

    # ------------------------------------------------------------ roles
    def _ask(self, user_id, who) -> tuple[str, InlineKeyboardMarkup] | None:
        """The admins' question about a person without access; None within ROLE_ASK_EVERY of
        the last one for that person, so repeated presses do not flood the admins."""
        now = self._clock()
        self._asked = {u: t for u, t in self._asked.items() if now - t < ROLE_ASK_EVERY}
        if user_id in self._asked:
            return None
        try:   # the journal holds the request: an answer is checked against it
            self._store.log(None, None, "role_requested", {"user_id": user_id, "name": who})
        except Exception:
            log.exception("cannot record the access request of %s", user_id)
            return None
        self._asked[user_id] = now
        return (ROLE_ASK.format(who=who or "без имени", id=user_id),
                role_request_markup(user_id))

    def on_role(self, user_id: int, data: str, who: str | None = None, *,
                name: str | None = None) -> RoleReply | None:
        """A press on a role button; None when the data is not a role button."""
        m = _ROLE_DATA.fullmatch(data or "")
        if not m:
            return None
        if not self.allowed(user_id):
            return RoleReply(NO_ACCESS)
        what, uid = m[1], int(m[2])
        by, admin = user_label(user_id, who), name or who or str(user_id)
        if what == "revoke":
            return self._revoke(uid, by)
        req = self._store.open_role_request(uid)
        if req is None or self.can_press(uid):
            return RoleReply(ROLE_DECIDED)
        person = req.get("name")
        payload = {"user_id": uid, "name": person, "by": by}
        hm = self._clock().astimezone(MSK).strftime("%H:%M")
        if what == "grant":
            self._store.add_manager(uid, person, admin)
            self._db_managers.add(uid)
            self._store.log(None, None, "role_granted", payload)
            return RoleReply(None, f"✅ {person or f'id {uid}'} теперь менеджер — назначил "
                                   f"{admin}, {hm}")
        self._store.log(None, None, "role_denied", payload)
        return RoleReply(None, f"Отказано — {admin}, {hm}")

    def _revoke(self, uid: int, by: str) -> RoleReply:
        if uid in self._env_managers:
            return RoleReply(ROLE_FROM_ENV)
        m = next((x for x in self._store.managers() if x.user_id == uid), None)
        removed = m is not None and self._store.remove_manager(uid)
        self._db_managers.discard(uid)
        if removed:
            self._store.log(None, None, "role_revoked",
                            {"user_id": uid, "name": m.name, "by": by})
        s = self._managers_screen()
        return RoleReply("Убран" if removed else "Уже убран", s.text, s.markup)

    def _managers_screen(self) -> Screen:
        stored = [m for m in self._store.managers() if m.user_id not in self._env_managers]
        if not stored and not self._env_managers:
            return Screen("Менеджеров нет. Когда сотрудник нажмёт кнопку под заказом, вам "
                          "придёт запрос «Сделать менеджером».")
        lines = ["Менеджеры (нажимают кнопки под заказами):"]
        rows = []
        for m in stored:
            label = m.name or f"id {m.user_id}"
            lines.append(f"• {m.name or 'без имени'}, id {m.user_id} — назначил "
                         f"{m.granted_by or '?'}, {m.ts.astimezone(MSK):%d.%m %H:%M}")
            rows.append([_btn(f"Убрать {label}"[:60], f"role:revoke:{m.user_id}")])
        for uid in sorted(self._env_managers):
            lines.append(f"• id {uid} — из .env (убрать можно только в .env)")
        return Screen("\n".join(lines),
                      InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    def fresh_request(self, key):
        try:
            return self._store.get_order(*key), self._store.get_decision(*key)
        except KeyError:
            return None


class PauseReminder:
    def __init__(self, store: Store, *, clock=None, every=REMINDER_EVERY):
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._every = every
        self._base: datetime | None = None
        self._since: datetime | None = None

    def check(self) -> str | None:
        since = self._store.paused_since()
        if since is None:
            self._base = self._since = None
            return None
        if since != self._since:
            self._since, self._base = since, since
        now = self._clock()
        if now - self._base >= self._every:
            self._base = now
            minutes = int((now - since).total_seconds() // 60)
            return (f"Робот на паузе уже {minutes} мин, заказы ждут. Когда закончите работу "
                    "в 1С, нажмите «Я закончил».")
        return None


# ------------------------------------------------------------------- wiring
def _who(user) -> str | None:
    if user is None:
        return None
    name = getattr(user, "full_name", "") or ""
    nick = getattr(user, "username", None)
    return " ".join(x for x in (name, f"@{nick}" if nick else "") if x) or None


def _name(user) -> str:
    """Short name for result lines: first name, else @username, else the id."""
    first = getattr(user, "first_name", None)
    if first:
        return first
    nick = getattr(user, "username", None)
    return f"@{nick}" if nick else str(getattr(user, "id", ""))


async def _apply(reply: ActionReply, key, notifier, msg) -> None:
    """Puts the result into every stored copy of the order message; a message the store does
    not know (sent before copies were stored) is changed directly."""
    if reply.finish_line is None and reply.markup is KEEP:
        return
    touched = 0
    if notifier is not None:
        try:
            if reply.finish_line is not None:
                touched = await notifier.finish(*key, reply.finish_line, reply.markup)
            else:
                touched = await notifier.set_markup(*key, reply.markup)
        except Exception:
            log.exception("cannot update the messages of order %s", key)
            return
    if touched or msg is None:
        return
    if reply.markup is not KEEP:
        try:
            await msg.edit_reply_markup(reply_markup=reply.markup)
        except Exception:
            log.exception("cannot change buttons")
    if reply.finish_line is not None:
        try:
            await msg.answer(reply.finish_line, parse_mode="HTML")
        except Exception:
            log.exception("cannot send the result line")


async def _role_click(controller: BotController, query) -> None:
    """Role buttons change only the pressed message (an access request or the list)."""
    user = query.from_user
    try:
        r = controller.on_role(user.id, query.data, _who(user), name=_name(user))
    except Exception:
        log.exception("role button failed")
        r = RoleReply("Не получилось, нажмите ещё раз")
    try:
        await query.answer(r.toast if r else None)
    except Exception:
        log.exception("callback answer failed")
    if r is not None and r.text is not None and query.message is not None:
        try:
            await query.message.edit_text(r.text, reply_markup=r.markup)
        except Exception:
            log.exception("cannot change the role message")


async def handle_click(controller: BotController, notifier, query) -> None:
    """A press is answered with its result as a pop-up when the result comes within
    ANSWER_WAIT. A slow one (a reserve answer can run a poll and a 1C read) is answered empty
    at once; its result then shows in the order message, and its text follows as a reply when
    the message alone would not tell what happened."""
    user, data = query.from_user, query.data or ""
    if data.startswith("role:"):
        await _role_click(controller, query)
        return
    task = asyncio.ensure_future(
        controller.on_action(user.id, data, _who(user), name=_name(user)))
    slow = False
    try:
        reply = await asyncio.wait_for(asyncio.shield(task), ANSWER_WAIT)
    except TimeoutError:
        slow = True
        try:
            await query.answer()
        except Exception:
            log.exception("callback answer failed")
        try:
            reply = await task
        except Exception:
            log.exception("button press failed")
            reply = ActionReply("Не получилось, нажмите ещё раз")
    except Exception:
        log.exception("button press failed")
        reply = ActionReply("Не получилось, нажмите ещё раз")
    if not slow:
        try:
            await query.answer(reply.toast if reply else None)
        except Exception:
            log.exception("callback answer failed")
    if reply is not None and reply.ask and notifier is not None:
        try:
            await notifier.to_admins(*reply.ask)
        except Exception:
            log.exception("cannot ask the admins about access")
    if reply is None:
        return
    key = tuple(data.split(":", 3)[2:4])
    msg = query.message
    if controller.order_state(key) is None:
        return    # an unknown order (or a forged press): its message is not touched
    if slow and reply.toast and msg is not None and (
            reply.markup is KEEP or controller.order_state(key) in REPLY_STATES):
        try:
            await msg.answer(reply.toast)
        except Exception:
            log.exception("cannot send the result text")
    await _apply(reply, key, notifier, msg)
    if reply.again and notifier is not None:
        found = controller.fresh_request(reply.again)
        if found and found[1] is not None:
            try:
                await notifier.reserve_request(found[0], found[1], [], False)
            except Exception:
                log.exception("cannot resend the reserve request")


def build_dispatcher(controller: BotController, notifier=None) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(F.text, F.chat.type == "private")   # the alert group is not read
    async def on_message(message: Message):
        user = message.from_user
        reply = await controller.on_text(user.id if user else None, message.text, _who(user))
        if isinstance(reply, Screen):
            await message.answer(reply.text, reply_markup=reply.markup or main_keyboard())
        elif reply:
            await message.answer(reply, reply_markup=main_keyboard())

    @dp.callback_query()
    async def on_click(query: CallbackQuery):
        await handle_click(controller, notifier, query)

    return dp
