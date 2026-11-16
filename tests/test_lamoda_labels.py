"""Lamoda FBS orders with their item and package labels in the group topic."""
import asyncio
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from orderbot import lamoda_labels as ll
from orderbot.config import Config, load_config
from orderbot.lamoda_labels import (
    Item,
    LamodaApi,
    LamodaError,
    LamodaLabels,
    article,
    build_lamoda,
    label_plan,
    order_text,
)
from orderbot.store import Store

API = "https://api-b2b.lamoda.ru"
RPC = "https://seller.lamoda.ru/jsonrpc/v1"
PDF = b"%PDF-1.4 label"
CHAT, THREAD = -100500, 42
# a fixed «now»: 12:00 MSK
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)

SALAD = "MRS181-RIA(MRS181-00918W)"


def items_of(order_nr, *skus, names=None, status="Awaiting shipment"):
    names = names or ["Салатник BRAND-X"] * len(skus)
    return [{"id": f"{order_nr}-{i:03d}", "sku": s, "description": n, "status": status}
            for i, (s, n) in enumerate(zip(skus, names), 1)]


# ------------------------------------------------------------------ pure parts
def test_article_is_the_sku_before_parentheses():
    assert article(SALAD) == "MRS181-RIA"
    assert article("PLAIN-1") == "PLAIN-1"
    assert article("  A B (x)") == "A B"


def test_order_text_matches_the_agreed_format():
    items = [Item("RU000000-000001-001", SALAD, "Салатник BRAND-X"),
             Item("RU000000-000001-002", SALAD, "Салатник BRAND-X")]
    assert order_text("RU000000-000001", items) == (
        "🚚 <b>Номер заказа: № RU000000-000001</b>\n\n"
        "1. MRS181-RIA(MRS181-00918W)(Салатник BRAND-X) x 1\n\n"
        "2. MRS181-RIA(MRS181-00918W)(Салатник BRAND-X) x 1\n\n"
        "<b>Всего товаров:</b> 2 шт.")


def test_order_text_two_second_articles():
    items = [Item("RU1-001", "RTC111-ARD(RTC111-VC7191)", "Кружка BrandX"),
             Item("RU1-002", "RTP222-ARD(RTP222-VC7191)", "Тарелка BrandX")]
    assert order_text("RU1", items) == (
        "🚚 <b>Номер заказа: № RU1</b>\n\n"
        "1. RTC111-ARD(RTC111-VC7191)(Кружка BrandX) x 1\n\n"
        "2. RTP222-ARD(RTP222-VC7191)(Тарелка BrandX) x 1\n\n"
        "<b>Всего товаров:</b> 2 шт.")


def test_label_plan_numbers_units_of_one_article_and_maps_packs_by_item():
    items = [Item("RU1-001", SALAD, "С"), Item("RU1-002", SALAD, "С")]
    # Lamoda lists the packs in any order: the pack is found by the item it holds
    packs = {"FBS-B": ["RU1-002"], "FBS-A": ["RU1-001"]}
    plan = label_plan(items, packs)
    assert [(l.filename, l.kind, l.number) for l in plan] == [
        ("MRS181-RIA пакет 1.pdf", "pack", "FBS-A"),
        ("MRS181-RIA товар 1.pdf", "item", "RU1-001"),
        ("MRS181-RIA пакет 2.pdf", "pack", "FBS-B"),
        ("MRS181-RIA товар 2.pdf", "item", "RU1-002")]


def test_label_plan_single_units_have_no_number():
    items = [Item("RU1-001", "A-1(x)", "a"), Item("RU1-002", "B-2(y)", "b"),
             Item("RU1-003", "A-1(x)", "a")]
    packs = {"P1": ["RU1-001"], "P2": ["RU1-002"], "P3": ["RU1-003"]}
    assert [l.filename for l in label_plan(items, packs)] == [
        "A-1 пакет 1.pdf", "A-1 товар 1.pdf", "B-2 пакет.pdf", "B-2 товар.pdf",
        "A-1 пакет 2.pdf", "A-1 товар 2.pdf"]


