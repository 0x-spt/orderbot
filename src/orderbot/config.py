import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from orderbot.domain import DEFAULT_COMPANY_NAMES, MAIN, SECOND


@dataclass
class Config:
    fixed_price_articles: dict = field(default_factory=dict)
    ozon_client_id: str = ""
    ozon_api_key: str = ""
    ozon_paid_field: str | None = None
    # None: unknown whether the paid amount is per unit; quantity > 1 then goes to a human.
    ozon_paid_is_unit: bool | None = None
    ym_token: str = ""
    ym_campaign_id: str = ""
    ym_business_id: str = ""
    ym_paid_field: str | None = None
    wb_token: str = ""
    # Which WB order price is compared with 1C (kopecks in the API): price, salePrice, ...
    wb_paid_field: str | None = None
    # The operator's own Microsoft Remote Desktop window; empty means the app's only window.
    rdp_window_title_contains: str = ""
    # Seconds without mouse or keyboard use on the Mac before the robot sends input.
    idle_min_seconds: float = 30.0
    bot_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    # Group that gets order messages; None means every admin gets them privately.
    alert_chat_id: int | None = None
    alert_thread_id: int | None = None   # forum topic of the alert group
    manager_user_ids: list[int] = field(default_factory=list)
    warehouses: dict[str, dict[str, str]] = field(default_factory=dict)
    # marketplace -> marketplace warehouse names (Ozon) / campaign ids (Yandex) of express.
    express: dict[str, set[str]] = field(default_factory=dict)
    # Where the database, snapshots, labels and the lock live; empty DATA_DIR means "data".
    data_dir: str = "data"
    # Second seller company «Бета»: its own Ozon and Yandex keys; the
    # paid-field settings, warehouses file, 1C and modes are shared. Empty keys: not used.
    company_name: str = DEFAULT_COMPANY_NAMES[MAIN]
    second_company_name: str = DEFAULT_COMPANY_NAMES[SECOND]
    second_ozon_client_id: str = ""
    second_ozon_api_key: str = ""
    second_ym_token: str = ""
    second_ym_business_id: str = ""
    # Lamoda FBS labels: orders «Ждёт отгрузки» with their item and package
    # labels go to this topic of the ALERT_CHAT_ID group. Any of the three empty: off.
    lamoda_client_id: str = ""
    lamoda_client_secret: str = ""
    lamoda_thread_id: int | None = None
    # Shipment reminders: 10 min before each handover the counts go to this
    # topic of the ALERT_CHAT_ID group; times and warehouses are in SHIPMENTS_FILE.
    shipments_thread_id: int | None = None
    shipments_file: str = "shipments.yaml"
    # API and 1C outages: this topic of the ALERT_CHAT_ID group; unset → admins.
    health_thread_id: int | None = None

    @property
    def company_names(self) -> dict[str, str]:
        """account key -> the name people see in messages."""
        return {MAIN: self.company_name, SECOND: self.second_company_name}


IDLE_MIN_FLOOR = 10.0
DEFAULT_WAREHOUSES_FILE = "warehouses.yaml"
DEFAULT_DATA_DIR = "data"


class ConfigError(ValueError):
    """A setting has a value the robot must not run with."""


def _idle_min(raw: str | None) -> float:
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigError(f"IDLE_MIN_SECONDS must be a number, got {raw!r}") from e
    if not value >= IDLE_MIN_FLOOR:
        raise ConfigError(f"IDLE_MIN_SECONDS must be at least {IDLE_MIN_FLOOR:g}, got {raw!r}")
    return value


def _int_list(name: str, raw: str) -> list[int]:
    try:
        return [int(x) for x in raw.replace(" ", "").split(",") if x]
    except ValueError:
        raise ConfigError(f"{name} must be comma-separated integers, got {raw!r}") from None


def _scalar(v) -> bool:
    return isinstance(v, (str, int, float)) and not isinstance(v, bool)


def _warehouses(env, warehouses_path) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """marketplace -> {marketplace warehouse: 1C warehouse}. An empty WAREHOUSES_FILE means
    the default file; the default may be missing (every order then goes to a person), a
    file named explicitly must exist."""
    explicit = env.get("WAREHOUSES_FILE") or None
    path = Path(warehouses_path or explicit or DEFAULT_WAREHOUSES_FILE)
    if not path.exists():
        if explicit and not warehouses_path:
            raise ConfigError(f"WAREHOUSES_FILE: файл {path} не найден")
        return {}, {}
    if not path.is_file():
        raise ConfigError(f"WAREHOUSES_FILE: {path} не файл")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ConfigError(f"файл складов {path} не читается ({type(e).__name__})") from e
    bad = ConfigError(f"файл складов {path}: нужен вид «площадка: {{склад площадки: "
                      "склад 1С}}», как в warehouses.example.yaml")
    if not isinstance(raw, dict):
        raise bad
    out = {}
    express = {}
    exp_raw = raw.pop("express", None) or {}
    if not isinstance(exp_raw, dict):
        raise bad
    for mp, names in exp_raw.items():
        if not isinstance(names, list) or not all(_scalar(n) for n in names):
            raise bad
        express[str(mp)] = {str(n) for n in names}
    for mp, table in raw.items():
        table = table or {}
        if not isinstance(table, dict) or not all(
                _scalar(k) and _scalar(v) for k, v in table.items()):
            raise bad
        out[str(mp)] = {str(k): str(v) for k, v in table.items()}
    return out, express


def _tri_bool(name, raw):
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    raise ValueError(f"{name}: ожидается true или false, получено {raw!r}")


