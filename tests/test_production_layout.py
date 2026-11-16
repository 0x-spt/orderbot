"""The production layout against real captures of the operator's 1C window."""
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from orderbot.domain import StockRow
from orderbot.screen import calibrate
from orderbot.screen.reader import (
    Layout,
    ReadUncertain,
    UnexpectedScreen,
    check_screen,
    read_prices,
    read_rows,
    read_search_field,
    read_stock,
    tab_shown,
)

ROOT = Path(__file__).parent.parent
LAYOUTS = ROOT / "layouts"
SHOTS = LAYOUTS / "shots"
LAYOUT_PATH = LAYOUTS / "production.yaml"
SPEC_PATH = LAYOUTS / "production-crops.yaml"

ALL_SHOTS = [
    "probe-1",
    "den091-gry-stock",
    "den091-gry-prices",
    "al27018-stock",
    "al27018-prices",
    "tp213-wrd-stock",
    "tp213-wrd-prices",
    "tp213-wht-stock",
    "tp273-wrd-stock",
    "tp273-obl-stock",
]


def narrow_layout() -> Layout:
    """The production layout as it was before the article column was widened
   : the captures below were taken then. Only the grid header reference and
    the article column differ; everything else is the production layout itself."""
    import dataclasses

    from orderbot.screen import ocr

    lay = Layout.load(LAYOUT_PATH)
    lay.references["grid_header"] = ocr.load_mask(ROOT / "tests" / "fixtures" / "narrow-grid_header.png")
    results = lay.grids["results"]
    lay.grids["results"] = dataclasses.replace(
        results, columns={**results.columns, "article": (788, 932)})
    return lay


@pytest.fixture(scope="module")
def layout():
    return narrow_layout()


def shot(name):
    return Image.open(SHOTS / f"{name}.png").convert("RGB")


def whiten(img, rect):
    ImageDraw.Draw(img).rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill="white")


def rows(*items):
    return {name: StockRow(name, qty, reserve) for name, qty, reserve in items}


# --- layout and calibration ---------------------------------------------


def test_layout_size_and_settle(layout):
    assert layout.name == "production"
    assert layout.size == (2880, 1630)
    assert layout.data_settle_s == 2.0


def test_calibrate_rebuilds_committed_templates(tmp_path):
    report = calibrate.build(SPEC_PATH, out_root=tmp_path)
    for sub in ("production-digits", "production-labels"):
        built = {p.relative_to(tmp_path / sub): p.read_bytes() for p in (tmp_path / sub).rglob("*.png")}
        committed = {p.relative_to(LAYOUTS / sub): p.read_bytes() for p in (LAYOUTS / sub).rglob("*.png")}
        assert built == committed
    assert report.missing_digits["regular"] == []
    assert report.missing_labels == []
    assert report.missing_digits["total"] == [",", "."]  # never part of a total


@pytest.mark.parametrize("name", ALL_SHOTS)
def test_every_capture_is_the_expected_screen(layout, name):
    check_screen(shot(name), layout)


@pytest.mark.parametrize(
    "name, tab",
    [
        ("probe-1", "stock"),
        ("den091-gry-stock", "stock"),
        ("den091-gry-prices", "prices"),
        ("al27018-stock", "stock"),
        ("al27018-prices", "prices"),
        ("tp213-wrd-stock", "stock"),
        ("tp213-wrd-prices", "prices"),
        ("tp213-wht-stock", "stock"),
        ("tp273-wrd-stock", "stock"),
        ("tp273-obl-stock", "stock"),
    ],
)
def test_tab_shown(layout, name, tab):
    assert tab_shown(shot(name), layout) == tab


def test_foreign_window_is_unexpected(layout):
    img = shot("tp213-wrd-stock")
    d = ImageDraw.Draw(img)
    d.rectangle((40, 450, 1200, 1300), fill=(240, 240, 240), outline="black", width=3)
    with pytest.raises(UnexpectedScreen):
        check_screen(img, layout)


# --- search field and result rows ---------------------------------------


@pytest.mark.parametrize(
    "name, text",
    [
        ("probe-1", ""),
        ("den091-gry-prices", "DEN091-GRY(DEN091-01114Z)"),
        ("al27018-stock", "AL27018"),
        ("tp213-wrd-prices", "TP213-WRD(TP213-00425B)"),
        ("tp213-wht-stock", "TP213"),
        ("tp273-wrd-stock", "TP273"),
        ("tp273-obl-stock", "TP273"),
    ],
)
def test_search_field(layout, name, text):
    assert read_search_field(shot(name), layout) == text


