"""Reads stock and prices from a frame of the 1C window.

A wrong value read as confident is the worst outcome here, because later steps approve
orders from these numbers. Whatever is in doubt raises instead.
"""
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

from orderbot.domain import StockRow
from orderbot.screen import ocr

Rect = tuple[int, int, int, int]

# Row slots hold text between grid lines; these keep the lines themselves out of the crop.
ROW_INSET_TOP = 3
ROW_INSET_BOTTOM = 2

_QTY = re.compile(r"0|[1-9][0-9]*")
_PRICE = re.compile(r"((?:0|[1-9][0-9]*),[0-9]*)\.*")
_ARTICLE = re.compile(r"[A-Za-z0-9()\-_/ .]+")
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")

DIGIT_FILES = {**{d: d for d in "0123456789"}, ",": "comma", ".": "dot"}
DIGIT_STYLES = {"regular": "", "total": "total"}


class UnexpectedScreen(Exception):
    """This is not the screen the layout describes."""


class ReadUncertain(Exception):
    """The right screen, but a value could not be read with certainty."""


@dataclass(frozen=True)
class Grid:
    top: int
    pitch: int
    rows: int
    columns: dict[str, tuple[int, int]]

    def cell(self, row: int, column: str) -> Rect:
        x0, x1 = self.columns[column]
        y = self.top + row * self.pitch
        return (x0, y + ROW_INSET_TOP, x1, y + self.pitch - ROW_INSET_BOTTOM)


@dataclass(frozen=True)
class PriceRow:
    id: str
    type: str
    currency: str


@dataclass
class Layout:
    name: str
    size: tuple[int, int]
    rects: dict[str, Rect]
    grids: dict[str, Grid]
    anchors: dict[str, dict[str, Rect]]
    blank_regions: dict[str, Rect]
    warehouses: dict[str, str]
    price_rows: list[PriceRow]
    epr_row: str
    ppr_row: str
    digits: dict[str, dict[str, np.ndarray]]
    references: dict[str, np.ndarray]
    data_settle_s: float = 2.0
    # Blank regions the picture frame of the «Изображение» tab covers: a picture may be
    # drawn there, so they are not required blank while that tab is recognised.
    image_tab_unchecked: tuple[str, ...] = ()

    @classmethod
    def load(cls, path) -> "Layout":
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        name = raw["name"]
        return cls(
            name=name,
            size=tuple(raw["size"]),
            rects={k: tuple(v) for k, v in raw["rects"].items()},
            grids={
                k: Grid(g["top"], g["pitch"], g["rows"], {c: tuple(x) for c, x in g["columns"].items()})
                for k, g in raw["grids"].items()
            },
            anchors={
                group: {k: tuple(v) for k, v in items.items()}
                for group, items in raw["anchors"].items()
            },
            blank_regions={k: tuple(v) for k, v in raw["blank_regions"].items()},
            warehouses=dict(raw["warehouses"]),
            price_rows=[PriceRow(r["id"], r["type"], r["currency"]) for r in raw["price_rows"]],
            epr_row=raw["epr_row"],
            ppr_row=raw["ppr_row"],
            digits=_load_digits(path.parent / f"{name}-digits"),
            references=_load_references(path.parent / f"{name}-labels"),
            data_settle_s=float(raw.get("data_settle_s", 2.0)),
            image_tab_unchecked=tuple(raw.get("image_tab_unchecked") or ()),
        )


def _load_digits(root: Path) -> dict[str, dict[str, np.ndarray]]:
    """A missing template is simply absent, so such a digit can never match."""
    styles = {}
    for style, sub in DIGIT_STYLES.items():
        found = {}
        for char, stem in DIGIT_FILES.items():
            file = root / sub / f"{stem}.png"
            if file.exists():
                found[char] = ocr.load_mask(file)
        styles[style] = found
    return styles


def _load_references(root: Path) -> dict[str, np.ndarray]:
    return {p.stem: ocr.load_mask(p) for p in root.glob("*.png")}


def _crop(img: Image.Image, rect: Rect) -> Image.Image:
    return img.crop(rect)


