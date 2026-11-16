from dataclasses import dataclass
from datetime import date
from typing import Protocol

from orderbot.domain import MAIN, Order


@dataclass(frozen=True)
class StockAt:
    """An article's stock at one marketplace warehouse, for the stock sync.
    `count` is the free stock: what can still be sold there, reserves already taken off
    (Ozon `free_stock`, Yandex `AVAILABLE`, WB `amount`); `ref` is what the write needs
    (Ozon warehouse_id, WB chrtId, None for Yandex); `reserved` is informational."""
    count: int
    ref: object = None
    reserved: int = 0


@dataclass(frozen=True)
class ToShip:
    """An order still to be handed over, for the shipment reminders:
    its 1C warehouse, the Moscow day the marketplace expects it, whether it is assembled
    already (Ozon awaiting_deliver, Yandex READY_TO_SHIP) and its units."""
    order_id: str
    warehouse: str
    day: date
    assembled: bool
    units: int


class ToShipPartial(Exception):
    """to_ship read some campaigns but not others: `items` from the ones read, `failed`
    maps each 1C warehouse that could not be read to its error (Yandex)."""

    def __init__(self, items, failed):
        super().__init__(f"не прочитаны склады: {', '.join(sorted(failed))}")
        self.items = list(items)
        self.failed = dict(failed)


class StockError(Exception):
    """A stock read or write that must not go ahead; the message is for the report line."""


def adapter_key(m) -> tuple[str, str]:
    """(marketplace, seller company) of an adapter: the key orders are routed by. An adapter
    without `account` (older fakes) belongs to the main company."""
    return m.name, getattr(m, "account", None) or MAIN


def https_url(value) -> str | None:
    """A product photo address from the API: a non-empty https URL, or None. A list (some
    API versions give one) counts by its first element."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048:
        return value
    return None


class Marketplace(Protocol):
    name: str
    account: str   # seller company (domain.MAIN / SECOND); its orders carry the same

    async def new_orders(self) -> list[Order]: ...

    async def get_order(self, order_id: str) -> Order: ...

    async def ready_quantities(self) -> dict[tuple[str, str], int]:
        """(article, 1C warehouse name) -> qty already assembled, awaiting shipment."""
        ...

    async def is_awaiting(self, order: Order) -> bool:
        """Order is still waiting for assembly (fresh status from the API)."""
        ...

    async def is_ready(self, order: Order) -> bool:
        """Order is 'ready to ship' or further along (used to recover after a crash)."""
        ...

    async def confirm(self, order: Order) -> None: ...

    # Adapters also have `async photo_url(article) -> str | None`: the main product photo
    # for the order message, read-only, from the brand request's data.

    # Stock sync (src/orderbot/stock_sync.py), main company only:
    # `async stock_read(key, article) -> StockAt | None` (None: no such article there) and
    # `async stock_write(key, article, ref, count)`: one request, never retried. `key` is the
    # warehouses.yaml key (Ozon warehouse name, Yandex campaign id, WB warehouseId).

    # Shipment reminders (src/orderbot/shipments.py), Ozon and Yandex, read-only:
    # `shipment_warehouses() -> set[str]` (1C warehouses of the non-express mapping) and
    # `async to_ship(only=None) -> list[ToShip]` (orders not handed over yet; `only` limits
    # the 1C warehouses that need reading).

    async def cancel_by_human(self, order: Order) -> None:
        """Only for the human "Отменить" button (human_actions.py); the robot never calls it."""
        ...
