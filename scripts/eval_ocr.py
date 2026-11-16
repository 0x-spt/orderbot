"""Robustness of number reading on real 1C frames: template matcher vs RapidOCR vs both.

    python scripts/eval_ocr.py            # prints a markdown table

Ground truth: hand-checked quantity, reserve and total cells of the stock tables in real
production captures (layouts/shots/labels.yaml).
Every cell is then degraded in controlled ways, and each strategy's answer is classified:

* correct - the true value;
* refused - no value (the robot hands the order to a person);
* wrong   - another value, a silent error. This is the column that must stay at zero.
"""
from __future__ import annotations

import io
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from orderbot.screen import ocr, reader  # noqa: E402

LAYOUT = reader.Layout.load(ROOT / "layouts" / "production.yaml")


def jpeg(q):
    def f(im):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q)
        return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    return f


def noise(sigma):
    def f(im):
        a = np.asarray(im, dtype=np.float32)
        a += np.random.default_rng(0).normal(0, sigma, a.shape)
        return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    return f


def rescale(k):
    def f(im):
        w, h = im.size
        small = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.BILINEAR)
        return small.resize((w, h), Image.BILINEAR)
    return f


# (name, image transform, crop offset in px): the offset moves the cell rectangle, as a
# layout that is slightly off would
PERTURBATIONS = [
    ("clean", None, (0, 0)),
    ("noise σ=8", noise(8), (0, 0)),
    ("noise σ=16", noise(16), (0, 0)),
    ("blur r=0.7", lambda im: im.filter(ImageFilter.GaussianBlur(0.7)), (0, 0)),
    ("blur r=1.2", lambda im: im.filter(ImageFilter.GaussianBlur(1.2)), (0, 0)),
    ("contrast ×0.6", lambda im: ImageEnhance.Contrast(im).enhance(0.6), (0, 0)),
    ("JPEG q=50", jpeg(50), (0, 0)),
    ("JPEG q=20", jpeg(20), (0, 0)),
    ("rescale ×0.75", rescale(0.75), (0, 0)),
    ("cell shifted 2 px", None, (2, 1)),
    ("cell shifted 4 px", None, (4, 2)),
]


def cells():
    """(frame, rect, digit templates, true value) for every labelled non-empty number cell.

    Truth comes from layouts/shots/labels.yaml (hand-checked), not from the engines. A row is
    matched to its label by the warehouse name, read with the reference-crop classifier
    (a separate mechanism from the digit readers). The screen check is skipped: several
    frames predate later layout changes, as in the tests."""
    labels = yaml.safe_load((ROOT / "layouts" / "shots" / "labels.yaml").read_text(encoding="utf-8"))
    grid = LAYOUT.grids["stock"]
    refs = reader._references_for(LAYOUT, LAYOUT.warehouses)
    out, skipped = [], Counter()
    for name, spec in labels.items():
        img = Image.open(ROOT / "layouts" / "shots" / f"{name}.png").convert("RGB")
        rows = reader._present_rows(img, grid, list(grid.columns), reader.ReadUncertain)
        for row in rows:
            try:
                wh = LAYOUT.warehouses[ocr.match_reference(img.crop(grid.cell(row, "warehouse")), refs)]
            except ocr.Refused:
                skipped["warehouse name not recognised"] += 2
                continue
            if wh not in spec["rows"]:
                continue                       # an unlabelled row of a partly labelled frame
            for col, truth in zip(("qty", "reserve"), spec["rows"][wh]):
                rect = grid.cell(row, col)
                if ocr.is_blank(img.crop(rect)):
                    skipped["blank cell (read as 0 without OCR)"] += 1
                else:
                    out.append((img, rect, LAYOUT.digits["regular"], truth))
        if spec["complete"]:
            total = sum(q for q, _ in spec["rows"].values())
            out.append((img, tuple(LAYOUT.rects["stock_total"]), LAYOUT.digits["total"], total))
    return out, skipped, len(labels)


def answer(fn, *args):
    """fn(*args), or None when the reader refuses."""
    try:
        return fn(*args)
    except (ocr.Refused, reader.ReadUncertain):
        return None


def main() -> None:
    data, skipped, frames = cells()
    timing = Counter()
    rows = []
    for name, transform, (dx, dy) in PERTURBATIONS:
        tally = {s: Counter() for s in ("matcher", "rapidocr", "both")}
        for img, (x0, y0, x1, y1), templates, truth in data:
            cell = img.crop((x0 + dx, y0 + dy, x1 + dx, y1 + dy))
            if transform:
                cell = transform(cell)
            t = time.perf_counter()
            m = answer(lambda c, t: reader._parse_int(ocr.read_digits(c, t)), cell, templates)
            timing["matcher"] += time.perf_counter() - t
            t = time.perf_counter()
            r = answer(lambda c: reader._parse_int(ocr.rapid_text(c)), cell)
            timing["rapidocr"] += time.perf_counter() - t
            both = m if (m is not None and m == r) else None
            for s, v in (("matcher", m), ("rapidocr", r), ("both", both)):
                tally[s]["refused" if v is None else "correct" if v == truth else "wrong"] += 1
        rows.append((name, tally))

    n = len(data)
    print(f"{n} labelled number cells from {frames} real frames; "
          f"left out: {dict(skipped) or 'none'}")
    print("cell = % correct / refused / **wrong** (silent error)\n")
    print("| perturbation | template matcher | RapidOCR | both must agree (production) |")
    print("|---|---|---|---|")
    def fmt(c):
        pct = {k: 100 * c[k] / n for k in ("correct", "refused", "wrong")}
        return f"{pct['correct']:.0f} / {pct['refused']:.0f} / **{pct['wrong']:.1f}**"

    for name, tally in rows:
        print(f"| {name} | {fmt(tally['matcher'])} | {fmt(tally['rapidocr'])} | {fmt(tally['both'])} |")
    k = n * len(PERTURBATIONS)
    print(f"\nmean time per cell: matcher {1000 * timing['matcher'] / k:.1f} ms, "
          f"RapidOCR {1000 * timing['rapidocr'] / k:.1f} ms (CPU)")


if __name__ == "__main__":
    main()
