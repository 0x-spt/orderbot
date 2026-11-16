import numpy as np
import pytest
from PIL import Image
from test_reader import (
    FIXTURES,
    LAYOUT_PATH,
    STOCK_FRAME_OFFSET_Y,
    blank_row,
    copy_row,
    raw_stock,
    whiten,
)

from orderbot.domain import ItemRead
from orderbot.screen import lookup as lookup_mod
from orderbot.screen.guard import Guard
from orderbot.screen.lookup import ArticleNotFound, lookup, search_key
from orderbot.screen.reader import Layout, ReadUncertain, UnexpectedScreen

ARTICLE = "DEN091-CRM(DEN091-00422U)"
KEY = "den091-00422u"
OTHER = "DEN091-CRM(DEN091-OTHER)"
STATE = {}


@pytest.fixture(scope="module")
def layout():
    return Layout.load(LAYOUT_PATH)


@pytest.fixture(scope="module")
def stock(layout):
    w, h = layout.size
    return raw_stock().crop((0, STOCK_FRAME_OFFSET_Y, w, STOCK_FRAME_OFFSET_Y + h))


@pytest.fixture(scope="module")
def prices():
    return Image.open(FIXTURES / "onec_prices.png").convert("RGB")


def with_dot(img, point):
    img = img.copy()
    img.putpixel(point, (255, 0, 0))
    return img


@pytest.fixture(scope="module")
def corner(layout):
    r = layout.rects["stock_table"]
    return (r[2] - 1, r[3] - 1)


@pytest.fixture
def old_table(stock, corner):
    """The stock tab of the previously looked up article: another table."""
    return with_dot(stock, corner)


@pytest.fixture
def empty(old_table, layout):
    """Before the search: the stock tab with the previous article's table, no result row."""
    img = old_table.copy()
    blank_row(img, layout.grids["results"], 0)
    return img


@pytest.fixture
def fresh_row(stock, layout):
    """The found row, repainted (one pixel differs from `stock` in the row crop)."""
    x, y = layout.grids["results"].cell(0, "row")[:2]
    return with_dot(stock, (x + 5, y + 5))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class World:
    """A fake 1C: input events switch which frame the screenshot shows. A frame may be
    a function of the world, for screens that change with time. The "x" button empties
    the field and typing appends to it, unless the field is stuck. `on_enter["clear"]`,
    if given, is the frame shown after the "x" click; `on_enter["unfiltered"]` the frame
    shown by Return on the empty field (the unfiltered list; default: unchanged), and
    `on_enter["return"]` the one shown by Return with the key typed."""

    def __init__(self, layout, frame, on_enter=None, field=""):
        self.layout = layout
        self.frame = frame
        self.on_enter = on_enter if on_enter is not None else {}
        self.log = []
        self.last_article = None
        self.last_stock_crop = None
        self.shots = 0
        self.field = field
        self.stuck = False
        self.enter_shot = self.tab_shot = self.clear_shot = self.unfiltered_shot = None

    def screenshot(self):
        self.shots += 1
        return self.frame(self) if callable(self.frame) else self.frame

    def click(self, x, y):
        self.log.append(("click", x, y))
        r = self.layout.rects["search_clear"]
        if r[0] <= x < r[2] and r[1] <= y < r[3]:
            self.clear_shot = self.shots
            if not self.stuck:
                self.field = ""
            if "clear" in self.on_enter:
                self.frame = self.on_enter["clear"]
        for tab, key in (("stock", "tab_stock"), ("prices", "tab_prices"),
                         ("image", "tab_image")):
            r = self.layout.anchors["common"][key]
            if r[0] <= x < r[2] and r[1] <= y < r[3] and tab in self.on_enter:
                self.frame = self.on_enter[tab]
                self.tab_shot = self.shots

    def type_text(self, text):
        self.log.append(("type", text))
        if not self.stuck:
            self.field += text

    def press_return(self):
        self.log.append(("return",))
        if self.field == "":
            # Enter on the empty field: the unfiltered list (the frame stays if not given)
            self.unfiltered_shot = self.shots
            if "unfiltered" in self.on_enter:
                self.frame = self.on_enter["unfiltered"]
            return
        self.enter_shot = self.shots
        if "return" in self.on_enter:
            self.frame = self.on_enter["return"]

    def kinds(self):
        return [e[0] for e in self.log]


class Spy:
    def __init__(self):
        self.calls = []
        self.frames = {}


@pytest.fixture(autouse=True)
def spy(monkeypatch):
    s = Spy()

    def field(img, lay):
        s.calls.append("read_search_field")
        return STATE["world"].field

    monkeypatch.setattr(lookup_mod, "read_search_field", field)

    def wrap(name):
        orig = getattr(lookup_mod, name)

        def inner(img, *a):
            s.calls.append(name)
            s.frames[name] = img
            return orig(img, *a)

        monkeypatch.setattr(lookup_mod, name, inner)

    for name in ("read_rows", "read_rows_detailed", "read_stock", "read_prices"):
        wrap(name)
    return s


def run(world, layout, article=ARTICLE, clock=None, **kw):
    STATE["world"] = world
    clock = clock or Clock()
    guard = Guard(world, layout, screenshot=world.screenshot)
    return lookup(article, world, guard, layout, sleep=clock.sleep, clock=clock, **kw)


def same(a, b):
    return np.array_equal(np.asarray(a), np.asarray(b))


def center(r):
    return ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)


