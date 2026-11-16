import shutil
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from orderbot.domain import StockRow
from orderbot.screen import calibrate, ocr, reader
from orderbot.screen.reader import (
    Layout,
    ReadUncertain,
    UnexpectedScreen,
    check_screen,
    crop_tables,
    parse_price,
    read_prices,
    read_rows,
    read_search_field,
    read_stock,
)

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
LAYOUT_PATH = ROOT / "layouts" / "fixture.yaml"
SPEC_PATH = ROOT / "layouts" / "fixture-crops.yaml"
STOCK_FRAME_OFFSET_Y = 2  # the two screenshots were cut by hand and differ by this shift


def raw_stock():
    return Image.open(FIXTURES / "onec_stock.png").convert("RGB")


@pytest.fixture(scope="module")
def layout():
    return Layout.load(LAYOUT_PATH)


@pytest.fixture
def stock_img(layout):
    w, h = layout.size
    return raw_stock().crop((0, STOCK_FRAME_OFFSET_Y, w, STOCK_FRAME_OFFSET_Y + h))


@pytest.fixture
def prices_img():
    return Image.open(FIXTURES / "onec_prices.png").convert("RGB")


def whiten(img, rect):
    ImageDraw.Draw(img).rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill="white")


def paste_cell(img, source, dest_rect):
    """Copies a same-size crop into a cell of the frame."""
    img.paste(source, (dest_rect[0], dest_rect[1]))


def copy_row(img, grid, src_row, dst_row):
    for col in grid.columns:
        paste_cell(img, img.crop(grid.cell(src_row, col)), grid.cell(dst_row, col))


def blank_row(img, grid, row):
    for col in grid.columns:
        whiten(img, grid.cell(row, col))


def glyph_at(img, rect, index):
    """A tight crop of one glyph of a cell, as an image, plus its top offset."""
    crop = img.crop(rect)
    x0, x1, y0, y1 = ocr.glyph_boxes(ocr.ink_mask(crop))[index]
    return crop.crop((x0, y0, x1, y1)), y0


def paste_glyph(img, rect, glyph, top, right_margin=4):
    """Pastes a glyph right-aligned in a cell, as a number is drawn there."""
    x = rect[2] - right_margin - glyph.width
    img.paste(glyph, (x, rect[1] + top))


# --- the two screenshots -------------------------------------------------


def test_stock_fixture(stock_img, layout):
    assert read_stock(stock_img, layout) == {
        "СЕВЕР": StockRow("СЕВЕР", 6, 0),
        "Южная,9": StockRow("Южная,9", 3, 0),
        "Склад (восто": StockRow("Склад (восто", 7, 0),
        "СПБ": StockRow("СПБ", 6, 0),
    }


def test_prices_fixture(prices_img, layout):
    assert read_prices(prices_img, layout) == (Decimal("1880"), Decimal("1340"))


def test_check_screen_accepts_both_tabs(stock_img, prices_img, layout):
    check_screen(stock_img, layout)
    check_screen(prices_img, layout)


def test_layout_holds_size_and_rects(layout):
    assert layout.size == (2880, 1672)
    for name in ("search_field", "result_rows", "tabs", "stock_table", "prices_table", "stock_total"):
        assert name in layout.rects
    assert layout.grids["results"].rows > 1


def test_search_field(prices_img, layout):
    assert read_search_field(prices_img, layout) == "DEN091-CRM(DEN091-00422U)"


def test_crop_tables(stock_img, layout):
    x0, y0, x1, y1 = layout.rects["tables_crop"]
    assert crop_tables(stock_img, layout).size == (x1 - x0, y1 - y0)


# --- which screen this is ------------------------------------------------


def test_wrong_size_raises_unexpected_screen(layout):
    frame = raw_stock()
    assert frame.size != layout.size
    with pytest.raises(UnexpectedScreen):
        check_screen(frame, layout)
    with pytest.raises(UnexpectedScreen):
        read_stock(frame, layout)


def test_blank_screen_raises_unexpected_screen(layout):
    with pytest.raises(UnexpectedScreen):
        check_screen(Image.new("RGB", layout.size, "white"), layout)


