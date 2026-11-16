import itertools
import subprocess

import pytest
from PIL import Image
from test_reader import LAYOUT_PATH, STOCK_FRAME_OFFSET_Y, raw_stock, whiten

import orderbot.screen.guard as guard_mod
from orderbot.screen.guard import ActionRefused, Guard, _center
from orderbot.screen.idle import UserActive
from orderbot.screen.reader import Layout, UnexpectedScreen
from orderbot.screen.session import SessionLost


@pytest.fixture(scope="module")
def layout():
    return Layout.load(LAYOUT_PATH)


@pytest.fixture(scope="module")
def frame(layout):
    w, h = layout.size
    return raw_stock().crop((0, STOCK_FRAME_OFFSET_Y, w, STOCK_FRAME_OFFSET_Y + h))


class Recorder:
    def __init__(self):
        self.calls = []

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def type_text(self, text):
        self.calls.append(("type", text))

    def press_return(self):
        self.calls.append(("return",))


def public(obj):
    return {n for n in dir(obj) if not n.startswith("_")}


def test_public_api_is_exactly_whitelist(layout, frame):
    guard = Guard(Recorder(), layout, sleep=lambda s: None, screenshot=lambda: frame)
    # dismiss_task_notice: the x of «Оповещение о задачах», allowed explicitly
    assert public(Guard) == {"clear_search", "enter_article", "press_enter", "click_result_row",
                             "open_tab", "dismiss_task_notice"}
    assert public(guard) == public(Guard)


def inside(point, rect):
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def test_click_only_inside_allowed_rects(layout, frame):
    rec = Recorder()
    guard = Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: frame)
    guard.clear_search()
    guard.enter_article("den091-00422u")
    guard.press_enter()
    guard.click_result_row()
    guard.open_tab("stock")
    guard.open_tab("prices")
    guard.open_tab("image")   # the «Изображение» tab, read-only
    allowed = [
        layout.rects["search_clear"],
        layout.rects["search_field"],
        layout.grids["results"].cell(0, "row"),
        layout.anchors["common"]["tab_stock"],
        layout.anchors["common"]["tab_prices"],
        layout.anchors["common"]["tab_image"],
    ]
    clicks = [c[1:] for c in rec.calls if c[0] == "click"]
    assert len(clicks) == 6
    for point, rect in zip(clicks, allowed):
        assert inside(point, rect)
    assert [c[0] for c in rec.calls] == ["click", "click", "type", "return", "click", "click",
                                         "click", "click"]


def test_clear_button_is_where_it_was_measured(layout):
    """Production: the live click at (1980, 319) emptied the field; the fixture is the
    same form 100 px higher."""
    from test_reader import ROOT

    from orderbot.screen.guard import _center
    from orderbot.screen.reader import Layout as L

    production = L.load(ROOT / "layouts" / "production.yaml")
    assert _center(production.rects["search_clear"]) == (1980, 319)
    assert _center(layout.rects["search_clear"]) == (1980, 219)
    for lay in (layout, production):
        clear, field = lay.rects["search_clear"], lay.rects["search_field"]
        assert clear[0] > field[2]          # right of the text area, not inside it


@pytest.mark.parametrize("name", ["", "Stock", "Image", "tab_image", "tab_stock", "images",
                                  None, 1])
def test_open_tab_rejects_other_names(layout, frame, name):
    rec = Recorder()
    with pytest.raises(ValueError):
        Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: frame).open_tab(name)
    assert rec.calls == []


@pytest.mark.parametrize("text", [
    "", "  ", "a\nb", "a\tb", "a\x1bb", "a\rb", "a\x7fb", "x" * 500,
    "Артикул", "caf\u00e9", "a;b", "a\u200bb", "a\u2028b", "a\u00a0b", "a+b", "a'b", "a\\b",
    # characters that need Shift on a US layout, which never reaches Windows
    "DEN091", "den091(x)", "a_b", "a b", "a:b", "A",
])
def test_enter_article_rejects_what_cannot_be_typed_without_shift(layout, frame, text):
    rec = Recorder()
    with pytest.raises(ValueError):
        Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: frame).enter_article(text)
    assert rec.calls == []


@pytest.mark.parametrize("action", [
    lambda g: g.clear_search(),
    lambda g: g.enter_article("den091"),
    lambda g: g.press_enter(),
    lambda g: g.open_tab("prices"),
    lambda g: g.open_tab("image"),
])
def test_wrong_screen_sends_nothing(layout, frame, action):
    foreign = frame.copy()
    whiten(foreign, layout.anchors["common"]["menu"])
    rec = Recorder()
    with pytest.raises(UnexpectedScreen):
        action(Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: foreign))
    assert rec.calls == []


