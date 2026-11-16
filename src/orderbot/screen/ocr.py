"""Two independent ways of reading a cell image. Everything here is local.

* ``rapid_text``: RapidOCR. Its bundled model has no Cyrillic, so it is used only for
  digits and Latin text.
* ``read_digits`` and ``match_reference``: comparison of the cell's ink with stored crops
  made for this layout by ``orderbot.screen.calibrate``.

Every function raises ``Refused`` instead of guessing; reader.py turns that into
``ReadUncertain``.
"""
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image


class Refused(Exception):
    pass


# An empty table cell is white. Anything darker, however faint or uniform, is not blank:
# it goes on to be read and refused, and is never taken for an empty cell.
BLANK_MIN_CHANNEL = 248
# Below this contrast with its background a cell holds nothing that could be text.
NO_INK_CONTRAST = 25
# A pixel is ink when it is at least this share of the cell's strongest contrast away from
# the background. Relative, so it does not depend on the colours of the row or selection.
INK_SHARE = 0.5

# Same glyph across the fixture cells scored at least 0.96, different digits at most 0.69.
DIGIT_MIN_SCORE = 0.90
DIGIT_MIN_MARGIN = 0.10
# "Южная,9" against "Южная,8" scores 0.93, so one changed character must not pass.
LABEL_MIN_SCORE = 0.97
LABEL_MIN_MARGIN = 0.02
MAX_SHIFT = 2  # pixels of misalignment forgiven when comparing masks

OCR_SCALE = 2
OCR_PADDING = 16
OCR_MIN_CONFIDENCE = 0.5


def _contrast(img: Image.Image) -> np.ndarray:
    """Distance of each pixel from the cell background, the commonest grey value."""
    grey = np.asarray(img.convert("L"), dtype=np.int16)
    values, counts = np.unique(grey, return_counts=True)
    return np.abs(grey - values[counts.argmax()])


def is_blank(img: Image.Image) -> bool:
    """A really empty cell or region: white in every channel of every pixel."""
    return bool(np.asarray(img.convert("RGB")).min() >= BLANK_MIN_CHANNEL)


SPECK_MAX_PIXELS = 3       # a few stray light pixels next to a grid line are not content
SPECK_MIN_CHANNEL = 200    # ... as long as none of them is darker than this (glyphs are)


def is_nearly_blank(img: Image.Image) -> bool:
    """Blank, or holding only a speck: at most SPECK_MAX_PIXELS off-white pixels, none of
    them dark. Seen live: one (253, 252, 246) pixel at the corner of an empty
    stock row made it count as a row. Text always has dark pixels, so no content is lost."""
    a = np.asarray(img.convert("RGB")).min(axis=2)
    off = a < BLANK_MIN_CHANNEL
    return bool(off.sum() <= SPECK_MAX_PIXELS and (a[off] >= SPECK_MIN_CHANNEL).all())


def has_ink(img: Image.Image) -> bool:
    """Anything standing out from the cell's own background, whatever colour that is."""
    return bool(_contrast(img).max() > NO_INK_CONTRAST)


def ink_mask(img: Image.Image) -> np.ndarray:
    contrast = _contrast(img)
    return contrast > max(NO_INK_CONTRAST, contrast.max() * INK_SHARE)