@pytest.mark.parametrize(
    "box",
    [
        (800, 500, 1800, 1100),  # empty part of the result grid
        (40, 400, 400, 900),  # empty part of the tree
        (300, 1000, 1200, 1400),  # over the tables
        (0, 0, 2880, 1672),  # everything
    ],
)
def test_foreign_window_raises_unexpected_screen(stock_img, layout, box):
    d = ImageDraw.Draw(stock_img)
    d.rectangle(box, fill=(240, 240, 240), outline="black", width=3)
    d.rectangle((box[0], box[1], box[2], box[1] + 40), fill=(0, 80, 200))
    with pytest.raises(UnexpectedScreen):
        check_screen(stock_img, layout)
    with pytest.raises(UnexpectedScreen):
        read_stock(stock_img, layout)


def test_read_stock_on_prices_tab_raises(prices_img, layout):
    with pytest.raises(UnexpectedScreen):
        read_stock(prices_img, layout)


def test_read_prices_on_stock_tab_raises(stock_img, layout):
    with pytest.raises(UnexpectedScreen):
        read_prices(stock_img, layout)


# --- stock ---------------------------------------------------------------


def qty_glyph(img, layout, row):
    return glyph_at(img, layout.grids["stock"].cell(row, "qty"), 0)


def test_total_mismatch_raises(stock_img, prices_img, layout):
    grid = layout.grids["stock"]
    eight, top = glyph_at(prices_img, layout.grids["prices"].cell(0, "price"), 1)  # "1880": 8
    rect = grid.cell(0, "qty")
    whiten(stock_img, rect)
    paste_glyph(stock_img, rect, eight, top)
    with pytest.raises(ReadUncertain, match="total"):
        read_stock(stock_img, layout)


def test_reserve_column_is_read(stock_img, layout):
    grid = layout.grids["stock"]
    three, top = qty_glyph(stock_img, layout, 1)
    paste_glyph(stock_img, grid.cell(0, "reserve"), three, top)
    result = read_stock(stock_img, layout)
    assert result["СЕВЕР"] == StockRow("СЕВЕР", 6, 3)
    assert result["СПБ"] == StockRow("СПБ", 6, 0)