def tabs(stock, prices):
    return {"stock": stock, "prices": prices}


def test_happy_path_returns_itemread_and_action_order(layout, stock, prices, empty):
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    item = run(world, layout)
    assert isinstance(item, ItemRead)
    assert item.article == ARTICLE
    assert sum(r.on_hand for r in item.stock.values()) == 22
    assert item.epr > 0 and item.ppr > 0
    assert item.snapshot_path is None
    assert world.kinds() == ["click", "return", "click", "type", "return", "click", "click", "click",
                             "click", "click"]
    centers = [(e[1], e[2]) for e in world.log if e[0] == "click"]
    assert centers == [
        center(layout.rects["search_clear"]),
        center(layout.rects["search_field"]),
        center(layout.grids["results"].cell(0, "row")),
        center(layout.anchors["common"]["tab_stock"]),
        center(layout.anchors["common"]["tab_prices"]),
        center(layout.anchors["common"]["tab_image"]),   # best effort
        center(layout.anchors["common"]["tab_stock"]),   # back, so 1C is left on «Остатки»
    ]
    assert world.log[3] == ("type", KEY)
    assert world.last_article == ARTICLE
    assert world.last_stock_frame is not None   # the frame the stock was read from
    assert world.last_stock_frame.size == stock.size


def test_field_holding_text_is_cleared_before_typing(layout, stock, prices, empty):
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)}, field=ARTICLE)
    run(world, layout)
    assert [e for e in world.log if e[0] == "type"] == [("type", KEY)]
    assert world.kinds()[:5] == ["click", "return", "click", "type", "return"]


def test_field_never_empties_gives_no_further_input(layout, stock, empty):
    """The "x" click did not empty the field (it missed, or 1C ignored it): nothing is
    typed and Return is not pressed."""
    world = World(layout, empty, {"return": stock}, field=KEY)
    world.stuck = True
    clock = Clock()
    with pytest.raises(ReadUncertain):
        run(world, layout, clock=clock)
    assert world.kinds() == ["click"]
    assert clock.now >= 10


def test_field_mismatch_after_typing_does_not_press_enter(layout, stock, empty):
    world = World(layout, empty, {"return": stock}, field="")
    world.stuck = True   # empty already, but the typed keys never arrive
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type"]


def test_field_with_other_text_after_typing_does_not_press_enter(layout, stock, empty):
    class Autofill(World):
        def type_text(self, text):
            super().type_text(text)
            self.field += "x"

    world = Autofill(layout, empty, {"return": stock})
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert world.kinds().count("return") == 1   # only the one on the empty field


def test_transient_unexpected_screen_after_clear_is_tolerated(layout, stock, prices, empty):
    """Seen live: right after the "x" click the list refreshes and the grid header is
    missing for a moment."""
    refreshing = empty.copy()
    whiten(refreshing, layout.anchors["common"]["grid_header"])
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)}, field=ARTICLE)
    world.on_enter["clear"] = lambda w: refreshing if w.shots < w.clear_shot + 5 else empty
    item = run(world, layout)
    assert item.article == ARTICLE
    assert world.kinds() == ["click", "return", "click", "type", "return", "click", "click", "click",
                             "click", "click"]


def test_lasting_unexpected_screen_after_clear_stops_without_typing(layout, stock, empty):
    foreign = empty.copy()
    whiten(foreign, layout.anchors["common"]["menu"])
    world = World(layout, empty, {"return": stock, "clear": foreign}, field=ARTICLE)
    clock = Clock()
    with pytest.raises(UnexpectedScreen):
        run(world, layout, clock=clock)
    assert world.kinds() == ["click"]
    assert clock.now >= 10


def test_same_article_twice_accepts_unchanged_result(layout, stock, prices, empty):
    world = World(layout, stock, {"unfiltered": empty, "return": stock, **tabs(stock, prices)},
                  field=KEY)
    world.last_article = ARTICLE
    item = run(world, layout)
    assert item.article == ARTICLE
    assert world.last_article == ARTICLE


def test_zero_rows_raises_not_found(layout, stock, empty):
    gone = stock.copy()
    blank_row(gone, layout.grids["results"], 0)
    world = World(layout, stock, {"return": gone})
    with pytest.raises(ArticleNotFound):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_two_rows_raises_not_found(layout, stock, empty):
    two = stock.copy()
    copy_row(two, layout.grids["results"], 0, 1)
    world = World(layout, empty, {"return": two})
    with pytest.raises(ArticleNotFound):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_row_match_ignores_case(layout, stock, prices, empty, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [("den091-crm", True)])
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    assert run(world, layout).article == ARTICLE


def test_row_match_takes_zero_for_the_letter_o(layout, stock, prices, empty, monkeypatch):
    """the order says «YG148-OYF», the 1C row read «YG148-0YF»."""
    monkeypatch.setattr(lookup_mod, "read_rows_detailed",
                        lambda img, lay: [(ARTICLE.replace("0", "O"), False),
                                          ("DEN092-CRM", False)])
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    assert run(world, layout).article == ARTICLE


def test_row_match_takes_one_for_the_letters_i_and_l():
    """Live: «MA432-CRM(MX131-00201I)» read in the row as «…-002011»."""
    fold = lookup_mod._fold
    assert fold("MA432-CRM(MX131-002011)").startswith(fold("MA432-CRM(MX131-00201I)"))
    assert fold("ABl1") == fold("abI1")
    assert fold("AB12") != fold("AB17")      # other digits stay strict


