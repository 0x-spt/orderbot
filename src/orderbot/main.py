"""Entry point: `python -m orderbot`. One asyncio process on the operator's Mac.

Threads: the event loop thread owns the Store, the Telegram bot and all marketplace calls.
Only the 1C connect/read/disconnect (screen capture, OCR, input) run in worker threads
through asyncio.to_thread, started by Pipeline and awaited one at a time. Those calls never
touch the Store (MacOneC has no reference to it), so one sqlite connection is enough and no
second connection is needed. tests/test_main.py pins this with a real Store, which refuses
use from another thread.

Stops on Ctrl+C (SIGINT) or SIGTERM: the loops end, Telegram gets "Робот выключен". A second
Ctrl+C leaves at once (after a running input call finishes, if it does in time). A crash of
any part also ends in the stop message. Whatever starts the stop, 1C input stops first.
"""
import asyncio
import fcntl
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from orderbot.bot import BotController, PauseReminder, TelegramNotifier, build_dispatcher
from orderbot.config import Config, ConfigError, load_config
from orderbot.domain import MAIN, SECOND
from orderbot.human_actions import HumanActions
from orderbot.lamoda_labels import build_lamoda
from orderbot.marketplaces.base import adapter_key
from orderbot.marketplaces.ozon import OzonMarketplace
from orderbot.marketplaces.wildberries import WildberriesMarketplace
from orderbot.marketplaces.yandex import YandexMarket
from orderbot.pipeline import Pipeline
from orderbot.product_photos import ProductPhotos
from orderbot.screen.onec import MacOneC
from orderbot.screen.reader import Layout
from orderbot.shipments import build_shipments
from orderbot.stock_sync import StockSync
from orderbot.store import Store

log = logging.getLogger("orderbot")

TICK_INTERVAL = 300.0
BUSY_RETRY = 30.0  # orders waiting for a free Mac are retried this often
REMINDER_CHECK_PERIOD = 60.0
TICK_ALERT_EVERY = 3600.0
NOTIFY_TIMEOUT = 5.0
GRACE = 30.0


class AlreadyRunning(Exception):
    pass


def acquire_lock(path):
    """An exclusive flock on the file; the system drops it when the process dies."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise AlreadyRunning(f"робот уже запущен (замок {path})") from None
    return fh


def load_env_file(path, environ=None) -> None:
    environ = os.environ if environ is None else environ
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        environ.setdefault(key, value)


def env_permission_warning(path) -> str | None:
    """The file holds the bot token and the Ozon key: only its owner should read it."""
    try:
        mode = Path(path).stat().st_mode & 0o777
    except OSError:
        return None
    if mode & 0o077:
        return (f"Файл {path} с ключами доступен другим пользователям Mac (права {mode:o}). "
                f"Выполните: chmod 600 {path}")
    return None


def companies_status(cfg: Config, mps) -> dict[str, list[str]]:
    """Company name -> its connected marketplaces, for /status."""
    return {name: [m.name for m in mps if adapter_key(m)[1] == account]
            for account, name in cfg.company_names.items()}


SECOND_SUFFIX = "_" + SECOND   # warehouses.yaml sections of «Бета»: ozon_second, ...


def build_marketplaces(cfg: Config) -> list:
    """The adapters of every company whose keys are set: the main company first, then
    Бета. Бета's warehouses are the `ozon_second` / `yandex_second` sections."""
    mps = []
    if cfg.ozon_client_id and cfg.ozon_api_key:
        mps.append(OzonMarketplace(cfg.ozon_client_id, cfg.ozon_api_key,
                                   warehouses=cfg.warehouses.get("ozon"),
                                   paid_field=cfg.ozon_paid_field,
                                   paid_is_unit=cfg.ozon_paid_is_unit,
                                   express=cfg.express.get("ozon", ())))
    ym_campaigns = cfg.warehouses.get("yandex") or {}
    if cfg.ym_token and (ym_campaigns or cfg.ym_campaign_id):
        mps.append(YandexMarket(cfg.ym_token, cfg.ym_campaign_id or None,
                                warehouses=ym_campaigns,
                                paid_field=cfg.ym_paid_field,
                                business_id=cfg.ym_business_id or None,
                                labels_dir=Path(cfg.data_dir) / "labels",
                                express=cfg.express.get("yandex", ())))
    elif cfg.ym_token or cfg.ym_campaign_id:
        log.warning("Яндекс Маркет не подключён: нужны YM_TOKEN и кампании в разделе "
                    "yandex файла складов (или YM_CAMPAIGN_ID)")
    if cfg.wb_token:
        mps.append(WildberriesMarketplace(cfg.wb_token, warehouses=cfg.warehouses.get("wb"),
                                          paid_field=cfg.wb_paid_field))
    mps += _second_marketplaces(cfg)
    return mps