def test_unreadable_reserve_raises_not_zero(stock_img, layout):
    x0, y0, _, _ = layout.grids["stock"].cell(0, "reserve")
    ImageDraw.Draw(stock_img).rectangle((x0 + 40, y0 + 8, x0 + 60, y0 + 24), fill="black")
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_faint_mark_in_reserve_is_not_blank(stock_img, layout):
    x0, y0, _, _ = layout.grids["stock"].cell(0, "reserve")
    ImageDraw.Draw(stock_img).rectangle((x0 + 40, y0 + 8, x0 + 44, y0 + 24), fill=(200, 200, 200))
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_solid_colour_over_reserve_is_not_blank(stock_img, layout):
    """Uniform but not the empty-cell white: something covers the cell, it is not zero."""
    rect = layout.grids["stock"].cell(0, "reserve")
    ImageDraw.Draw(stock_img).rectangle(
        (rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=(236, 233, 216)
    )
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def move_east_to_reserve(img, layout):
    """Москва's 7 goes from the quantity to the reserve, leaving the quantity truly blank."""
    grid = layout.grids["stock"]
    seven, top = qty_glyph(img, layout, 2)
    whiten(img, grid.cell(2, "qty"))
    paste_glyph(img, grid.cell(2, "reserve"), seven, top)


def total_reads(monkeypatch, value):
    """The total cell reads as `value`; every other number is read for real."""
    real = reader._read_int
    monkeypatch.setattr(
        reader, "_read_int", lambda cell, t, what: value if what == "total" else real(cell, t, what)
    )


def test_blank_quantity_with_reserve_reads_zero(stock_img, layout, monkeypatch):
    move_east_to_reserve(stock_img, layout)
    total_reads(monkeypatch, 15)  # 6 + 3 + 0 + 6
    assert read_stock(stock_img, layout)["Склад (восто"] == StockRow("Склад (восто", 0, 7)


def test_blank_quantity_goes_into_the_total_as_zero(stock_img, layout):
    move_east_to_reserve(stock_img, layout)
    with pytest.raises(ReadUncertain, match="add up to 15, the total reads 22"):
        read_stock(stock_img, layout)


def test_row_with_quantity_and_reserve_blank_is_zero(stock_img, layout, monkeypatch):
    whiten(stock_img, layout.grids["stock"].cell(3, "qty"))  # СПБ 6, reserve already blank
    total_reads(monkeypatch, 16)
    result = read_stock(stock_img, layout)
    assert result["СПБ"] == StockRow("СПБ", 0, 0)
    assert result["СЕВЕР"] == StockRow("СЕВЕР", 6, 0)


def test_mark_in_quantity_is_not_blank(stock_img, layout):
    rect = layout.grids["stock"].cell(3, "qty")
    whiten(stock_img, rect)
    ImageDraw.Draw(stock_img).rectangle(
        (rect[0] + 40, rect[1] + 8, rect[0] + 44, rect[1] + 24), fill=(200, 200, 200)
    )
    with pytest.raises(ReadUncertain, match="quantity in row 3"):
        read_stock(stock_img, layout)


@pytest.mark.parametrize("column", ["warehouse", "qty"])
def test_row_with_one_column_missing_raises(stock_img, layout, column):
    whiten(stock_img, layout.grids["stock"].cell(2, column))
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_row_with_only_reserve_raises(stock_img, layout):
    """The row below the last one holds nothing but one digit: it must not be dropped."""
    grid = layout.grids["stock"]
    three, top = qty_glyph(stock_img, layout, 1)
    paste_glyph(stock_img, grid.cell(4, "reserve"), three, top)
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_row_below_a_gap_raises(stock_img, layout):
    grid = layout.grids["stock"]
    copy_row(stock_img, grid, 3, 5)
    blank_row(stock_img, grid, 3)
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_unknown_warehouse_raises_not_dropped(stock_img, layout):
    """A label that is no reference: "Москва" with the rest of the text removed."""
    grid = layout.grids["stock"]
    rect = grid.cell(2, "warehouse")
    x1 = rect[0] + 90
    whiten(stock_img, (x1, rect[1], rect[2], rect[3]))
    with pytest.raises(ReadUncertain, match="warehouse"):
        read_stock(stock_img, layout)


def test_warehouse_differing_in_one_character_raises(stock_img, prices_img, layout):
    """"Южная,8" must not pass for "Южная,9"."""
    rect = layout.grids["stock"].cell(1, "warehouse")
    crop = stock_img.crop(rect)
    x0, x1, y0, y1 = ocr.glyph_boxes(ocr.ink_mask(crop))[-1]
    eight, _ = glyph_at(prices_img, layout.grids["prices"].cell(0, "price"), 1)
    whiten(stock_img, (rect[0] + x0, rect[1], rect[0] + x1, rect[3]))
    stock_img.paste(eight, (rect[0] + x0, rect[1] + y0))
    with pytest.raises(ReadUncertain, match="warehouse"):
        read_stock(stock_img, layout)


def test_duplicate_warehouse_raises(stock_img, layout):
    grid = layout.grids["stock"]
    paste_cell(stock_img, stock_img.crop(grid.cell(3, "warehouse")), grid.cell(0, "warehouse"))
    with pytest.raises(ReadUncertain):  # the quantities and the total still agree
        read_stock(stock_img, layout)


def test_no_stock_rows_raises(stock_img, layout):
    grid = layout.grids["stock"]
    for row in range(4):
        blank_row(stock_img, grid, row)
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_blank_total_raises(stock_img, layout):
    rect = layout.rects["stock_total"]
    ImageDraw.Draw(stock_img).rectangle(
        (rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=stock_img.getpixel((rect[0], rect[1]))
    )
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_total_digit_without_template_refuses(stock_img, layout):
    """Only the "2" of the total style exists on the fixtures: another digit refuses."""
    rect = layout.rects["stock_total"]
    bg = stock_img.getpixel((rect[0], rect[1]))
    ImageDraw.Draw(stock_img).rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=bg)
    three = stock_img.crop(layout.grids["stock"].cell(1, "qty"))
    paste_cell(stock_img, three, (rect[0], rect[1]))
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, layout)


def test_missing_digit_template_refuses(stock_img, tmp_path):
    shutil.copy(LAYOUT_PATH, tmp_path)
    shutil.copytree(ROOT / "layouts" / "fixture-digits", tmp_path / "fixture-digits")
    shutil.copytree(ROOT / "layouts" / "fixture-labels", tmp_path / "fixture-labels")
    (tmp_path / "fixture-digits" / "7.png").unlink()
    with pytest.raises(ReadUncertain):
        read_stock(stock_img, Layout.load(tmp_path / "fixture.yaml"))