SNYATO = ARTICLE + "СНЯТО"


@pytest.mark.parametrize("read", [ARTICLE + "CHATO", ARTICLE + "CH9TO", ARTICLE.lower() + "chato"])
def test_snyato_row_read_with_latin_lookalikes_is_taken(layout, stock, prices, empty,
                                                        monkeypatch, read):
    """16.11: «…СНЯТО» items are «…СНЯТО» in 1C too; the row reads «…CHATO» / «…CH9TO»."""
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [(read, False)])
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    assert run(world, layout, article=SNYATO).article == SNYATO


def test_snyato_article_does_not_take_the_item_without_it(layout, stock, empty, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [(ARTICLE, False)])
    world = World(layout, empty, {"return": stock})
    with pytest.raises(ArticleNotFound):
        run(world, layout, article=SNYATO)


def test_snyato_picks_its_row_among_both_items(layout, stock, prices, empty, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed",
                        lambda img, lay: [(ARTICLE, False), (ARTICLE + "CHATO", False)])
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    assert run(world, layout, article=SNYATO).article == SNYATO


def test_plain_article_does_not_take_the_snyato_item(layout, stock, empty, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed",
                        lambda img, lay: [(ARTICLE + "CHATO", False)])
    world = World(layout, empty, {"return": stock})
    with pytest.raises(ArticleNotFound):
        run(world, layout)


def test_cyrillic_lookalike_match_is_strict_elsewhere():
    m = lookup_mod._matches
    assert m("AB1CHATO", "AB1СНЯТО", whole=True)
    assert not m("AB1CHAT0X", "AB1СНЯТО", whole=True)
    assert not m("AB1XHATO", "AB1СНЯТО", whole=True)     # С must read as C
    assert not m("AB1CHAT", "AB1СНЯТО", whole=True)
    assert m("AB1CH", "AB1СНЯТО", whole=False)


def test_row_is_matched_against_the_full_article_not_the_key(layout, stock, empty,
                                                            monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [("DEN091-00422U", True)])
    world = World(layout, empty, {"return": stock})
    with pytest.raises(ArticleNotFound):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_visible_article_prefix_mismatch_raises(layout, stock, empty, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [("DEN092-C", True)])
    world = World(layout, empty, {"return": stock})
    with pytest.raises(ArticleNotFound):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_too_short_visible_prefix_raises(layout, stock, empty, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [("DE", True)])
    world = World(layout, empty, {"return": stock})
    with pytest.raises(ArticleNotFound):
        run(world, layout)


def test_whole_short_article_is_accepted_as_prefix(layout, stock, prices, empty, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [("AB1", True)])
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    assert run(world, layout, article="AB1").article == "AB1"
    assert [e for e in world.log if e[0] == "type"] == [("type", "ab1")]


def test_unexpected_screen_stops_with_no_more_actions(layout, stock, empty):
    foreign = stock.copy()
    whiten(foreign, layout.anchors["common"]["menu"])
    world = World(layout, empty, {"return": foreign})
    with pytest.raises(UnexpectedScreen):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_unexpected_screen_before_typing_sends_nothing(layout, stock):
    foreign = stock.copy()
    whiten(foreign, layout.anchors["common"]["menu"])
    world = World(layout, foreign)
    with pytest.raises(UnexpectedScreen):
        run(world, layout)
    assert world.log == []


def test_stale_result_rejected(layout, stock, prices, old_table):
    """The results never change after Enter and the previous article had the same visible
    prefix: waits out the timeout and refuses."""
    world = World(layout, stock, tabs(stock, prices))
    world.last_article = OTHER
    clock = Clock()
    with pytest.raises(ReadUncertain):
        run(world, layout, clock=clock)
    assert clock.now >= 10
    assert world.kinds() == ["click", "return", "click", "type", "return"]
    assert world.last_article is None


def test_stale_with_unknown_previous_is_rejected(layout, stock):
    world = World(layout, stock)
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_focus_repaint_does_not_defeat_stale_check(layout, stock, prices):
    """Moving focus into the field repaints one pixel of the row. The reference frame is
    taken after that, so the unchanged old result is still recognised as stale."""
    cell = layout.grids["results"].cell(0, "row")
    focused = with_dot(stock, (cell[0] + 5, cell[1] + 5))

    class W(World):
        def click(self, x, y):
            r = layout.rects["search_field"]
            if r[0] <= x < r[2] and r[1] <= y < r[3]:
                self.frame = stock
            super().click(x, y)

    world = W(layout, focused, tabs(stock, prices))
    world.last_article = OTHER
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_late_result_is_accepted(layout, stock, prices, fresh_row, old_table):
    """The old result stays for a few polls, then the new one appears. The table before
    differs from the new one, so only the row decides."""
    world = World(layout, old_table, {**tabs(stock, prices)})
    world.last_article = OTHER
    world.on_enter["return"] = lambda w: old_table if w.shots < w.enter_shot + 7 else fresh_row
    # the row of fresh_row differs from old_table's, which has the same row as `stock`
    item = run(world, layout)
    assert item.article == ARTICLE


# --- tables -----------------------------------------------------------------


