"""Stock sync on the other Альфа marketplace warehouses after a collected order."""
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from orderbot.domain import SECOND, Order, OrderLine
from orderbot.marketplaces.base import StockAt, StockError
from orderbot.stock_sync import StockSync, select_targets
from orderbot.store import Store

UTC = UTC

# the real layout of warehouses.yaml, shortened
WH = {
    "ozon": {"Южная 9": "Южная,9", "СПБ": "СПБ", "СЕВЕР": "СЕВЕР",
             "СЕВЕР EXPRESS 🚀": "СЕВЕР"},
    "yandex": {"900000008": "СЕВЕР", "900000009": "СЕВЕР", "900000010": "Южная,9",
               "900000014": "СПБ"},
    "wb": {"9000017": "Южная,9", "9000015": "СПБ", "9000019": "СЕВЕР"},
    "yandex_second": {"900000001": "СЕВЕР", "900000002": "Южная,9"},
    "ozon_second": {"Beta Sever": "СЕВЕР"},
}
GROUPS = {"900000008": ("g:900000008", "900000008", "Север"),
          "900000009": ("g:900000008", "900000008", "Север EXPRESS"),
          "900000010": ("900000010", "900000010", "Южная 9"),
          "900000014": ("900000014", "900000014", "СПБ")}


def order(mp="ozon", oid="1", wh="СЕВЕР", qty=1, article="A1", account="main",
          mp_warehouse="СЕВЕР", campaign=None, lines=None):
    return Order(mp, oid, lines or [OrderLine(article, qty, wh, Decimal("100"), None)],
                 datetime(2026, 11, 6, 9, tzinfo=UTC), None, "x", campaign=campaign,
                 mp_warehouse=mp_warehouse, account=account)


class FakeMp:
    def __init__(self, name, stock=None, fail_write=None, fail_read=None, groups=None):
        self.name = name
        self.stock = dict(stock or {})          # (key, article) -> count
        self.reads, self.writes = [], []
        self.fail_write, self.fail_read = fail_write or {}, fail_read or {}
        self.groups = groups

    async def stock_read(self, key, article):
        self.reads.append((key, article))
        await asyncio.sleep(0)   # a real read waits for the network
        if key in self.fail_read:
            raise self.fail_read[key]
        n = self.stock.get((key, article))
        return None if n is None else StockAt(n, ref=f"ref-{key}")

    async def stock_write(self, key, article, ref, count):
        await asyncio.sleep(0)
        if key in self.fail_write:
            raise self.fail_write[key]
        assert ref == f"ref-{key}"
        self.writes.append((key, article, count))
        self.stock[(key, article)] = count

    async def stock_groups(self):
        if isinstance(self.groups, Exception):
            raise self.groups
        return self.groups


class FakeNotifier:
    def __init__(self, fail=False):
        self.notes, self.fail = [], fail

    async def add_note(self, mp, oid, line):
        if self.fail:
            raise RuntimeError("telegram down")
        self.notes.append((mp, oid, line))
        return 1


def http_error(code):
    req = httpx.Request("PUT", "https://x")
    return httpx.HTTPStatusError(f"Client error '{code}'", request=req,
                                 response=httpx.Response(code, request=req))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.set_stocksync(True, "admin")
    s.set_stocksync(True, "admin", "yandex")
    return s


def adapters(**stock):
    return {"ozon": FakeMp("ozon", stock.get("ozon")),
            "yandex": FakeMp("yandex", stock.get("yandex"), groups=GROUPS),
            "wb": FakeMp("wb", stock.get("wb"))}


def make(store, mps, notifier=None):
    return StockSync(store, mps, WH, notifier or FakeNotifier())


# ------------------------------------------------------------- targets
def test_targets_skip_source_include_express_sibling_never_second():
    t = select_targets(order("ozon", mp_warehouse="СЕВЕР"), "СЕВЕР", WH)
    assert t == [("ozon", "СЕВЕР EXPRESS 🚀"), ("yandex", "900000008"),
                 ("yandex", "900000009"), ("wb", "9000019")]


def test_targets_ozon_express_order_decrements_plain_sibling():
    t = select_targets(order("ozon", mp_warehouse="СЕВЕР EXPRESS"), "СЕВЕР", WH)
    assert ("ozon", "СЕВЕР") in t and ("ozon", "СЕВЕР EXPRESS 🚀") not in t


def test_targets_yandex_and_wb_source_excluded():
    t = select_targets(order("yandex", campaign="900000010", wh="Южная,9"), "Южная,9", WH)
    assert t == [("ozon", "Южная 9"), ("wb", "9000017")]
    t = select_targets(order("wb", wh="СПБ", mp_warehouse="СПБ"), "СПБ", WH)
    assert t == [("ozon", "СПБ"), ("yandex", "900000014")]