@pytest.mark.parametrize(
    "name, article",
    [
        ("den091-gry-stock", "DEN091-G"),
        ("al27018-prices", "AL27018"),
        ("tp213-wrd-stock", "TP213-WR"),
    ],
)
def test_one_result_row(layout, name, article):
    assert read_rows(shot(name), layout) == [article]


def test_several_result_rows_fill_the_grid(layout):
    found = read_rows(shot("probe-1"), layout)
    assert len(found) == layout.grids["results"].rows == 27
    assert found[:2] == ["SOA231-M", "SOA231-SE"]
    assert found[-2:] == ["TP273-BBL(", "TP241-BBL("]


@pytest.mark.parametrize("name, row", [("tp213-wht-stock", 16), ("tp273-wrd-stock", 12)])
def test_mixed_case_article_in_results_refuses(layout, name, row):
    """"TP2x3-Crea…" comes out of RapidOCR as two boxes: refused, never joined."""
    with pytest.raises(ReadUncertain, match=f"article in row {row}"):
        read_rows(shot(name), layout)


# --- stock ---------------------------------------------------------------


def test_stock_den091(layout):
    assert read_stock(shot("den091-gry-stock"), layout) == rows(
        ("СЕВЕР", 4, 0), ("ВОСТОК 2", 10, 0), ("Южная,9", 2, 0), ("СПБ", 5, 0)
    )


def test_stock_al27018_with_focused_row(layout):
    assert read_stock(shot("al27018-stock"), layout) == rows(("Главный склад", 2, 0), ("Южная,9", 7, 0))


def test_stock_tp213_wrd_blank_quantities(layout):
    assert read_stock(shot("tp213-wrd-stock"), layout) == rows(
        ("СЕВЕР", 8, 0),
        ("ВОСТОК 2", 216, 0),
        ("Южная,9", 3, 0),
        ("ЕВРОПА", 6, 0),
        ("Европа Дубай", 84, 0),
        ("Склад (восто", 0, 120),  # blank quantity, reserve 120
        ("СПБ", 4, 0),
        ("Шоурум ДУБ", 0, 0),  # both blank
    )


def test_stock_tp213_wht(layout):
    assert read_stock(shot("tp213-wht-stock"), layout) == rows(
        ("СЕВЕР", 1, 0),
        ("ВОСТОК 2", 436, 0),
        ("Южная,9", 14, 0),
        ("Склад ДУБА", 0, 0),
        ("СПБ", 5, 0),
        ("ШАНХАЙ M50", 0, 0),
        ("Шоурум ДУБ", 0, 0),
    )


def test_stock_tp273_wrd(layout):
    assert read_stock(shot("tp273-wrd-stock"), layout) == rows(
        ("СЕВЕР", 5, 0),
        ("ВОСТОК 2", 180, 0),
        ("Южная,9", 5, 0),
        ("Европа Дубай", 90, 0),
        ("Склад (восто", 0, 150),
        ("СПБ", 6, 0),
        ("Шоурум ДУБ", 0, 0),
    )


def test_stock_tp273_obl_reserves_only(layout):
    assert read_stock(shot("tp273-obl-stock"), layout) == rows(
        ("СЕВЕР", 0, 1),
        ("ЕВРОПА", 300, 0),
        ("Склад (восто", 0, 78),
        ("Шоурум ДУБ", 0, 0),
    )


def test_reserve_total_is_checked(layout):
    """Reserves 1 + 78 against the reserve total 79: one reserve whitened away refuses."""
    img = shot("tp273-obl-stock")
    whiten(img, layout.grids["stock"].cell(2, "reserve"))
    with pytest.raises(ReadUncertain, match="reserves add up to 1, the reserve total reads 79"):
        read_stock(img, layout)


def test_reserve_where_the_reserve_total_is_empty_refuses(layout):
    """den091 has no reserve and an empty reserve total; a reserve put into a row refuses."""
    img = shot("den091-gry-stock")
    src = layout.grids["stock"].cell(0, "qty")  # "4"
    dst = layout.grids["stock"].cell(0, "reserve")
    img.paste(img.crop(src), (dst[2] - (src[2] - src[0]), dst[1]))
    with pytest.raises(ReadUncertain, match="reserves add up to 4, the reserve total reads 0"):
        read_stock(img, layout)


def test_stock_empty_table_refuses(layout):
    with pytest.raises(ReadUncertain, match="no rows"):
        read_stock(shot("probe-1"), layout)


