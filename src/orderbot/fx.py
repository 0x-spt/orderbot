"""Ruble value of a price paid in another currency (Ozon sells to Kazakhstan,
customer_price comes in KZT). Rates are the Bank of Russia's daily ones (read-only GET of
the public XML_daily). No rate means no price: the order goes to a person, a foreign amount
is never taken for rubles."""
import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

log = logging.getLogger(__name__)

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
RUBLE = {"RUB", "RUR", "643", "810"}   # letter and numeric ISO codes of the ruble
FRESH_FOR = timedelta(hours=6)       # fetched again after this
USABLE_FOR = timedelta(days=3)       # a stale table still serves when the site is down
CENT = Decimal("0.01")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def parse_cbr(xml: bytes) -> dict[str, Decimal]:
    """CharCode and NumCode (no leading zeros) -> rubles per one unit (Value / Nominal,
    decimal comma). WB gives the numeric ISO code (51 = AMD)."""
    root = ET.fromstring(xml)
    out = {}
    for v in root.iter("Valute"):
        try:
            code = (v.findtext("CharCode") or "").strip().upper()
            nominal = Decimal((v.findtext("Nominal") or "").strip())
            value = Decimal((v.findtext("Value") or "").strip().replace(",", "."))
        except InvalidOperation:
            continue
        if code and nominal > 0 and value > 0:
            out[code] = value / nominal
            num = (v.findtext("NumCode") or "").strip().lstrip("0")
            if num:
                out[num] = value / nominal
                out[f"#{num}"] = code    # numeric -> letter code, for showing it
    return out


class Rates:
    def __init__(self, url: str = CBR_URL, *, clock=_utc_now, timeout: float = 15.0):
        self._url = url
        self._clock = clock
        self._timeout = timeout
        self._table: dict[str, Decimal] = {}
        self._at: datetime | None = None

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(self._url)
            r.raise_for_status()
        table = parse_cbr(r.content)
        if not table:
            raise ValueError("empty rate table")
        self._table, self._at = table, self._clock()

    async def rate(self, code: str) -> Decimal | None:
        code = (code or "").strip().upper()
        code = code.lstrip("0") or code
        if code in RUBLE:
            return Decimal(1)
        now = self._clock()
        if self._at is None or now - self._at > FRESH_FOR:
            try:
                await self._fetch()
            except Exception as e:
                log.warning("currency rates not fetched: %s", type(e).__name__)
        if self._at is None or now - self._at > USABLE_FOR:
            return None
        rate = self._table.get(code)
        return rate if isinstance(rate, Decimal) else None

    def name(self, code) -> str:
        """Letter code for a numeric one when the table knows it (51 -> AMD)."""
        c = str(code).strip().lstrip("0")
        found = self._table.get(f"#{c}")
        return found if isinstance(found, str) else str(code)

    async def to_rub(self, amount: Decimal | None, code: str | None) -> Decimal | None:
        if amount is None:
            return None
        if not code:
            return amount         # no currency given: rubles
        rate = await self.rate(code)
        if rate is None:
            return None
        return (amount * rate).quantize(CENT)


def grouped(amount: Decimal) -> str:
    """14309 -> "14 309", 1814.5 -> "1 814.50": a foreign amount as people read it."""
    q = (amount.quantize(Decimal(1)) if amount == amount.to_integral()
         else amount.quantize(CENT))
    whole, _, frac = f"{q:f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return sign + " ".join(groups) + (f".{frac}" if frac else "")