# --- prices --------------------------------------------------------------


def test_engines_disagree_on_price_raises(prices_img, layout, monkeypatch):
    real = ocr.rapid_text

    def wrong(img):
        text = real(img)
        return text.replace("1880", "1980")

    monkeypatch.setattr(ocr, "rapid_text", wrong)
    with pytest.raises(ReadUncertain, match="disagree"):
        read_prices(prices_img, layout)


def test_price_cut_before_comma_raises(prices_img, layout):
    rect = layout.grids["prices"].cell(0, "price")
    crop = prices_img.crop(rect)
    boxes = ocr.glyph_boxes(ocr.ink_mask(crop))  # 1 8 8 0 , 0 0 . . .
    whiten(prices_img, rect)
    x = boxes[0][0]
    for i in (0, 1, 2, 3, 5, 7, 8, 9):  # "18800..."
        g0, g1, _, _ = boxes[i]
        prices_img.paste(crop.crop((g0, 0, g1, crop.height)), (rect[0] + x, rect[1]))
        x += g1 - g0 + 3
    with pytest.raises(ReadUncertain):
        read_prices(prices_img, layout)


def test_ppr_aed_not_used(prices_img, layout):
    epr, ppr = read_prices(prices_img, layout)
    assert Decimal("50") not in (epr, ppr)
    assert ppr == Decimal("1340")


def test_only_ppr_aed_left_raises(prices_img, layout):
    blank_row(prices_img, layout.grids["prices"], 4)
    with pytest.raises(ReadUncertain):
        read_prices(prices_img, layout)


def test_missing_epr_row_raises(prices_img, layout):
    grid = layout.grids["prices"]
    for row in range(4):
        copy_row(prices_img, grid, row + 1, row)
    blank_row(prices_img, grid, 4)
    with pytest.raises(ReadUncertain):
        read_prices(prices_img, layout)


def test_blanked_epr_row_with_rows_below_raises(prices_img, layout):
    blank_row(prices_img, layout.grids["prices"], 0)
    with pytest.raises(ReadUncertain):
        read_prices(prices_img, layout)


def test_epr_in_other_currency_raises(prices_img, layout):
    grid = layout.grids["prices"]
    paste_cell(prices_img, prices_img.crop(grid.cell(1, "currency")), grid.cell(0, "currency"))
    with pytest.raises(ReadUncertain):
        read_prices(prices_img, layout)


def test_second_epr_row_raises(prices_img, layout):
    grid = layout.grids["prices"]
    copy_row(prices_img, grid, 0, 5)
    with pytest.raises(ReadUncertain):
        read_prices(prices_img, layout)


def test_unknown_price_type_in_rubles_raises(prices_img, layout):
    """An unreadable type in rubles may be EPR or PPR рубли misread: it refuses. (Unknown
    types in other currencies are skipped, see test_unknown_price_type_in_other_currency.)"""
    rect = layout.grids["prices"].cell(4, "type")
    whiten(prices_img, (rect[0] + 60, rect[1], rect[2], rect[3]))  # "PPR рубли" cut to "PP"
    with pytest.raises(ReadUncertain):
        read_prices(prices_img, layout)


def test_unknown_price_type_in_euro_is_skipped(prices_img, layout):
    rect = layout.grids["prices"].cell(2, "type")
    whiten(prices_img, (rect[0] + 60, rect[1], rect[2], rect[3]))  # "HR евро" cut to "HR"
    assert read_prices(prices_img, layout) == (Decimal("1880"), Decimal("1340"))