@pytest.mark.parametrize("name", ["den091-gry-prices", "al27018-prices", "tp213-wrd-prices"])
def test_stock_on_prices_tab_is_unexpected(layout, name):
    with pytest.raises(UnexpectedScreen):
        read_stock(shot(name), layout)


def test_focus_border_lies_outside_every_cell(layout):
    """The dotted border of the focused row (al27018, row 0) is drawn next to the grid
    lines; no cell of the row or of its neighbour takes any of it in."""
    img = np.asarray(shot("al27018-stock").convert("L")).astype(int)
    grid = layout.grids["stock"]
    border = img[1034:1036, 40:470]
    assert (border < 128).any()  # the border is there
    for column in grid.columns:
        x0, y0, x1, y1 = grid.cell(0, column)
        assert y0 >= 1036 and y1 <= 1069 and x0 >= 34 and x1 <= 478
    for row in (0, 1):
        x0, y0, x1, y1 = grid.cell(row, "order")
        assert img[y0:y1, x0:x1].min() >= 248  # the empty column next to the border stays blank


def test_blank_quantity_counts_as_zero_in_the_total(layout):
    """The real total (321) only agrees when blank quantities count as 0; a quantity
    whitened away leaves the total short and refuses."""
    img = shot("tp213-wrd-stock")
    whiten(img, layout.grids["stock"].cell(6, "qty"))  # СПБ 4
    with pytest.raises(ReadUncertain, match="total"):
        read_stock(img, layout)


def test_row_with_both_blank_is_zero_zero(layout):
    img = shot("tp213-wrd-stock")
    whiten(img, layout.grids["stock"].cell(5, "reserve"))  # Москва: reserve 120 removed
    total = layout.rects["reserve_total"]  # and the reserve total 120 with it
    ImageDraw.Draw(img).rectangle(
        (total[0], total[1], total[2] - 1, total[3] - 1), fill=img.getpixel((total[0], total[1]))
    )
    assert read_stock(img, layout)["Склад (восто"] == StockRow("Склад (восто", 0, 0)


def test_mark_in_blank_quantity_refuses(layout):
    img = shot("tp213-wrd-stock")
    x0, y0, _, _ = layout.grids["stock"].cell(7, "qty")  # Шоурум: blank
    ImageDraw.Draw(img).rectangle((x0 + 40, y0 + 8, x0 + 44, y0 + 24), fill=(200, 200, 200))
    with pytest.raises(ReadUncertain, match="quantity in row 7"):
        read_stock(img, layout)


# --- prices --------------------------------------------------------------


@pytest.mark.parametrize(
    "name, epr, ppr",
    [
        ("den091-gry-prices", "1880", "1340"),
        ("al27018-prices", "7300", "5150"),
        ("tp213-wrd-prices", "4160", "2970"),
    ],
)
def test_prices(layout, name, epr, ppr):
    assert read_prices(shot(name), layout) == (Decimal(epr), Decimal(ppr))


@pytest.mark.parametrize("name", ["probe-1", "den091-gry-stock", "tp213-wht-stock"])
def test_prices_on_stock_tab_is_unexpected(layout, name):
    with pytest.raises(UnexpectedScreen):
        read_prices(shot(name), layout)


def test_live_frame_passes_screen_check():
    """Frame captured by the robot itself on the operator's Mac: the status
    line differs from the calibration frame by a few antialiased pixels."""
    from PIL import Image

    from orderbot.domain import StockRow
    from orderbot.screen.reader import check_screen, read_stock

    root = Path(__file__).resolve().parent.parent
    layout = narrow_layout()
    img = Image.open(root / "layouts" / "shots" / "live-den091-gry-stock.png")
    check_screen(img, layout)
    assert read_stock(img, layout) == {
        "СЕВЕР": StockRow("СЕВЕР", 4, 0), "ВОСТОК 2": StockRow("ВОСТОК 2", 9, 0),
        "Южная,9": StockRow("Южная,9", 2, 0), "СПБ": StockRow("СПБ", 5, 0)}


def test_empty_search_field_with_blinking_caret_reads_empty():
    """Live frame right after the × click: the field is empty but shows the text cursor."""
    from PIL import Image

    from orderbot.screen.reader import read_search_field

    root = Path(__file__).resolve().parent.parent
    layout = narrow_layout()
    img = Image.open(root / "layouts" / "shots" / "live-empty-field-caret.png")
    assert read_search_field(img, layout) == ""
    typed = Image.open(root / "layouts" / "shots" / "live-den091-gry-stock.png")
    assert read_search_field(typed, layout) == "den091-01114z"