def test_label_plan_one_pack_with_two_items_gets_one_package_label():
    items = [Item("RU1-001", "A-1(x)", "a"), Item("RU1-002", "B-2(y)", "b")]
    plan = label_plan(items, {"P1": ["RU1-001", "RU1-002"]})
    assert [(l.filename, l.number) for l in plan] == [
        ("A-1 пакет.pdf", "P1"), ("A-1 товар.pdf", "RU1-001"), ("B-2 товар.pdf", "RU1-002")]


def test_label_plan_item_without_pack_is_an_error():
    with pytest.raises(LamodaError, match="пакет"):
        label_plan([Item("RU1-001", "A(x)", "a")], {"P1": ["RU1-999"]})


# ------------------------------------------------------------------ fakes
class FakeBot:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.fail = set()   # names of methods that raise

    def _msg(self):
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    async def send_message(self, chat_id, text, **kw):
        if "send_message" in self.fail:
            raise RuntimeError("group unreachable")
        m = self._msg()
        self.calls.append(("message", chat_id, text, kw, m.message_id))
        return m

    async def send_media_group(self, chat_id, media, **kw):
        if "send_media_group" in self.fail:
            raise RuntimeError("group unreachable")
        names = [x.media.filename for x in media]
        datas = [x.media.data for x in media]
        self.calls.append(("group", chat_id, names, kw, datas))
        return [self._msg() for _ in media]

    async def send_document(self, chat_id, document, **kw):
        if "send_document" in self.fail:
            raise RuntimeError("group unreachable")
        self.calls.append(("document", chat_id, [document.filename], kw, [document.data]))
        return self._msg()

    async def edit_message_text(self, **kw):
        self.calls.append(("edit", kw))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class Lamoda:
    """respx routes of a Lamoda account with the given orders."""

    def __init__(self, router, orders):
        self.orders = orders    # list of dicts: nr, created, items, packs, status
        self.label_url = "https://storage.yandexcloud.net/l.pdf"
        self.pdf = PDF
        self.label_fail = False
        router.post(f"{API}/auth/token").mock(side_effect=self.token)
        router.get(f"{API}/api/v1/orders").mock(side_effect=self.list)
        router.get(url__regex=rf"{API}/api/v1/orders/[^/?]+$").mock(side_effect=self.detail)
        router.post(f"{RPC}/fbs/order/pack.list").mock(side_effect=self.packs)
        router.post(f"{RPC}/label-items.get").mock(side_effect=self.label)
        router.post(f"{RPC}/label-packs.get").mock(side_effect=self.label)
        self.download = router.get(url__startswith="https://storage.yandexcloud.net/").mock(
            side_effect=lambda req: httpx.Response(200, content=self.pdf))
        self.tokens = 0
        self.label_calls = []

    def token(self, req):
        self.tokens += 1
        return httpx.Response(200, json={"access_token": f"t{self.tokens}", "expires_in": 900})

    def list(self, req):
        assert req.headers["authorization"].startswith("Bearer t")
        page = int(req.url.params["page"])
        limit = int(req.url.params["limit"])
        rows = sorted(self.orders, key=lambda o: o["created"], reverse=True)
        chunk = rows[(page - 1) * limit: page * limit]
        return httpx.Response(200, json={
            "page": page, "pages": max(1, -(-len(rows) // limit)), "total": len(rows),
            "_embedded": {"orders": [{
                "orderNr": o["nr"], "id": o["nr"], "status": o.get("status", "Awaiting shipment"),
                "itemQuantity": len(o["items"]), "createdAt": o["created"],
                "_embedded": {"partner": {"partnerId": 990}, "customer": {"name": "SECRET"}},
            } for o in chunk]}})

    def detail(self, req):
        nr = req.url.path.rsplit("/", 1)[1]
        o = next(o for o in self.orders if o["nr"] == nr)
        return httpx.Response(200, json={"orderNr": nr, "_embedded": {"items": o["items"]}})

    def packs(self, req):
        body = json.loads(req.content)
        assert body["method"] == "v1.fbs.order.pack.list"
        p = body["params"]
        assert p["partner_id"] == 990
        data = [{"order_nr": o["nr"], "pack_number": pn, "item_nrs": nrs}
                for o in self.orders if o["nr"] in p["order_nrs"]
                for pn, nrs in o["packs"].items()]
        page, limit = p.get("page", 1), p.get("limit", 10)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "data": data[(page - 1) * limit: page * limit],
            "pagination": {"limit": limit, "page": page, "total": len(data)}}})

    def label(self, req):
        body = json.loads(req.content)
        self.label_calls.append((body["method"], body["params"]))
        url = "" if self.label_fail else self.label_url
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"url": url}})