def test_prices_follow_the_pixels(prices_img, layout):
    """Both real engines read an altered value: "1880" drawn as "1340"-like "1380"."""
    grid = layout.grids["prices"]
    rect = grid.cell(0, "price")
    crop = prices_img.crop(rect)
    x0, x1, _, _ = ocr.glyph_boxes(ocr.ink_mask(crop))[1]
    source = prices_img.crop(grid.cell(4, "price"))
    s0, s1, _, _ = ocr.glyph_boxes(ocr.ink_mask(source))[1]  # "1340": 3
    assert s1 - s0 == x1 - x0
    prices_img.paste(source.crop((s0, 0, s1, source.height)), (rect[0] + x0, rect[1]))
    assert read_prices(prices_img, layout) == (Decimal("1380"), Decimal("1340"))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1880,00...", Decimal("1880")),
        ("1880,00.", Decimal("1880")),
        ("1880,", Decimal("1880")),
        ("15,7100", Decimal("15.71")),
        ("18800...", None),
        ("18800", None),
        ("1880.00", None),
        (",50", None),
        ("1 880,00", None),
    ],
)
def test_price_format(text, expected):
    assert parse_price(text) == expected


# --- frames unlike the fixtures: a correct value or a refusal, nothing else --


def noisy(img, sigma):
    a = np.asarray(img, dtype=float)
    noise = np.random.default_rng(1).normal(0, sigma, a.shape)
    return Image.fromarray(np.clip(a + noise, 0, 255).astype(np.uint8))


def shifted(img, dx, dy):
    moved = Image.new("RGB", img.size, "white")
    moved.paste(img, (dx, dy))
    return moved


def dimmed(img, factor):
    return Image.fromarray((np.asarray(img, dtype=float) * factor).astype(np.uint8))


DEGRADATIONS = {
    "noise_2": lambda i: noisy(i, 2),
    "noise_8": lambda i: noisy(i, 8),
    "noise_30": lambda i: noisy(i, 30),
    "shift_1": lambda i: shifted(i, 1, 1),
    "shift_4": lambda i: shifted(i, 4, 4),
    "dimmed": lambda i: dimmed(i, 0.8),
    "blurred": lambda i: i.filter(ImageFilter.GaussianBlur(1)),
}


@pytest.mark.parametrize("name", DEGRADATIONS)
def test_degraded_frames_give_right_values_or_refuse(stock_img, prices_img, layout, name):
    degrade = DEGRADATIONS[name]
    try:
        stock = read_stock(degrade(stock_img), layout)
    except (ReadUncertain, UnexpectedScreen):
        stock = None
    try:
        prices = read_prices(degrade(prices_img), layout)
    except (ReadUncertain, UnexpectedScreen):
        prices = None
    assert stock in (None, read_stock(stock_img, layout))
    assert prices in (None, (Decimal("1880"), Decimal("1340")))


@pytest.mark.parametrize("name", DEGRADATIONS)
def test_degraded_cells_give_right_values_or_refuse(stock_img, prices_img, layout, name, monkeypatch):
    """The same with the screen checks off, as these frames rarely get past them."""
    expected_stock = read_stock(stock_img, layout)
    monkeypatch.setattr(reader, "check_screen", lambda img, layout: None)
    monkeypatch.setattr(reader, "_require_anchors", lambda img, layout, group: None)
    degrade = DEGRADATIONS[name]
    try:
        assert read_stock(degrade(stock_img), layout) == expected_stock
    except ReadUncertain:
        pass
    try:
        assert read_prices(degrade(prices_img), layout) == (Decimal("1880"), Decimal("1340"))
    except ReadUncertain:
        pass


# --- result rows ---------------------------------------------------------


def test_one_row(prices_img, layout):
    assert read_rows(prices_img, layout) == ["DEN091-C"]


def test_zero_rows(prices_img, layout):
    blank_row(prices_img, layout.grids["results"], 0)
    assert read_rows(prices_img, layout) == []


def test_several_rows(prices_img, layout):
    copy_row(prices_img, layout.grids["results"], 0, 1)
    assert read_rows(prices_img, layout) == ["DEN091-C", "DEN091-C"]


def test_row_with_unreadable_article_raises(prices_img, layout):
    whiten(prices_img, layout.grids["results"].cell(0, "article"))
    with pytest.raises(ReadUncertain):
        read_rows(prices_img, layout)


def test_row_below_a_gap_is_unexpected(prices_img, layout):
    grid = layout.grids["results"]
    copy_row(prices_img, grid, 0, 2)
    with pytest.raises(UnexpectedScreen):
        read_rows(prices_img, layout)


# --- digit matcher -------------------------------------------------------


def square(w, h, hole=None):
    m = np.ones((h, w), bool)
    if hole:
        m[hole[0] : hole[1], hole[2] : hole[3]] = False
    return m