def test_caret_right_after_typed_text_is_not_read_as_a_letter():
    """Live frame: the cursor touches the last typed glyph; OCR used to read it as "l"."""
    from PIL import Image

    from orderbot.screen.reader import read_search_field

    root = Path(__file__).resolve().parent.parent
    layout = narrow_layout()
    img = Image.open(root / "layouts" / "shots" / "live-typed-caret-tp213.png")
    assert read_search_field(img, layout) == "tp213-00425b"


def test_new_warehouses_from_live_snapshot(monkeypatch):
    """КИТАЙ and Склад MAINL appeared live (DF25001/GREEN); labels were cut
    from the robot's own tables snapshot pasted at the tables position."""
    from PIL import Image

    from orderbot.domain import StockRow
    from orderbot.screen import reader

    root = Path(__file__).resolve().parent.parent
    layout = narrow_layout()
    img = Image.open(root / "layouts" / "shots" / "live-df25001-stock-tables.png")
    monkeypatch.setattr(reader, "check_screen", lambda i, l: None)
    monkeypatch.setattr(reader, "_require_anchors", lambda *a: None)
    stock = reader.read_stock(img, layout)
    assert stock["КИТАЙ"] == StockRow("КИТАЙ", 624, 0)
    assert stock["Склад MAINL"] == StockRow("Склад MAINL", 92, 0)
    assert stock["Главный склад"] == StockRow("Главный склад", 419, 0)


def test_podsort_warehouse_from_live_capture():
    """ПОДСОРТИР… appeared live (STP223-NAC): unknown, it sent every such
    order to a person as unreadable."""
    from PIL import Image

    from orderbot.domain import StockRow
    from orderbot.screen import reader

    root = Path(__file__).resolve().parent.parent
    layout = narrow_layout()
    img = Image.open(root / "layouts" / "shots" / "live-stp-stock.png")
    stock = reader.read_stock(img, layout)
    assert stock["ПОДСОРТИР"] == StockRow("ПОДСОРТИР", 6, 0)
    assert stock["ВОСТОК 2"] == StockRow("ВОСТОК 2", 19, 0)
    assert stock["СПБ"] == StockRow("СПБ", 6, 0)
    assert len(stock) == 7


def test_two_found_rows_with_split_ellipsis_from_live_capture():
    """Seen live for STP281-NAC(STP281-01117N): the set NX-STP281… comes first and
    its ellipsis came back from OCR as a box of its own, which refused the whole list."""
    from PIL import Image

    from orderbot.screen import reader

    root = Path(__file__).resolve().parent.parent
    layout = narrow_layout()
    img = Image.open(root / "layouts" / "shots" / "live-stp281-two-rows.png")
    assert reader.read_rows_detailed(img, layout) == [("NX-STP281", True), ("STP281-NA", True)]



def test_widened_article_column_from_live_capture():
    """The production layout itself on the screen after the article column was widened
   : both found rows read in full, so the exact one can be chosen."""
    layout = Layout.load(LAYOUT_PATH)
    img = Image.open(SHOTS / "live-wide-article.png")
    check_screen(img, layout)
    assert read_search_field(img, layout) == "stp281-01117n"
    from orderbot.screen.reader import read_rows_detailed
    rows = read_rows_detailed(img, layout)
    assert rows[1] == ("STP281-NAC(STP281-01117N)", False)
    assert rows[0][0].startswith("NX-STP281-NAC-2") and not rows[0][1]


def test_narrow_capture_is_refused_by_the_widened_layout():
    """A screen with the old narrow article column is not the expected screen any more."""
    with pytest.raises(UnexpectedScreen):
        check_screen(Image.open(SHOTS / "probe-1.png"), Layout.load(LAYOUT_PATH))


def test_samples_warehouse_from_live_snapshot(monkeypatch):
    """«Образцы пер…» appeared live (order 1203-6): the label was cut from the
    robot's own tables snapshot pasted at the tables position."""
    from orderbot.screen import reader

    layout = Layout.load(LAYOUT_PATH)
    img = Image.open(SHOTS / "live-1203-6-stock-tables.png")
    monkeypatch.setattr(reader, "check_screen", lambda i, l: None)
    monkeypatch.setattr(reader, "_require_anchors", lambda *a: None)
    stock = reader.read_stock(img, layout)
    assert stock["Образцы пер"] == StockRow("Образцы пер", 1, 0)
    assert stock["СЕВЕР"] == StockRow("СЕВЕР", 19, 0)
    assert stock["Главный склад"] == StockRow("Главный склад", 10, 0)
    assert stock["Южная,9"] == StockRow("Южная,9", 7, 0)
    assert stock["СПБ"] == StockRow("СПБ", 15, 0)
    assert len(stock) == 5