STOCK_SYNC_WAIT_S = 10.0


def build_stock_sync(cfg: Config, mps, store, notifier) -> StockSync:
    """The stock sync works with the main company's adapters only; Бета's
    are never given to it, so its warehouses cannot be written."""
    main = {m.name: m for m in mps if adapter_key(m)[1] == MAIN}
    return StockSync(store, main, cfg.warehouses, notifier)


def _second_marketplaces(cfg: Config) -> list:
    """Second company. Keys not set: nothing is created, one info line. Keys
    set but no warehouse section: Ozon runs with no mapping (its orders go to a person,
    «склад не сопоставлен», as Альфа does for an unmapped warehouse); Yandex knows its shops
    only from the section, so it is not connected (as Альфа without the yandex section)."""
    name, mps, missing = cfg.second_company_name, [], []
    oz_section, ym_section = "ozon" + SECOND_SUFFIX, "yandex" + SECOND_SUFFIX
    if cfg.second_ozon_client_id and cfg.second_ozon_api_key:
        if oz_section not in cfg.warehouses:
            log.warning("%s: Озон подключён, но в файле складов нет раздела %s: заказы "
                        "уйдут человеку («склад не сопоставлен»). Склады покажет "
                        "python -m orderbot.recon warehouses second", name, oz_section)
        mps.append(OzonMarketplace(cfg.second_ozon_client_id, cfg.second_ozon_api_key,
                                   warehouses=cfg.warehouses.get(oz_section),
                                   paid_field=cfg.ozon_paid_field,
                                   paid_is_unit=cfg.ozon_paid_is_unit,
                                   express=cfg.express.get(oz_section, ()),
                                   account=SECOND))
    else:
        missing.append("Озон (SECOND_OZON_CLIENT_ID, SECOND_OZON_API_KEY)")
    ym_campaigns = cfg.warehouses.get(ym_section) or {}
    if cfg.second_ym_token and ym_campaigns:
        mps.append(YandexMarket(cfg.second_ym_token, None, warehouses=ym_campaigns,
                                paid_field=cfg.ym_paid_field,
                                business_id=cfg.second_ym_business_id or None,
                                labels_dir=Path(cfg.data_dir) / "labels",
                                express=cfg.express.get(ym_section, ()),
                                account=SECOND))
    elif cfg.second_ym_token:
        log.warning("%s: Яндекс Маркет не подключён: в файле складов нет кампаний в разделе "
                    "%s. Кампании покажет python -m orderbot.recon warehouses second",
                    name, ym_section)
    else:
        missing.append("Маркет (SECOND_YM_TOKEN)")
    if missing:
        log.info("%s: ключи не заданы, не подключено: %s", name, "; ".join(missing))
    return mps


async def _wait(stop: asyncio.Event, seconds: float, wake: asyncio.Event | None = None) -> None:
    """Waits `seconds`, or less when the robot stops or a person asks for a check now
    (`wake`; the robot's stop sets it too, see _relay)."""
    try:
        await asyncio.wait_for((wake or stop).wait(), seconds)
    except TimeoutError:
        pass