@pytest.mark.parametrize("stale_shots", [4, 12])
def test_table_refreshes_later_than_tab_anchor(layout, stock, prices, empty, old_table, spy, stale_shots):
    """The tab header is there at once, the table is still the previous article's for
    1.2 s (inside the settle window) or 3.6 s (longer than it)."""
    on_enter = {
        "return": stock,
        "stock": lambda w: old_table if w.shots < w.tab_shot + stale_shots else stock,
        "prices": prices,
    }
    world = World(layout, empty, on_enter)
    item = run(world, layout)
    assert same(spy.frames["read_stock"], stock)
    assert sum(r.on_hand for r in item.stock.values()) == 22


def test_slow_tab_appears_within_timeout(layout, stock, prices, empty, spy):
    on_enter = {
        "return": stock,
        "stock": lambda w: prices if w.shots < w.tab_shot + 8 else stock,
        "prices": prices,
    }
    world = World(layout, empty, on_enter)
    run(world, layout)
    assert same(spy.frames["read_stock"], stock)


def test_tab_that_never_appears_is_uncertain_not_unexpected(layout, stock, prices, empty):
    world = World(layout, empty, {"return": stock, "stock": prices, "prices": prices})
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert world.kinds() == ["click", "return", "click", "type", "return", "click", "click"]


def test_stale_stock_after_previous_failed_read(layout, stock, prices, empty, old_table, monkeypatch):
    """The previous lookup failed on its stock read and left the prices tab showing. The
    table remembered from it is what the new article must not show."""
    pre = prices.copy()
    blank_row(pre, layout.grids["results"], 0)
    world = World(layout, pre, {"return": stock, **tabs(stock, prices)})
    world.last_article = OTHER
    world.last_stock_crop = stock.crop(layout.rects["stock_table"])
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert "click" == world.kinds()[-1] and world.kinds().count("click") == 4  # x, field, row, stock tab
    assert same(world.last_stock_crop, stock.crop(layout.rects["stock_table"]))
    # the stale table must not be offered later as this article's stock image
    assert getattr(world, "last_stock_frame", None) is None


@pytest.fixture
def unfiltered_other(stock, layout):
    """The unfiltered list whose first row is another article, the stock tab showing."""
    img = stock.copy()
    blank_row(img, layout.grids["results"], 0)
    return img


def test_identical_stock_refuses(layout, stock, prices, fresh_row, unfiltered_other, spy):
    """Result row changed, stock tab already showing, and after the tab click the table is
    pixel-identical to the one before: refused, nothing read."""
    world = World(layout, unfiltered_other, {"return": fresh_row, **tabs(stock, prices)})
    world.last_article = OTHER
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert "read_stock" not in spy.calls
    assert world.kinds().count("click") == 4
    assert getattr(world, "last_stock_frame", None) is None


def test_identical_prices_with_changed_stock_passes(layout, stock, prices, empty, spy):
    # different articles may share prices, so unchanged prices are not a reason to refuse
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    world.last_article = OTHER
    world.last_stock_crop = None
    item = run(world, layout)
    assert item.epr > 0 and same(spy.frames["read_prices"], prices)


def test_last_stock_crop_updated_even_when_read_fails(layout, stock, prices, empty, monkeypatch):
    def boom(img, lay):
        raise ReadUncertain("no")

    monkeypatch.setattr(lookup_mod, "read_stock", boom)
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert same(world.last_stock_crop, stock.crop(layout.rects["stock_table"]))


# --- instability ------------------------------------------------------------


def flicker(a, b):
    return lambda w: a if w.shots % 2 else b


def test_unstable_frame_never_read(layout, stock, spy):
    other = with_dot(stock, (5, 5))
    world = World(layout, flicker(stock, other))
    clock = Clock()
    with pytest.raises(ReadUncertain):
        run(world, layout, clock=clock)
    assert spy.calls == []
    assert clock.now >= 10
    assert world.log == []


def test_unstable_after_enter_never_read(layout, stock, empty, spy):
    world = World(layout, empty, {"return": flicker(stock, with_dot(stock, (5, 5)))})
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert spy.calls.count("read_rows") == 1   # the unfiltered list only, never the result
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_unstable_after_stock_tab_never_read(layout, stock, prices, empty, spy):
    flick = flicker(stock, with_dot(stock, (5, 5)))
    world = World(layout, empty, {"return": stock, "stock": flick, "prices": prices})
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert "read_stock" not in spy.calls
    assert "read_prices" not in spy.calls


def test_unstable_after_prices_tab_never_read(layout, stock, prices, empty, spy):
    flick = flicker(prices, with_dot(prices, (5, 5)))
    world = World(layout, empty, {"return": stock, "stock": stock, "prices": flick})
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert "read_stock" in spy.calls
    assert "read_prices" not in spy.calls


@pytest.mark.parametrize("bad", ["Артикул-1", "AB+1", "AB,1", "AB#1", "AB\n1", "", "x" * 300,
                                 "AB_1", "AB 1", "DEN091-CRM(DEN_1)", "DEN091()", "A(B)(C)",
                                 None])
def test_untypable_article_is_uncertain_with_no_action(layout, stock, empty, spy, bad):
    world = World(layout, empty, {"return": stock})
    with pytest.raises(ReadUncertain):
        run(world, layout, article=bad)
    assert world.log == []
    assert world.shots == 0
    assert spy.calls == []


def test_foreign_window_after_tab_click_stops_at_once(layout, stock, prices, empty):
    foreign = stock.copy()
    whiten(foreign, layout.anchors["common"]["menu"])
    world = World(layout, empty, {"return": stock, "stock": foreign, "prices": prices})
    clock = Clock()
    with pytest.raises(UnexpectedScreen):
        run(world, layout, clock=clock)
    assert world.kinds() == ["click", "return", "click", "type", "return", "click", "click"]
    assert clock.now < 10