def test_targets_unknown_source_skips_whole_marketplace():
    # an Ozon order without its warehouse name: no Ozon warehouse is touched
    t = select_targets(order("ozon", mp_warehouse=None), "СЕВЕР", WH)
    assert all(mp != "ozon" for mp, _ in t)




def Q(*lines):
    """The stock report: a collapsed quote."""
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"

# ------------------------------------------------------------- ON
async def test_on_writes_max_zero_cur_minus_qty_and_reports(store):
    mps = adapters(ozon={("СЕВЕР EXPRESS 🚀", "A1"): 5},
                   yandex={("900000008", "A1"): 1},
                   wb={("9000019", "A1"): 3})
    n = FakeNotifier()
    text = await make(store, mps, n).run(order(qty=2))
    assert mps["ozon"].writes == [("СЕВЕР EXPRESS 🚀", "A1", 3)]
    # Sever and Sever EXPRESS are one Yandex group: one write through the main campaign
    assert mps["yandex"].writes == [("900000008", "A1", 0)]
    assert mps["wb"].writes == [("9000019", "A1", 1)]
    assert text == Q("📉 <b>Остатки</b>", "<b>SKU:</b> A1", "Озон СЕВЕР EXPRESS 5→3",
                     "Маркет Север + Север EXPRESS 1→0", "WB СЕВЕР 3→1")
    assert n.notes == [("ozon", "1", text)]
    assert store.has_event("ozon", "1", "stock_synced")


async def test_on_no_article_or_zero_is_skipped(store):
    mps = adapters(ozon={("СЕВЕР EXPRESS 🚀", "A1"): 0}, wb={("9000019", "A1"): 2})
    text = await make(store, mps).run(order())
    assert mps["ozon"].writes == [] and mps["yandex"].writes == []
    assert mps["wb"].writes == [("9000019", "A1", 1)]
    assert text == Q("📉 <b>Остатки</b>", "<b>SKU:</b> A1", "Озон СЕВЕР EXPRESS — нет товара",
                     "Маркет Север + Север EXPRESS — нет товара", "WB СЕВЕР 2→1")


async def test_failures_are_listed_and_others_still_written(store):
    mps = adapters(ozon={("СЕВЕР EXPRESS 🚀", "A1"): 5}, wb={("9000019", "A1"): 2},
                   yandex={("900000008", "A1"): 4})
    mps["ozon"].fail_write = {"СЕВЕР EXPRESS 🚀": http_error(403)}
    mps["yandex"].fail_read = {"900000008": StockError("что-то <не так>")}
    text = await make(store, mps).run(order())
    assert mps["wb"].writes == [("9000019", "A1", 1)]
    assert text == Q("📉 <b>Остатки</b>", "<b>SKU:</b> A1",
                     "⚠️ не удалось: Озон СЕВЕР EXPRESS (HTTP 403)",
                     "⚠️ не удалось: Маркет Север + Север EXPRESS (что-то &lt;не так&gt;)",
                     "WB СЕВЕР 2→1")
    done = [e for e in store.events("ozon", "1") if e["event"] == "stock_sync_done"]
    assert len(done) == 1 and done[0]["payload"]["on"] is True


async def test_yandex_groups_unknown_refuses_yandex_only(store):
    mps = adapters(wb={("9000019", "A1"): 2}, yandex={("900000008", "A1"): 4})
    mps["yandex"].groups = StockError("нет YM_BUSINESS_ID: группы складов неизвестны")
    text = await make(store, mps).run(order())
    assert mps["yandex"].writes == [] and mps["yandex"].reads == []
    assert "Маркет (нет YM_BUSINESS_ID: группы складов неизвестны)" in text
    assert mps["wb"].writes == [("9000019", "A1", 1)]


async def test_yandex_order_from_group_member_skips_whole_group(store):
    mps = adapters(ozon={("СЕВЕР", "A1"): 5, ("СЕВЕР EXPRESS 🚀", "A1"): 5},
                   yandex={("900000008", "A1"): 4})
    await make(store, mps).run(order("yandex", campaign="900000009", mp_warehouse=None))
    assert mps["yandex"].reads == [] and mps["yandex"].writes == []
    assert sorted(w[0] for w in mps["ozon"].writes) == ["СЕВЕР", "СЕВЕР EXPRESS 🚀"]