async def _relay(stop: asyncio.Event, wake: asyncio.Event) -> None:
    await stop.wait()
    wake.set()


async def tick_loop(pipeline, say, stop: asyncio.Event, interval: float = TICK_INTERVAL) -> None:
    last_alert: float | None = None
    wake = getattr(pipeline, "wake", None)
    if not isinstance(wake, asyncio.Event):
        wake = None
    else:
        # keep a reference (a task nobody holds may be garbage-collected); it ends with stop
        _relay_task = asyncio.ensure_future(_relay(stop, wake))
    while not stop.is_set():
        if wake is not None:
            wake.clear()     # a press during this tick sets it again: one more tick follows
        try:
            await pipeline.tick()
        except Exception as e:
            log.exception("tick failed")
            now = time.monotonic()
            if last_alert is None or now - last_alert >= TICK_ALERT_EVERY:
                last_alert = now
                try:
                    await say(f"Сбой цикла проверки заказов ({type(e).__name__}). Робот "
                              "продолжает работать, подробности в журнале на Mac.")
                except Exception:
                    log.exception("cannot report tick failure")
        busy = getattr(pipeline, "waiting_for_mac", False) is True
        seconds = min(interval, BUSY_RETRY) if busy else interval
        if wake is None:
            await _wait(stop, seconds)
        else:
            await _wait(stop, seconds, wake)


async def reminder_loop(reminder, say, stop: asyncio.Event,
                        period: float = REMINDER_CHECK_PERIOD) -> None:
    while not stop.is_set():
        try:
            text = reminder.check()
            if text:
                await say(text)
        except Exception:
            log.exception("pause reminder failed")
        await _wait(stop, period)


def make_signal_handler(onec, stop: asyncio.Event, exit=os._exit):
    """First Ctrl+C: no new 1C input, the loops end. Second: leave at once, after the
    running input call (if any) has released what it pressed or the wait timed out. The
    robot never presses a modifier, so nothing needs lifting on a hard exit."""
    interrupts = []

    def on_signal():
        onec.request_stop()          # no new 1C input from this moment
        if interrupts:
            onec.stop_and_wait()
            exit(130)
            return
        interrupts.append(1)
        stop.set()

    return on_signal


def start_text(store: Store) -> str:
    """"Робот включён", and if a pause was left from the last run: since when and why."""
    since = store.paused_since()
    if since is None:
        return "Робот включён"
    paused = store.last_event("paused")
    user = (paused or {}).get("payload", {}).get("user")
    if user == "system":
        stopped = store.last_event("onec_stopped")
        detail = (stopped or {}).get("payload", {}).get("text")
        why = "1С была недоступна" + (f": {detail}" if detail else "")
    else:
        why = "нажата кнопка «Я работаю в 1С»" + (f" ({user})" if user else "")
    return (f"Робот включён, но стоит на паузе с {since:%d.%m %H:%M}: {why}. Заказы ждут. "
            "Когда 1С свободна и форма «Номенклатура» открыта, нажмите «Я закончил».")


def warehouse_warning(warehouses: dict, layout) -> str | None:
    """1C warehouse names in warehouses.yaml that the screen layout does not know: orders of
    those warehouses can never be matched and always go to a person."""
    known = set(layout.warehouses.values())
    unknown = sorted({f"{mp}: {name}" for mp, table in warehouses.items()
                      for name in table.values() if name not in known})
    if not unknown:
        return None
    return ("Склады из файла складов, которых нет в разметке экрана 1С (заказы с них всегда "
            "уйдут человеку): " + "; ".join(unknown) + ". Исправьте warehouses.yaml или "
            "разметку.")