# --- the «Изображение» tab (the 1C picture goes into the order message) -----


def test_image_tab_recognised_on_live_capture():
    """Live captures with the current layout: the same item on the «Остатки»
    tab and on the «Изображение» tab. The open tab has no line under it in the tab strip."""
    layout = Layout.load(LAYOUT_PATH)
    image = Image.open(SHOTS / "live-image-tab.png")
    stock = Image.open(SHOTS / "live-stock-tab-0611.png")
    check_screen(image, layout, image_tab_ok=True)   # the picture step only
    check_screen(stock, layout)
    assert tab_shown(image, layout) == "image"
    assert tab_shown(stock, layout) == "stock"


@pytest.mark.parametrize("name", ALL_SHOTS)
def test_image_tab_not_seen_on_stock_and_prices_captures(layout, name):
    assert tab_shown(shot(name), layout) != "image"


def test_tables_tail_still_checked_off_the_image_tab():
    """The exemption is only for the «Изображение» tab: the same ink on the stock tab is
    still a foreign window."""
    layout = Layout.load(LAYOUT_PATH)
    img = Image.open(SHOTS / "live-stock-tab-0611.png").convert("RGB")
    r = layout.blank_regions["tables_tail"]
    ImageDraw.Draw(img).rectangle((r[0] + 10, r[1], r[0] + 40, r[3] - 1), fill="black")
    with pytest.raises(UnexpectedScreen):
        check_screen(img, layout)


def test_foreign_window_on_the_image_tab_is_unexpected():
    layout = Layout.load(LAYOUT_PATH)
    img = Image.open(SHOTS / "live-image-tab.png").convert("RGB")
    ImageDraw.Draw(img).rectangle((40, 450, 1200, 1300), fill=(240, 240, 240),
                                   outline="black", width=3)
    with pytest.raises(UnexpectedScreen):
        check_screen(img, layout, image_tab_ok=True)


def test_image_tab_fails_the_screen_check_outside_the_picture_step():
    """no input of a lookup may pass the check with the tables_tail
    exemption; only the picture step (and the click back to «Остатки») asks for it."""
    layout = Layout.load(LAYOUT_PATH)
    image = Image.open(SHOTS / "live-image-tab.png")
    with pytest.raises(UnexpectedScreen, match="tables_tail"):
        check_screen(image, layout)
    _, degraded = _degraded_image_tab()
    with pytest.raises(UnexpectedScreen, match="tables_tail"):
        check_screen(degraded, layout)


def test_picture_from_live_image_tab():
    from orderbot.screen.reader import read_picture

    layout = Layout.load(LAYOUT_PATH)
    pic = read_picture(Image.open(SHOTS / "live-image-tab.png"), layout)
    assert pic is not None
    # the plate (x 131-378, y 1089-1335 of the frame) with a small white margin
    assert 240 <= pic.width <= 300 and 240 <= pic.height <= 300
    assert not np.all(np.asarray(pic.convert("L")) > 240)


def test_blank_picture_frame_gives_no_picture():
    from orderbot.screen.reader import read_picture

    layout = Layout.load(LAYOUT_PATH)
    img = Image.open(SHOTS / "live-image-tab.png").convert("RGB")
    whiten(img, layout.rects["picture"])
    assert read_picture(img, layout) is None


def test_picture_is_not_read_off_the_image_tab():
    from orderbot.screen.reader import read_picture

    layout = Layout.load(LAYOUT_PATH)
    with pytest.raises(ReadUncertain):
        read_picture(Image.open(SHOTS / "live-stock-tab-0611.png"), layout)


def _degraded_image_tab():
    """The live image-tab capture with a few stray dark pixels in the tab strip, so its
    reference scores between 0.80 and 0.90 (hover shading, antialiasing)."""
    from orderbot.screen import ocr

    layout = Layout.load(LAYOUT_PATH)
    img = Image.open(SHOTS / "live-image-tab.png").convert("RGB")
    ImageDraw.Draw(img).line((250, 982, 279, 982), fill=(150, 145, 120))
    rect = layout.anchors["image"]["tab_image_on"]
    score = ocr.similarity(ocr.ink_mask(img.crop(rect)), layout.references["tab_image_on"])
    assert 0.80 <= score < ocr.ANCHOR_MIN_SCORE
    return layout, img


