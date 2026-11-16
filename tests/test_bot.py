import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.types import FSInputFile

import orderbot.bot as bot_mod
from orderbot.alerts import result_line
from orderbot.bot import (
    BotController,
    CommandError,
    PauseReminder,
    TelegramNotifier,
    cancel_confirm_markup,
    handle_click,
    order_markup,
    parse_command,
    reason_text,
)
from orderbot.domain import Action, Decision, ItemRead, Order, OrderLine, Reason, StockRow
from orderbot.store import Store

UTC = UTC
ALLOWED = 111


def at(h, m=0):
    return datetime(2026, 11, 2, h, m, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_order(oid="1234-1", article="A1"):
    return Order("ozon", oid, [OrderLine(article, 2, "СПБ", Decimal("2000"), "BrandX")],
                 at(9), at(20), "awaiting_packaging")


def make_decision(reason=Reason.PRICE_LOW, article="A1", action=Action.ESCALATE):
    return Decision(action, [(article, reason)], {article: Decimal("1500")})


def make_read():
    return ItemRead("A1", {"СПБ": StockRow("СПБ", 10, 1)}, Decimal("1000"), Decimal("800"), None)


class FakePipeline:
    def __init__(self):
        self.calls = []

    async def resolve_reserve(self, mp, order_id, approved, user):
        self.calls.append((mp, order_id, approved, user))
        return "готово"


@pytest.fixture
def env(tmp_path):
    clock = Clock(at(10))
    store = Store(tmp_path / "s.db", clock=clock)
    pipe = FakePipeline()
    ctl = BotController(store, pipe, {ALLOWED}, ["ozon"], clock=clock)
    return ctl, store, pipe, clock


# ------------------------------------------------------------------ parsing
def test_parse_discount_with_quoted_brand():
    c = parse_command('/discount ozon "BrandX" 20')
    assert (c.name, c.marketplace, c.brand, c.value) == ("discount", "ozon", "BrandX", Decimal(20))


def test_parse_discount_with_typographic_quotes_and_comma():
    c = parse_command("/discount ozon «BrandX» 12,5")
    assert c.brand == "BrandX" and c.value == Decimal("12.5")


@pytest.mark.parametrize("bad", ["0", "100", "abc", "-5", "101", "nan", "inf", ""])
def test_reject_percent_out_of_range(bad):
    with pytest.raises(CommandError):
        parse_command(f'/discount ozon "BrandX" {bad}')


@pytest.mark.parametrize("text", [
    "/discount ozon Costa 20 30", "/soinvest ozon maybe", "/mode ozon fast",
    "/tolerance ozon -1", "/tolerance ozon x", "/nope ozon", "/discount_off ozon",
    "/multiqty ozon", '/discount ozon "unclosed 20'])
def test_other_bad_commands_rejected(text):
    with pytest.raises(CommandError):
        parse_command(text)


def test_parse_simple_commands():
    assert parse_command("/soinvest ozon on").value is True
    assert parse_command("/tolerance ozon 50").value == Decimal(50)
    assert parse_command("/mode ozon confirm").value == "confirm"
    assert parse_command("/multiqty ozon on").value is True
    c = parse_command('/discount_off ozon "BrandX"')
    assert (c.name, c.brand) == ("discount_off", "BrandX")
    assert parse_command("/status@mybot").name == "status"


# -------------------------------------------------------------- allowlist
async def test_user_not_in_allowlist_ignored(env):
    ctl, store, pipe, _ = env
    for text in ("/soinvest ozon on", "Я работаю в 1С", "/status", '/discount ozon "X" 10'):
        assert await ctl.on_text(999, text) is None
    assert await ctl.on_callback(999, "rsv:y:ozon:1234-1") == "Нет доступа"
    assert pipe.calls == [] and not store.is_paused()
    assert store.settings_at("ozon", at(23)).soinvest is False


# ---------------------------------------------------------------- commands
async def test_settings_are_stored_with_user(env):
    ctl, store, _, clock = env
    await ctl.on_text(ALLOWED, "/soinvest ozon on")
    await ctl.on_text(ALLOWED, '/discount ozon "BrandX" 20')
    await ctl.on_text(ALLOWED, "/tolerance ozon 70")
    await ctl.on_text(ALLOWED, "/multiqty ozon on")
    clock.t = at(11)
    s = store.settings_at("ozon", at(12))
    assert s.soinvest and s.tolerance == Decimal(70) and s.multi_qty_verified
    assert s.brand_discounts == {"brandx": Decimal(20)}
    await ctl.on_text(ALLOWED, '/discount_off ozon "BrandX"')
    assert store.settings_at("ozon", at(13)).brand_discounts == {}
    assert any(e["event"] == "setting_changed" and e["payload"]["user"] == str(ALLOWED)
               for e in store.events("ozon"))


async def test_unknown_marketplace_rejected(env):
    ctl, store, _, _ = env
    reply = await ctl.on_text(ALLOWED, "/soinvest yandex on")
    assert "yandex" in reply
    assert store.settings_at("yandex", at(23)).soinvest is False


async def test_pause_buttons(env):
    ctl, store, _, _ = env
    await ctl.on_text(ALLOWED, "Я работаю в 1С")
    assert store.is_paused()
    await ctl.on_text(ALLOWED, "Я закончил")
    assert not store.is_paused()


async def test_check_now_button_wakes_the_pipeline(env):
    ctl, store, pipe, _ = env
    pipe.state = "started"
    pipe.check_now = lambda: (pipe.__dict__.setdefault("woken", []).append(1), pipe.state)[1]
    assert "сейчас" in await ctl.on_text(ALLOWED, bot_mod.BTN_CHECK)
    pipe.state = "running"
    assert "уже идёт" in await ctl.on_text(ALLOWED, bot_mod.BTN_CHECK)
    await ctl.on_text(ALLOWED, "Я работаю в 1С")
    assert "пауза" in (await ctl.on_text(ALLOWED, bot_mod.BTN_CHECK)).lower()
    assert len(pipe.woken) == 3
    assert await ctl.on_text(999, bot_mod.BTN_CHECK) is None and len(pipe.woken) == 3


def test_main_keyboard_has_check_button():
    texts = [b.text for row in bot_mod.main_keyboard().keyboard for b in row]
    assert bot_mod.BTN_CHECK in texts and bot_mod.BTN_DONE in texts


async def test_status_text(env):
    ctl, store, _, _ = env
    store.upsert_new(make_order())
    await ctl.on_text(ALLOWED, "Я работаю в 1С")
    text = await ctl.on_text(ALLOWED, "/status")
    for part in ("пауза", "ozon", "observe", "50", "очеред", "проверок нет"):
        assert part in text.lower()


async def test_mode_confirm_requires_reply_confirmation(env):
    ctl, store, _, _ = env
    reply = await ctl.on_text(ALLOWED, "/mode ozon confirm")
    assert store.get_mode("ozon") == "observe"
    assert "наблюден" in reply and "да" in reply.lower()
    await ctl.on_text(ALLOWED, "да")
    assert store.get_mode("ozon") == "confirm"


async def test_mode_confirm_not_given_by_other_message_or_other_user(env):
    ctl, store, _, _ = env
    await ctl.on_text(ALLOWED, "/mode ozon confirm")
    await ctl.on_text(ALLOWED, "/status")
    await ctl.on_text(ALLOWED, "да")
    assert store.get_mode("ozon") == "observe"
    await ctl.on_text(ALLOWED, "/mode ozon confirm")
    await ctl.on_text(999, "да")
    assert store.get_mode("ozon") == "observe"


async def test_mode_confirm_expires(env):
    ctl, store, _, clock = env
    await ctl.on_text(ALLOWED, "/mode ozon confirm")
    clock.t = at(10, 30)
    await ctl.on_text(ALLOWED, "да")
    assert store.get_mode("ozon") == "observe"


async def test_mode_observe_is_immediate(env):
    ctl, store, _, _ = env
    store.set_mode("ozon", "confirm", "x")
    await ctl.on_text(ALLOWED, "/mode ozon observe")
    assert store.get_mode("ozon") == "observe"


# ------------------------------------------------------------------ alerts
@pytest.mark.parametrize("reason", list(Reason))
def test_every_reason_has_text(reason):
    assert reason_text(reason).strip()


class Sent:
    def __init__(self, i):
        self.message_id = i


class FakeBot:
    def __init__(self, fail_for=(), fail_edit=False, fail_photo=False, fail_caption=False,
                 fail_album=False):
        self.fail_photo, self.fail_caption = fail_photo, fail_caption
        self.albums, self.fail_album, self.album_captions = [], fail_album, []
        self.messages, self.photos, self.fail_for = [], [], set(fail_for)
        self.edits, self.fail_edit, self._n = [], fail_edit, 0

    def _id(self):
        self._n += 1
        return Sent(self._n)

    async def send_media_group(self, chat_id, media, **kw):
        if chat_id in self.fail_for or self.fail_album:
            raise RuntimeError("blocked")
        self.albums.append((chat_id, [m.media.path for m in media], kw))
        self.album_captions.append([(m.caption, m.parse_mode) for m in media])
        return [self._id() for _ in media]

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        self.messages.append((chat_id, text, reply_markup, parse_mode))
        return self._id()

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None,
                         reply_markup=None):
        if chat_id in self.fail_for or self.fail_photo:
            raise RuntimeError("blocked")
        self.photos.append((chat_id, photo, caption, parse_mode, reply_markup))
        return self._id()

    async def _edit(self, kind, **kw):
        if self.fail_edit and kind != "markup":
            raise RuntimeError("cannot edit")
        self.edits.append((kind, kw))

    async def edit_message_caption(self, **kw):
        if self.fail_caption:
            raise RuntimeError("caption too long")
        await self._edit("caption", **kw)

    async def edit_message_text(self, **kw):
        await self._edit("text", **kw)

    async def edit_message_reply_markup(self, **kw):
        await self._edit("markup", **kw)


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