def _require_anchors(img: Image.Image, layout: Layout, group: str,
                     min_score: float = ocr.ANCHOR_MIN_SCORE) -> None:
    for anchor, rect in layout.anchors[group].items():
        reference = layout.references.get(anchor)
        if reference is None or not ocr.references_match(_crop(img, rect), reference,
                                                          min_score):
            raise UnexpectedScreen(f"anchor {anchor} not found")


TABS = ("stock", "prices", "image")


# The image tab's strip reference scored 1.0 on the live capture and at most 0.36 on every
# stock and prices capture: a lower bar than the other anchors' leaves room for hover
# shading, and it is used only to tell that tab (for the picture step and the tables_tail
# exemption), always together with both table headers being absent.
IMAGE_ANCHOR_MIN_SCORE = 0.80


def _anchors_match(img: Image.Image, layout: Layout, group: str,
                   min_score: float = ocr.ANCHOR_MIN_SCORE) -> bool:
    if group not in layout.anchors:
        return False
    try:
        _require_anchors(img, layout, group, min_score)
        return True
    except UnexpectedScreen:
        return False


def _image_tab(img: Image.Image, layout: Layout) -> bool:
    """The «Изображение» tab: no stock or prices table header, and the tab strip with that
    tab open (at the image bar)."""
    return ("image" in layout.anchors
            and not _anchors_match(img, layout, "stock")
            and not _anchors_match(img, layout, "prices")
            and _anchors_match(img, layout, "image", IMAGE_ANCHOR_MIN_SCORE))


# The band under the last row slot shows the next row of a table longer than its slots.
OVERFLOW_REGION = "tables_tail"
SCROLLBAR_DARK = 235          # grey of the scroll bar edge and track
SCROLLBAR_MIN_SHARE = 0.95    # its edge runs the whole height of the table
LONG_STOCK = ("the stock table is longer than the window (scroll bar): rows are out of "
              "sight and the columns are narrowed")


def stock_scrolls(img: Image.Image, layout: Layout) -> bool:
    """«Остатки» with more warehouses than row slots (seen with
    an item kept in many warehouses): 1C adds a vertical scroll bar and narrows the columns. The robot
    never scrolls 1C, so such a table is not read, but it is the expected window."""
    rect = layout.rects.get("stock_scrollbar")
    if rect is None:
        return False
    edge = np.asarray(_crop(img, rect).convert("L"))
    return (bool((edge < SCROLLBAR_DARK).mean() >= SCROLLBAR_MIN_SHARE)
            and not _image_tab(img, layout))


def tab_shown(img: Image.Image, layout: Layout) -> str | None:
    """Which tab is on screen: "stock" or "prices" by its table header, "image" by the tab
    strip (the open tab has no line under it; the picture tab has no header), or None."""
    for tab in ("stock", "prices"):
        if _anchors_match(img, layout, tab):
            return tab
    if stock_scrolls(img, layout):
        return "stock"     # its header is narrowed, read_stock refuses it
    return "image" if _image_tab(img, layout) else None


def task_notice_shown(img: Image.Image, layout: Layout) -> bool:
    """1C's own «Оповещение о задачах» window (a task reminder) open over «Номенклатура»
    (seen live): its title where «Номенклатура»'s is, the 1C menu and status bar, and not
    the «Номенклатура» form. The robot closes it."""
    if (img.size != layout.size or "task_notice" not in layout.anchors
            or "notice_close" not in layout.rects):
        return False
    return (_anchors_match(img, layout, "task_notice")
            and not _anchors_match(img, layout, "common"))