def order(nr, created, *skus, packs=None, **kw):
    its = items_of(nr, *skus)
    packs = packs or {f"FBS-{it['id']}": [it["id"]] for it in its}
    return {"nr": nr, "created": created, "items": its, "packs": packs, **kw}


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "orderbot.db")
    store.lamoda_set_baseline([])   # past the first run; first-run tests use a fresh store
    bot = FakeBot()
    alerts = []

    async def say(text):
        alerts.append(text)

    def make(clock=lambda: NOW, store=None):
        api = LamodaApi("cid", "csecret")
        return LamodaLabels(api, store or env_ns.store, bot, CHAT, THREAD, clock=clock,
                            say=say)

    env_ns = SimpleNamespace(store=store, bot=bot, make=make, alerts=alerts, path=tmp_path)
    return env_ns


# ------------------------------------------------------------------ the poll
async def test_message_and_labels_as_replies_in_the_topic(env):
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU000000-000001", "2026-11-06", SALAD, SALAD)])
        await env.make().poll()
    [msg] = env.bot.of("message")
    _, chat, text, kw, mid = msg
    assert chat == CHAT and kw["message_thread_id"] == THREAD
    assert kw["parse_mode"] == "HTML"  # bold heading and total
    assert text.startswith("🚚 <b>Номер заказа: № RU000000-000001</b>\n\n1. MRS181-RIA(")
    assert text.endswith("<b>Всего товаров:</b> 2 шт.") and "⚠️" not in text
    [group] = env.bot.of("group")
    _, chat, names, kw, datas = group
    assert chat == CHAT and kw["message_thread_id"] == THREAD
    assert kw["reply_parameters"].message_id == mid
    assert names == ["MRS181-RIA пакет 1.pdf", "MRS181-RIA товар 1.pdf",
                     "MRS181-RIA пакет 2.pdf", "MRS181-RIA товар 2.pdf"]
    assert datas == [PDF] * 4
    assert ("v1.label-items.get", {"item_nrs": ["RU000000-000001-001"], "size": "s"}) \
        in lam.label_calls
    assert ("v1.label-packs.get", {"pack_numbers": ["FBS-RU000000-000001-001"], "size": "m"}) \
        in lam.label_calls
    assert "SECRET" not in json.dumps([str(c) for c in env.bot.calls])
    assert not env.bot.of("edit")


async def test_exactly_once_across_restarts(env):
    with respx.mock(assert_all_called=False) as r:
        Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        await env.make().poll()
        await env.make().poll()          # a restart: a new object on the same database
        await env.make(store=Store(env.path / "orderbot.db")).poll()
    assert len(env.bot.of("message")) == 1 and len(env.bot.of("group")) == 1


async def test_first_run_posts_nothing_awaiting_then_posts_new_orders(tmp_path, env):
    """Nothing currently awaiting is posted on the first start (whatever its date); an order
    appearing on the next poll is posted."""
    store = Store(tmp_path / "fresh.db")
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU-OLD", "2026-11-05", "A-1(x)"),
                         order("RU-TODAY", "2026-11-06", "A-1(x)")])
        await env.make(store=store).poll()
        assert env.bot.calls == [] and lam.label_calls == []
        assert store.lamoda_known("RU-OLD") and store.lamoda_known("RU-TODAY")
        lam.orders.append(order("RU-NEW", "2026-11-05", "B-2(y)"))
        await env.make(store=store).poll()
    texts = [c[2] for c in env.bot.of("message")]
    assert len(texts) == 1 and "RU-NEW" in texts[0]