async def test_two_lines_name_their_articles(store):
    lines = [OrderLine("A1", 1, "СПБ", None, None), OrderLine("B2", 1, "СПБ", None, None)]
    mps = adapters(ozon={("СПБ", "A1"): 2, ("СПБ", "B2"): 7})
    text = await make(store, mps).run(order("wb", mp_warehouse="СПБ", lines=lines))
    assert mps["ozon"].writes == [("СПБ", "A1", 1), ("СПБ", "B2", 6)]
    assert text == Q("📉 <b>Остатки</b>", "<b>SKU:</b> A1", "Озон СПБ 2→1",
                     "Маркет СПБ — нет товара", "<b>SKU:</b> B2", "Озон СПБ 7→6",
                     "Маркет СПБ — нет товара")


async def test_line_without_1c_warehouse_is_said(store):
    text = await make(store, adapters()).run(order(wh=None))
    assert "склад 1С не сопоставлен" in text


async def test_html_is_escaped_in_labels(store):
    wh = {"ozon": {"<b>X</b>": "W"}, "wb": {}, "yandex": {}}
    mps = adapters(ozon={("<b>X</b>", "A1"): 2})
    text = await StockSync(store, mps, wh, FakeNotifier()).run(
        order("wb", wh="W", mp_warehouse="W"))
    assert "Озон &lt;b&gt;X&lt;/b&gt; 2→1" in text


# ------------------------------------------------------------- OFF / once / scope
async def test_off_reads_but_writes_nothing(store):
    store.set_stocksync(False, "admin")
    mps = adapters(wb={("9000019", "A1"): 3})
    text = await make(store, mps).run(order())
    assert all(not m.writes for m in mps.values())
    assert text.startswith("<blockquote expandable>📉 <b>Уменьшил бы</b>")
    assert "\nWB СЕВЕР 3→2" in text
    assert store.has_event("ozon", "1", "stock_synced")


async def test_runs_once_per_order(store):
    mps = adapters(wb={("9000019", "A1"): 3})
    sync = make(store, mps)
    await sync.run(order())
    assert await sync.run(order()) is None
    assert await make(store, mps).run(order()) is None   # after a restart too
    assert mps["wb"].writes == [("9000019", "A1", 2)]


async def test_concurrent_runs_write_once(store):
    mps = adapters(wb={("9000019", "A1"): 3})
    sync = make(store, mps)
    await asyncio.gather(sync.run(order()), sync.run(order()))
    assert mps["wb"].writes == [("9000019", "A1", 2)]


async def test_second_company_never_triggers(store):
    mps = adapters(wb={("9000019", "A1"): 3})
    assert await make(store, mps).run(order(account=SECOND)) is None
    assert all(not m.reads for m in mps.values())
    assert not store.has_event("ozon", "1", "stock_synced")


async def test_errors_never_raise(store):
    mps = adapters(wb={("9000019", "A1"): 3})
    mps["wb"].fail_read = {"9000019": RuntimeError("boom")}
    text = await make(store, mps, FakeNotifier(fail=True)).run(order())
    assert "WB СЕВЕР (RuntimeError)" in text


async def test_schedule_runs_in_background(store):
    mps = adapters(wb={("9000019", "A1"): 3})
    sync = make(store, mps)
    assert sync.schedule(order()) is None
    await sync.wait_idle()
    assert mps["wb"].writes == [("9000019", "A1", 2)]


# ------------------------------------------------------------- edge cases
async def test_two_orders_of_one_article_both_decrement(store):
    mps = adapters(wb={("9000019", "A1"): 10})
    sync = make(store, mps)
    sync.schedule(order(oid="1", qty=2))
    sync.schedule(order(oid="2", qty=3))
    await sync.wait_idle()
    assert mps["wb"].writes == [("9000019", "A1", 8), ("9000019", "A1", 5)]
    assert mps["wb"].stock[("9000019", "A1")] == 5


async def test_yandex_writes_need_their_own_switch(store):
    store.set_stocksync(False, "admin", "yandex")
    mps = adapters(yandex={("900000008", "A1"): 4}, wb={("9000019", "A1"): 2})
    text = await make(store, mps).run(order())
    assert mps["yandex"].writes == [] and mps["wb"].writes == [("9000019", "A1", 1)]
    assert text == Q("📉 <b>Остатки</b>", "<b>SKU:</b> A1", "Озон СЕВЕР EXPRESS — нет товара",
                     "Маркет Север + Север EXPRESS 4→3 (не записано)", "WB СЕВЕР 2→1")


async def test_yandex_switch_alone_does_nothing_while_off(store):
    store.set_stocksync(False, "admin")
    mps = adapters(yandex={("900000008", "A1"): 4})
    text = await make(store, mps).run(order())
    assert mps["yandex"].writes == []
    assert text == Q("📉 <b>Уменьшил бы</b>", "<b>SKU:</b> A1", "Озон СЕВЕР EXPRESS — нет товара",
                     "Маркет Север + Север EXPRESS 4→3", "WB СЕВЕР — нет товара")