def check_screen(img: Image.Image, layout: Layout, *, image_tab_ok: bool = False) -> None:
    """Raises UnexpectedScreen unless this is the expected window. The «Изображение» tab
    passes only with image_tab_ok, which the picture step alone asks for (its frames and
    the one click back to «Остатки»): no other input is ever sent on that tab."""
    if img.size != layout.size:
        raise UnexpectedScreen(f"frame is {img.size}, layout is calibrated for {layout.size}")
    _require_anchors(img, layout, "common")
    unchecked = (set(layout.image_tab_unchecked)
                 if image_tab_ok and layout.image_tab_unchecked and _image_tab(img, layout)
                 else set())
    if OVERFLOW_REGION in layout.blank_regions and stock_scrolls(img, layout):
        unchecked.add(OVERFLOW_REGION)
    for region, rect in layout.blank_regions.items():
        if region in unchecked:
            continue
        if not ocr.is_blank(_crop(img, rect)):
            raise UnexpectedScreen(f"something covers the empty region {region}")
    _present_rows(img, layout.grids["results"], ["row"], UnexpectedScreen)


def _present_rows(img, grid: Grid, columns: list[str], error: type[Exception]) -> list[int]:
    """Indexes of slots holding anything. Rows are contiguous, so a slot with ink below an
    empty one is not a row of this table."""
    filled = [
        row
        for row in range(grid.rows)
        if any(not ocr.is_nearly_blank(_crop(img, grid.cell(row, c))) for c in columns)
    ]
    if filled != list(range(len(filled))):
        raise error(f"table rows are not contiguous: {filled}")
    return filled


CARET_MAX_WIDTH = 3        # px: the text cursor is a thin vertical line
CARET_MIN_HEIGHT = 0.75    # share of the field height it spans (glyphs reach about 0.5)


def _without_caret(cell: Image.Image) -> Image.Image:
    """The search field with the blinking text cursor painted white. The cursor is a
    vertical line of at most CARET_MAX_WIDTH columns reaching almost the full field height,
    taller than any glyph (seen live: glyphs span rows 7-22 of 30, the cursor 2-27), alone
    or touching the last glyph (then OCR read it as a trailing "l")."""
    a = np.asarray(cell.convert("RGB")).copy()
    dark = a.min(axis=2) < ocr.BLANK_MIN_CHANNEL
    h = a.shape[0]
    tall_cols = []
    for x in np.flatnonzero(dark.any(axis=0)):
        rows = np.flatnonzero(dark[:, x])
        if rows[-1] - rows[0] + 1 >= CARET_MIN_HEIGHT * h:
            tall_cols.append(int(x))
    groups, cur = [], []
    for x in tall_cols:
        if cur and x != cur[-1] + 1:
            groups.append(cur)
            cur = []
        cur.append(x)
    if cur:
        groups.append(cur)
    for g in groups:
        if len(g) <= CARET_MAX_WIDTH:
            a[:, g[0]:g[-1] + 1] = 255
    return Image.fromarray(a)


def read_search_field(img: Image.Image, layout: Layout) -> str:
    check_screen(img, layout)
    cell = _without_caret(_crop(img, layout.rects["search_field"]))
    if ocr.is_blank(cell):
        return ""
    return _rapid(cell, "search field", unscaled_retry=True)


def read_rows_detailed(img: Image.Image, layout: Layout) -> list[tuple[str, bool]]:
    """Per found row: the visible part of the article, and whether 1C cut it short (it ends
    with an ellipsis, read as trailing dots). [] when nothing was found."""
    check_screen(img, layout)
    grid = layout.grids["results"]
    rows = []
    for row in _present_rows(img, grid, ["row"], UnexpectedScreen):
        text = _rapid(_crop(img, grid.cell(row, "article")), f"article in row {row}",
                      unscaled_retry=True)
        if not _ARTICLE.fullmatch(text):
            raise ReadUncertain(f"article in row {row} reads as {text!r}")
        article = text.rstrip(". …")
        if not article:
            raise ReadUncertain(f"article in row {row} reads as {text!r}")
        rows.append((article, article != text.strip()))
    return rows


def read_rows(img: Image.Image, layout: Layout) -> list[str]:
    """The visible part of the article in each found row; [] when nothing was found."""
    return [article for article, _ in read_rows_detailed(img, layout)]


