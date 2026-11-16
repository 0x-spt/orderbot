import asyncio
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from orderbot.config import Config
from orderbot.domain import ItemRead, Order, OrderLine, StockRow
from orderbot.main import (
    AlreadyRunning,
    acquire_lock,
    build_marketplaces,
    env_permission_warning,
    load_env_file,
    reminder_loop,
    run,
    supervise,
    tick_loop,
)
from orderbot.pipeline import Pipeline
from orderbot.store import Store


# --------------------------------------------------------------------- lock
def test_second_instance_is_refused(tmp_path):
    first = acquire_lock(tmp_path / "robot.lock")
    with pytest.raises(AlreadyRunning):
        acquire_lock(tmp_path / "robot.lock")
    first.close()
    acquire_lock(tmp_path / "robot.lock").close()   # free again after the first one ends


def test_lock_creates_data_dir(tmp_path):
    acquire_lock(tmp_path / "data" / "robot.lock").close()


# ------------------------------------------------------------------- config
def test_env_file_does_not_override_environment(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# c\nA=1\nB = two words \nC=\nBAD LINE\n")
    env = {"A": "keep"}
    load_env_file(f, env)
    assert env == {"A": "keep", "B": "two words", "C": ""}
    load_env_file(tmp_path / "missing", env)


def test_env_file_strips_matching_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text('A="abc"\nB=\'x y\'\nC="open\nD=""\n')
    env = {}
    load_env_file(f, env)
    assert env == {"A": "abc", "B": "x y", "C": '"open', "D": ""}


def test_env_permissions_warning(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\n")
    f.chmod(0o644)
    assert "chmod 600" in env_permission_warning(f)
    f.chmod(0o600)
    assert env_permission_warning(f) is None
    assert env_permission_warning(tmp_path / "none") is None


def test_ozon_only_with_its_keys():
    assert build_marketplaces(Config()) == []
    assert build_marketplaces(Config(ozon_client_id="1")) == []
    mps = build_marketplaces(Config(ozon_client_id="1", ozon_api_key="k"))
    assert [m.name for m in mps] == ["ozon"]


def test_yandex_needs_token_and_campaign():
    mps = build_marketplaces(Config(ozon_client_id="1", ozon_api_key="k", ym_token="t"))
    assert "yandex" not in [m.name for m in mps]
    mps = build_marketplaces(Config(ym_token="t", ym_campaign_id="9"))
    assert [m.name for m in mps] == ["yandex"]


def test_yandex_campaigns_from_warehouses_section():
    cfg = Config(ym_token="t", warehouses={"yandex": {"1": "A", "2": "B"}})
    (m,) = build_marketplaces(cfg)
    assert m.campaigns == ["1", "2"]
    cfg.ym_campaign_id = "2"
    (m,) = build_marketplaces(cfg)
    assert m.campaigns == ["2"]
    assert build_marketplaces(Config(warehouses={"yandex": {"1": "A"}})) == []


# --------------------------------------------------------------------- loops
class Sink:
    def __init__(self):
        self.texts = []

    async def __call__(self, text):
        self.texts.append(text)


async def test_tick_loop_survives_errors_and_alerts_once():
    class P:
        n = 0

        async def tick(self):
            self.n += 1
            raise RuntimeError("boom")

    stop, sink, p = asyncio.Event(), Sink(), P()
    task = asyncio.ensure_future(tick_loop(p, sink, stop, interval=0.01))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert p.n >= 3 and len(sink.texts) == 1
    assert "RuntimeError" in sink.texts[0] and "boom" not in sink.texts[0]


async def test_tick_loop_stops_promptly_during_wait():
    class P:
        async def tick(self):
            pass

    stop = asyncio.Event()
    task = asyncio.ensure_future(tick_loop(P(), Sink(), stop, interval=3600))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_tick_loop_wakes_on_check_now():
    class P:
        def __init__(self):
            self.wake, self.n = asyncio.Event(), 0

        async def tick(self):
            self.n += 1

    stop, p = asyncio.Event(), P()
    task = asyncio.ensure_future(tick_loop(p, Sink(), stop, interval=3600))
    await asyncio.sleep(0.05)
    assert p.n == 1
    p.wake.set()
    await asyncio.sleep(0.05)
    assert p.n == 2
    stop.set()                       # the stop still ends the wait at once
    await asyncio.wait_for(task, 1)
    assert p.n == 2


async def test_pipeline_check_now_reports_a_running_check():
    from orderbot.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p.wake, p._lock = asyncio.Event(), asyncio.Lock()
    assert p.check_now() == "started" and p.wake.is_set()
    async with p._lock:
        assert p.check_now() == "running"


async def test_reminder_loop_sends_text():
    class R:
        def __init__(self):
            self.answers = ["remind", None]

        def check(self):
            return self.answers.pop(0) if self.answers else None

    stop, sink = asyncio.Event(), Sink()
    task = asyncio.ensure_future(reminder_loop(R(), sink, stop, period=0.01))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert sink.texts == ["remind"]


# ----------------------------------------------------------------- supervise
async def run_supervise(workers, say, startup=None, **kw):
    stop = asyncio.Event()
    stopped = []

    async def stopper():
        stopped.append(1)

    code = await asyncio.wait_for(
        supervise(say, workers(stop), stop, [stopper], startup=startup, grace=1, **kw), 3)
    return code, stop, stopped


async def test_start_and_stop_messages():
    sink = Sink()

    def workers(stop):
        async def w():
            await stop.wait()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        return {"w": w}

    code, stop, stopped = await run_supervise(workers, sink)
    assert code == 0 and stopped
    assert sink.texts[0] == "Робот включён"
    assert sink.texts[-1].startswith("Робот выключен") and len(sink.texts) == 2


async def test_worker_crash_reports_and_stops_others():
    sink = Sink()
    other_done = []

    def workers(stop):
        async def bad():
            await asyncio.sleep(0.02)
            raise RuntimeError("kaput")

        async def good():
            await stop.wait()
            other_done.append(1)
        return {"bad": bad, "good": good}

    code, stop, _ = await run_supervise(workers, sink)
    assert code == 1 and other_done
    assert "Робот выключен" in sink.texts[-1] and "RuntimeError" in sink.texts[-1]
    assert "kaput" not in sink.texts[-1]


async def test_startup_crash_still_says_stopped():
    sink = Sink()

    async def startup():
        raise RuntimeError("recover failed")

    code, _, _ = await run_supervise(lambda stop: {}, sink, startup=startup)
    assert code == 1 and "RuntimeError" in sink.texts[-1]
    assert "recover failed" not in sink.texts[-1]


async def test_hanging_telegram_does_not_block_stop():
    async def hang(text):
        await asyncio.sleep(3600)

    def workers(stop):
        asyncio.get_running_loop().call_later(0.02, stop.set)

        async def w():
            await stop.wait()
        return {"w": w}

    code, _, _ = await run_supervise(workers, hang, notify_timeout=0.05)
    assert code == 0


async def test_failing_telegram_does_not_break_stop():
    async def broken(text):
        raise RuntimeError("no network")

    def workers(stop):
        asyncio.get_running_loop().call_later(0.02, stop.set)

        async def w():
            await stop.wait()
        return {"w": w}

    code, _, _ = await run_supervise(workers, broken)
    assert code == 0


# ------------------------------------------- Store stays in the loop thread
async def test_store_stays_in_loop_thread_while_1c_read_runs_in_a_worker(tmp_path):
    loop_thread = threading.get_ident()
    seen = []

    class Mp:
        name = "ozon"
        order = Order("ozon", "1", [OrderLine("A1", 1, "СПБ", Decimal("2000"), None)],
                      datetime(2026, 11, 2, 9, tzinfo=UTC), None, "awaiting_packaging")

        async def new_orders(self):
            return [self.order]

        async def ready_quantities(self):
            return {}

        async def is_awaiting(self, o):
            return True

        async def is_ready(self, o):
            return False

        async def confirm(self, o):
            pass

    class OneC:
        def connect(self):
            seen.append(threading.get_ident())

        def disconnect(self):
            seen.append(threading.get_ident())

        def read(self, a):
            seen.append(threading.get_ident())
            return ItemRead(a, {"СПБ": StockRow("СПБ", 5, 0)}, Decimal(1000), Decimal(800), None)

    class Notifier:
        async def order_alert(self, *a, **k):
            pass

        async def reserve_request(self, *a, **k):
            pass

        async def system_alert(self, t):
            pass

        async def health_alert(self, t):
            pass

    store = Store(tmp_path / "s.db")        # sqlite refuses use from another thread
    await Pipeline(store, [Mp()], OneC(), Notifier()).tick()
    assert store.get_state("ozon", "1") == "OBSERVED"
    assert seen and all(t != loop_thread for t in seen)


async def test_crash_in_run_before_supervise_sends_stop_message(tmp_path, monkeypatch):
    sent = []

    class FakeNotifier:
        def __init__(self, bot, ids, store=None, alert_chat_id=None, alert_thread_id=None,
                     companies=None, health_thread_id=None):
            pass

        async def say(self, text, keyboard=False):
            sent.append(text)

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("secret detail")

    import orderbot.main as m
    monkeypatch.setattr(m, "TelegramNotifier", FakeNotifier)
    monkeypatch.setattr(m, "MacOneC", Boom)
    code = await run(Config(bot_token="123:abc", allowed_user_ids=[1],
                            ozon_client_id="1", ozon_api_key="k"), None, tmp_path)
    assert code == 1
    assert sent and sent[0].startswith("Робот выключен") and "RuntimeError" in sent[0]
    assert "secret detail" not in sent[0]


# ------------------------------------------------------------- Ctrl+C handler
class StopOneC:
    def __init__(self, waits=True):
        self.waits = waits
        self.calls = []

    def request_stop(self):
        self.calls.append("request_stop")

    def stop_and_wait(self):
        self.calls.append("stop_and_wait")
        return self.waits


class Exit(Exception):
    pass


def test_first_ctrl_c_stops_input_and_sets_stop():
    from orderbot.main import make_signal_handler

    onec, stop = StopOneC(), asyncio.Event()
    handler = make_signal_handler(onec, stop, exit=lambda code: (_ for _ in ()).throw(Exit()))
    handler()
    assert onec.calls == ["request_stop"] and stop.is_set()


@pytest.mark.parametrize("waits, calls", [
    (True, ["request_stop", "stop_and_wait"]),
    (False, ["request_stop", "stop_and_wait"]),
])
def test_second_ctrl_c_waits_for_running_input_then_exits(waits, calls):
    from orderbot.main import make_signal_handler

    onec, stop = StopOneC(waits), asyncio.Event()
    exits = []

    def fake_exit(code):
        exits.append((code, list(onec.calls)))
        raise Exit()

    handler = make_signal_handler(onec, stop, exit=fake_exit)
    handler()
    onec.calls.clear()
    with pytest.raises(Exit):
        handler()
    assert exits == [(130, calls)]


# ------------------------------------------------------- start text, warnings
def test_start_text_plain_when_not_paused(tmp_path):
    from orderbot.main import start_text

    assert start_text(Store(tmp_path / "s.db")) == "Робот включён"


def test_start_text_says_pause_by_a_person(tmp_path):
    from orderbot.main import start_text

    clock = lambda: datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    s = Store(tmp_path / "s.db", clock=clock)
    s.set_paused(True, "42 Анна")
    text = start_text(s)
    assert "на паузе" in text and "02.11 09:30" in text
    assert "Я работаю в 1С" in text and "42 Анна" in text and "Я закончил" in text


def test_start_text_says_pause_by_1c_failure(tmp_path):
    from orderbot.main import start_text

    s = Store(tmp_path / "s.db")
    s.set_paused(True, "system")
    s.log(None, None, "onec_stopped", {"text": "SessionLost (заказ ozon 1 остался в очереди)"})
    text = start_text(s)
    assert "на паузе" in text and "1С" in text and "SessionLost" in text


def test_warehouse_warning_names_values_missing_from_layout():
    from types import SimpleNamespace

    from orderbot.main import warehouse_warning

    layout = SimpleNamespace(warehouses={"wh_spb": "СПБ", "wh_msk": "Склад (восто"})
    assert warehouse_warning({"ozon": {"A": "СПБ", "B": "Склад (восто"}}, layout) is None
    text = warehouse_warning({"ozon": {"A": "СПБ", "B": "Казань"}, "yandex": {"C": "Тверь"}},
                             layout)
    assert "Казань" in text and "Тверь" in text and "СПБ" not in text.split(":", 1)[1]


async def test_supervise_says_the_given_start_text():
    sink = Sink()

    def workers(stop):
        asyncio.get_running_loop().call_later(0.02, stop.set)

        async def w():
            await stop.wait()
        return {"w": w}

    await run_supervise(workers, sink, start_text="Робот включён, но стоит на паузе")
    assert sink.texts[0] == "Робот включён, но стоит на паузе"


# ------------------------------------------------- run(): wiring with fakes only
async def test_run_stops_1c_input_when_a_part_crashes_and_warns_at_start(tmp_path, monkeypatch):
    """No network, no real window: Telegram, the dispatcher, the marketplaces and 1C are
    fakes. The bot part crashes; the stop must reach 1C input even without a signal."""
    from types import SimpleNamespace

    import orderbot.main as m

    said, alerts, onecs = [], [], []

    class FakeNotifier:
        def __init__(self, bot, ids, store=None, alert_chat_id=None, alert_thread_id=None,
                     companies=None, health_thread_id=None):
            pass

        async def say(self, text, keyboard=False):
            said.append(text)

        async def system_alert(self, text):
            alerts.append(text)

        async def health_alert(self, text):
            alerts.append(text)

    class FakeOneC:
        def __init__(self, *a, **k):
            self.calls = []
            onecs.append(self)

        def request_stop(self):
            self.calls.append("request_stop")

    class FakeDp:
        async def start_polling(self, bot, handle_signals=False):
            await asyncio.sleep(0.02)
            raise RuntimeError("polling died")

        async def stop_polling(self):
            pass

    monkeypatch.setattr(m, "TelegramNotifier", FakeNotifier)
    monkeypatch.setattr(m, "MacOneC", FakeOneC)
    monkeypatch.setattr(m, "build_dispatcher", lambda controller, notifier: FakeDp())
    monkeypatch.setattr(m, "build_marketplaces", lambda cfg: [])
    monkeypatch.setattr(m, "_caffeinate", lambda: None)
    pre = Store(tmp_path / "orderbot.db")
    pre.set_paused(True, "7 Иван")
    del pre
    layout = SimpleNamespace(warehouses={"wh_spb": "СПБ"})
    cfg = Config(bot_token="123:abc", allowed_user_ids=[1],
                 warehouses={"ozon": {"Склад": "Казань"}})
    code = await asyncio.wait_for(m.run(cfg, layout, tmp_path), 10)
    assert code == 1
    assert onecs[0].calls and onecs[0].calls[0] == "request_stop"
    assert said[0].startswith("Робот включён") and "на паузе" in said[0]
    assert said[-1].startswith("Робот выключен")
    assert any("Казань" in a for a in alerts)


async def test_tick_loop_retries_soon_while_orders_wait_for_the_mac(monkeypatch):
    """A person who stepped away should not wait 5 minutes."""
    import asyncio

    from orderbot import main as main_mod

    waits = []

    async def fake_wait(stop, seconds):
        waits.append(seconds)
        if len(waits) >= 2:
            stop.set()

    class P:
        waiting_for_mac = True
        async def tick(self):
            pass

    monkeypatch.setattr(main_mod, "_wait", fake_wait)
    stop = asyncio.Event()

    async def say(text):
        pass

    await main_mod.tick_loop(P(), say, stop, interval=300)
    assert waits[0] == main_mod.BUSY_RETRY
    P.waiting_for_mac = False
    waits.clear(); stop.clear()
    await main_mod.tick_loop(P(), say, stop, interval=300)
    assert waits[0] == 300


def test_wb_connected_by_token():
    from orderbot.marketplaces.wildberries import WildberriesMarketplace
    assert build_marketplaces(Config()) == []
    cfg = Config(wb_token="t", wb_paid_field="salePrice", warehouses={"wb": {"9001": "СПБ"}})
    [m] = build_marketplaces(cfg)
    assert isinstance(m, WildberriesMarketplace) and m.name == "wb"
    assert m.warehouses == {"9001": "СПБ"} and m.paid_field == "salePrice"


async def test_run_wires_group_managers_and_human_buttons(tmp_path, monkeypatch):
    """ALERT_CHAT_ID goes to the notifier, MANAGER_USER_IDS and HumanActions to the controller;
    HumanActions gets the marketplaces by name and the same pipeline."""
    from types import SimpleNamespace

    import orderbot.main as m

    seen = {}

    class FakeNotifier:
        def __init__(self, bot, ids, store=None, alert_chat_id=None, alert_thread_id=None,
                     companies=None, health_thread_id=None):
            seen["notifier"] = (list(ids), alert_chat_id, alert_thread_id)
            seen["companies"] = companies

        async def say(self, text, keyboard=False):
            pass

        async def system_alert(self, text):
            pass

        async def health_alert(self, text):
            pass

    class FakeOneC:
        def __init__(self, *a, **k):
            pass

        def request_stop(self):
            pass

    class FakeDp:
        async def start_polling(self, bot, handle_signals=False):
            raise RuntimeError("stop here")

        async def stop_polling(self):
            pass

    real_ctl, real_actions = m.BotController, m.HumanActions

    def ctl(*a, **k):
        c = real_ctl(*a, **k)
        seen["controller"] = (a, k, c)
        return c

    def actions(*a, **k):
        h = real_actions(*a, **k)
        seen["actions"] = (a, k, h)
        return h

    mp = SimpleNamespace(name="ozon")
    monkeypatch.setattr(m, "TelegramNotifier", FakeNotifier)
    monkeypatch.setattr(m, "MacOneC", FakeOneC)
    monkeypatch.setattr(m, "BotController", ctl)
    monkeypatch.setattr(m, "HumanActions", actions)
    monkeypatch.setattr(m, "build_dispatcher", lambda controller, notifier: FakeDp())
    monkeypatch.setattr(m, "build_marketplaces", lambda cfg: [mp])
    monkeypatch.setattr(m, "_caffeinate", lambda: None)
    cfg = Config(bot_token="123:abc", allowed_user_ids=[1], alert_chat_id=-100500,
                 alert_thread_id=77, manager_user_ids=[7, 8])
    await asyncio.wait_for(m.run(cfg, SimpleNamespace(warehouses={}), tmp_path), 10)
    assert seen["notifier"] == ([1], -100500, 77)
    a, _, human = seen["actions"]
    # buttons find the adapter by (marketplace, company); one without `account` is Альфа
    assert a[1] == {("ozon", "main"): mp}
    assert seen["companies"] == {"main": "Альфа", "second": "Бета"}
    assert a[2]._companies == {"main": "Альфа", "second": "Бета"}
    _, k, c = seen["controller"]
    assert k["companies"] == {"Альфа": ["ozon"], "Бета": []}
    assert k["actions"] is human and set(k["manager_ids"]) == {7, 8}
    # marketplace product photos are cached under the data dir
    photos = a[2]._photos
    assert photos is not None and photos._root == tmp_path / "product_photos"
    assert c.can_press(7) and not c.allowed(7) and c.allowed(1)
    assert a[2] is c._pipeline


async def test_run_starts_the_lamoda_labels_loop_beside_the_robot(tmp_path, monkeypatch):
    """Lamoda labels run as their own part; the group's bot and the store are shared."""
    from types import SimpleNamespace

    import orderbot.main as m

    seen = {}

    class FakeNotifier:
        def __init__(self, bot, ids, store=None, alert_chat_id=None, alert_thread_id=None,
                     companies=None, health_thread_id=None):
            pass

        async def say(self, text, keyboard=False):
            pass

        async def system_alert(self, text):
            pass

        async def health_alert(self, text):
            pass

    class FakeOneC:
        def __init__(self, *a, **k):
            pass

        def request_stop(self):
            pass

    class FakeDp:
        async def start_polling(self, bot, handle_signals=False):
            await asyncio.sleep(0.05)
            raise RuntimeError("stop here")

        async def stop_polling(self):
            pass

    class FakeLamoda:
        async def run(self, stop):
            seen["ran"] = True
            await stop.wait()

    def build(cfg, store, bot, say, health=None):
        seen["args"] = (cfg, store, bot, say)
        return FakeLamoda()

    monkeypatch.setattr(m, "TelegramNotifier", FakeNotifier)
    monkeypatch.setattr(m, "MacOneC", FakeOneC)
    monkeypatch.setattr(m, "build_dispatcher", lambda controller, notifier: FakeDp())
    monkeypatch.setattr(m, "build_marketplaces", lambda cfg: [])
    monkeypatch.setattr(m, "_caffeinate", lambda: None)
    monkeypatch.setattr(m, "build_lamoda", build)
    cfg = Config(bot_token="123:abc", allowed_user_ids=[1], alert_chat_id=-100500)
    await asyncio.wait_for(m.run(cfg, SimpleNamespace(warehouses={}), tmp_path), 10)
    assert seen.get("ran") is True
    assert seen["args"][0] is cfg and isinstance(seen["args"][1], Store)