def test_unreadable_field_while_clearing_is_waited_out(layout, stock, prices, empty,
                                                      monkeypatch):
    """A field that cannot be read for a moment after the "x" click does not stop the
    lookup; Enter still follows the field read as empty and then as the key."""
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)}, field=ARTICLE)
    calls = []

    def field(img, lay):
        calls.append(1)
        if len(calls) == 1:
            raise ReadUncertain("field unreadable")
        return world.field

    monkeypatch.setattr(lookup_mod, "read_search_field", field)
    item = run(world, layout)
    assert item.article == ARTICLE
    assert [e for e in world.log if e[0] == "type"] == [("type", KEY)]
    assert len(calls) == 4   # unreadable, empty, empty again after Enter, the key


def test_unreadable_field_gives_no_typing_and_no_return(layout, stock, empty, monkeypatch):
    world = World(layout, empty, {"return": stock})

    def field(img, lay):
        raise ReadUncertain("field unreadable")

    monkeypatch.setattr(lookup_mod, "read_search_field", field)
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert world.kinds() == ["click"]


# --- search key -------------------------------------------------------------


@pytest.mark.parametrize("article, key", [
    ("DEN091-GRY(DEN091-01114Z)", "den091-01114z"),
    ("DEN091-CRM(DEN091-00422U)", "den091-00422u"),
    ("(AB-12)", "ab-12"),
    ("TP213-WHT", "tp213-wht"),
    ("AL27018", "al27018"),
    ("DEN091()", "den091()"),          # empty INNER: the whole article
    ("A(B)(C)", "a(b)(c)"),            # not of the form OUTER(INNER)
    ("A(B)C", "a(b)c"),
    ("L9404-CELADONСНЯТО", "l9404-celadon"),        # Cyrillic tail not typed
    ("MA227-MOC(MCS02-02005J)СНЯТО", "mcs02-02005j"),
    ("00602801СНЯТО", "00602801"),
    ("СНЯТО", "снято"),                             # nothing Latin: refused by validation
])
def test_search_key(article, key):
    assert search_key(article) == key


def test_search_key_keeps_untypable_characters_for_lookup_to_refuse():
    assert search_key("X(AB_1)") == "ab_1"
    assert search_key("AB 1") == "ab 1"


def test_caret_blinking_in_filter_row_does_not_unsettle(layout, stock):
    from PIL import ImageDraw

    from orderbot.screen.lookup import _same, _settled_same

    x0, y0, x1, y1 = layout.rects["search_field"]
    blink = stock.copy()
    ImageDraw.Draw(blink).line([(x1 + 300, y0 + 3), (x1 + 300, y1 - 3)], fill="black", width=2)
    assert _settled_same(stock, blink, layout)      # cursor in «бренд» blinked
    assert not _same(stock, blink)                  # stale/freshness checks stay exact
    moved = stock.copy()
    ImageDraw.Draw(moved).point((300, y1 + 200), fill="black")
    assert not _settled_same(stock, moved, layout)  # any change outside the filter row counts


# --- unfiltered list, selected article, settling ----------------------------


def test_identical_stock_refused_when_unfiltered_first_row_is_another_article(
        layout, stock, prices, fresh_row, monkeypatch):
    real = lookup_mod.read_rows
    seen = []

    def rows(img, lay):
        seen.append(1)
        return ["DEN092-C"] if len(seen) == 1 else real(img, lay)

    monkeypatch.setattr(lookup_mod, "read_rows", rows)
    world = World(layout, stock, {"return": fresh_row, **tabs(stock, prices)})
    world.last_article = OTHER
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert getattr(world, "last_stock_frame", None) is None


def test_identical_stock_accepted_when_article_already_selected(layout, stock, prices, fresh_row,
                                                                spy):
    """The unfiltered list keeps the current item on its first row: if that is this very
    article, the stock pane legitimately shows the same table."""
    world = World(layout, stock, {"return": fresh_row, **tabs(stock, prices)})
    world.last_article = OTHER
    item = run(world, layout)
    assert item.article == ARTICLE
    assert same(spy.frames["read_stock"], stock)


def test_enter_on_empty_field_only_after_field_reads_empty(layout, stock, prices, empty,
                                                           monkeypatch):
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)}, field=ARTICLE)

    def field(img, lay):
        if world.clear_shot is None or world.shots < world.clear_shot + 5:
            return ARTICLE   # the x click has not taken effect on screen yet
        return world.field

    monkeypatch.setattr(lookup_mod, "read_search_field", field)
    run(world, layout)
    assert world.unfiltered_shot >= world.clear_shot + 5
    assert world.kinds()[:5] == ["click", "return", "click", "type", "return"]


def test_result_identical_to_unfiltered_list_keeps_waiting_then_uncertain(layout, empty):
    """Even the same article as last time: a result list equal to the unfiltered one is the
    old screen, not an answer."""
    world = World(layout, empty, {"return": empty})
    world.last_article = ARTICLE
    clock = Clock()
    with pytest.raises(ReadUncertain):
        run(world, layout, clock=clock)
    assert clock.now >= 10
    assert world.kinds() == ["click", "return", "click", "type", "return"]