GROUP = -1001234


@pytest.fixture
def nenv(tmp_path):
    store = Store(tmp_path / "n.db", clock=Clock(at(10)))
    snap = tmp_path / "a.png"
    snap.write_bytes(b"x")
    return store, str(snap)


def seen(store, state="ESCALATED", oid="1234-1"):
    store.upsert_new(make_order(oid))
    store.set_state("ozon", oid, state)


async def test_order_alert_goes_to_group_once_as_photo_with_caption(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    n = TelegramNotifier(bot, [1, 2], store, GROUP)
    await n.order_alert(make_order(), make_decision(), [snap], False, False,
                        reads={"A1": make_read()})
    assert bot.messages == [] and len(bot.photos) == 1
    chat, photo, caption, mode, markup = bot.photos[0]
    assert chat == GROUP and mode == "HTML" and isinstance(photo, FSInputFile)
    assert "1234-1" in caption
    assert [b.callback_data for b in buttons(markup)] == ["act:col:ozon:1234-1",
                                                          "act:cx:ozon:1234-1"]
    saved = store.alert_messages("ozon", "1234-1")
    assert [(m.chat_id, m.kind, m.html) for m in saved] == [(GROUP, "photo", caption)]


async def test_long_caption_falls_back_to_text_then_photo(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store, GROUP)
    dec = Decision(Action.ESCALATE, [("A1", Reason.PRICE_LOW)] +
                   [("A1", Reason.NO_STOCK)] * 30, {})
    await n.order_alert(make_order(), dec, [snap], False, False)
    assert len(bot.messages) == 1 and len(bot.messages[0][1]) > 1024
    assert bot.messages[0][3] == "HTML" and bot.messages[0][2] is not None
    assert len(bot.photos) == 1 and bot.photos[0][2] is None and bot.photos[0][4] is None
    assert [m.kind for m in store.alert_messages("ozon", "1234-1")] == ["text"]


async def test_no_snapshot_sends_text_with_buttons(nenv):
    store, _ = nenv
    seen(store)
    bot = FakeBot()
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), [], False, False)
    assert bot.photos == [] and bot.messages[0][0] == GROUP and bot.messages[0][2]


async def test_no_group_sends_to_every_admin(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    await TelegramNotifier(bot, [1, 2], store).order_alert(
        make_order(), make_decision(), [snap], False, False)
    assert [p[0] for p in bot.photos] == [1, 2]
    assert sorted(m.chat_id for m in store.alert_messages("ozon", "1234-1")) == [1, 2]


async def test_reserve_request_has_reserve_buttons(nenv):
    store, snap = nenv
    seen(store, "AWAITING_RESERVE")
    bot = FakeBot()
    dec = make_decision(Reason.RESERVE, action=Action.ASK_RESERVE)
    await TelegramNotifier(bot, [1], store, GROUP).reserve_request(
        make_order(), dec, [snap], False, reads={"A1": make_read()})
    markup = bot.photos[0][4]
    assert [[b.callback_data for b in row] for row in markup.inline_keyboard] == [
        ["rsv:y:ozon:1234-1", "rsv:n:ozon:1234-1"], ["act:cx:ozon:1234-1"]]


def test_order_markup_and_cancel_confirm():
    assert order_markup("ozon", "1", "COLLECTED") is None
    assert order_markup("ozon", "1", "RESERVE_APPROVED").inline_keyboard[0][0].callback_data \
        == "act:cx:ozon:1"
    assert [b.callback_data for b in buttons(cancel_confirm_markup("ozon", "1"))] == [
        "act:cxy:ozon:1", "act:back:ozon:1"]
    long_id = "9" * 40
    for st in ("ESCALATED", "AWAITING_RESERVE"):
        assert all(len(b.callback_data.encode()) <= 64
                   for b in buttons(order_markup("wb", long_id, st)))


async def test_system_alert_stays_private(nenv):
    store, _ = nenv
    bot = FakeBot()
    n = TelegramNotifier(bot, [1, 2], store, GROUP)
    await n.system_alert("hi")
    await n.say("yo")
    assert [m[0] for m in bot.messages] == [1, 2, 1, 2]


async def test_finish_edits_every_copy_and_removes_buttons(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    n = TelegramNotifier(bot, [1, 2], store)
    await n.order_alert(make_order(), make_decision(), [snap], False, False)
    line = result_line("collected", "Иван", at(10))
    await n.finish("ozon", "1234-1", line, None)
    assert len(bot.edits) == 2
    for kind, kw in bot.edits:
        assert kind == "caption" and kw["caption"].endswith("\n\n" + line)
        assert kw["reply_markup"] is None and kw["parse_mode"] == "HTML"


async def test_finish_text_copy_and_survives_edit_errors(nenv):
    store, _ = nenv
    seen(store)
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store)
    await n.order_alert(make_order(), make_decision(), [], False, False)
    await n.finish("ozon", "1234-1", "L", None)
    assert bot.edits[0][0] == "text" and bot.edits[0][1]["text"].endswith("\n\nL")
    bot.fail_edit = True
    await n.finish("ozon", "1234-1", "L", None)    # must not raise
    await n.set_markup("ozon", "1234-1", None)


async def test_set_markup_changes_only_keyboard(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store)
    await n.order_alert(make_order(), make_decision(), [snap], False, False)
    m = cancel_confirm_markup("ozon", "1234-1")
    await n.set_markup("ozon", "1234-1", m)
    assert bot.edits == [("markup", {"chat_id": 1, "message_id": 1, "reply_markup": m})]


async def test_finish_after_restart_uses_store(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), [snap], False, False)
    await TelegramNotifier(bot, [1], store, GROUP).finish("ozon", "1234-1", "L", None)
    assert bot.edits[0][1]["chat_id"] == GROUP


async def test_one_blocked_chat_does_not_stop_others(nenv):
    store, _ = nenv
    bot = FakeBot(fail_for=[1])
    await TelegramNotifier(bot, [1, 2], store).system_alert("hi")
    assert [m[0] for m in bot.messages] == [2]


# ----------------------------------------------------------- reserve buttons
async def test_reserve_button_calls_pipeline_once(env):
    ctl, _, pipe, _ = env
    first = await ctl.on_callback(ALLOWED, "rsv:y:ozon:1234-1")
    assert first == "готово"
    second = await ctl.on_callback(ALLOWED, "rsv:y:ozon:1234-1")
    third = await ctl.on_callback(ALLOWED, "rsv:n:ozon:1234-1")
    assert pipe.calls == [("ozon", "1234-1", True, str(ALLOWED))]
    assert "уже" in second and "уже" in third


class SlowPipeline:
    """resolve_reserve takes a while, like a poll plus a 1C read."""

    def __init__(self, store, final_state=None, fail=False):
        self.calls, self.release = [], asyncio.Event()
        self.store, self.final_state, self.fail = store, final_state, fail

    async def resolve_reserve(self, mp, order_id, approved, user):
        self.calls.append((mp, order_id, approved, user))
        await self.release.wait()
        if self.fail:
            raise RuntimeError("poll failed")
        if self.final_state:
            self.store.upsert_new(make_order(order_id))
            self.store.set_state(mp, order_id, self.final_state)
        return "результат"


async def test_second_click_on_reserve_ignored(env):
    ctl, store, _, _ = env
    pipe = SlowPipeline(store, "CONFIRMED")
    ctl._pipeline = pipe
    first = asyncio.ensure_future(ctl.on_callback(ALLOWED, "rsv:y:ozon:7"))
    await asyncio.sleep(0.01)                       # the first one is still awaiting
    second = await ctl.on_callback(ALLOWED, "rsv:y:ozon:7")
    third = await ctl.on_callback(ALLOWED, "rsv:n:ozon:7")
    assert "принят" in second and "принят" in third
    pipe.release.set()
    assert await first == "результат"
    assert "принят" in await ctl.on_callback(ALLOWED, "rsv:y:ozon:7")
    assert len(pipe.calls) == 1