def read_stock(img: Image.Image, layout: Layout) -> dict[str, StockRow]:
    check_screen(img, layout)
    if stock_scrolls(img, layout):
        raise ReadUncertain(LONG_STOCK)
    _require_anchors(img, layout, "stock")
    grid = layout.grids["stock"]
    rows = _present_rows(img, grid, list(grid.columns), ReadUncertain)
    if not rows:
        raise ReadUncertain("the stock table has no rows")

    candidates = _references_for(layout, layout.warehouses)
    digits = layout.digits["regular"]
    result: dict[str, StockRow] = {}
    for row in rows:
        try:
            label = ocr.match_reference(_crop(img, grid.cell(row, "warehouse")), candidates)
        except ocr.Refused as e:
            raise ReadUncertain(f"warehouse in row {row} is not a known one: {e}") from e
        name = layout.warehouses[label]
        if name in result:
            raise ReadUncertain(f"warehouse {name} appears twice")
        on_hand = _read_count(_crop(img, grid.cell(row, "qty")), digits, f"quantity in row {row}")
        reserve = _read_count(_crop(img, grid.cell(row, "reserve")), digits, f"reserve in row {row}")
        result[name] = StockRow(name, on_hand, reserve)

    total = _read_int(_crop(img, layout.rects["stock_total"]), layout.digits["total"], "total")
    column_sum = sum(r.on_hand for r in result.values())
    if column_sum != total:
        raise ReadUncertain(f"quantities add up to {column_sum}, the total reads {total}")
    if "reserve_total" in layout.rects:
        # Unlike the quantity total, an empty reserve total is normal: no reserve anywhere.
        # It sits on the grey totals row, so "empty" here means no ink, not white.
        cell = _crop(img, layout.rects["reserve_total"])
        reserve_total = (
            _read_int(cell, layout.digits["total"], "reserve total") if ocr.has_ink(cell) else 0
        )
        reserve_sum = sum(r.reserve for r in result.values())
        if reserve_sum != reserve_total:
            raise ReadUncertain(f"reserves add up to {reserve_sum}, the reserve total reads {reserve_total}")
    return result


RUB = "RUB"


def read_prices(img: Image.Image, layout: Layout) -> tuple[Decimal, Decimal]:
    """(EPR, PPR) in rubles."""
    check_screen(img, layout)
    _require_anchors(img, layout, "prices")
    grid = layout.grids["prices"]
    known = {(r.type, r.currency): r for r in layout.price_rows}
    seen: dict[str, list[int]] = {}
    for row in _present_rows(img, grid, list(grid.columns), ReadUncertain):
        currency = _rapid(_crop(img, grid.cell(row, "currency")), f"currency in row {row}")
        try:
            price_type = _read_price_type(_crop(img, grid.cell(row, "type")), layout, row)
        except ReadUncertain:
            # Items carry price types the layout never saw (seen live: "HR $", "HR+" in USD).
            # A row of another currency cannot be EPR or PPR рубли and is skipped; an unknown
            # type in rubles might be one of ours misread, so it still refuses.
            if _normalize_label(currency) != RUB:
                continue
            raise
        entry = known.get((price_type, _normalize_label(currency)))
        if entry is None:
            raise ReadUncertain(f"row {row}: {price_type!r} in {currency!r} is not expected")
        seen.setdefault(entry.id, []).append(row)
    return tuple(_read_price(img, layout, seen, row_id) for row_id in (layout.epr_row, layout.ppr_row))


def _read_price(img, layout: Layout, seen: dict[str, list[int]], row_id: str) -> Decimal:
    rows = seen.get(row_id, [])
    if len(rows) != 1:
        raise ReadUncertain(f"expected exactly one row {row_id}, found {len(rows)}")
    cell = _crop(img, layout.grids["prices"].cell(rows[0], "price"))
    return _both_engines(cell, layout.digits["regular"], parse_price, f"price {row_id}")