def cell_with(mask):
    h, w = mask.shape
    canvas = np.full((h + 8, w + 8), 255, np.uint8)
    canvas[4:4 + h, 4:4 + w][mask] = 0
    return Image.fromarray(canvas).convert("RGB")


def test_matcher_reads_exact_template():
    assert ocr.read_digits(cell_with(square(10, 16)), {"1": square(10, 16)}) == "1"


def test_matcher_refuses_low_score():
    with pytest.raises(ocr.Refused):
        ocr.read_digits(cell_with(square(10, 16)), {"1": square(5, 8)})


def test_matcher_refuses_two_close_candidates():
    templates = {"1": square(10, 16, hole=(5, 6, 5, 6)), "2": square(10, 16, hole=(9, 10, 5, 6))}
    with pytest.raises(ocr.Refused):
        ocr.read_digits(cell_with(square(10, 16)), templates)


def test_matcher_refuses_dot_except_as_trailing_ellipsis():
    templates = {"1": square(10, 16), ",": square(2, 5), ".": square(2, 2, None)}

    def line(*masks):
        rows = 20
        parts = []
        for m in masks:
            block = np.zeros((rows, m.shape[1] + 3), bool)
            block[rows - m.shape[0] :, : m.shape[1]] = m
            parts.append(block)
        return cell_with(np.hstack(parts))

    one, comma, dot = templates["1"], templates[","], templates["."]
    assert ocr.read_digits(line(one, comma, one, dot, dot, dot), templates) == "1,1..."
    for bad in (
        line(one, dot, one),  # dot between digits
        line(one, dot, dot, dot),  # ellipsis with no comma before it
        line(one, comma, one, dot, dot),  # not three dots
    ):
        with pytest.raises(ocr.Refused):
            ocr.read_digits(bad, templates)


# --- calibration ---------------------------------------------------------


def test_calibrate_rebuilds_committed_templates(tmp_path):
    report = calibrate.build(SPEC_PATH, out_root=tmp_path)
    for sub in ("fixture-digits", "fixture-labels"):
        built = {
            p.relative_to(tmp_path / sub): p.read_bytes()
            for p in (tmp_path / sub).rglob("*.png")
        }
        committed = {
            p.relative_to(ROOT / "layouts" / sub): p.read_bytes()
            for p in (ROOT / "layouts" / sub).rglob("*.png")
        }
        assert built == committed
    assert report.missing_digits["regular"] == []
    assert report.missing_labels == []


def test_calibrate_reports_missing_digit_templates(tmp_path):
    report = calibrate.build(SPEC_PATH, out_root=tmp_path)
    assert report.present_digits["total"] == ["2"]
    assert set(report.missing_digits["total"]) == set("013456789,.")
    assert "digits total: MISSING 0 1 3 4 5 6 7 8 9 ," in report.text()


def test_calibrate_rejects_wrong_label_count(tmp_path):
    bad = tmp_path / "bad.yaml"
    shutil.copy(LAYOUT_PATH, tmp_path / "fixture.yaml")
    img = str(FIXTURES / "onec_prices.png")
    bad.write_text(
        "layout: fixture.yaml\n"
        f"images: {{prices: {{path: '{img}'}}}}\n"
        "digits:\n"
        "  - {image: prices, rect: [258, 1200, 367, 1233], text: '15'}\n"
        "labels: []\n"
    )
    with pytest.raises(calibrate.CalibrationError):
        calibrate.build(bad, out_root=tmp_path)


def test_calibrate_rejects_conflicting_crops(tmp_path):
    """The same character from two crops with different shapes means another rendering."""
    bad = tmp_path / "bad.yaml"
    shutil.copy(LAYOUT_PATH, tmp_path / "fixture.yaml")
    img = str(FIXTURES / "onec_prices.png")
    bad.write_text(
        "layout: fixture.yaml\n"
        f"images: {{prices: {{path: '{img}'}}}}\n"
        "digits:\n"
        "  - {image: prices, rect: [258, 1200, 367, 1233], text: '15,7100'}\n"
        "  - {image: prices, rect: [258, 1238, 367, 1271], text: '1,5700'}\n"
        "labels: []\n"
    )
    with pytest.raises(calibrate.CalibrationError):
        calibrate.build(bad, out_root=tmp_path)