async def test_answer_kept_open_when_order_still_awaits_reserve(env):
    ctl, store, _, _ = env
    pipe = SlowPipeline(store, "AWAITING_RESERVE")   # observe mode put it back
    pipe.release.set()
    ctl._pipeline = pipe
    r = await ctl.on_action(ALLOWED, "rsv:y:ozon:7")
    assert r.again == ("ozon", "7") and r.finish_line is None
    assert "Резерв можно" in [b.text for b in buttons(r.markup)]
    await ctl.on_action(ALLOWED, "rsv:y:ozon:7")      # asks again: a new answer is taken
    assert len(pipe.calls) == 2


async def test_failed_resolution_can_be_retried_and_keeps_buttons(env):
    ctl, store, _, _ = env
    pipe = SlowPipeline(store, fail=True)
    pipe.release.set()
    ctl._pipeline = pipe
    r = await ctl.on_action(ALLOWED, "rsv:y:ozon:7")
    assert r.markup is KEEP and r.finish_line is None and "ещё раз" in r.toast
    await ctl.on_action(ALLOWED, "rsv:y:ozon:7")
    assert len(pipe.calls) == 2


class FakeMessage:
    def __init__(self, log):
        self.log = log

    async def answer(self, text, **kw):
        self.log.append(("message", text))

    async def edit_reply_markup(self, reply_markup=None):
        self.log.append(("buttons_removed",) if reply_markup is None
                        else ("buttons", [b.text for b in buttons(reply_markup)]))


class FakeQuery:
    def __init__(self, data, log, user_id=ALLOWED):
        self.data, self.log = data, log
        self.from_user = type("U", (), {"id": user_id, "full_name": "Анна К", "username": "ak",
                                        "first_name": "Анна"})()
        self.message = FakeMessage(log)

    async def answer(self, text=None, **kw):
        self.log.append(("answered",) if text is None else ("answered", text))


async def test_click_answers_query_before_the_slow_resolution(env, monkeypatch):
    monkeypatch.setattr(bot_mod, "ANSWER_WAIT", 0.005)
    ctl, store, _, _ = env
    pipe = SlowPipeline(store, "CONFIRMED")
    ctl._pipeline = pipe
    log = []
    task = asyncio.ensure_future(handle_click(ctl, None, FakeQuery("rsv:y:ozon:7", log)))
    await asyncio.sleep(0.05)
    assert log == [("answered",)]                    # answered while resolve_reserve waits
    pipe.release.set()
    await task
    # a message the store does not know: its buttons go, the result line follows it
    assert log == [("answered",), ("buttons_removed",),
                   ("message", result_line("collected_by_robot", "", at(10)))]
    assert pipe.calls[0][3] == f"{ALLOWED} Анна К @ak"


async def test_buttons_stay_after_failure_and_fresh_request_when_awaiting_again(env):
    ctl, store, _, _ = env
    pipe = SlowPipeline(store, fail=True)
    pipe.release.set()
    ctl._pipeline = pipe
    log = []
    await handle_click(ctl, None, FakeQuery("rsv:y:ozon:7", log))
    assert len(log) == 1 and "ещё раз" in log[0][1]   # a fast failure: the pop-up says so,
                                                      # buttons stay, nothing else changes

    sent = []

    class N:
        async def reserve_request(self, order, decision, snap, urgent, reads=None):
            sent.append(order.order_id)

        async def set_markup(self, mp, order_id, markup):
            return 0                                   # an old message, not in the store

    pipe2 = SlowPipeline(store, "AWAITING_RESERVE")
    pipe2.release.set()
    ctl._pipeline = pipe2
    store.upsert_new(make_order("8"))
    store.set_state("ozon", "8", "AWAITING_RESERVE", make_decision(Reason.RESERVE))
    log2 = []
    await handle_click(ctl, N(), FakeQuery("rsv:y:ozon:8", log2))
    assert ("buttons_removed",) not in log2 and sent == ["8"]
    assert ("buttons", ["Резерв можно", "Резерв нельзя", "❌ Отменить"]) in log2


async def test_journal_records_name_for_settings(env):
    ctl, store, _, _ = env
    await ctl.on_text(ALLOWED, "/soinvest ozon on", who="Анна К @ak")
    ev = [e for e in store.events("ozon") if e["event"] == "setting_changed"][0]
    assert ev["payload"]["user"] == f"{ALLOWED} Анна К @ak"


async def test_bad_callback_data_is_ignored(env):
    ctl, _, pipe, _ = env
    assert await ctl.on_callback(ALLOWED, "garbage") is None
    assert await ctl.on_callback(ALLOWED, "rsv:y:yandex:1") is None
    assert pipe.calls == []


# ------------------------------------------------------------ pause reminder
def test_pause_reminder_after_60_min(tmp_path):
    clock = Clock(at(10))
    store = Store(tmp_path / "s.db", clock=clock)
    rem = PauseReminder(store, clock=clock)
    assert rem.check() is None                       # not paused
    store.set_paused(True, "1")
    clock.t = at(10, 59)
    assert rem.check() is None
    clock.t = at(11, 0)
    assert "пауз" in rem.check()
    clock.t = at(11, 30)
    assert rem.check() is None                       # not again before another hour
    clock.t = at(12, 0)
    assert rem.check() is not None
    store.set_paused(False, "1")
    assert rem.check() is None
    store.set_paused(True, "1")
    clock.t = at(12, 30)
    assert rem.check() is None                       # a new pause counts from its own start


async def test_wb_confirm_mode_is_refused(tmp_path):
    """Ruling A: the robot never confirms WB orders by itself; people collect them with the
    button."""
    clock = Clock(at(10))
    store = Store(tmp_path / "s.db", clock=clock)
    ctl = BotController(store, FakePipeline(), {ALLOWED}, ["ozon", "wb"], clock=clock)
    reply = await ctl.on_text(ALLOWED, "/mode wb confirm")
    assert "только наблюдение" in reply
    await ctl.on_text(ALLOWED, "да")
    assert store.get_mode("wb") == "observe"


async def test_photo_failure_falls_back_to_text_with_buttons(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot(fail_photo=True)
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), [snap], False, False)
    assert len(bot.messages) == 1 and bot.messages[0][2] is not None
    assert [m.kind for m in store.alert_messages("ozon", "1234-1")] == ["text"]


async def test_caption_without_room_for_result_goes_as_text(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    dec = Decision(Action.ESCALATE, [("A1", Reason.PRICE_LOW)] +
                   [("A1", Reason.NO_STOCK)] * 17, {})
    from orderbot.alerts import format_caption
    n = len(format_caption(make_order(), dec, None, False, at(10)))
    assert 1024 - 200 < n <= 1024, n
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), dec, [snap], False, False)
    assert bot.photos[0][2] is None and len(bot.messages) == 1