async def test_first_run_rule_applies_once_even_with_no_orders(tmp_path, env):
    store = Store(tmp_path / "fresh.db")
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [])
        await env.make(store=store).poll()
        lam.orders.append(order("RU-Y", "2026-11-05", "A-1(x)"))
        await env.make(store=store).poll()
    assert len(env.bot.of("message")) == 1


async def test_first_run_baseline_only_after_a_good_list_read(tmp_path, env):
    store = Store(tmp_path / "fresh.db")
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{API}/auth/token").mock(return_value=httpx.Response(500))
        await env.make(store=store).poll()
    assert not store.lamoda_baseline_done()


async def test_oldest_order_first_and_only_awaiting_items(env):
    o = order("RU2", "2026-11-06", "A-1(x)", "B-2(y)")
    o["items"][1]["status"] = "Canceled by seller"   # a dropped unit of the order
    with respx.mock(assert_all_called=False) as r:
        Lamoda(r, [order("RU1", "2026-11-06", "C-3(z)"), o])
        await env.make().poll()
    texts = [c[2] for c in env.bot.of("message")]
    # Lamoda lists newest first (RU1 is above RU2): posted in reverse, the oldest first
    assert len(texts) == 2 and "RU2" in texts[0] and "RU1" in texts[1]
    assert "B-2" not in texts[0] and "<b>Всего товаров:</b> 1 шт." in texts[0]


async def test_label_failure_adds_warning_then_retries_later(env):
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        lam.label_fail = True
        await env.make().poll()
        [msg] = env.bot.of("message")
        assert "⚠️" not in msg[2]                       # the message went first
        [warn] = env.bot.of("edit")
        assert warn[1]["message_id"] == msg[4]
        assert warn[1]["text"] == msg[2] + \
            "\n\n⚠️ Этикетки не получены: Lamoda не дала ссылку на этикетку"
        assert not env.bot.of("group")
        await env.make().poll()            # still failing: no new message, no new edit
        assert len(env.bot.of("message")) == 1 and len(env.bot.of("edit")) == 1
        lam.label_fail = False
        await env.make().poll()
        await env.make().poll()
    assert len(env.bot.of("message")) == 1
    [group] = env.bot.of("group")
    assert group[2] == ["A-1 пакет.pdf", "A-1 товар.pdf"]
    assert group[3]["reply_parameters"].message_id == msg[4]
    # the warning goes away from the order message once the labels are there
    edits = env.bot.of("edit")
    assert len(edits) == 2 and edits[1][1]["text"] == msg[2]