async def supervise(say, workers: dict, stop: asyncio.Event, stoppers=(), *, startup=None,
                    grace: float = GRACE, notify_timeout: float = NOTIFY_TIMEOUT,
                    start_text: str = "Робот включён") -> int:
    """Runs the workers until `stop` is set or one ends. Says "включён" first and "выключен"
    last whatever happens; both are best effort and never wait longer than notify_timeout."""

    async def tell(text):
        try:
            await asyncio.wait_for(say(text), notify_timeout)
        except Exception as e:
            log.warning("message %r not delivered: %r", text, e)

    reason, code = None, 0
    tasks: dict[str, asyncio.Future] = {}
    await tell(start_text)
    try:
        if startup is not None:
            await startup()
        tasks = {name: asyncio.ensure_future(fn()) for name, fn in workers.items()}
        stop_wait = asyncio.ensure_future(stop.wait())
        await asyncio.wait([stop_wait, *tasks.values()], return_when=asyncio.FIRST_COMPLETED)
        for name, t in tasks.items():
            if t.done() and (t.exception() is not None or not stop.is_set()):
                exc = t.exception()
                log.error("part %s ended: %r", name, exc, exc_info=exc)
                reason, code = (f"сбой в части «{name}»"
                                + (f" ({type(exc).__name__})" if exc else "")
                                + ", подробности в журнале на Mac"), 1
                break
    except Exception as e:
        log.exception("startup or supervision failed")
        reason, code = f"сбой при запуске ({type(e).__name__}), подробности в журнале на Mac", 1
    finally:
        stop.set()
        for stopper in stoppers:
            try:
                await stopper()
            except Exception:
                log.exception("stopper failed")
        if tasks:
            _, hung = await asyncio.wait(list(tasks.values()), timeout=grace)
            if hung:
                log.warning("%d part(s) did not stop in %s s", len(hung), grace)
        await tell(f"Робот выключен: {reason or 'остановлен вручную'}")
    return code


def _caffeinate() -> subprocess.Popen | None:
    """While the robot runs the Mac does not sleep and the screen does not lock for idleness."""
    try:
        return subprocess.Popen(["caffeinate", "-dis", "-w", str(os.getpid())])
    except OSError:
        log.warning("caffeinate недоступен: Mac может уйти в сон")
        return None