async def test_finish_falls_back_to_removing_buttons(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot(fail_caption=True)
    n = TelegramNotifier(bot, [1], store)
    await n.order_alert(make_order(), make_decision(), [snap], False, False)
    await n.finish("ozon", "1234-1", "L", None)
    assert bot.edits == [("markup", {"chat_id": 1, "message_id": 1, "reply_markup": None})]


# ------------------------------------------------- collect / cancel buttons, roles
from orderbot.bot import CANCEL_CONFIRM_TTL, KEEP, build_dispatcher  # noqa: E402
from orderbot.human_actions import ActionResult  # noqa: E402

MANAGER = 222
STRANGER = 999


class FakeActions:
    def __init__(self, collect="collected", cancel="cancelled", error=None):
        self.calls, self.res = [], {"collect": collect, "cancel": cancel}
        self.error = error

    async def collect(self, mp, order_id, who):
        self.calls.append(("collect", mp, order_id, who))
        return ActionResult(self.res["collect"], self.error)

    async def cancel(self, mp, order_id, who):
        self.calls.append(("cancel", mp, order_id, who))
        return ActionResult(self.res["cancel"], self.error)


@pytest.fixture
def aenv(tmp_path):
    clock = Clock(at(10))
    store = Store(tmp_path / "a.db", clock=clock)
    pipe = FakePipeline()
    acts = FakeActions()
    ctl = BotController(store, pipe, {ALLOWED}, ["ozon"], manager_ids={MANAGER},
                        actions=acts, clock=clock)
    store.upsert_new(make_order("7"))
    store.set_state("ozon", "7", "ESCALATED")
    return ctl, store, pipe, clock, acts


def texts(markup):
    return [b.text for b in buttons(markup)]


async def test_manager_can_collect_but_not_command(aenv):
    ctl, store, _, clock, acts = aenv
    r = await ctl.on_action(MANAGER, "act:col:ozon:7", "Мария")
    assert acts.calls == [("collect", "ozon", "7", f"{MANAGER} Мария")]
    assert r.finish_line == result_line("collected", "Мария", clock())
    assert r.markup is None and r.toast is None
    assert await ctl.on_text(MANAGER, "/mode ozon confirm") is None
    assert await ctl.on_text(MANAGER, "Я работаю в 1С") is None
    assert not store.is_paused()
    assert ctl.can_press(MANAGER) and ctl.can_press(ALLOWED) and not ctl.allowed(MANAGER)


async def test_stranger_gets_no_access(aenv):
    ctl, _, pipe, _, acts = aenv
    for data in ("act:col:ozon:7", "act:cx:ozon:7", "act:cxy:ozon:7", "rsv:y:ozon:7"):
        r = await ctl.on_action(STRANGER, data, "Кто-то")
        assert r.toast == "Нет доступа" and r.finish_line is None and r.markup is KEEP
    assert acts.calls == [] and pipe.calls == []


async def test_cancel_needs_confirmation(aenv):
    ctl, _, _, clock, acts = aenv
    r = await ctl.on_action(MANAGER, "act:cx:ozon:7", "Мария")
    assert "Да, отменить" in texts(r.markup) and r.finish_line is None
    assert acts.calls == []
    clock.t += timedelta(seconds=119)
    r = await ctl.on_action(MANAGER, "act:cxy:ozon:7", "Мария")
    assert acts.calls == [("cancel", "ozon", "7", f"{MANAGER} Мария")]
    assert "Отменён</b> — Мария" in r.finish_line and r.markup is None


async def test_cancel_confirmation_expires_after_two_minutes(aenv):
    ctl, _, _, clock, acts = aenv
    assert CANCEL_CONFIRM_TTL == timedelta(minutes=2)
    await ctl.on_action(ALLOWED, "act:cx:ozon:7", "Анна")
    clock.t += timedelta(seconds=121)
    r = await ctl.on_action(ALLOWED, "act:cxy:ozon:7", "Анна")
    assert r.toast.startswith("Время вышло") and acts.calls == []
    assert texts(r.markup) == ["✅ Собрать", "❌ Отменить"]
    # only the person who pressed "cancel" confirms; the others leave the buttons as they are
    await ctl.on_action(ALLOWED, "act:cx:ozon:7", "Анна")
    r = await ctl.on_action(MANAGER, "act:cxy:ozon:7", "Мария")
    assert r.toast == "Подтвердить может тот, кто нажал «Отменить»" and r.markup is KEEP
    assert acts.calls == []
    r = await ctl.on_action(ALLOWED, "act:cxy:ozon:7", "Анна")   # still valid for Анна
    assert acts.calls == [("cancel", "ozon", "7", f"{ALLOWED} Анна")]


async def test_back_restores_buttons(aenv):
    ctl, _, _, _, acts = aenv
    await ctl.on_action(ALLOWED, "act:cx:ozon:7", "Анна")
    r = await ctl.on_action(ALLOWED, "act:back:ozon:7", "Анна")
    assert texts(r.markup) == ["✅ Собрать", "❌ Отменить"] and r.finish_line is None
    r = await ctl.on_action(ALLOWED, "act:cxy:ozon:7", "Анна")   # "back" dropped the confirmation
    assert r.toast.startswith("Время вышло") and acts.calls == []


async def test_collect_failure_keeps_buttons_and_shows_error(aenv):
    ctl, _, _, clock, acts = aenv
    acts.res["collect"], acts.error = "collect_failed", "wb: supply WB-GI-1 left empty"
    r = await ctl.on_action(ALLOWED, "act:col:ozon:7", "Анна")
    assert r.finish_line == result_line("collect_failed", "Анна", clock(),
                                        "wb: supply WB-GI-1 left empty")
    assert texts(r.markup) == ["✅ Собрать", "❌ Отменить"]


async def test_busy_and_already_done(aenv):
    ctl, _, _, clock, acts = aenv
    acts.res["collect"], acts.error = "busy", "робот уже собирает заказ"
    r = await ctl.on_action(ALLOWED, "act:col:ozon:7", "Анна")
    assert r.toast == "робот уже собирает заказ" and r.markup is KEEP and r.finish_line is None
    acts.res["collect"], acts.error = "already_done", None
    r = await ctl.on_action(ALLOWED, "act:col:ozon:7", "Анна")
    assert "уже не ждёт сборки" in r.finish_line and r.markup is None


async def test_bad_action_data_is_ignored(aenv):
    ctl, _, _, _, acts = aenv
    for data in ("act:zz:ozon:7", "act:col:yandex:7", "act:col", "garbage"):
        assert await ctl.on_action(ALLOWED, data, "Анна") is None
    assert acts.calls == []


class StatePipeline:
    def __init__(self, store, state, text="ответ"):
        self.store, self.state, self.text, self.calls = store, state, text, []

    async def resolve_reserve(self, mp, order_id, approved, user):
        self.calls.append((mp, order_id, approved, user))
        self.store.set_state(mp, order_id, self.state)
        return self.text


async def test_reserve_yes_in_observe_shows_collect_buttons(aenv):
    ctl, store, _, _, _ = aenv
    store.set_state("ozon", "7", "AWAITING_RESERVE")
    ctl._pipeline = StatePipeline(store, "OBSERVED", "Резерв разрешён.")
    r = await ctl.on_action(MANAGER, "rsv:y:ozon:7", "Мария")
    assert r.toast == "Резерв разрешён." and r.finish_line is None
    assert texts(r.markup) == ["✅ Собрать", "❌ Отменить"]


async def test_reserve_yes_in_confirm_collected_by_robot(aenv):
    ctl, store, _, clock, _ = aenv
    store.set_state("ozon", "7", "AWAITING_RESERVE")
    ctl._pipeline = StatePipeline(store, "CONFIRMED")
    r = await ctl.on_action(ALLOWED, "rsv:y:ozon:7", "Анна")
    assert r.finish_line == result_line("collected_by_robot", "", clock()) and r.markup is None


async def test_reserve_no_leaves_only_cancel(aenv):
    ctl, store, _, _, _ = aenv
    store.set_state("ozon", "7", "AWAITING_RESERVE")
    ctl._pipeline = StatePipeline(store, "REJECTED_BY_HUMAN")
    r = await ctl.on_action(ALLOWED, "rsv:n:ozon:7", "Анна")
    assert texts(r.markup) == ["❌ Отменить"] and r.finish_line is None


class Notif:
    def __init__(self, stored=1):
        self.calls, self.stored, self.asks = [], stored, []

    async def finish(self, mp, order_id, line, markup):
        self.calls.append(("finish", mp, order_id, line, markup))
        return self.stored

    async def set_markup(self, mp, order_id, markup):
        self.calls.append(("markup", mp, order_id, markup))
        return self.stored

    async def to_admins(self, text, markup):
        self.asks.append((text, markup))


class AQuery:
    def __init__(self, data, log, user_id=ALLOWED, first="Анна", username="ak"):
        self.data, self.log = data, log
        self.from_user = type("U", (), {"id": user_id, "first_name": first,
                                        "full_name": f"{first} К", "username": username})()
        self.message = FakeMessage(log)

    async def answer(self, text=None, **kw):
        self.log.append(("answered", text))


async def test_click_on_collect_answers_with_result_and_edits_every_copy(aenv):
    ctl, _, _, clock, acts = aenv
    log, n = [], Notif()
    await handle_click(ctl, n, AQuery("act:col:ozon:7", log))
    assert log == [("answered", None)]
    assert n.calls == [("finish", "ozon", "7", result_line("collected", "Анна", clock()), None)]
    assert acts.calls[0][3] == f"{ALLOWED} Анна К @ak"

    log, n = [], Notif()
    await handle_click(ctl, n, AQuery("act:cx:ozon:7", log, user_id=STRANGER))
    assert log == [("answered", "Нет доступа")] and n.calls == []

    log, n = [], Notif()
    await handle_click(ctl, n, AQuery("rsv:y:ozon:7", log, user_id=STRANGER))
    assert log == [("answered", "Нет доступа")] and n.calls == []

    log, n = [], Notif()
    await handle_click(ctl, n, AQuery("act:cx:ozon:7", log))
    assert n.calls[0][0] == "markup" and "Да, отменить" in texts(n.calls[0][3])


async def test_click_on_message_not_in_store_edits_that_message(aenv):
    ctl, _, _, _, _ = aenv
    log = []
    await handle_click(ctl, Notif(stored=0), AQuery("act:col:ozon:7", log))
    assert ("buttons_removed",) in log and any(e[0] == "message" for e in log)


def test_short_name_for_result_lines():
    from orderbot.bot import _name
    U = lambda **kw: type("U", (), {"id": 5, "first_name": None, "username": None, **kw})()  # noqa
    assert _name(U(first_name="Анна", username="ak")) == "Анна"
    assert _name(U(username="ak")) == "@ak"
    assert _name(U()) == "5"


async def test_group_text_messages_are_ignored(aenv):
    from aiogram.types import Chat, Message, User
    ctl = aenv[0]
    dp = build_dispatcher(ctl, None)
    handler = dp.message.handlers[0]

    def msg(chat_type, chat_id):
        return Message(message_id=1, date=at(10), text="/status",
                       chat=Chat(id=chat_id, type=chat_type),
                       from_user=User(id=ALLOWED, is_bot=False, first_name="Анна"))

    assert (await handler.check(msg("private", ALLOWED)))[0]
    assert not (await handler.check(msg("supergroup", GROUP)))[0]
    assert not (await handler.check(msg("group", GROUP)))[0]


# ------------------------------------------- answer within ANSWER_WAIT, else a reply later
class GatedPipeline:
    """resolve_reserve waits for `release`, then sets `state` or raises."""

    def __init__(self, store, state=None, text="ответ", fail=False):
        self.store, self.state, self.text, self.fail = store, state, text, fail
        self.release = asyncio.Event()

    async def resolve_reserve(self, mp, order_id, approved, user):
        await self.release.wait()
        if self.fail:
            raise RuntimeError("poll failed")
        self.store.set_state(mp, order_id, self.state)
        return self.text


async def test_reserve_failure_text_reaches_the_person_fast_and_slow(aenv, monkeypatch):
    ctl, store, _, _, _ = aenv
    store.set_state("ozon", "7", "AWAITING_RESERVE")
    pipe = GatedPipeline(store, fail=True)
    pipe.release.set()
    ctl._pipeline = pipe
    log, n = [], Notif()
    await handle_click(ctl, n, AQuery("rsv:y:ozon:7", log))      # fast: in the pop-up
    assert len(log) == 1 and log[0][0] == "answered" and "ещё раз" in log[0][1]
    assert n.calls == []

    monkeypatch.setattr(bot_mod, "ANSWER_WAIT", 0.01)
    pipe = GatedPipeline(store, fail=True)
    ctl._pipeline = pipe
    log, n = [], Notif()
    task = asyncio.ensure_future(handle_click(ctl, n, AQuery("rsv:y:ozon:7", log)))
    await asyncio.sleep(0.05)
    assert log == [("answered", None)]                           # answered empty in time
    pipe.release.set()
    await task
    assert len(log) == 2 and log[1][0] == "message" and "ещё раз" in log[1][1]


async def test_slow_reserve_in_pause_tells_why_nothing_happens(aenv, monkeypatch):
    monkeypatch.setattr(bot_mod, "ANSWER_WAIT", 0.01)
    ctl, store, _, _, _ = aenv
    store.set_state("ozon", "7", "AWAITING_RESERVE")
    text = "Заказ 7: разрешение записано, обработаю после снятия паузы."
    pipe = GatedPipeline(store, "RESERVE_APPROVED", text)
    ctl._pipeline = pipe
    log, n = [], Notif()
    task = asyncio.ensure_future(handle_click(ctl, n, AQuery("rsv:y:ozon:7", log)))
    await asyncio.sleep(0.05)
    pipe.release.set()
    await task
    assert ("message", text) in log
    assert n.calls and n.calls[-1][0] == "markup" and texts(n.calls[-1][3]) == ["❌ Отменить"]


async def test_slow_reserve_with_visible_result_sends_no_extra_reply(aenv, monkeypatch):
    monkeypatch.setattr(bot_mod, "ANSWER_WAIT", 0.01)
    ctl, store, _, _, _ = aenv
    store.set_state("ozon", "7", "AWAITING_RESERVE")
    pipe = GatedPipeline(store, "CONFIRMED", "Заказ 7: собран.")
    ctl._pipeline = pipe
    log, n = [], Notif()
    task = asyncio.ensure_future(handle_click(ctl, n, AQuery("rsv:y:ozon:7", log)))
    await asyncio.sleep(0.05)
    pipe.release.set()
    await task
    assert log == [("answered", None)] and n.calls[-1][0] == "finish"


async def test_slow_collect_answers_empty_then_finishes(aenv, monkeypatch):
    monkeypatch.setattr(bot_mod, "ANSWER_WAIT", 0.01)
    ctl, _, _, clock, _ = aenv
    gate = asyncio.Event()

    class SlowActions(FakeActions):
        async def collect(self, mp, order_id, who):
            await gate.wait()
            return await super().collect(mp, order_id, who)

    ctl._actions = SlowActions()
    log, n = [], Notif()
    task = asyncio.ensure_future(handle_click(ctl, n, AQuery("act:col:ozon:7", log)))
    await asyncio.sleep(0.05)
    assert log == [("answered", None)] and n.calls == []
    gate.set()
    await task
    assert log == [("answered", None)]
    assert n.calls == [("finish", "ozon", "7", result_line("collected", "Анна", clock()), None)]


# ------------------------------------------------- final review fixes
GROUP_DOWN = "Группа для заказов недоступна"


async def test_unreachable_group_sends_orders_to_admins_and_says_so_once(nenv):
    store, snap = nenv
    seen(store)
    seen(store, oid="1234-2")
    bot = FakeBot(fail_for={GROUP})
    n = TelegramNotifier(bot, [1, 2], store, GROUP)
    await n.order_alert(make_order(), make_decision(), [snap], False, False)
    assert [p[0] for p in bot.photos] == [1, 2]
    assert all(p[4] is not None for p in bot.photos)            # the same buttons
    assert sorted(m.chat_id for m in store.alert_messages("ozon", "1234-1")) == [1, 2]
    sys_msgs = [m for m in bot.messages if GROUP_DOWN in m[1]]
    assert [m[0] for m in sys_msgs] == [1, 2]
    assert "blocked" in sys_msgs[0][1] and "Заказы приходят в личку." in sys_msgs[0][1]
    await n.order_alert(make_order("1234-2"), make_decision(), [snap], False, False)
    assert len([m for m in bot.messages if GROUP_DOWN in m[1]]) == 2   # not again
    assert sorted(m.chat_id for m in store.alert_messages("ozon", "1234-2")) == [1, 2]
    # finish edits the private copies
    assert await n.finish("ozon", "1234-1", "готово", None) == 2


async def test_group_back_then_down_again_says_so_again(nenv):
    store, _ = nenv
    seen(store)
    bot = FakeBot(fail_for={GROUP})
    n = TelegramNotifier(bot, [1], store, GROUP)
    await n.order_alert(make_order(), make_decision(), [], False, False)
    bot.fail_for = set()
    await n.order_alert(make_order(), make_decision(), [], False, False)
    assert bot.messages[-1][0] == GROUP
    bot.fail_for = {GROUP}
    await n.order_alert(make_order(), make_decision(), [], False, False)
    assert len([m for m in bot.messages if GROUP_DOWN in m[1]]) == 2


async def test_migrated_group_gives_new_id_in_the_alert(nenv):
    from aiogram.exceptions import TelegramMigrateToChat
    from aiogram.methods import SendMessage
    store, _ = nenv
    seen(store)

    class MigratedBot(FakeBot):
        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            if chat_id == GROUP:
                raise TelegramMigrateToChat(SendMessage(chat_id=GROUP, text="x"),
                                            "group chat was upgraded", -1009876543210)
            return await super().send_message(chat_id, text, parse_mode, reply_markup)
    bot = MigratedBot()
    n = TelegramNotifier(bot, [1], store, GROUP)
    await n.order_alert(make_order(), make_decision(), [], False, False)
    sys_msgs = [m[1] for m in bot.messages if GROUP_DOWN in m[1]]
    assert len(sys_msgs) == 1 and "-1009876543210" in sys_msgs[0]
    assert [m.chat_id for m in store.alert_messages("ozon", "1234-1")] == [1]


async def test_order_done_finishes_every_copy_without_buttons(nenv):
    store, _ = nenv
    seen(store, "CONFIRMED")
    store.save_alert_message("ozon", "1234-1", GROUP, 5, "text", "заказ")
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store, GROUP)
    line = result_line("collected_by_robot", "", at(10))
    assert await n.order_done("ozon", "1234-1", line) == 1
    kind, kw = bot.edits[0]
    assert kind == "text" and kw["text"] == "заказ\n\n" + line and kw["reply_markup"] is None


