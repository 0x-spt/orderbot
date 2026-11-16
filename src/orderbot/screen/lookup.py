"""Looks one article up in 1C and reads its stock and prices.

The search field is emptied with its "x" button and the search key is typed in lowercase
without any modifier (Remote Desktop drops them); the 1C search is case-insensitive. The
key is the code in parentheses of an article like DEN091-GRY(DEN091-01114Z), otherwise
the whole article.

A frame is read only once two consecutive captures are identical, so a half-drawn window
is never read. A result row that did not change after Enter is not taken for the answer
unless the same article was searched just before.

After stock and prices are read, the «Изображение» tab is opened and the item's picture is
kept in session.last_image_crop, for the person who gets the order. That step
is best effort: whatever goes wrong there, the read stands and there is just no picture.
"""
import logging
import re
import time
from collections.abc import Callable

import numpy as np
from PIL import Image

from orderbot.domain import ItemRead
from orderbot.screen.guard import Guard, validate_text
from orderbot.screen.reader import (
    Layout,
    ReadUncertain,
    UnexpectedScreen,
    check_screen,
    read_picture,
    read_prices,
    read_rows,
    read_rows_detailed,
    read_search_field,
    read_stock,
    tab_shown,
)
from orderbot.screen.session import SessionLost

__all__ = ["ArticleNotFound", "ReadUncertain", "UnexpectedScreen", "SessionLost", "lookup",
           "search_key"]

log = logging.getLogger(__name__)

RESULT_TIMEOUT = 10.0
IMAGE_TIMEOUT = 6.0   # the picture tab: data_settle_s plus drawing; then no picture
POLL_INTERVAL = 0.3
MIN_PREFIX = 4
MULTI_ROW_PREFIX = 8  # visible characters needed to pick one of several found rows

_OUTER_INNER = re.compile(r"[^()]*\(([^()]+)\)")


_CYR_TAIL = re.compile(r"(.*[^а-яё])([а-яё]+)", re.I)


def split_tail(article: str) -> tuple[str, str]:
    """(Latin part, trailing Cyrillic word): «L9404-CELADONСНЯТО» -> («L9404-CELADON»,
    «СНЯТО») (such items carry СНЯТО in 1C too). ('', '') parts stay empty."""
    m = _CYR_TAIL.fullmatch(article)
    return (m.group(1), m.group(2)) if m else (article, "")


def search_key(article: str) -> str:
    """INNER of OUTER(INNER), else the whole article; lowercased, since Shift never
    reaches Windows. A trailing Cyrillic word («СНЯТО») is left out: Cyrillic cannot be
    typed without switching the layout, and 1C finds the item by its Latin part."""
    latin, _ = split_tail(article)
    m = _OUTER_INNER.fullmatch(latin)
    return (m.group(1) if m else latin).lower()


class ArticleNotFound(Exception):
    """The search did not give exactly one row for this article."""