def test_image_tab_recognised_at_the_lower_bar():
    layout, img = _degraded_image_tab()
    assert tab_shown(img, layout) == "image"
    check_screen(img, layout, image_tab_ok=True)   # the tables_tail exemption applies too


def test_lower_bar_needs_both_table_headers_absent():
    """A frame that shows a table header is never the image tab, whatever the strip says."""
    from orderbot.screen.reader import _image_tab

    layout, img = _degraded_image_tab()
    stock = Image.open(SHOTS / "live-stock-tab-0611.png").convert("RGB")
    for rect in layout.anchors["stock"].values():
        img.paste(stock.crop(rect), rect[:2])
    assert not _image_tab(img, layout)


def test_transit_warehouse_from_live_snapshot(monkeypatch):
    """«Транзитный в…» with an empty quantity appeared live (SOP161-CHS):
    unknown, it sent the order to a person as unreadable."""
    from PIL import Image

    from orderbot.domain import StockRow
    from orderbot.screen import reader

    root = Path(__file__).resolve().parent.parent
    layout = reader.Layout.load(root / "layouts" / "production.yaml")
    img = Image.open(root / "layouts" / "shots" / "live-sop161-stock-tables.png")
    monkeypatch.setattr(reader, "check_screen", lambda i, l: None)
    monkeypatch.setattr(reader, "_require_anchors", lambda *a: None)
    stock = reader.read_stock(img, layout)
    assert stock["Транзитный в"] == StockRow("Транзитный в", 0, 0)
    assert stock["Шоурум ДУБ"] == StockRow("Шоурум ДУБ", 0, 0)
    assert stock["Склад (восто"] == StockRow("Склад (восто", 25, 0)
    assert len(stock) == 6


def test_faint_dot_under_the_only_row_is_not_a_row(monkeypatch):
    """Live (NRC121-DNP, one warehouse): a single pixel (253, 252, 246) at the
    top right of the empty row 1 made it count as a row with no warehouse name."""
    from PIL import Image

    from orderbot.domain import StockRow
    from orderbot.screen import reader

    root = Path(__file__).resolve().parent.parent
    layout = reader.Layout.load(root / "layouts" / "production.yaml")
    img = Image.open(root / "layouts" / "shots" / "live-nrc121-stock-tables.png")
    monkeypatch.setattr(reader, "check_screen", lambda i, l: None)
    monkeypatch.setattr(reader, "_require_anchors", lambda *a: None)
    assert reader.read_stock(img, layout) == {"СЕВЕР": StockRow("СЕВЕР", 8, 0)}


def test_search_field_with_slash_read_at_own_size():
    """Live: «df05199/f» in the search field gave no text at all at x2; at its
    own size it reads exactly. The order went to a person as «the screen did not settle»."""
    from PIL import Image

    from orderbot.screen import reader

    root = Path(__file__).resolve().parent
    cell = Image.open(root / "fixtures" / "live-field-df05199.png")
    assert reader._rapid(cell, "search field", unscaled_retry=True) == "df05199/f"
    with pytest.raises(reader.ReadUncertain):
        reader._rapid(cell, "currency")  # values never get the second reading



def test_long_stock_table_is_the_expected_window_but_not_read():
    """Live: TP231-WHT has ten warehouse rows, 1C adds a scroll bar, narrows the
    columns and shows the tenth row in the tail band. Not a foreign window (the robot used
    to stop and pause on it), but not readable: the item goes to a person."""
    layout = Layout.load(LAYOUT_PATH)
    img = shot("live-tp231-stock-overflow")
    check_screen(img, layout)
    assert tab_shown(img, layout) == "stock"
    with pytest.raises(ReadUncertain, match="longer than the window"):
        read_stock(img, layout)


@pytest.mark.parametrize("name", ALL_SHOTS)
def test_no_scroll_bar_on_the_usual_captures(name):
    from orderbot.screen.reader import stock_scrolls
    assert not stock_scrolls(shot(name), Layout.load(LAYOUT_PATH))


def test_tail_ink_without_the_scroll_bar_is_still_a_foreign_window():
    layout = Layout.load(LAYOUT_PATH)
    img = shot("live-tp231-stock-overflow")
    r = layout.rects["stock_scrollbar"]
    whiten(img, (r[0] - 2, r[1], r[2] + 30, r[3]))
    with pytest.raises(UnexpectedScreen, match="tables_tail"):
        check_screen(img, layout)