def glyph_boxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Runs of columns holding ink, each as (x0, x1, y0, y1) with tight rows."""
    columns = mask.any(axis=0)
    boxes = []
    x = 0
    while x < len(columns):
        if not columns[x]:
            x += 1
            continue
        end = x
        while end < len(columns) and columns[end]:
            end += 1
        rows = np.flatnonzero(mask[:, x:end].any(axis=1))
        boxes.append((x, end, int(rows[0]), int(rows[-1]) + 1))
        x = end
    return boxes


def similarity(a: np.ndarray, b: np.ndarray, shift: int = MAX_SHIFT) -> float:
    """Best intersection over union of two masks over small shifts."""
    h = max(a.shape[0], b.shape[0]) + 2 * shift
    w = max(a.shape[1], b.shape[1]) + 2 * shift
    base = np.zeros((h, w), bool)
    base[shift : shift + a.shape[0], shift : shift + a.shape[1]] = a
    best = 0.0
    for dy in range(h - b.shape[0] + 1):
        for dx in range(w - b.shape[1] + 1):
            moved = np.zeros((h, w), bool)
            moved[dy : dy + b.shape[0], dx : dx + b.shape[1]] = b
            union = np.count_nonzero(base | moved)
            if union:
                best = max(best, np.count_nonzero(base & moved) / union)
    return best


def best_match(
    mask: np.ndarray, candidates: dict[str, np.ndarray], min_score: float, min_margin: float
) -> str:
    scores = sorted(((similarity(mask, t), name) for name, t in candidates.items()), reverse=True)
    if not scores or scores[0][0] < min_score:
        top = f"{scores[0][1]} at {scores[0][0]:.2f}" if scores else "no candidates"
        raise Refused(f"no match: best is {top}")
    if len(scores) > 1 and scores[0][0] - scores[1][0] < min_margin:
        (s0, l0), (s1, l1) = scores[0], scores[1]
        raise Refused(f"ambiguous: {l0} at {s0:.2f}, {l1} at {s1:.2f}")
    return scores[0][1]


def read_digits(img: Image.Image, templates: dict[str, np.ndarray]) -> str:
    """Reads a number glyph by glyph. A truncated value keeps its ending as "..."."""
    if not has_ink(img):
        raise Refused("no ink in the cell")
    mask = ink_mask(img)
    text = ""
    for x0, x1, y0, y1 in glyph_boxes(mask):
        text += best_match(mask[y0:y1, x0:x1], templates, DIGIT_MIN_SCORE, DIGIT_MIN_MARGIN)
    if "." in text and not re.fullmatch(r"[0-9]+,[0-9]*\.\.\.", text):
        raise Refused(f"dots are valid only as an ellipsis after a comma: {text}")
    return text


def match_reference(img: Image.Image, references: dict[str, np.ndarray]) -> str:
    """Which stored crop this cell is. References have the full cell's shape, so a label
    that moved within the cell does not match."""
    if not has_ink(img):
        raise Refused("no ink in the cell")
    return best_match(ink_mask(img), references, LABEL_MIN_SCORE, LABEL_MIN_MARGIN)


# Screen anchors (menu, headers, status line) only prove that the expected form is shown;
# live frames differ from the calibration frames by a few antialiased pixels (status line
# scored 0.935 live), so they get a looser bar than labels, which must tell names apart.
ANCHOR_MIN_SCORE = 0.90


def references_match(img: Image.Image, reference: np.ndarray,
                     min_score: float = LABEL_MIN_SCORE) -> bool:
    if reference.shape != (img.height, img.width) or not has_ink(img):
        return False
    return similarity(ink_mask(img), reference) >= min_score


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _for_ocr(img: Image.Image, scale: int = OCR_SCALE) -> np.ndarray:
    """Dark text on white whatever the cell colours, enlarged and with a margin."""
    contrast = _contrast(img).astype(float)
    grey = (255 * (1 - contrast / max(contrast.max(), 1))).astype(np.uint8)
    big = Image.fromarray(grey).resize(
        (img.width * scale, img.height * scale), Image.BICUBIC
    )
    return np.pad(np.asarray(big.convert("RGB")), ((OCR_PADDING,) * 2, (OCR_PADDING,) * 2, (0, 0)),
                  constant_values=255)


_DOTS = set(".。…·．")


def _without_trailing_dots(found):
    """The ellipsis of a cut cell can come back as a box of its own (seen live:
    ['NX-STP281.', '。']). A box made only of dots that lies to the right of the single
    text box is folded into that box as "…"; any other extra box still refuses the cell."""
    if not found or len(found) < 2:
        return found
    def is_dots(f):
        t = unicodedata.normalize("NFKC", f[1]).strip()
        return bool(t) and set(t) <= _DOTS
    text = [f for f in found if not is_dots(f)]
    dots = [f for f in found if is_dots(f)]
    if len(text) != 1:
        return found
    right_edge = max(p[0] for p in text[0][0])
    # seen live: the dot box starts a few px inside the text box (265 vs 272)
    if any(min(p[0] for p in d[0]) < right_edge - 10 for d in dots):
        return found
    box, value, confidence = text[0]
    return [(box, value.rstrip("".join(_DOTS)) + "…", confidence)]


class NoText(Refused):
    """The text recogniser found no text at all in a cell that has ink."""


def rapid_text(img: Image.Image) -> str:
    """The one line of text in a cell. Several detected boxes are refused: joining them
    could make up a value that is not on the screen."""
    if not has_ink(img):
        raise Refused("no ink in the cell")
    found, _ = _engine()(_for_ocr(img))
    found = _without_trailing_dots(found)
    if not found:
        raise NoText(f"expected one line of text, got {found}")
    if len(found) != 1:
        raise Refused(f"expected one line of text, got {found and [f[1] for f in found]}")
    _, text, confidence = found[0]
    if float(confidence) < OCR_MIN_CONFIDENCE:
        raise Refused(f"low confidence {float(confidence):.2f} for {text!r}")
    return unicodedata.normalize("NFKC", text).strip()


LINE_MARGIN = 4   # px of the cell's own background kept around the ink
LINE_SCALE = 3


def rapid_line(img: Image.Image) -> str:
    """Recognition alone on the ink of a cell known to hold one line of text, for when the
    detector finds no box at all (seen live: «df05199/f» in the wide search field
    gave no box at any scale; cut to its ink and recognised directly it reads 0.99)."""
    a = np.asarray(img.convert("RGB")).min(axis=2)
    xs = np.where((a < SPECK_MIN_CHANNEL).any(axis=0))[0]
    if not len(xs):
        raise Refused("no ink in the cell")
    cut = img.crop((max(0, int(xs[0]) - LINE_MARGIN), 0,
                    min(img.width, int(xs[-1]) + 1 + LINE_MARGIN), img.height))
    found, _ = _engine()(_for_ocr(cut, LINE_SCALE), use_det=False, use_cls=False)
    if not found or len(found) != 1:
        raise Refused(f"expected one line of text, got {found}")
    text, confidence = found[0][0], float(found[0][1])
    if confidence < OCR_MIN_CONFIDENCE:
        raise Refused(f"low confidence {confidence:.2f} for {text!r}")
    return unicodedata.normalize("NFKC", text).strip()


def save_mask(mask: np.ndarray, path: Path) -> None:
    Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).save(path)


def load_mask(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L")) < 128