def test_result_list_that_changes_late_is_accepted_for_same_article(layout, stock, prices, empty):
    world = World(layout, empty, {**tabs(stock, prices)})
    world.last_article = ARTICLE
    world.on_enter["return"] = lambda w: empty if w.shots < w.enter_shot + 7 else stock
    assert run(world, layout).article == ARTICLE


def _caret(img, layout):
    from PIL import ImageDraw

    x0, y0, x1, y1 = layout.rects["search_field"]
    img = img.copy()
    ImageDraw.Draw(img).line([(x1 + 300, y0 + 3), (x1 + 300, y1 - 3)], fill="black", width=2)
    return img


def test_caret_blinking_in_filter_row_does_not_block_settling(layout, stock, prices, empty):
    world = World(layout, flicker(empty, _caret(empty, layout)), {
        "return": flicker(stock, _caret(stock, layout)),
        "stock": flicker(stock, _caret(stock, layout)),
        "prices": flicker(prices, _caret(prices, layout)),
    })
    assert run(world, layout).article == ARTICLE


def test_change_elsewhere_still_blocks_settling(layout, empty):
    y1 = layout.rects["search_field"][3]
    world = World(layout, flicker(empty, with_dot(empty, (300, y1 + 200))))
    clock = Clock()
    with pytest.raises(ReadUncertain):
        run(world, layout, clock=clock)
    assert world.log == []
    assert clock.now >= 10



def test_several_rows_pick_the_one_showing_the_article_in_full(layout, stock, prices, fresh_row,
                                                                 monkeypatch):
    """Seen live: "16028" is contained in other items too. The row whose article is shown
    in full and equals ours is clicked, not the first one."""
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [
        ("160281-A", True), ("A-16028", False), ("16028", False), ("16028-X", False)])
    world = World(layout, stock, {"return": fresh_row, **tabs(stock, prices)})
    world.last_article = "16028"
    run(world, layout, article="16028")
    row = layout.grids["results"].cell(2, "row")
    clicks = [e for e in world.log if e[0] == "click"]
    assert any(row[0] <= x < row[2] and row[1] <= y < row[3] for _, x, y in clicks)


@pytest.mark.parametrize("rows", [
    [("16028", False), ("16028", False)],          # two exact rows: cannot tell
    [("16028", True), ("A16028", False)],          # only a cut-short row matches
    [("A16028", False), ("16028-X", False)],       # none matches
])
def test_several_rows_without_a_single_exact_match_go_to_a_person(layout, stock, prices,
                                                                  fresh_row, monkeypatch, rows):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: rows)
    world = World(layout, stock, {"return": fresh_row, **tabs(stock, prices)})
    world.last_article = "16028"
    with pytest.raises(ArticleNotFound):
        run(world, layout, article="16028")
    assert "tab" not in str(world.log)



def test_several_rows_pick_the_single_row_starting_the_article(layout, stock, prices, fresh_row,
                                                              monkeypatch):
    """Seen live: "tp172-00201d" finds the set "NX-TP172-T…" and the item "TP172-TRQ…"."""
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: [
        ("NX-TP172-T", True), ("TP172-TRQ", True)])
    world = World(layout, stock, {"return": fresh_row, **tabs(stock, prices)})
    world.last_article = "TP172-TRQ(TP172-00201D)"
    run(world, layout, article="TP172-TRQ(TP172-00201D)")
    row = layout.grids["results"].cell(1, "row")
    assert any(e[0] == "click" and row[0] <= e[1] < row[2] and row[1] <= e[2] < row[3]
               for e in world.log)


@pytest.mark.parametrize("rows", [
    [("TP172-TRQ", True), ("TP172-TRQ", True)],   # two items start alike
    [("NX-TP172-T", True), ("TP172", True)],      # too short a prefix to pick
])
def test_several_rows_ambiguous_prefix_goes_to_a_person(layout, stock, prices, fresh_row,
                                                       monkeypatch, rows):
    monkeypatch.setattr(lookup_mod, "read_rows_detailed", lambda img, lay: rows)
    world = World(layout, stock, {"return": fresh_row, **tabs(stock, prices)})
    world.last_article = "X"
    with pytest.raises(ArticleNotFound):
        run(world, layout, article="TP172-TRQ(TP172-00201D)")


def test_lowercase_l_read_as_capital_i_still_matches(layout, stock, prices, empty,
                                                     monkeypatch):
    """Seen live: "lsp161-05924l" typed, the field OCR gives "Isp161-05924l" (the
    1C font draws l and I alike). The key is always lowercase, so a capital I in the read
    can only be our l: Return follows."""
    def field(img, lay):
        return STATE["world"].field.replace("l", "I", 1)

    monkeypatch.setattr(lookup_mod, "read_search_field", field)
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})
    # the fixture list holds another item, so the lookup ends there, after Return
    with pytest.raises(ArticleNotFound):
        run(world, layout, article="LSP161-SUN(LSP161-05924L)")
    assert world.kinds()[:5] == ["click", "return", "click", "type", "return"]


def test_capital_i_does_not_stand_for_other_letters(layout, stock, empty, monkeypatch):
    """Only I↔l is forgiven: a read with a different letter still stops before Return."""
    def field(img, lay):
        return STATE["world"].field.replace("s", "S", 1)

    monkeypatch.setattr(lookup_mod, "read_search_field", field)
    world = World(layout, empty, {"return": stock})
    with pytest.raises(ReadUncertain):
        run(world, layout, article="LSP161-SUN(LSP161-05924L)")
    assert world.kinds().count("return") == 1