async def test_warning_line_is_a_fixed_text_not_the_servers_words(env):
    with respx.mock(assert_all_called=False) as r:
        Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        r.post(f"{RPC}/fbs/order/pack.list").mock(return_value=httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1", "error": {"code": -1, "message": "internal ip 10.0.0.1"}}))
        await env.make().poll()
    [edit] = env.bot.of("edit")
    assert edit[1]["text"].endswith("⚠️ Этикетки не получены: Lamoda не отдала пакеты заказа")
    assert "10.0.0.1" not in edit[1]["text"]


async def test_labels_given_up_after_max_attempts_with_one_reply(env, monkeypatch):
    monkeypatch.setattr(ll, "MAX_LABEL_ATTEMPTS", 3)
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        lam.label_fail = True
        for _ in range(6):
            await env.make().poll()
    msgs = env.bot.of("message")
    assert len(msgs) == 2
    assert "кабинете Lamoda" in msgs[1][2]
    assert msgs[1][3]["reply_parameters"].message_id == msgs[0][4]
    assert len(lam.label_calls) == 3


async def test_bad_pdf_is_a_label_failure(env):
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        lam.pdf = b"<html>not a pdf</html>"
        await env.make().poll()
    [edit] = env.bot.of("edit")
    assert "⚠️ Этикетки не получены: этикетка не скачалась" in edit[1]["text"]
    assert not env.bot.of("group")


async def test_label_on_a_foreign_host_is_a_label_failure(env):
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        lam.label_url = "https://evil.example.com/l.pdf"
        r.get(url__startswith="https://evil.example.com/").mock(
            return_value=httpx.Response(200, content=PDF))
        await env.make().poll()
    assert not env.bot.of("group") and len(env.bot.of("edit")) == 1


async def test_group_unreachable_no_downloads_retries_next_poll_and_never_goes_private(env):
    env.bot.fail.add("send_message")
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        await env.make().poll()
        assert env.bot.calls == [] and lam.label_calls == [] and not lam.download.called
        env.bot.fail.clear()
        await env.make().poll()
    [msg] = env.bot.of("message")
    assert msg[1] == CHAT and len(env.bot.of("group")) == 1
    assert env.alerts == []


async def test_group_unreachable_for_long_tells_the_admins_once(env, monkeypatch):
    monkeypatch.setattr(ll, "GROUP_ALERT_AFTER", 3)
    env.bot.fail.add("send_message")
    with respx.mock(assert_all_called=False) as r:
        Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        lab = env.make()
        for _ in range(6):
            await lab.poll()
    assert len(env.alerts) == 1 and "группа недоступна" in env.alerts[0]


async def test_documents_failing_are_resent_next_poll_without_a_second_message(env):
    env.bot.fail.add("send_media_group")
    with respx.mock(assert_all_called=False) as r:
        Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        await env.make().poll()
        env.bot.fail.clear()
        await env.make().poll()
        await env.make().poll()
    assert len(env.bot.of("message")) == 1 and len(env.bot.of("group")) == 1
    assert not env.bot.of("edit")    # no warning was shown, nothing to edit


async def test_a_retry_sends_only_the_files_not_yet_sent(env):
    calls = {"n": 0}
    real = FakeBot.send_media_group

    async def flaky(self, chat_id, media, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("network")
        return await real(self, chat_id, media, **kw)

    with respx.mock(assert_all_called=False) as r:
        Lamoda(r, [order("RU1", "2026-11-06", *["A-1(x)"] * 6, "B-2(y)", "C-3(z)")])
        env.bot.send_media_group = flaky.__get__(env.bot)
        await env.make().poll()        # first group of 10 sent, the second fails
        await env.make().poll()
    groups = env.bot.of("group")
    assert [len(g[2]) for g in groups] == [10, 6]
    assert groups[1][2][0] == "A-1 пакет 6.pdf"
    assert env.store.lamoda_pending() == []


async def test_changed_label_plan_after_partial_send_gives_up_once(env):
    with respx.mock(assert_all_called=False) as r:
        lam = Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
        env.bot.fail.add("send_media_group")
        await env.make().poll()
        env.store.lamoda_update("RU1", sent_files=["Z-9 пакет.pdf"])   # sent before, gone now
        env.bot.fail.clear()
        await env.make().poll()
        await env.make().poll()
    msgs = env.bot.of("message")
    assert len(msgs) == 2 and "изменился" in msgs[1][2]
    assert not env.bot.of("group") and env.store.lamoda_pending() == []


async def test_more_than_ten_documents_go_in_several_groups(env):
    with respx.mock(assert_all_called=False) as r:
        Lamoda(r, [order("RU1", "2026-11-06", *["A-1(x)"] * 6)])
        await env.make().poll()
    groups = env.bot.of("group")
    assert [len(g[2]) for g in groups] == [10, 2]
    assert groups[1][2] == ["A-1 пакет 6.pdf", "A-1 товар 6.pdf"]


def test_chunks_never_leave_a_single_document_in_a_group():
    assert [len(c) for c in ll.document_chunks(list(range(11)))] == [10, 1]
    assert [len(c) for c in ll.document_chunks(list(range(4)))] == [4]


async def test_a_single_document_chunk_goes_as_document(env):
    bot = env.bot
    lab = env.make()
    await lab._send_documents(7, [("a.pdf", PDF)] * 11)
    assert [c[0] for c in bot.calls] == ["group", "document"]
    assert bot.calls[1][3]["reply_parameters"].message_id == 7
    assert bot.calls[1][3]["message_thread_id"] == THREAD


async def test_list_failure_is_logged_and_alerted_once_after_many_polls(env, monkeypatch):
    monkeypatch.setattr(ll, "ALERT_AFTER_FAILURES", 2)
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{API}/auth/token").mock(return_value=httpx.Response(500, text="csecret oops"))
        lab = env.make()
        for _ in range(5):
            await lab.poll()
    assert len(env.alerts) == 1 and "Lamoda" in env.alerts[0]
    assert "csecret" not in env.alerts[0]
    assert env.bot.calls == []


async def test_no_secret_or_signed_link_in_any_log_record(env, caplog):
    """httpx logs request addresses at INFO; the robot quiets it, and the token request
    keeps the secret in its body. Our own lines never carry the label links."""
    from orderbot.main import quiet_http_logs
    quiet_http_logs()
    signed = "https://storage.yandexcloud.net/l.pdf?X-Amz-Signature=SIGNED"
    with caplog.at_level("DEBUG", logger="orderbot"), caplog.at_level("INFO"):
        with respx.mock(assert_all_called=False) as r:
            lam = Lamoda(r, [order("RU1", "2026-11-06", "A-1(x)")])
            lam.label_url = signed
            await env.make().poll()
            lam.orders.append(order("RU2", "2026-11-06", "B-2(y)"))
            lam.label_fail = True
            await env.make().poll()
            token_req = r.calls[0].request
    assert "csecret" not in str(token_req.url) and b"csecret" in token_req.content
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "csecret" not in text and "SIGNED" not in text and "X-Amz" not in text
    assert len(env.bot.of("group")) == 1


def test_quiet_http_logs_raises_httpx_to_warning():
    import logging

    from orderbot.main import quiet_http_logs
    quiet_http_logs()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


# ------------------------------------------------------------------ API client
async def test_token_is_cached_and_refreshed_on_401():
    api = LamodaApi("cid", "csecret")
    seen = []
    with respx.mock(assert_all_called=False) as r:
        tok = r.post(f"{API}/auth/token").mock(side_effect=[
            httpx.Response(200, json={"access_token": "t1", "expires_in": 900}),
            httpx.Response(200, json={"access_token": "t2", "expires_in": 900})])

        def detail(req):
            seen.append(req.headers["authorization"])
            if len(seen) == 2:
                return httpx.Response(401)
            return httpx.Response(200, json={"_embedded": {"items": []}})

        r.get(f"{API}/api/v1/orders/RU1").mock(side_effect=detail)
        await api.items("RU1")
        await api.items("RU1")
    assert tok.call_count == 2
    assert seen == ["Bearer t1", "Bearer t1", "Bearer t2"]
    body = tok.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in body and "client_secret=csecret" in body
    assert "csecret" not in str(tok.calls[0].request.url)


async def test_errors_never_carry_the_secret():
    api = LamodaApi("cid", "csecret")
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{API}/auth/token").mock(return_value=httpx.Response(403))
        with pytest.raises(LamodaError) as e:
            await api.items("RU1")
    assert "csecret" not in str(e.value) and "403" in str(e.value)


async def test_jsonrpc_error_is_a_lamoda_error():
    api = LamodaApi("cid", "csecret")
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{API}/auth/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 900}))
        r.post(f"{RPC}/label-packs.get").mock(return_value=httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1", "error": {"code": -32000, "message": "boom"}}))
        with pytest.raises(LamodaError):
            await api.label_url("pack", "P1")


async def test_packs_are_read_page_by_page():
    api = LamodaApi("cid", "csecret")
    pages = []
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{API}/auth/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 900}))

        def packs(req):
            p = json.loads(req.content)["params"]
            pages.append(p["page"])
            assert p["order_nrs"] == ["RU1"] and "order_nr" not in p
            data = [{"order_nr": "RU1", "pack_number": f"P{i}", "item_nrs": [f"RU1-{i}"]}
                    for i in range(1, 4)]
            lim = p["limit"]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {
                "data": data[(p["page"] - 1) * lim: p["page"] * lim],
                "pagination": {"limit": lim, "page": p["page"], "total": 3}}})

        r.post(f"{RPC}/fbs/order/pack.list").mock(side_effect=packs)
        monkey = ll.PACK_PAGE
        ll.PACK_PAGE = 2
        try:
            got = await api.packs(990, "RU1")
        finally:
            ll.PACK_PAGE = monkey
    assert got == {"P1": ["RU1-1"], "P2": ["RU1-2"], "P3": ["RU1-3"]}
    assert pages == [1, 2]