async def test_refresh_buttons_puts_buttons_of_current_state(nenv):
    store, _ = nenv
    seen(store, "OBSERVED")
    store.save_alert_message("ozon", "1234-1", GROUP, 5, "text", "заказ")
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store, GROUP)
    assert await n.refresh_buttons("ozon", "1234-1") == 1
    kind, kw = bot.edits[0]
    assert kind == "markup"
    assert [b.text for b in buttons(kw["reply_markup"])] == ["✅ Собрать", "❌ Отменить"]
    assert await n.refresh_buttons("ozon", "404") == 0


async def test_forged_press_on_unknown_order_does_not_touch_the_message(aenv):
    ctl = aenv[0]
    log, n = [], Notif(stored=0)
    await handle_click(ctl, n, AQuery("act:col:ozon:404", log))
    assert n.calls == []
    assert not any(e[0] in ("buttons_removed", "buttons", "message") for e in log)


async def test_cancel_confirm_accepted_from_presser_even_if_another_also_pressed(aenv):
    ctl, _, _, clock, acts = aenv
    await ctl.on_action(ALLOWED, "act:cx:ozon:7", "Анна")
    await ctl.on_action(MANAGER, "act:cx:ozon:7", "Мария")
    r = await ctl.on_action(ALLOWED, "act:cxy:ozon:7", "Анна")
    assert acts.calls == [("cancel", "ozon", "7", f"{ALLOWED} Анна")]
    assert r.finish_line and "Отменён" in r.finish_line


