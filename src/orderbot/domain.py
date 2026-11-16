from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

# Seller companies: an adapter and its orders belong to one of them. "main" is
# company A (the keys without a prefix), "second" the SECOND_* keys. Display
# names come from the config; these are the defaults.
MAIN = "main"
SECOND = "second"
DEFAULT_COMPANY_NAMES = {MAIN: "Альфа", SECOND: "Бета"}


class Action(Enum):
    CONFIRM = "confirm"
    ASK_RESERVE = "ask_reserve"
    ESCALATE = "escalate"


class Reason(Enum):
    RESERVE = "reserve"
    NO_STOCK = "no_stock"
    PRICE_LOW = "price_low"
    READ_UNCERTAIN = "read_uncertain"
    ARTICLE_NOT_FOUND = "article_not_found"
    WAREHOUSE_NOT_ON_SCREEN = "warehouse_not_on_screen"
    WAREHOUSE_UNMAPPED = "warehouse_unmapped"
    QTY_UNVERIFIED = "qty_unverified"
    PAID_MISSING = "paid_missing"
    INVALID_ORDER = "invalid_order"
    CONFIRM_UNKNOWN = "confirm_unknown"
    INTERNAL_ERROR = "internal_error"
    READY_UNKNOWN = "ready_unknown"
    API_UNAVAILABLE = "api_unavailable"


@dataclass
class OrderLine:
    article: str
    qty: int
    warehouse: str | None
    paid_total: Decimal | None
    brand: str | None
    promos: list[str] = field(default_factory=list)  # promo codes the buyer used (Ozon, Yandex)
    # Yandex Plus points the buyer paid with, line total; already included in paid_total
    # (the Market pays them back to the seller), shown apart in the alert.
    points: Decimal | None = None
    # the unit price as paid in a foreign currency, e.g. "14 309 KZT"; paid_total is
    # then its ruble value at the Bank of Russia rate (None when no rate was had)
    paid_foreign: str | None = None


@dataclass
class Order:
    marketplace: str
    order_id: str
    lines: list[OrderLine]
    created_at: datetime
    deadline: datetime | None
    status: str
    campaign: str | None = None  # Yandex shop the order belongs to
    express: bool = False
    mp_warehouse: str | None = None  # warehouse/shop name on the marketplace, for captions
    account: str = MAIN  # the seller company (MAIN / SECOND) whose adapter got the order


@dataclass
class StockRow:
    warehouse: str
    on_hand: int
    reserve: int


@dataclass
class ItemRead:
    article: str
    stock: dict[str, StockRow]
    epr: Decimal
    ppr: Decimal
    snapshot_path: str | None
    # Pictures for the person who gets the order, never used in decisions:
    # the 1C «Изображение» tab and the marketplace's main product photo.
    photo_1c: str | None = None
    photo_mp: str | None = None


@dataclass
class PriceSettings:
    soinvest: bool
    brand_discounts: dict[str, Decimal]  # keys lower-case
    tolerance: Decimal
    multi_qty_verified: bool
    # Articles sold at a fixed price below PPR on purpose: {article
    # lower-case: price}. Their floor is that price minus FIXED_PRICE_TOLERANCE instead of
    # EPR/PPR; with soinvest on they are not price-checked at all. Stock is always checked.
    fixed_price: dict = field(default_factory=dict)


@dataclass
class Decision:
    action: Action
    reasons: list[tuple[str, Reason]]
    floors: dict[str, Decimal]