async def run(cfg: Config, layout: Layout, data_dir: Path,
              env_warning: str | None = None) -> int:
    from aiogram import Bot

    notifier = bot = caffeinate = stock_sync = None
    signals = []
    try:
        store = Store(data_dir / "orderbot.db")
        mps = build_marketplaces(cfg)
        bot = Bot(cfg.bot_token)
        notifier = TelegramNotifier(bot, cfg.allowed_user_ids, store, cfg.alert_chat_id,
                                    alert_thread_id=cfg.alert_thread_id,
                                    companies=cfg.company_names,
                                    health_thread_id=cfg.health_thread_id)
        onec = MacOneC(layout, cfg.rdp_window_title_contains, cfg.idle_min_seconds,
                       data_dir / "snapshots")
        stock_sync = build_stock_sync(cfg, mps, store, notifier)
        pipeline = Pipeline(store, mps, onec, notifier, snapshots_dir=data_dir / "snapshots",
                            fixed_price=cfg.fixed_price_articles,
                            product_photos=ProductPhotos(data_dir / "product_photos"),
                            companies=cfg.company_names, on_confirmed=stock_sync.schedule)
        actions = HumanActions(store, {adapter_key(m): m for m in mps}, pipeline,
                               on_collected=stock_sync.schedule)
        controller = BotController(store, pipeline, cfg.allowed_user_ids,
                                   [m.name for m in mps], manager_ids=cfg.manager_user_ids,
                                   actions=actions, companies=companies_status(cfg, mps))
        dp = build_dispatcher(controller, notifier)
        reminder = PauseReminder(store)
        # Lamoda labels: their own loop, outside 1C, the pipeline and the pause
        lamoda = build_lamoda(cfg, store, bot, notifier.system_alert,
                              health=notifier.health_alert)
        # Shipment reminders: their own loop, read-only, outside the pause
        shipments, shipments_warning = build_shipments(cfg, mps, store, bot,
                                                       notifier.system_alert)
        stop = asyncio.Event()

        loop = asyncio.get_running_loop()
        on_signal = make_signal_handler(onec, stop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal)
            signals.append(sig)

        async def stop_onec():
            onec.request_stop()          # a crash or the end of supervision: no new 1C input

        async def stop_polling():
            try:
                await dp.stop_polling()
            except RuntimeError:
                pass

        async def polling():
            await dp.start_polling(bot, handle_signals=False)

        async def say_start_stop(text):
            await notifier.say(text, keyboard=True)

        async def startup():
            await pipeline.recover()
            for warning in (env_warning, warehouse_warning(cfg.warehouses, layout),
                            shipments_warning):
                if warning:
                    log.warning(warning)
                    await notifier.system_alert(warning)

        workers = {
            "бот": polling,
            "проверка заказов": lambda: tick_loop(pipeline, notifier.system_alert, stop),
            "напоминание о паузе": lambda: reminder_loop(reminder, notifier.system_alert, stop)}
        if lamoda is not None:
            workers["этикетки Lamoda"] = lambda: lamoda.run(stop)
        if shipments is not None:
            workers["отгрузки"] = lambda: shipments.run(stop)
        caffeinate = _caffeinate()
        return await supervise(
            say_start_stop, workers,
            stop, [stop_onec, stop_polling], startup=startup, start_text=start_text(store))
    except Exception as e:
        log.exception("start failed")
        if notifier is not None:
            try:
                await asyncio.wait_for(
                    notifier.say(f"Робот выключен: сбой при запуске ({type(e).__name__}), "
                                 "подробности в журнале на Mac"), NOTIFY_TIMEOUT)
            except Exception:
                log.exception("stop message not delivered")
        return 1
    finally:
        for sig in signals:
            try:
                asyncio.get_running_loop().remove_signal_handler(sig)
            except Exception:
                log.exception("signal handler not removed")
        if caffeinate:
            caffeinate.terminate()
        if stock_sync is not None:   # a running stock write gets a moment to finish
            try:
                await asyncio.wait_for(stock_sync.wait_idle(), STOCK_SYNC_WAIT_S)
            except Exception:
                log.exception("stock sync still running at shutdown")
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                log.exception("bot session close failed")


def quiet_http_logs() -> None:
    """httpx logs every request address at INFO; addresses may hold keys or signed links."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        # Also to a file, so the run can be examined later whoever started it.
        Path("data").mkdir(exist_ok=True)
        handlers.append(logging.FileHandler("data/robot.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    quiet_http_logs()
    env_warning = env_permission_warning(".env")
    if env_warning:
        log.warning(env_warning)
    load_env_file(".env")
    try:
        cfg = load_config()
    except (ConfigError, ValueError) as e:
        sys.exit(f"Ошибка настройки: {e}")
    layout_file = os.environ.get("LAYOUT_FILE", "")
    problems = []
    if not cfg.bot_token:
        problems.append("BOT_TOKEN не задан")
    if not cfg.allowed_user_ids:
        problems.append("ALLOWED_USER_IDS не задан")
    if not layout_file:
        problems.append("LAYOUT_FILE не задан (файл разметки экрана 1С)")
    if not build_marketplaces(cfg):
        problems.append("нет ни одной площадки: задайте ключи Ozon, Яндекс Маркета или WB")
    if problems:
        sys.exit("Ошибка настройки: " + "; ".join(problems))
    data_dir = Path(cfg.data_dir)
    try:
        lock = acquire_lock(data_dir / "robot.lock")
    except AlreadyRunning as e:
        sys.exit(str(e))
    try:
        layout = Layout.load(layout_file)
        code = asyncio.run(run(cfg, layout, data_dir, env_warning))
    finally:
        lock.close()
    sys.exit(code)