# ------------------------------------------------- forum topic of the alert group
class ThreadBot(FakeBot):
    """Records message_thread_id of every send."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.threads = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None,
                           message_thread_id=None):
        r = await super().send_message(chat_id, text, parse_mode, reply_markup)
        self.threads.append(("message", chat_id, message_thread_id))
        return r

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None,
                         reply_markup=None, message_thread_id=None):
        r = await super().send_photo(chat_id, photo, caption, parse_mode, reply_markup)
        self.threads.append(("photo", chat_id, message_thread_id))
        return r


async def test_group_sends_go_to_the_topic(nenv, tmp_path):
    store, snap = nenv
    seen(store)
    snap2 = tmp_path / "b.png"
    snap2.write_bytes(b"y")
    bot = ThreadBot()
    n = TelegramNotifier(bot, [1], store, GROUP, alert_thread_id=55)
    await n.order_alert(make_order(), make_decision(), [snap], False, False)
    assert bot.threads == [("photo", GROUP, 55)]
    bot.threads.clear()
    await n.order_alert(make_order(), make_decision(), [], False, False)   # text message
    assert bot.threads == [("message", GROUP, 55)]
    bot.threads.clear()
    await n.reserve_request(make_order(), make_decision(), [], False)
    assert bot.threads == [("message", GROUP, 55)]


async def test_private_fallback_and_system_messages_have_no_topic(nenv):
    store, snap = nenv
    seen(store)
    bot = ThreadBot(fail_for={GROUP})
    n = TelegramNotifier(bot, [1], store, GROUP, alert_thread_id=55)
    await n.order_alert(make_order(), make_decision(), [snap], False, False)
    assert ("photo", 1, None) in bot.threads
    assert all(t[2] is None for t in bot.threads if t[1] == 1)
    await n.system_alert("x")
    assert bot.threads[-1] == ("message", 1, None)


async def test_without_topic_no_thread_is_passed(nenv):
    store, _ = nenv
    seen(store)
    bot = FakeBot()      # its send_message has no message_thread_id parameter
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), [], False, False)
    assert bot.messages[0][0] == GROUP


# ------------------------------------------------------------------- roles
from orderbot.bot import Screen, role_request_markup  # noqa: E402

ASK_TEXT = "Кнопку под заказом нажал Пётр С @ps (id 999) — доступа нет."


def datas(markup):
    return [b.callback_data for b in buttons(markup)]


def role_events(store, name):
    return [e["payload"] for e in store.events() if e["event"] == name]


async def test_stranger_press_asks_admins_once_per_ten_minutes(aenv):
    ctl, store, pipe, clock, acts = aenv
    r = await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps")
    assert r.toast == "Нет доступа" and r.finish_line is None and r.markup is KEEP
    text, markup = r.ask
    assert text == ASK_TEXT
    assert texts(markup) == ["✅ Сделать менеджером", "Отказать"]
    assert datas(markup) == ["role:grant:999", "role:deny:999"]
    assert role_events(store, "role_requested") == [{"user_id": 999, "name": "Пётр С @ps"}]
    for data in ("act:cx:ozon:7", "rsv:y:ozon:7"):      # spam presses: no new request
        clock.t += timedelta(minutes=4)
        r = await ctl.on_action(STRANGER, data, "Пётр С @ps")
        assert r.toast == "Нет доступа" and r.ask is None
    clock.t += timedelta(minutes=2, seconds=1)           # 10 min after the first request
    r = await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps")
    assert r.ask is not None
    assert acts.calls == [] and pipe.calls == [] and store.get_state("ozon", "7") == "ESCALATED"


async def test_stranger_without_username_still_named_by_id(aenv):
    ctl = aenv[0]
    r = await ctl.on_action(STRANGER, "act:col:ozon:7", None)
    assert "(id 999)" in r.ask[0]


async def test_admins_and_managers_never_trigger_a_request(aenv):
    ctl, store, *_ = aenv
    for uid in (ALLOWED, MANAGER):
        r = await ctl.on_action(uid, "act:cx:ozon:7", "x")
        assert r.ask is None
    assert role_events(store, "role_requested") == []


def test_role_callback_data_fits_64_bytes():
    big = 10 ** 18
    assert all(len(d.encode()) <= 64 for d in datas(role_request_markup(big)))


async def test_grant_makes_a_manager_at_once_and_is_decided_once(aenv):
    ctl, store, _, clock, acts = aenv
    await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps")
    assert not ctl.can_press(STRANGER)
    r = ctl.on_role(ALLOWED, "role:grant:999", "Анна К @ak", name="Анна")
    assert r.text == "✅ Пётр С @ps теперь менеджер — назначил Анна, 13:00"
    assert r.markup is None
    assert ctl.can_press(STRANGER) and not ctl.allowed(STRANGER)
    assert role_events(store, "role_granted") == [
        {"user_id": 999, "name": "Пётр С @ps", "by": f"{ALLOWED} Анна К @ak"}]
    assert [m.user_id for m in store.managers()] == [999]
    # the second admin, or a second press: nothing changes
    for data in ("role:grant:999", "role:deny:999"):
        r = ctl.on_role(ALLOWED, data, "Олег", name="Олег")
        assert r.toast == "Уже решено" and r.text is None
    assert len(role_events(store, "role_granted")) == 1 and role_events(store, "role_denied") == []
    # the new manager presses order buttons but gives no commands
    r = await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps", name="Пётр")
    assert acts.calls == [("collect", "ozon", "7", "999 Пётр С @ps")] and r.ask is None
    assert await ctl.on_text(STRANGER, "/status") is None
    assert await ctl.on_text(STRANGER, "/managers") is None


async def test_grant_survives_restart(aenv, tmp_path):
    ctl, store, pipe, clock, acts = aenv
    await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр")
    ctl.on_role(ALLOWED, "role:grant:999", "Анна", name="Анна")
    ctl2 = BotController(store, pipe, {ALLOWED}, ["ozon"], actions=acts, clock=clock)
    assert ctl2.can_press(STRANGER)


async def test_deny_leaves_no_access_and_is_decided_once(aenv):
    ctl, store, _, clock, _ = aenv
    await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps")
    clock.t += timedelta(minutes=5)
    r = ctl.on_role(ALLOWED, "role:deny:999", "Анна К @ak", name="Анна")
    assert r.text == "Отказано — Анна, 13:05" and r.markup is None
    assert not ctl.can_press(STRANGER) and store.managers() == []
    assert role_events(store, "role_denied") == [
        {"user_id": 999, "name": "Пётр С @ps", "by": f"{ALLOWED} Анна К @ak"}]
    r = ctl.on_role(ALLOWED, "role:grant:999", "Анна", name="Анна")
    assert r.toast == "Уже решено" and r.text is None and not ctl.can_press(STRANGER)


async def test_role_buttons_are_for_admins_only(aenv):
    ctl, store, *_ = aenv
    await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр")
    for uid in (MANAGER, STRANGER, 555):
        for data in ("role:grant:999", "role:deny:999", "role:revoke:999"):
            r = ctl.on_role(uid, data, "x", name="x")
            assert r.toast == "Нет доступа" and r.text is None
    assert not ctl.can_press(STRANGER)
    assert [e["event"] for e in store.events()] == ["role_requested"]


async def test_forged_role_data_is_ignored(aenv):
    ctl, store, *_ = aenv
    for data in ("role:grant:abc", "role:zz:999", "role:grant", "role:grant:9:9", "role:",
                 "role:grant:", "role:grant:-5x"):
        assert ctl.on_role(ALLOWED, data, "Анна", name="Анна") is None
    # a grant without a request is not a way in
    r = ctl.on_role(ALLOWED, "role:grant:4242", "Анна", name="Анна")
    assert r.toast == "Уже решено" and not ctl.can_press(4242)


async def test_managers_list_with_remove_buttons_and_env_managers(aenv):
    ctl, store, *_ = aenv
    assert parse_command("/managers").name == "managers"
    await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps")
    ctl.on_role(ALLOWED, "role:grant:999", "Анна К @ak", name="Анна")
    s = await ctl.on_text(ALLOWED, "/managers", "Анна К @ak")
    assert isinstance(s, Screen)
    assert "Пётр С @ps" in s.text and "999" in s.text and "Анна" in s.text
    assert "02.11 13:00" in s.text
    assert "222" in s.text and "из .env" in s.text
    assert datas(s.markup) == ["role:revoke:999"]
    assert texts(s.markup)[0].startswith("Убрать")
    assert await ctl.on_text(MANAGER, "/managers") is None


async def test_managers_list_when_empty(tmp_path):
    store = Store(tmp_path / "e.db")
    ctl = BotController(store, FakePipeline(), {ALLOWED}, ["ozon"])
    s = await ctl.on_text(ALLOWED, "/managers")
    assert "нет" in s.text.lower() and s.markup is None


async def test_revoke_takes_effect_at_once_and_refreshes_the_list(aenv):
    ctl, store, *_ = aenv
    await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps")
    ctl.on_role(ALLOWED, "role:grant:999", "Анна", name="Анна")
    r = ctl.on_role(ALLOWED, "role:revoke:999", "Анна К @ak", name="Анна")
    assert not ctl.can_press(STRANGER) and store.managers() == []
    assert "999" not in r.text and "из .env" in r.text and r.markup is None
    assert role_events(store, "role_revoked") == [
        {"user_id": 999, "name": "Пётр С @ps", "by": f"{ALLOWED} Анна К @ak"}]
    r2 = ctl.on_role(ALLOWED, "role:revoke:999", "Анна", name="Анна")   # again: no new event
    assert r2.toast and len(role_events(store, "role_revoked")) == 1
    r3 = ctl.on_role(ALLOWED, "role:revoke:222", "Анна", name="Анна")   # .env manager stays
    assert ctl.can_press(MANAGER) and ".env" in r3.toast
    assert len(role_events(store, "role_revoked")) == 1
    r = await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр С @ps")
    assert r.toast == "Нет доступа"


async def test_notifier_sends_role_request_privately_to_every_admin(nenv):
    store, _ = nenv
    bot = FakeBot()
    n = TelegramNotifier(bot, [1, 2], store, GROUP)
    await n.to_admins(ASK_TEXT, role_request_markup(999))
    assert [m[0] for m in bot.messages] == [1, 2]
    assert all(m[1] == ASK_TEXT and datas(m[2]) == ["role:grant:999", "role:deny:999"]
               for m in bot.messages)


class RoleMessage(FakeMessage):
    async def edit_text(self, text, reply_markup=None, **kw):
        self.log.append(("edited", text, reply_markup))


async def test_click_flow_stranger_request_then_admin_grant(aenv):
    ctl, store, *_ = aenv
    log, n = [], Notif()
    q = AQuery("act:col:ozon:7", log, user_id=STRANGER, first="Пётр", username="ps")
    await handle_click(ctl, n, q)
    assert log == [("answered", "Нет доступа")] and n.calls == []
    assert n.asks == [("Кнопку под заказом нажал Пётр К @ps (id 999) — доступа нет.",
                       n.asks[0][1])]
    await handle_click(ctl, n, AQuery("act:col:ozon:7", [], user_id=STRANGER))
    assert len(n.asks) == 1                                     # rate-limited

    log = []
    q = AQuery("role:grant:999", log)
    q.message = RoleMessage(log)
    await handle_click(ctl, n, q)
    assert log == [("answered", None),
                   ("edited", "✅ Пётр К @ps теперь менеджер — назначил Анна, 13:00", None)]
    assert ctl.can_press(STRANGER) and n.calls == []

    log = []
    q = AQuery("role:deny:999", log)
    q.message = RoleMessage(log)
    await handle_click(ctl, n, q)
    assert log == [("answered", "Уже решено")]

    log = []
    q = AQuery("role:grant:abc", log, user_id=STRANGER)
    q.message = RoleMessage(log)
    await handle_click(ctl, n, q)
    assert log == [("answered", None)]


async def test_role_press_by_non_admin_sends_nothing_to_admins(aenv):
    ctl, *_ = aenv
    log, n = [], Notif()
    q = AQuery("role:grant:999", log, user_id=STRANGER)
    q.message = RoleMessage(log)
    await handle_click(ctl, n, q)
    assert log == [("answered", "Нет доступа")] and n.asks == [] and n.calls == []


async def test_managers_command_answered_with_its_buttons(aenv):
    from aiogram.types import User
    ctl, store, *_ = aenv
    await ctl.on_action(STRANGER, "act:col:ozon:7", "Пётр")
    ctl.on_role(ALLOWED, "role:grant:999", "Анна", name="Анна")
    dp = build_dispatcher(ctl, None)
    handler = dp.message.handlers[0]
    sent = []

    class M:
        text = "/managers"
        from_user = User(id=ALLOWED, is_bot=False, first_name="Анна")

        async def answer(self, text, reply_markup=None, **kw):
            sent.append((text, reply_markup))

    await handler.callback(M())
    assert len(sent) == 1 and datas(sent[0][1]) == ["role:revoke:999"]



# --- product photos in the order message ---------------------------
def png(path, size, color):
    from PIL import Image
    Image.new("RGB", size, color).save(path)
    return str(path)


def photo_read(article, snap, photo_1c=None, photo_mp=None):
    return ItemRead(article, {"СПБ": StockRow("СПБ", 10, 0)}, Decimal("1000"), Decimal("800"),
                    snap, photo_1c=photo_1c, photo_mp=photo_mp)




async def album_env(nenv, tmp_path):
    store, _ = nenv
    seen(store)
    s1 = png(tmp_path / "s1.png", (470, 1030), "red")
    s2 = png(tmp_path / "s2.png", (470, 1030), "blue")
    pic1 = png(tmp_path / "s1-1c.png", (200, 100), "blue")
    mp1 = png(tmp_path / "mp1.jpg", (516, 688), "green")
    mp2 = png(tmp_path / "mp2.jpg", (516, 688), "green")
    reads = {"A1": photo_read("A1", s1, pic1, mp1), "A2": photo_read("A2", s2, photo_mp=mp2)}
    return store, [s1, s2], reads, [s1, pic1, mp1, s2, mp2]


async def test_several_photos_go_as_an_album_with_the_text_and_buttons_below(nenv, tmp_path):
    """the order text is the album's caption; Telegram puts no
    buttons under an album, so a short «Заказ N» message with the buttons follows it."""
    store, snaps, reads, expected = await album_env(nenv, tmp_path)
    bot = FakeBot()
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), snaps, False, False, reads=reads)
    assert len(bot.albums) == 1 and bot.photos == []
    chat, paths, _ = bot.albums[0]
    assert chat == GROUP and paths == expected        # per article: tables, 1C picture, photo
    caps = bot.album_captions[0]
    assert "1234-1" in caps[0][0] and caps[0][1] == "HTML"
    assert all(c is None for c, _ in caps[1:])
    assert len(bot.messages) == 1
    _, text, markup, mode = bot.messages[0]
    assert text == "Заказ <b>1234-1</b>" and mode == "HTML" and markup is not None
    saved = store.alert_messages("ozon", "1234-1")
    assert [(m.kind, m.html) for m in saved] == [("text", "Заказ <b>1234-1</b>")]


async def test_too_long_text_stays_with_the_buttons_under_a_bare_album(nenv, tmp_path):
    store, snaps, reads, _ = await album_env(nenv, tmp_path)
    bot = FakeBot()
    dec = Decision(Action.ESCALATE, [("A1", Reason.PRICE_LOW)] + [("A1", Reason.NO_STOCK)] * 40, {})
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), dec, snaps, False, False, reads=reads)
    assert all(c is None for c, _ in bot.album_captions[0])
    assert len(bot.messages[0][1]) > 1024 and bot.messages[0][2] is not None


async def test_album_goes_to_the_topic(nenv, tmp_path):
    store, snaps, reads, _ = await album_env(nenv, tmp_path)
    bot = FakeBot()
    await TelegramNotifier(bot, [1], store, GROUP, alert_thread_id=2).order_alert(
        make_order(), make_decision(), snaps, False, False, reads=reads)
    assert bot.albums[0][2] == {"message_thread_id": 2}


async def test_album_failure_still_sends_the_text_with_buttons(nenv, tmp_path):
    store, snaps, reads, _ = await album_env(nenv, tmp_path)
    bot = FakeBot(fail_album=True)
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), snaps, False, False, reads=reads)
    assert len(bot.messages) == 1 and bot.messages[0][2] is not None


async def test_many_tables_without_pictures_go_three_per_album(nenv, tmp_path):
    """At most MAX_PHOTOS (10) articles' tables; three articles per album, a
    last part of one photo goes as a photo."""
    store, _ = nenv
    seen(store)
    snaps = [png(tmp_path / f"s{i}.png", (47, 103), "red") for i in range(12)]
    bot = FakeBot()
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), snaps, False, False)
    assert [len(a[1]) for a in bot.albums] == [3, 3, 3]
    assert [str(p[1].path) for p in bot.photos] == [snaps[9]]



def _rows_env(tmp_path, n):
    reads, snaps = {}, []
    for i in range(n):
        s = png(tmp_path / f"s{i}.png", (47, 103), "red")
        reads[f"A{i}"] = photo_read(f"A{i}", s, png(tmp_path / f"c{i}.png", (20, 10), "blue"),
                                    png(tmp_path / f"m{i}.jpg", (20, 26), "green"))
        snaps.append(s)
    return snaps, reads


async def test_more_than_three_articles_go_as_albums_of_three_before_the_order(nenv, tmp_path):
    """4 articles -> an album of 3 articles (9 photos), one of 1 article
    (3 photos), then the order text itself with its buttons."""
    store, _ = nenv
    seen(store)
    snaps, reads = _rows_env(tmp_path, 4)
    bot = FakeBot()
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), snaps, False, False, reads=reads)
    assert [len(a[1]) for a in bot.albums] == [9, 3]
    assert bot.albums[0][1][:3] == [snaps[0], str(tmp_path / "c0.png"), str(tmp_path / "m0.jpg")]
    caps = [c[0][0] for c in bot.album_captions]
    assert caps == ["📷 Заказ <b>1234-1</b> · артикулы 1–3 из 4",
                    "📷 Заказ <b>1234-1</b> · артикулы 4–4 из 4"]
    assert len(bot.messages) == 1 and bot.photos == []
    _, text, markup, _ = bot.messages[0]
    assert "1234-1" in text and text != "Заказ <b>1234-1</b>" and markup is not None
    assert [m.kind for m in store.alert_messages("ozon", "1234-1")] == ["text"]


async def test_three_articles_keep_one_album_with_the_text(nenv, tmp_path):
    store, _ = nenv
    seen(store)
    snaps, reads = _rows_env(tmp_path, 3)
    bot = FakeBot()
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), snaps, False, False, reads=reads)
    assert [len(a[1]) for a in bot.albums] == [9]
    assert bot.messages[0][1] == "Заказ <b>1234-1</b>"


async def test_a_failed_album_part_still_sends_the_order(nenv, tmp_path):
    store, _ = nenv
    seen(store)
    snaps, reads = _rows_env(tmp_path, 5)
    bot = FakeBot(fail_album=True)
    await TelegramNotifier(bot, [1], store, GROUP).order_alert(
        make_order(), make_decision(), snaps, False, False, reads=reads)
    assert len(bot.messages) == 1 and bot.messages[0][2] is not None

# ------------------------------------------------- stock sync
async def test_add_note_after_result_keeps_both_and_current_buttons(nenv):
    store, _ = nenv
    seen(store)
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store)
    await n.order_alert(make_order(), make_decision(), [], False, False)
    await n.finish("ozon", "1234-1", "RESULT", None)
    store.set_state("ozon", "1234-1", "CONFIRMED")
    assert await n.add_note("ozon", "1234-1", "📉 NOTE") == 1
    kind, kw = bot.edits[-1]
    assert kind == "text" and kw["text"].endswith("\n\nRESULT\n📉 NOTE")
    assert kw["reply_markup"] is None and kw["parse_mode"] == "HTML"
    # a later result line does not drop the note
    await n.finish("ozon", "1234-1", "AGAIN", None)
    assert bot.edits[-1][1]["text"].endswith("\n\nAGAIN\n📉 NOTE")


async def test_add_note_before_result_keeps_buttons_of_state(nenv):
    store, _ = nenv
    seen(store)    # ESCALATED: buttons stay
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store)
    await n.order_alert(make_order(), make_decision(), [], False, False)
    await n.add_note("ozon", "1234-1", "NOTE")
    kw = bot.edits[-1][1]
    assert kw["text"].endswith("\n\nNOTE")
    assert [b.text for b in buttons(kw["reply_markup"])] == ["✅ Собрать", "❌ Отменить"]


async def test_add_note_too_long_for_caption_goes_as_message(nenv):
    store, snap = nenv
    seen(store)
    bot = FakeBot()
    n = TelegramNotifier(bot, [1], store)
    await n.order_alert(make_order(), make_decision(), [snap], False, False)
    await n.add_note("ozon", "1234-1", "x" * 1100)
    assert bot.messages[-1][1] == "x" * 1100


async def test_stocksync_command_admin_only_and_in_status(env):
    ctl, store, _, _ = env
    assert await ctl.on_text(999, "/stocksync on") is None
    assert store.get_stocksync() is False
    assert "выключено" in await ctl.on_text(ALLOWED, "/status")
    reply = await ctl.on_text(ALLOWED, "/stocksync on", who="Анна")
    assert "включено" in reply and store.get_stocksync() is True
    assert store.get_stocksync("yandex") is False
    assert ("Остатки на других площадках: включено (Озон, WB); Маркет: выключено"
            in await ctl.on_text(ALLOWED, "/status"))
    await ctl.on_text(ALLOWED, "/stocksync off")
    assert store.get_stocksync() is False
    assert (await ctl.on_text(ALLOWED, "/stocksync maybe")).startswith("Не понял")
    ev = [e["payload"] for e in store.events() if e["event"] == "stocksync_changed"]
    assert ev[0] == {"on": True, "user": "111 Анна"}


async def test_stocksync_yandex_switch(env):
    ctl, store, _, _ = env
    assert await ctl.on_text(999, "/stocksync yandex on") is None
    reply = await ctl.on_text(ALLOWED, "/stocksync yandex on")
    assert "Маркет" in reply and store.get_stocksync("yandex") is True
    assert store.get_stocksync() is False
    assert "Маркет: включено" in await ctl.on_text(ALLOWED, "/status")
    await ctl.on_text(ALLOWED, "/stocksync yandex off")
    assert store.get_stocksync("yandex") is False
    assert (await ctl.on_text(ALLOWED, "/stocksync ozon on")).startswith("Не понял")


async def test_add_note_without_stored_message_goes_to_group(nenv):
    store, _ = nenv
    seen(store, "CONFIRMED")
    bot = FakeBot()
    n = TelegramNotifier(bot, [1, 2], store, GROUP)
    assert await n.add_note("ozon", "1234-1", "📉 Остатки: X 2→1") == 0
    assert [(m[0], m[1], m[3]) for m in bot.messages] == [
        (GROUP, "Заказ 1234-1 · Озон · Альфа\n📉 Остатки: X 2→1", "HTML")]


async def test_add_note_without_stored_message_falls_back_to_admins(nenv):
    store, _ = nenv
    seen(store, "CONFIRMED")
    bot = FakeBot(fail_for=[GROUP])
    n = TelegramNotifier(bot, [1, 2], store, GROUP)
    await n.add_note("ozon", "1234-1", "L")
    order_msgs = [m for m in bot.messages if m[1].startswith("Заказ 1234-1")]
    assert [m[0] for m in order_msgs] == [1, 2]


async def test_add_note_without_stored_message_uses_the_topic(nenv):
    store, _ = nenv
    seen(store, "CONFIRMED")
    bot = ThreadBot()
    n = TelegramNotifier(bot, [1], store, GROUP, alert_thread_id=55)
    await n.add_note("ozon", "1234-1", "L")
    assert bot.threads == [("message", GROUP, 55)]


# ------------------------------------------------ health topic
class HealthBot(FakeBot):
    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None,
                           message_thread_id=None):
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        self.messages.append((chat_id, text, message_thread_id, parse_mode))
        return self._id()


async def test_health_alert_goes_to_its_topic_as_html(nenv):
    store, _ = nenv
    bot = HealthBot()
    await TelegramNotifier(bot, [1, 2], store, GROUP, health_thread_id=573).health_alert(
        "🔴 <b>x</b>")
    assert bot.messages == [(GROUP, "🔴 <b>x</b>", 573, "HTML")]


async def test_health_alert_falls_back_to_admins(nenv):
    store, _ = nenv
    bot = HealthBot(fail_for={GROUP})
    await TelegramNotifier(bot, [1, 2], store, GROUP, health_thread_id=573).health_alert("t")
    assert [(m[0], m[3]) for m in bot.messages] == [(1, "HTML"), (2, "HTML")]


async def test_health_alert_without_topic_goes_to_admins(nenv):
    store, _ = nenv
    bot = HealthBot()
    await TelegramNotifier(bot, [1], store, GROUP).health_alert("t")
    assert [m[0] for m in bot.messages] == [1]
