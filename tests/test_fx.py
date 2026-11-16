from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import respx

from orderbot.fx import CBR_URL, Rates, parse_cbr

XML = ('<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="11.11.2026">'
       '<Valute ID="R01335"><NumCode>398</NumCode><CharCode>KZT</CharCode>'
       '<Nominal>100</Nominal><Name>T</Name><Value>18,3858</Value></Valute>'
       '<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode>'
       '<Nominal>1</Nominal><Name>D</Name><Value>81,0000</Value></Valute></ValCurs>'
       ).encode("cp1251")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 11, 7, tzinfo=UTC)

    def __call__(self):
        return self.now


def test_parse_divides_by_nominal_and_reads_the_decimal_comma():
    t = parse_cbr(XML)
    assert t["KZT"] == Decimal("0.183858") and t["USD"] == Decimal("81")


async def test_kzt_to_rubles_and_rubles_untouched():
    with respx.mock() as r:
        route = r.get(CBR_URL).mock(return_value=httpx.Response(200, content=XML))
        rates = Rates(clock=Clock())
        assert await rates.to_rub(Decimal("14309"), "KZT") == Decimal("2630.82")
        assert await rates.to_rub(Decimal("100"), "RUB") == Decimal("100")
        assert await rates.to_rub(Decimal("100"), None) == Decimal("100")
        assert await rates.to_rub(Decimal("1"), "kzt") == Decimal("0.18")
    assert route.call_count == 1          # one table for the day


async def test_no_rate_gives_no_price():
    with respx.mock() as r:
        r.get(CBR_URL).mock(return_value=httpx.Response(500))
        assert await Rates(clock=Clock()).to_rub(Decimal("14309"), "KZT") is None
    with respx.mock() as r:
        r.get(CBR_URL).mock(return_value=httpx.Response(200, content=XML))
        assert await Rates(clock=Clock()).to_rub(Decimal("5"), "XYZ") is None


async def test_stale_table_serves_while_the_site_is_down_but_not_forever():
    clock = Clock()
    rates = Rates(clock=clock)
    with respx.mock() as r:
        r.get(CBR_URL).mock(return_value=httpx.Response(200, content=XML))
        await rates.rate("KZT")
    with respx.mock() as r:
        r.get(CBR_URL).mock(side_effect=httpx.ConnectError("down"))
        clock.now += timedelta(days=1)
        assert await rates.rate("KZT") == Decimal("0.183858")
        clock.now += timedelta(days=3)
        assert await rates.rate("KZT") is None


async def test_numeric_codes_as_wb_gives_them():
    xml = XML.replace(b"</ValCurs>", ('<Valute ID="R01060"><NumCode>051</NumCode><CharCode>AMD'
                                       '</CharCode><Nominal>100</Nominal><Name>A</Name>'
                                       '<Value>21,0000</Value></Valute></ValCurs>').encode("cp1251"))
    with respx.mock() as r:
        r.get(CBR_URL).mock(return_value=httpx.Response(200, content=xml))
        rates = Rates(clock=Clock())
        assert await rates.to_rub(Decimal("22400"), "51") == Decimal("4704.00")
        assert rates.name(51) == "AMD" and rates.name("051") == "AMD"
        assert await rates.to_rub(Decimal("10"), "643") == Decimal("10")


def test_grouped():
    from orderbot.fx import grouped
    assert grouped(Decimal("14309")) == "14 309" and grouped(Decimal("1814.5")) == "1 814.50"
    assert grouped(Decimal("22400.00")) == "22 400"