def test_screen_is_checked_before_every_backend_call(layout, frame):
    """The screen goes foreign after the click into the field: the text is not typed."""
    foreign = frame.copy()
    whiten(foreign, layout.anchors["common"]["menu"])
    rec = Recorder()
    shots = itertools.chain([frame], itertools.repeat(foreign))
    guard = Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: next(shots))
    with pytest.raises(UnexpectedScreen):
        guard.enter_article("den091")
    assert [c[0] for c in rec.calls] == ["click"]


def test_result_row_clicked_once_per_enter(layout, frame):
    rec = Recorder()
    guard = Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: frame)
    with pytest.raises(ActionRefused):
        guard.click_result_row()  # no Enter yet
    guard.press_enter()
    guard.click_result_row()
    with pytest.raises(ActionRefused):
        guard.click_result_row()  # a second click would be a double click
    assert [c[0] for c in rec.calls] == ["return", "click"]
    guard.press_enter()
    guard.clear_search()
    with pytest.raises(ActionRefused):
        guard.click_result_row()  # clearing the field disarms it
    guard.press_enter()
    guard.enter_article("den091")
    with pytest.raises(ActionRefused):
        guard.click_result_row()  # a new search disarms it


def test_wrong_screen_before_row_click_sends_nothing(layout, frame):
    foreign = frame.copy()
    whiten(foreign, layout.anchors["common"]["menu"])
    shots = itertools.chain([frame], itertools.repeat(foreign))  # fine for press_enter, foreign for the row click
    rec = Recorder()
    guard = Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: next(shots))
    guard.press_enter()
    with pytest.raises(UnexpectedScreen):
        guard.click_result_row()
    assert [c[0] for c in rec.calls] == ["return"]


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(1, "x"),
    subprocess.TimeoutExpired("x", 1),
    OSError("gone"),
    SessionLost("gone"),
])
def test_screenshot_failure_is_session_lost_and_stops_input(layout, frame, exc):
    rec = Recorder()
    state = {"fail": True}

    def shot():
        if state["fail"]:
            raise exc
        return frame

    guard = Guard(rec, layout, sleep=lambda s: None, screenshot=shot)
    with pytest.raises(SessionLost):
        guard.press_enter()
    state["fail"] = False  # even if the screen is back, nothing more is sent
    with pytest.raises(SessionLost):
        guard.press_enter()
    assert rec.calls == []


def test_backend_failure_is_session_lost_and_stops_input(layout, frame):
    class Broken(Recorder):
        def click(self, x, y):
            raise subprocess.TimeoutExpired("backend", 10)

    rec = Broken()
    guard = Guard(rec, layout, sleep=lambda s: None, screenshot=lambda: frame)
    with pytest.raises(SessionLost):
        guard.open_tab("stock")
    with pytest.raises(SessionLost):
        guard.press_enter()
    assert rec.calls == []


def test_user_active_from_backend_is_not_swallowed(layout, frame):
    class Busy(Recorder):
        def press_return(self):
            raise UserActive("x")

    guard = Guard(Busy(), layout, sleep=lambda s: None, screenshot=lambda: frame)
    with pytest.raises(UserActive):
        guard.press_enter()



# --- on the «Изображение» tab --------------------------------------


@pytest.fixture(scope="module")
def production():
    from test_reader import ROOT
    return Layout.load(ROOT / "layouts" / "production.yaml")


@pytest.fixture(scope="module")
def image_frame():
    from test_reader import ROOT
    return Image.open(ROOT / "layouts" / "shots" / "live-image-tab.png")


def test_only_the_click_to_stock_is_sent_from_the_image_tab(production, image_frame):
    rec = Recorder()
    Guard(rec, production, sleep=lambda s: None, screenshot=lambda: image_frame).open_tab("stock")
    assert rec.calls == [("click", *_center(production.anchors["common"]["tab_stock"]))]


@pytest.mark.parametrize("action", [
    lambda g: g.clear_search(),
    lambda g: g.enter_article("den091"),
    lambda g: g.press_enter(),
    lambda g: g.open_tab("prices"),
    lambda g: g.open_tab("image"),
])
def test_no_other_input_from_the_image_tab(production, image_frame, action):
    rec = Recorder()
    with pytest.raises(UnexpectedScreen):
        action(Guard(rec, production, sleep=lambda s: None, screenshot=lambda: image_frame))
    assert rec.calls == []