def _same(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and np.array_equal(np.asarray(a), np.asarray(b))


FILTER_BAND_MARGIN = 8  # px above and below the search field row


def _settled_same(a: Image.Image, b: Image.Image, layout: Layout | None) -> bool:
    """Equal for the purpose of "the screen stopped changing": the row of filter fields
    («содержит», «бренд», «производитель») is left out, because 1C keeps a blinking text
    cursor in one of them (seen live: after Enter it sits in «бренд» and blinks forever).
    Everything else must be pixel-identical; the field text itself is read separately."""
    if layout is None or "search_field" not in layout.rects:
        return _same(a, b)
    if a.size != b.size:
        return False
    y0 = max(0, layout.rects["search_field"][1] - FILTER_BAND_MARGIN)
    y1 = min(a.size[1], layout.rects["search_field"][3] + FILTER_BAND_MARGIN)
    x, y = np.asarray(a).copy(), np.asarray(b).copy()
    x[y0:y1] = 0
    y[y0:y1] = 0
    return bool(np.array_equal(x, y))


def _selected_is(frame: Image.Image, layout: Layout, article: str) -> bool:
    """The first row of the unfiltered list shows the start of this article. Unreadable
    rows give False, which keeps the stale-stock check on."""
    try:
        rows = read_rows(frame, layout)
    except (ReadUncertain, UnexpectedScreen):
        return False
    if not rows:
        return False
    visible = rows[0]
    return (len(visible) >= min(MIN_PREFIX, len(article))
            and article.lower().startswith(visible.lower()))


def _stable_frame(session, deadline: float, sleep, clock, poll: float,
                  layout: Layout | None = None) -> Image.Image:
    previous = session.screenshot()
    while True:
        if clock() >= deadline:
            raise ReadUncertain("the screen did not settle in time")
        sleep(poll)
        current = session.screenshot()
        if _settled_same(previous, current, layout):
            return current
        previous = current


_LOOKALIKES = {("I", "l"), ("1", "l"), ("0", "o")}   # (read, typed) pairs drawn alike


_FOLD = str.maketrans({"0": "o", "i": "1", "l": "1"})


def _fold(text: str) -> str:
    """An article for comparing with a result row: any case, zero equal to the letter O,
    and one equal to the letters I and l. The 1C font draws them almost alike (seen live:
    «YG148-OYF» read as «YG148-0YF»; «MA432-CRM(MX131-00201I)» read as
    «…-002011»)."""
    return text.lower().translate(_FOLD)


# Cyrillic letters the result rows show as Latin look-alikes (read live: «НЕ ИСПОЛЬЗОВАТЬ» as
# «HE VCONb3OBATb»; «СНЯТО» reads «CHATO» or «CH9TO»). A Cyrillic letter without a
# look-alike (Я, Б, Л…) matches any one read character.
_CYR_LATIN = str.maketrans("авекмнорстухАВЕКМНОРСТУХ", "abekmhopctyxABEKMHOPCTYX")


def _matches(visible: str, article: str, *, whole: bool) -> bool:
    """`visible` (a row read) equals the article (whole) or starts it, character by
    character after folding; Cyrillic in the article compares with its Latin look-alike,
    or with any character when it has none."""
    v = _fold(visible)
    a = _fold(article.translate(_CYR_LATIN))
    if len(v) > len(a) or (whole and len(v) != len(a)):
        return False
    return all(x == y or "а" <= y <= "я" or y == "ё" for x, y in zip(v, a))


def _field_reads(seen: str | None, text: str) -> bool:
    """The field read equals `text`. The key we type is always lowercase, and the 1C font
    draws some characters alike: l and capital I (seen live: "lsp161-05924l" read as
    "Isp161-05924l"), o and zero (seen live: "slp172-02419o" read as
    "slp172-024190"), l and one (seen live: "l9027-cream" read as "19027-cream").
    Only those, only in that direction; no other character is forgiven."""
    if seen is None or len(seen) != len(text):
        return False
    return all(a == b or (a, b) in _LOOKALIKES for a, b in zip(seen, text))


def _wait_field(session, layout: Layout, text: str, deadline: float, sleep, clock, poll: float):
    """A settled frame whose search field reads exactly `text`. Right after the field
    changes 1C refreshes the list and the screen check may fail for a moment (seen live):
    tolerated until the deadline, after which the last such failure is raised."""
    unexpected = None
    seen = None
    while True:
        try:
            frame = _stable_frame(session, deadline, sleep, clock, poll, layout)
        except ReadUncertain:
            if unexpected is not None:
                raise unexpected from None
            raise
        try:
            check_screen(frame, layout)
            seen = read_search_field(frame, layout)
            unexpected = None
        except UnexpectedScreen as e:
            seen, unexpected = None, e
        except ReadUncertain:
            seen = None
        if _field_reads(seen, text):
            return frame
        if clock() >= deadline:
            if unexpected is not None:
                raise unexpected
            raise ReadUncertain(f"the search field reads {seen!r}, not {text!r}")
        sleep(poll)


def _wait_tab(session, layout: Layout, tab: str, deadline: float, sleep, clock, poll: float,
              image_tab_ok: bool = False):
    """A frame showing this tab's header that then stays unchanged for data_settle_s, so a
    table that repaints after its tab is not read half-way. image_tab_ok: frames of the
    «Изображение» tab pass the screen check (picture step and the way back from it)."""
    def capture() -> Image.Image:
        frame = session.screenshot()
        check_screen(frame, layout, image_tab_ok=image_tab_ok)  # a foreign window stops us
        return frame

    while True:
        frame = capture()
        while tab_shown(frame, layout) != tab:
            if clock() >= deadline:
                raise ReadUncertain(f"the {tab} tab did not appear in time")
            sleep(poll)
            frame = capture()
        since = clock()
        while clock() - since < layout.data_settle_s:
            if clock() >= deadline:
                raise ReadUncertain(f"the {tab} tab did not settle in time")
            sleep(poll)
            current = capture()
            if not _settled_same(frame, current, layout):
                frame, since = current, clock()
        if tab_shown(frame, layout) == tab:
            return frame


def read_image_tab(session, guard: Guard, layout: Layout, *,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic,
                   timeout: float = IMAGE_TIMEOUT, poll: float = POLL_INTERVAL):
    """One click on «Изображение», a settled frame of that tab, the picture in it (None
    when the frame is empty), then one click back on «Остатки» without waiting, so the next
    lookup starts on the stock tab under the full screen check. Raises like the other
    steps (lookup() swallows it); a failed click back is only logged, and
    session.image_tab_left then stays True for the alert that may follow."""
    guard.open_tab("image")
    session.image_tab_left = True
    try:
        frame = _wait_tab(session, layout, "image", clock() + timeout, sleep, clock, poll,
                          image_tab_ok=True)
        return read_picture(frame, layout)
    finally:
        try:
            guard.open_tab("stock")
            session.image_tab_left = False
        except Exception as e:
            log.info("click back to the stock tab failed: %s: %s", type(e).__name__, e)


def lookup(
    article: str,
    session,
    guard: Guard,
    layout: Layout,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = RESULT_TIMEOUT,
    poll: float = POLL_INTERVAL,
) -> ItemRead:
    session.last_image_crop = None
    session.last_prices_frame = None
    try:
        key = validate_text(search_key(article))
    except (TypeError, ValueError) as e:
        raise ReadUncertain(f"the article cannot be typed into 1C: {e}") from e

    results = layout.grids["results"]
    row_cell = results.cell(0, "row")
    first, last = row_cell, results.cell(results.rows - 1, "row")
    list_area = (first[0], first[1], first[2], last[3])
    stock_rect = layout.rects["stock_table"]
    previous = getattr(session, "last_article", None)

    # A screen that does not settle stops the lookup before any input.
    start = _stable_frame(session, clock() + timeout, sleep, clock, poll, layout)
    if tab_shown(start, layout) == "image":
        # Left on «Изображение» (the click back was refused or not drawn yet): back to
        # «Остатки» first, so every input below is checked without the image-tab exemption.
        guard.open_tab("stock")
        try:
            _wait_tab(session, layout, "stock", clock() + timeout, sleep, clock, poll,
                      image_tab_ok=True)
        except ReadUncertain as e:
            raise UnexpectedScreen(f"1C stays on the image tab: {e}") from e
        session.image_tab_left = False

    # Enter follows only observed changes of the field: emptied by the "x" button (which
    # proves the click landed on the field), then holding exactly the typed key (which
    # proves where the keys went). Either one missing means no Return.
    # Until a result is accepted, nothing is known about what the screen shows: clearing
    # the field already refreshes the list.
    session.last_article = None
    guard.clear_search()
    _wait_field(session, layout, "", clock() + timeout, sleep, clock, poll)
    # The × button empties the field but leaves the previous result list on screen (seen
    # live), so the same article searched again would look stale. Enter on the verified
    # empty field shows the unfiltered list first; the reference row comes from it.
    guard.press_enter()
    emptied = _wait_field(session, layout, "", clock() + timeout, sleep, clock, poll)
    guard.enter_article(key)
    frame = _wait_field(session, layout, key, clock() + timeout, sleep, clock, poll)
    # Typing does not filter the list (seen live), so this frame still shows the unfiltered
    # list, captured after the click into the field: nothing but Return happens between it
    # and Enter. The whole visible list is compared, not only its first row: the unfiltered
    # list keeps the current item selected on top (seen live), which can be this article.
    before = frame.crop(list_area)
    if tab_shown(frame, layout) == "stock":
        before_stock = frame.crop(stock_rect)
    else:
        before_stock = getattr(session, "last_stock_crop", None)
    # The unfiltered list keeps the selected item on its first row (seen live). If that item
    # already is this article, the stock pane legitimately shows the same table afterwards.
    if before_stock is not None and _selected_is(emptied, layout, article):
        before_stock = None

    guard.press_enter()

    deadline = clock() + timeout
    while True:
        frame = _stable_frame(session, deadline, sleep, clock, poll, layout)
        if _same(frame.crop(list_area), before):
            continue  # the unfiltered list is still there
        break

    rows = read_rows_detailed(frame, layout)
    if not rows:
        raise ArticleNotFound("0 rows found")
    if len(rows) == 1:
        visible, cut = rows[0]
        # 1C finds the key in any case; the row shows the start of the full article.
        if (len(visible) < min(MIN_PREFIX, len(article))
                or not _matches(visible, article, whole=False)):
            raise ArticleNotFound(f"the row shows {visible!r}, which does not start the article")
        # «X…СНЯТО» typed as «x»: a row showing all of «X» and nothing after it is the
        # other item, the one without СНЯТО.
        if split_tail(article)[1] and not cut and not _matches(visible, article, whole=True):
            raise ArticleNotFound(f"the row shows {visible!r}, not the article with "
                                  f"{split_tail(article)[1]}")
        index = 0
    else:
        # A short article (seen live: "16028") is contained in other items' fields too. Only
        # a row whose article is shown in full and equals ours, and only one such, is taken.
        exact = [i for i, (visible, cut) in enumerate(rows)
                 if not cut and _matches(visible, article, whole=True)]
        # Otherwise a row whose visible, possibly cut, article starts ours (seen live:
        # "tp172-00201d" also finds the set "NX-TP172-T…" through its full name; the item
        # itself shows "TP172-TRQ…"). Exactly one such row, with a long enough prefix.
        starts = [i for i, (visible, cut) in enumerate(rows)
                  if cut and MULTI_ROW_PREFIX <= len(visible) < len(article)
                  and _matches(visible, article, whole=False)]
        if len(exact) == 1:
            index = exact[0]
        elif not exact and len(starts) == 1:
            index = starts[0]
        else:
            raise ArticleNotFound(f"{len(rows)} rows found, {len(exact)} show the article in "
                                  f"full, {len(starts)} start it")

    guard.click_result_row(index)
    guard.open_tab("stock")
    deadline = clock() + timeout
    while True:
        frame = _wait_tab(session, layout, "stock", deadline, sleep, clock, poll)
        crop = frame.crop(stock_rect)
        session.last_stock_crop = crop
        if article != previous and before_stock is not None and _same(crop, before_stock):
            if clock() >= deadline:
                raise ReadUncertain("the stock table is still the previous article's")
            continue
        # only now: a frame still showing the previous article's stock is not this article's
        session.last_stock_frame = frame
        session.image_tab_left = False   # 1C shows «Остатки» again
        break
    stock = read_stock(frame, layout)
    guard.open_tab("prices")
    frame = _wait_tab(session, layout, "prices", clock() + timeout, sleep, clock, poll)
    epr, ppr = read_prices(frame, layout)
    session.last_prices_frame = frame
    session.last_article = article
    item = ItemRead(article, stock, epr, ppr, None)
    try:
        session.last_image_crop = read_image_tab(session, guard, layout, sleep=sleep,
                                                 clock=clock, poll=poll)
        if session.last_image_crop is None:
            log.info("1C has no picture for %s", article)
    except Exception as e:
        # the person at the Mac (UserActive), a foreign window, a lost session or a stop
        # request show up again on the next input; this read stands without a picture
        log.info("no 1C picture for %s: %s: %s", article, type(e).__name__, e)
        session.last_image_crop = None
    return item