async def test_awaiting_orders_stop_at_the_lookback():
    api = LamodaApi("cid", "csecret")
    asked = []
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{API}/auth/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 900}))

        def lst(req):
            page = int(req.url.params["page"])
            asked.append(page)
            created = {1: "2026-11-06", 2: "2026-10-01", 3: "2026-09-01"}[page]
            return httpx.Response(200, json={"page": page, "pages": 3, "total": 3,
                                             "_embedded": {"orders": [{
                                                 "orderNr": f"RU{page}", "createdAt": created,
                                                 "status": "Awaiting shipment",
                                                 "_embedded": {"partner": {"partnerId": 1}}}]}})

        r.get(f"{API}/api/v1/orders").mock(side_effect=lst)
        got = await api.awaiting_orders(date(2026, 11, 6))
    assert asked == [1, 2] and [o.nr for o in got] == ["RU1"]


@pytest.mark.parametrize("url,resp", [
    ("http://storage.yandexcloud.net/l.pdf", httpx.Response(200, content=PDF)),
    ("https://storage.yandexcloud.net/l.pdf", httpx.Response(200, content=b"PK zip")),
    ("https://storage.yandexcloud.net/l.pdf",
     httpx.Response(302, headers={"location": "https://evil.example/x.pdf"})),
    ("https://storage.yandexcloud.net/l.pdf", httpx.Response(404)),
    ("https://storage.example.net/l.pdf", httpx.Response(200, content=PDF)),
    ("https://yandexcloud.net.evil.ru/l.pdf", httpx.Response(200, content=PDF)),
])
async def test_download_refuses_unsafe_or_wrong_files(url, resp):
    api = LamodaApi("cid", "csecret")
    with respx.mock(assert_all_called=False) as r:
        r.get(url__startswith="http").mock(return_value=resp)
        with pytest.raises(LamodaError):
            await api.download_pdf(url)