def test_calibrate_command_prints_what_is_missing(tmp_path, capsys):
    shutil.copy(LAYOUT_PATH, tmp_path / "fixture.yaml")
    spec = tmp_path / "crops.yaml"
    img = str(FIXTURES / "onec_prices.png")
    spec.write_text(
        "layout: fixture.yaml\n"
        f"images: {{prices: {{path: '{img}'}}}}\n"
        "digits:\n"
        "  - {image: prices, rect: [258, 1200, 367, 1233], text: '15,7100'}\n"
        "labels: []\n"
    )
    assert calibrate.main([str(spec)]) == 0
    out = capsys.readouterr().out
    assert "digits regular: have 0 1 5 7 ," in out
    assert "digits regular: MISSING 2 3 4 6 8 9 ." in out
    assert "wh_spb" in out
    assert (tmp_path / "fixture-digits" / "5.png").exists()


class CaptureWindow:
    """A fake RDP window: it only hands out a frame, as MacRdpWindow.screenshot() would."""

    def __init__(self, title, fail=None):
        self.title = title
        self.fail = fail
        self.calls = []

    def connect(self):
        self.calls.append("connect")
        if self.fail:
            raise self.fail

    def screenshot(self):
        self.calls.append("screenshot")
        return Image.new("RGB", (40, 30), "white")

    def disconnect(self):
        self.calls.append("disconnect")


def test_calibrate_capture_saves_the_robots_own_frame(tmp_path, capsys):
    made = []

    def factory(title):
        made.append(CaptureWindow(title))
        return made[-1]

    out = tmp_path / "shots" / "stock.png"
    code = calibrate.main(["--capture", str(out)], window_factory=factory,
                          env={"RDP_WINDOW_TITLE_CONTAINS": "rdp.example"})
    assert code == 0
    assert made[0].title == "rdp.example"
    assert made[0].calls == ["connect", "screenshot", "disconnect"]   # no input, no activation
    with Image.open(out) as img:
        assert img.size == (40, 30)
    assert "40x30" in capsys.readouterr().out


def test_calibrate_capture_reports_a_missing_window(tmp_path, capsys):
    from orderbot.screen.session import SessionLost

    code = calibrate.main(["--capture", str(tmp_path / "x.png")],
                          window_factory=lambda t: CaptureWindow(t, SessionLost("нет окна")),
                          env={})
    assert code == 1
    assert "нет окна" in capsys.readouterr().out
    assert not (tmp_path / "x.png").exists()


def test_calibrate_capture_default_window_is_the_robots():
    """The CLI captures through MacRdpWindow, the same `screencapture -l <id> -o` path."""
    from orderbot.screen.mac_window import MacRdpWindow

    w = calibrate._default_window("abc")
    assert isinstance(w, MacRdpWindow) and w.title_contains == "abc"


def test_unknown_price_type_in_other_currency_is_skipped(monkeypatch):
    """Seen live: rows "HR $" and "HR+" in USD, unknown to the layout, must not stop the
    reading; an unknown type in rubles still refuses."""
    from decimal import Decimal
    from pathlib import Path

    from PIL import Image

    from orderbot.screen import reader

    root = Path(__file__).resolve().parent.parent
    layout = reader.Layout.load(root / "layouts" / "production.yaml")
    img = Image.open(root / "layouts" / "shots" / "den091-gry-prices.png")
    # the capture predates the widened article column; prices are unaffected
    monkeypatch.setattr(reader, "check_screen", lambda i, l: None)
    real_type = reader._read_price_type
    real_rapid = reader._rapid

    def price_type(cell, lay, row):
        if row == 1:
            raise reader.ReadUncertain("price type in row 1 is not a known one")
        return real_type(cell, lay, row)

    def rapid(cell, what):
        if what == "currency in row 1":
            return currency[0]
        return real_rapid(cell, what)

    currency = ["USD"]
    monkeypatch.setattr(reader, "_read_price_type", price_type)
    monkeypatch.setattr(reader, "_rapid", rapid)
    assert reader.read_prices(img, layout) == (Decimal("1880"), Decimal("1340"))
    currency[0] = "RUB"
    with pytest.raises(reader.ReadUncertain):
        reader.read_prices(img, layout)