def test_a_moment_of_repaint_does_not_stop_input(layout, frame):
    """Live: right after the "x" click 1C repaints the list; the frame before Return
    missed the header. A later good frame lets Return go; nothing is sent before it."""
    torn = frame.copy()
    whiten(torn, layout.anchors["common"]["grid_header"])
    shots = iter([torn, torn, frame])
    rec, pauses = Recorder(), []
    guard = Guard(rec, layout, sleep=pauses.append, screenshot=lambda: next(shots))
    guard.press_enter()
    assert [c[0] for c in rec.calls] == ["return"] and len(pauses) == 2


def test_a_wrong_screen_that_stays_stops_input(layout, frame):
    foreign = frame.copy()
    whiten(foreign, layout.anchors["common"]["grid_header"])
    rec, seen = Recorder(), []
    def shot():
        seen.append(1)
        return foreign
    guard = Guard(rec, layout, sleep=lambda s: None, screenshot=shot)
    with pytest.raises(UnexpectedScreen):
        guard.press_enter()
    assert rec.calls == [] and len(seen) == guard_mod.VERIFY_TRIES


PROD_PATH = LAYOUT_PATH.parent / "production.yaml"
NOTICE_SHOT = LAYOUT_PATH.parent / "shots" / "live-task-notice.png"


@pytest.fixture(scope="module")
def prod():
    return Layout.load(PROD_PATH)


@pytest.fixture(scope="module")
def prod_frame():
    return Image.open(LAYOUT_PATH.parent / "shots" / "live-stock-tab-0611.png").convert("RGB")


@pytest.fixture(scope="module")
def notice():
    return Image.open(NOTICE_SHOT).convert("RGB")


def test_live_task_notice_is_recognised_and_normal_screens_are_not(prod, prod_frame, notice):
    from orderbot.screen.reader import task_notice_shown
    assert task_notice_shown(notice, prod)
    assert not task_notice_shown(prod_frame, prod)
    for shot in (LAYOUT_PATH.parent / "shots").glob("*.png"):
        img = Image.open(shot).convert("RGB")
        if img.size == prod.size and shot != NOTICE_SHOT:
            assert not task_notice_shown(img, prod), shot.name


def test_task_notice_is_closed_by_its_x_then_the_action_goes_on(prod, prod_frame, notice):
    """1C's «Оповещение о задачах» opened over «Номенклатура»; the robot closes it."""
    rec = Recorder()
    shots = itertools.chain([notice], itertools.repeat(prod_frame))
    guard = Guard(rec, prod, sleep=lambda s: None, screenshot=lambda: next(shots))
    guard.clear_search()
    x, y = _center(prod.rects["notice_close"])
    assert rec.calls[0] == ("click", x, y)
    assert inside((x, y), (2850, 205, 2870, 224))
    assert rec.calls[1][0] == "click" and inside(rec.calls[1][1:], prod.rects["search_clear"])
    assert len(rec.calls) == 2


def test_task_notice_that_stays_is_closed_twice_at_most_then_stops(prod, notice):
    rec = Recorder()
    guard = Guard(rec, prod, sleep=lambda s: None, screenshot=lambda: notice)
    with pytest.raises(UnexpectedScreen):
        guard.clear_search()
    x, y = _center(prod.rects["notice_close"])
    assert rec.calls == [("click", x, y)] * guard_mod.NOTICE_CLOSES


def test_foreign_screen_is_never_answered_with_the_notice_x(prod):
    """Anything that is not the recognised reminder (here: a blank frame) gets no click."""
    rec = Recorder()
    blank = Image.new("RGB", prod.size, "white")
    guard = Guard(rec, prod, sleep=lambda s: None, screenshot=lambda: blank)
    with pytest.raises(UnexpectedScreen):
        guard.clear_search()
    assert rec.calls == []


def test_notice_found_on_connect_is_closed_before_the_window_check(prod, prod_frame, notice):
    """Live: the reminder was open when an order came, connect refused the window."""
    rec = Recorder()
    shots = itertools.chain([notice], itertools.repeat(prod_frame))
    guard = Guard(rec, prod, sleep=lambda s: None, screenshot=lambda: next(shots))
    guard.dismiss_task_notice()
    assert rec.calls == [("click", *_center(prod.rects["notice_close"]))]


def test_dismiss_does_nothing_without_the_notice(prod, prod_frame):
    rec = Recorder()
    blank = Image.new("RGB", prod.size, "white")
    for frame in (prod_frame, blank):
        Guard(rec, prod, sleep=lambda s: None, screenshot=lambda: frame).dismiss_task_notice()
    assert rec.calls == []


def test_dismiss_gives_up_after_the_close_limit(prod, notice):
    rec = Recorder()
    Guard(rec, prod, sleep=lambda s: None, screenshot=lambda: notice).dismiss_task_notice()
    assert rec.calls == [("click", *_center(prod.rects["notice_close"]))] * guard_mod.NOTICE_CLOSES