async def test_download_has_a_size_cap(monkeypatch):
    monkeypatch.setattr(ll, "MAX_PDF_BYTES", 10)
    api = LamodaApi("cid", "csecret")
    with respx.mock(assert_all_called=False) as r:
        r.get("https://s.yandexcloud.net/l.pdf").mock(
            return_value=httpx.Response(200, content=PDF + b"x" * 100))
        with pytest.raises(LamodaError):
            await api.download_pdf("https://s.yandexcloud.net/l.pdf")


# ------------------------------------------------------------------ config & wiring
def test_config_reads_lamoda_settings(tmp_path):
    cfg = load_config({"LAMODA_CLIENT_ID": "c", "LAMODA_CLIENT_SECRET": "s",
                       "LAMODA_THREAD_ID": "77"}, warehouses_path=tmp_path / "none.yaml")
    assert (cfg.lamoda_client_id, cfg.lamoda_client_secret, cfg.lamoda_thread_id) == \
        ("c", "s", 77)
    assert load_config({}, warehouses_path=tmp_path / "none.yaml").lamoda_thread_id is None


@pytest.mark.parametrize("missing", ["lamoda_client_id", "lamoda_client_secret",
                                     "lamoda_thread_id", "alert_chat_id"])
def test_feature_off_without_any_setting(tmp_path, caplog, missing):
    full = dict(lamoda_client_id="c", lamoda_client_secret="s", lamoda_thread_id=77,
                alert_chat_id=CHAT)
    full[missing] = {"lamoda_thread_id": None, "alert_chat_id": None}.get(missing, "")
    with caplog.at_level("INFO"):
        got = build_lamoda(Config(**full), Store(tmp_path / "db"), FakeBot(), None)
    assert got is None
    assert sum("Lamoda" in r.getMessage() for r in caplog.records) == 1