def _read_price_type(cell: Image.Image, layout: Layout, row: int) -> str:
    """Latin types go through RapidOCR and Cyrillic ones through reference crops. A cell
    that satisfies both, or neither, is refused."""
    latin = {_normalize_label(r.type): r.type for r in layout.price_rows if not CYRILLIC.search(r.type)}
    cyrillic = {r.id: r.type for r in layout.price_rows if CYRILLIC.search(r.type)}
    found = []
    try:
        text = _normalize_label(ocr.rapid_text(cell))
    except ocr.Refused:
        text = None
    if text in latin:
        found.append(latin[text])
    try:
        found.append(cyrillic[ocr.match_reference(cell, _references_for(layout, cyrillic))])
    except ocr.Refused:
        pass
    if len(found) != 1:
        raise ReadUncertain(f"price type in row {row} is not a known one: {found or text!r}")
    return found[0]


def _normalize_label(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _references_for(layout: Layout, labels) -> dict[str, np.ndarray]:
    return {k: layout.references[k] for k in labels if k in layout.references}


def _rapid(cell: Image.Image, what: str, unscaled_retry: bool = False) -> str:
    """unscaled_retry: when the detector finds no text at all, recognise the cell's ink as one
    line (ocr.rapid_line). Only for texts the robot then compares with the article it typed
    (search field, article cells), so a different read is never taken as a value."""
    try:
        return ocr.rapid_text(cell)
    except ocr.Refused as e:
        if unscaled_retry and isinstance(e, ocr.NoText):
            try:
                return ocr.rapid_line(cell)
            except ocr.Refused:
                pass
        raise ReadUncertain(f"{what}: {e}") from e


def _parse_int(text: str) -> int | None:
    return int(text) if _QTY.fullmatch(text) else None


def parse_price(text: str) -> Decimal | None:
    match = _PRICE.fullmatch(text)
    return Decimal(match.group(1).replace(",", ".").rstrip(".")) if match else None


def _read_int(cell: Image.Image, templates, what: str) -> int:
    return _both_engines(cell, templates, _parse_int, what)


def _read_count(cell: Image.Image, templates, what: str) -> int:
    """1C leaves a zero quantity or reserve empty. Only a truly blank cell is 0; one with
    anything in it is read as a number or refused."""
    return 0 if ocr.is_blank(cell) else _read_int(cell, templates, what)


def _both_engines(cell: Image.Image, templates, parse, what: str):
    """The value, only if RapidOCR and the digit matcher both give it."""
    try:
        by_matcher = ocr.read_digits(cell, templates)
    except ocr.Refused as e:
        raise ReadUncertain(f"{what}: digit matcher refused: {e}") from e
    by_rapid = _rapid(cell, what)
    first, second = parse(by_matcher), parse(by_rapid)
    if first is None or second is None:
        raise ReadUncertain(f"{what}: not a number: {by_matcher!r}, {by_rapid!r}")
    if first != second:
        raise ReadUncertain(f"{what}: engines disagree: {by_matcher!r}, {by_rapid!r}")
    return first


PICTURE_INK = 240     # a pixel darker than this in some channel belongs to the picture
PICTURE_MIN_INK = 50  # fewer such pixels: the frame is empty, 1C has no picture
PICTURE_MARGIN = 8


def read_picture(img: Image.Image, layout: Layout) -> Image.Image | None:
    """The item's picture on the «Изображение» tab, trimmed to what is drawn plus a small
    white margin; None when the frame is empty. Only for showing to a person: nothing is
    decided from it."""
    check_screen(img, layout, image_tab_ok=True)
    if not _image_tab(img, layout):
        raise ReadUncertain("the image tab is not shown")
    frame = _crop(img, layout.rects["picture"]).convert("RGB")
    ink = np.asarray(frame).min(axis=2) < PICTURE_INK
    if np.count_nonzero(ink) < PICTURE_MIN_INK:
        return None
    ys, xs = np.nonzero(ink)
    box = (max(0, int(xs.min()) - PICTURE_MARGIN), max(0, int(ys.min()) - PICTURE_MARGIN),
           min(frame.width, int(xs.max()) + 1 + PICTURE_MARGIN),
           min(frame.height, int(ys.max()) + 1 + PICTURE_MARGIN))
    return frame.crop(box)


def crop_tables(img: Image.Image, layout: Layout) -> Image.Image:
    """Only the tables, for sending outside; the rest of the frame is left out."""
    return _crop(img, layout.rects["tables_crop"])