# --- the «Изображение» tab --------------------------------------


@pytest.fixture
def picture_frame(stock, layout):
    """The «Изображение» tab: the frame of the stock table with a picture in it. The
    fixture layout has no reference for that tab, so it is told by identity (below)."""
    img = stock.copy()
    r = layout.rects["picture"]
    whiten(img, r)
    from PIL import ImageDraw
    ImageDraw.Draw(img).ellipse((r[0] + 100, r[1] + 100, r[0] + 300, r[1] + 300), fill="red")
    return img


@pytest.fixture
def image_tab(monkeypatch, picture_frame, layout):
    """tab_shown says "image" for the picture frame; read_picture crops its rect."""
    orig = lookup_mod.tab_shown

    def tab_shown(img, lay):
        return "image" if same(img, picture_frame) else orig(img, lay)

    def read_picture(img, lay):
        if not same(img, picture_frame):
            raise ReadUncertain("not the image tab")
        return img.crop(lay.rects["picture"])

    monkeypatch.setattr(lookup_mod, "tab_shown", tab_shown)
    monkeypatch.setattr(lookup_mod, "read_picture", read_picture)
    return picture_frame


def test_image_tab_after_prices_keeps_the_picture(layout, stock, prices, empty, image_tab):
    world = World(layout, empty, {"return": stock, **tabs(stock, prices), "image": image_tab})
    item = run(world, layout)
    assert item.epr > 0 and sum(r.on_hand for r in item.stock.values()) == 22
    centers = [(e[1], e[2]) for e in world.log if e[0] == "click"]
    assert centers[-4:] == [center(layout.anchors["common"][f"tab_{t}"])
                            for t in ("stock", "prices", "image", "stock")]
    assert world.frame is world.on_enter["stock"]       # 1C is left on «Остатки»
    assert world.image_tab_left is False
    assert same(world.last_image_crop, image_tab.crop(layout.rects["picture"]))
    assert same(world.last_prices_frame, prices)   # the snapshot keeps the prices tab


def test_image_tab_that_never_appears_keeps_the_read(layout, stock, prices, empty, image_tab):
    world = World(layout, empty, {"return": stock, **tabs(stock, prices)})   # click: no change
    clock = Clock()
    item = run(world, layout, clock=clock)
    assert item.epr > 0 and sum(r.on_hand for r in item.stock.values()) == 22
    assert world.last_article == ARTICLE
    assert world.last_image_crop is None
    centers = [(e[1], e[2]) for e in world.log if e[0] == "click"]
    assert centers[-2:] == [center(layout.anchors["common"]["tab_image"]),
                            center(layout.anchors["common"]["tab_stock"])]   # back anyway


def test_blank_picture_gives_no_picture(layout, stock, prices, empty, image_tab, monkeypatch):
    monkeypatch.setattr(lookup_mod, "read_picture", lambda img, lay: None)
    world = World(layout, empty, {"return": stock, **tabs(stock, prices), "image": image_tab})
    item = run(world, layout)
    assert item.epr > 0 and world.last_image_crop is None


@pytest.mark.parametrize("error", ["user", "unexpected", "lost", "refused"])
def test_failed_image_click_keeps_the_read(layout, stock, prices, empty, image_tab, error):
    """The idle gate refuses the click (a person at the Mac), a foreign window, a lost
    session: the stock and prices already read stand, there is just no picture."""
    from orderbot.screen.idle import UserActive
    from orderbot.screen.session import SessionLost

    class W(World):
        def click(self, x, y):
            r = self.layout.anchors["common"]["tab_image"]
            if r[0] <= x < r[2] and r[1] <= y < r[3]:
                raise {"user": UserActive("busy"), "unexpected": UnexpectedScreen("x"),
                       "lost": SessionLost("gone"), "refused": ValueError("no")}[error]
            super().click(x, y)

    world = W(layout, empty, {"return": stock, **tabs(stock, prices), "image": image_tab})
    item = run(world, layout)
    assert item.epr > 0 and sum(r.on_hand for r in item.stock.values()) == 22
    assert world.last_image_crop is None
    assert world.last_article == ARTICLE


def test_no_image_click_when_prices_fail(layout, stock, empty, image_tab):
    world = World(layout, empty, {"return": stock, "stock": stock, "prices": stock,
                                  "image": image_tab})
    with pytest.raises(ReadUncertain):
        run(world, layout)
    r = layout.anchors["common"]["tab_image"]
    assert not [e for e in world.log if e[0] == "click" and r[0] <= e[1] < r[2]
                and r[1] <= e[2] < r[3]]
    assert getattr(world, "last_image_crop", None) is None


def test_next_lookup_starts_from_the_image_tab(layout, stock, prices, image_tab, old_table):
    """The previous lookup left the «Изображение» tab open: the next one works, and the
    stale-stock check uses the table remembered from the previous stock read."""
    pre = image_tab.copy()
    blank_row(pre, layout.grids["results"], 0)
    world = World(layout, pre, {"return": stock, **tabs(stock, prices), "image": image_tab})
    world.last_article = OTHER
    world.last_stock_crop = old_table.crop(layout.rects["stock_table"])
    item = run(world, layout)
    assert item.epr > 0 and sum(r.on_hand for r in item.stock.values()) == 22
    assert world.last_image_crop is not None