def test_feature_on_with_all_settings(tmp_path):
    cfg = Config(lamoda_client_id="c", lamoda_client_secret="s", lamoda_thread_id=77,
                 alert_chat_id=CHAT)
    assert isinstance(build_lamoda(cfg, Store(tmp_path / "db"), FakeBot(), None), LamodaLabels)


async def test_run_loop_survives_errors_and_stops(env, monkeypatch):
    lab = env.make()
    polls = []

    async def poll():
        polls.append(1)
        if len(polls) == 1:
            raise RuntimeError("boom")
        stop.set()

    monkeypatch.setattr(lab, "poll", poll)
    stop = asyncio.Event()
    await asyncio.wait_for(lab.run(stop, interval=0.01), 2)
    assert len(polls) == 2


def test_order_text_without_a_name_has_no_empty_parentheses():
    assert order_text("RU1", [Item("RU1-001", "A-1(x)", "")]) == (
        "🚚 <b>Номер заказа: № RU1</b>\n\n1. A-1(x) x 1\n\n<b>Всего товаров:</b> 1 шт.")


def test_non_numeric_thread_id_turns_the_labels_off_not_the_robot(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        cfg = load_config({"LAMODA_CLIENT_ID": "c", "LAMODA_CLIENT_SECRET": "s",
                           "LAMODA_THREAD_ID": "тема"}, warehouses_path=tmp_path / "none.yaml")
    assert cfg.lamoda_thread_id is None
    assert any("LAMODA_THREAD_ID" in r.getMessage() for r in caplog.records)
    assert build_lamoda(cfg, Store(tmp_path / "db"), FakeBot(), None) is None


def test_order_text_escapes_html():
    items = [Item("RU1-001", "A<1>&B", "Бокал <Italesse>")]
    text = order_text("RU1", items)
    assert "1. A&lt;1&gt;&amp;B(Бокал &lt;Italesse&gt;) x 1" in text



async def test_list_outage_and_recovery_go_to_the_health_topic(env, monkeypatch):
    """with the health topic set, the outage and its end go there (HTML)."""
    monkeypatch.setattr(ll, "ALERT_AFTER_FAILURES", 2)
    health = []

    async def to_health(text):
        health.append(text)

    with respx.mock(assert_all_called=False) as r:
        token = r.post(f"{API}/auth/token").mock(
            return_value=httpx.Response(500, text="csecret oops"))
        lab = LamodaLabels(LamodaApi("cid", "csecret"), env.store, env.bot, CHAT, THREAD,
                           clock=lambda: NOW, say=None, health=to_health)
        for _ in range(3):
            await lab.poll()
        assert len(health) == 1 and health[0].startswith("🔴 <b>Lamoda — API не отвечает</b>")
        assert "csecret" not in health[0]
        token.mock(return_value=httpx.Response(200, json={"access_token": "t"}))
        r.get(f"{API}/api/v1/orders").mock(return_value=httpx.Response(
            200, json={"page": 1, "pages": 1, "total": 0, "_embedded": {"orders": []}}))
        await lab.poll()
        await lab.poll()
    assert health[1:] == ["🟢 <b>Lamoda — API снова отвечает</b>\nЭтикетки снова приходят"]
    assert env.alerts == []