DEFAULT_PRICE_EXCEPTIONS_FILE = "price_exceptions.txt"


def _price_exceptions(env) -> dict:
    """Articles sold at a fixed price below PPR on purpose: lines "ARTICLE PRICE", "#"
    comments. Returns {article lower-case: Decimal price}. A missing file means none."""
    path = Path(env.get("PRICE_EXCEPTIONS_FILE") or DEFAULT_PRICE_EXCEPTIONS_FILE)
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"не удалось прочитать список исключений по цене {path}: {e}") from e
    out = {}
    for n, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        try:
            if len(parts) != 2:
                raise ValueError
            price = Decimal(parts[1].replace(",", "."))
            if not price > 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            raise ConfigError(f"{path}, строка {n}: нужно «АРТИКУЛ ЦЕНА», а не {raw!r}") from None
        out[parts[0].lower()] = price
    return out


def load_config(env=None, warehouses_path=None) -> Config:
    env = os.environ if env is None else env
    wh, express = _warehouses(env, warehouses_path)
    ids = [int(x) for x in env.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]
    managers = _int_list("MANAGER_USER_IDS", env.get("MANAGER_USER_IDS", ""))
    alert_chat = (env.get("ALERT_CHAT_ID") or "").strip()
    try:
        alert_chat_id = int(alert_chat) if alert_chat else None
    except ValueError:
        raise ConfigError(f"ALERT_CHAT_ID must be an integer, got {alert_chat!r}") from None
    alert_thread = (env.get("ALERT_THREAD_ID") or "").strip()
    try:
        alert_thread_id = int(alert_thread) if alert_thread else None
    except ValueError:
        raise ConfigError(f"ALERT_THREAD_ID must be an integer, got {alert_thread!r}") from None
    lamoda_thread = (env.get("LAMODA_THREAD_ID") or "").strip()
    try:
        lamoda_thread_id = int(lamoda_thread) if lamoda_thread else None
    except ValueError:
        # an optional part: a typo turns the Lamoda labels off, never stops the robot
        logging.getLogger(__name__).warning(
            "LAMODA_THREAD_ID должен быть числом, получено %r: этикетки Lamoda выключены",
            lamoda_thread)
        lamoda_thread_id = None
    shipments_thread = (env.get("SHIPMENTS_THREAD_ID") or "").strip()
    try:
        shipments_thread_id = int(shipments_thread) if shipments_thread else None
    except ValueError:
        logging.getLogger(__name__).warning(
            "SHIPMENTS_THREAD_ID должен быть числом, получено %r: напоминания об отгрузках "
            "выключены", shipments_thread)
        shipments_thread_id = None
    health_thread = (env.get("HEALTH_THREAD_ID") or "").strip()
    try:
        health_thread_id = int(health_thread) if health_thread else None
    except ValueError:
        logging.getLogger(__name__).warning(
            "HEALTH_THREAD_ID должен быть числом, получено %r: сбои API и 1С идут "
            "администраторам в личку", health_thread)
        health_thread_id = None
    return Config(
        fixed_price_articles=_price_exceptions(env),
        ozon_client_id=env.get("OZON_CLIENT_ID", ""),
        ozon_api_key=env.get("OZON_API_KEY", ""),
        ozon_paid_field=env.get("OZON_PAID_FIELD") or None,
        ozon_paid_is_unit=_tri_bool("OZON_PAID_IS_UNIT", env.get("OZON_PAID_IS_UNIT")),
        ym_token=env.get("YM_TOKEN", ""),
        ym_campaign_id=env.get("YM_CAMPAIGN_ID", ""),
        ym_business_id=env.get("YM_BUSINESS_ID", ""),
        ym_paid_field=env.get("YM_PAID_FIELD") or None,
        wb_token=env.get("WB_TOKEN", ""),
        wb_paid_field=env.get("WB_PAID_FIELD") or None,
        rdp_window_title_contains=env.get("RDP_WINDOW_TITLE_CONTAINS", ""),
        idle_min_seconds=_idle_min(env.get("IDLE_MIN_SECONDS")),
        bot_token=env.get("BOT_TOKEN", ""),
        allowed_user_ids=ids,
        alert_chat_id=alert_chat_id,
        alert_thread_id=alert_thread_id,
        manager_user_ids=managers,
        warehouses=wh,
        express=express,
        data_dir=env.get("DATA_DIR") or DEFAULT_DATA_DIR,
        company_name=(env.get("COMPANY_NAME") or "").strip() or DEFAULT_COMPANY_NAMES[MAIN],
        second_company_name=((env.get("SECOND_COMPANY_NAME") or "").strip()
                           or DEFAULT_COMPANY_NAMES[SECOND]),
        second_ozon_client_id=env.get("SECOND_OZON_CLIENT_ID", ""),
        second_ozon_api_key=env.get("SECOND_OZON_API_KEY", ""),
        second_ym_token=env.get("SECOND_YM_TOKEN", ""),
        second_ym_business_id=env.get("SECOND_YM_BUSINESS_ID", ""),
        lamoda_client_id=(env.get("LAMODA_CLIENT_ID") or "").strip(),
        lamoda_client_secret=(env.get("LAMODA_CLIENT_SECRET") or "").strip(),
        lamoda_thread_id=lamoda_thread_id,
        shipments_thread_id=shipments_thread_id,
        shipments_file=(env.get("SHIPMENTS_FILE") or "").strip() or "shipments.yaml",
        health_thread_id=health_thread_id,
    )