def test_next_lookup_from_the_image_tab_still_refuses_stale_stock(layout, stock, prices,
                                                                    image_tab, spy):
    pre = image_tab.copy()
    blank_row(pre, layout.grids["results"], 0)
    world = World(layout, pre, {"return": stock, **tabs(stock, prices), "image": image_tab})
    world.last_article = OTHER
    world.last_stock_crop = stock.crop(layout.rects["stock_table"])   # the same table again
    with pytest.raises(ReadUncertain):
        run(world, layout)
    assert "read_stock" not in spy.calls
    assert getattr(world, "last_stock_frame", None) is None


def test_image_tab_on_live_captures():
    """read_image_tab against the production layout and the live captures of 06.11: the
    click goes to «Изображение» and the plate comes back."""
    from test_reader import ROOT

    from orderbot.screen.lookup import read_image_tab

    production = Layout.load(ROOT / "layouts" / "production.yaml")
    shots = ROOT / "layouts" / "shots"
    stock_tab = Image.open(shots / "live-stock-tab-0611.png")
    image = Image.open(shots / "live-image-tab.png")
    world = World(production, stock_tab, {"image": image})
    clock = Clock()
    guard = Guard(world, production, screenshot=world.screenshot)
    pic = read_image_tab(world, guard, production, sleep=clock.sleep, clock=clock)
    assert world.log == [("click", *center(production.anchors["common"]["tab_image"])),
                         ("click", *center(production.anchors["common"]["tab_stock"]))]
    assert pic is not None and 240 <= pic.width <= 300



def test_next_lookup_after_the_picture_starts_on_the_stock_tab(layout, stock, prices, empty,
                                                              image_tab, monkeypatch):
    """After the picture 1C is back on «Остатки»: the next lookup's first clicks are made on
    a frame the full screen check (no image-tab exemption) accepts."""
    world = World(layout, empty, {"return": stock, **tabs(stock, prices), "image": image_tab})
    run(world, layout)
    assert lookup_mod.tab_shown(world.frame, layout) == "stock"
    checked = []
    from orderbot.screen import guard as guard_mod
    real = guard_mod.check_screen

    def spy_check(img, lay, image_tab_ok=False):
        checked.append((lookup_mod.tab_shown(img, lay), image_tab_ok))
        return real(img, lay, image_tab_ok=image_tab_ok)

    monkeypatch.setattr(guard_mod, "check_screen", spy_check)
    # the next lookup's first input: the "x" click, checked on the stock tab
    Guard(world, layout, screenshot=world.screenshot).clear_search()
    assert checked == [("stock", False)]     # the full check, no image-tab exemption


@pytest.mark.parametrize("error", ["unexpected", "user"])
def test_failed_click_back_is_only_logged(layout, stock, prices, empty, image_tab, error, caplog):
    from orderbot.screen.idle import UserActive

    class W(World):
        def click(self, x, y):
            r = self.layout.anchors["common"]["tab_stock"]
            if self.frame is image_tab and r[0] <= x < r[2] and r[1] <= y < r[3]:
                raise {"user": UserActive("busy"), "unexpected": UnexpectedScreen("x")}[error]
            super().click(x, y)

    world = W(layout, empty, {"return": stock, **tabs(stock, prices), "image": image_tab})
    import logging
    with caplog.at_level(logging.INFO, logger="orderbot.screen.lookup"):
        item = run(world, layout)
    assert item.epr > 0 and world.last_image_crop is not None
    assert world.image_tab_left is True
    assert any("stock tab" in r.message for r in caplog.records)



# --- a lookup that starts on the «Изображение» tab -----------------


def test_lookup_on_the_image_tab_first_goes_back_to_stock(layout, stock, prices, empty,
                                                          image_tab):
    world = World(layout, image_tab, {"stock": empty, "return": stock, "prices": prices,
                                      "image": image_tab})
    world.on_enter["stock"] = empty
    world.image_tab_left = True

    def click(x, y, _orig=world.click):
        _orig(x, y)
        # after the first «Остатки» click the later ones show the article's table
        world.on_enter["stock"] = stock

    world.click = click
    item = run(world, layout)
    assert item.epr > 0
    assert world.log[0] == ("click", *center(layout.anchors["common"]["tab_stock"]))
    assert world.kinds()[1:6] == ["click", "return", "click", "type", "return"]


def test_stock_tab_never_appearing_stops_before_any_input(layout, stock, prices, image_tab):
    world = World(layout, image_tab, {"return": stock, "prices": prices})   # click: no change
    clock = Clock()
    with pytest.raises(UnexpectedScreen):
        run(world, layout, clock=clock)
    assert world.log == [("click", *center(layout.anchors["common"]["tab_stock"]))]


def test_field_reads_forgives_only_the_known_lookalikes():
    from orderbot.screen.lookup import _field_reads
    assert _field_reads("Isp161-05924l", "lsp161-05924l")             # live 05.11
    assert _field_reads("slp172-024190", "slp172-02419o")             # live 08.11
    assert not _field_reads("slp172-02419o", "slp172-024190")         # typed 0, read o
    assert not _field_reads("slp172-02418o", "slp172-02419o")
    assert not _field_reads("slp172-02419", "slp172-02419o")
    assert not _field_reads(None, "a")


def test_field_reads_l_as_one():
    from orderbot.screen.lookup import _field_reads
    assert _field_reads("19027-cream", "l9027-cream")                 # live 09.11
    assert not _field_reads("l9027-cream", "19027-cream")
