"""Builds per-layout digit templates and label references from labeled crops.

    python -m orderbot.screen.calibrate layouts/<name>-crops.yaml

Writes layouts/<name>-digits/ and layouts/<name>-labels/ and prints what is still missing.
A digit without a template is never guessed: reading it refuses, so the report matters.

    python -m orderbot.screen.calibrate --capture <out.png>

Saves one frame of the Microsoft Remote Desktop window exactly as the robot captures it
(`screencapture -l <id> -o`: the window with its title bar, no shadow). It only captures:
no input, no activation. RDP_WINDOW_TITLE_CONTAINS (environment or .env) picks the window.
"""
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

from orderbot.screen import ocr
from orderbot.screen.reader import CYRILLIC, DIGIT_FILES, DIGIT_STYLES

SKIP = "?"


class CalibrationError(Exception):
    pass


@dataclass
class Report:
    present_digits: dict[str, list[str]]
    missing_digits: dict[str, list[str]]
    labels: list[str]
    missing_labels: list[str]

    def text(self) -> str:
        lines = []
        for style in self.present_digits:
            lines.append(f"digits {style}: have {' '.join(self.present_digits[style]) or '-'}")
            lines.append(f"digits {style}: MISSING {' '.join(self.missing_digits[style]) or '-'}")
        lines.append(f"labels: have {len(self.labels)}")
        lines.append(f"labels: MISSING {' '.join(self.missing_labels) or '-'}")
        return "\n".join(lines)


def _open_images(spec: dict, base: Path) -> dict[str, tuple[Image.Image, tuple[int, int]]]:
    images = {}
    for name, entry in spec["images"].items():
        offset = tuple(entry.get("offset", (0, 0)))
        images[name] = (Image.open(base / entry["path"]).convert("RGB"), offset)
    return images


def _cut(images, image: str, rect) -> Image.Image:
    img, (dx, dy) = images[image]
    x0, y0, x1, y1 = rect
    return img.crop((x0 + dx, y0 + dy, x1 + dx, y1 + dy))


def _collect_digits(spec: dict, images) -> dict[str, dict[str, np.ndarray]]:
    styles = {style: {} for style in DIGIT_STYLES}
    for item in spec.get("digits", []):
        style = item.get("style", "regular")
        if style not in styles:
            raise CalibrationError(f"unknown digit style {style}")
        crop = _cut(images, item["image"], item["rect"])
        mask = ocr.ink_mask(crop)
        boxes = ocr.glyph_boxes(mask)
        text = item["text"]
        if len(boxes) != len(text):
            raise CalibrationError(f"{item['rect']}: {len(boxes)} glyphs for label {text!r}")
        for char, (x0, x1, y0, y1) in zip(text, boxes):
            if char == SKIP:
                continue
            if char not in DIGIT_FILES:
                raise CalibrationError(f"{item['rect']}: no template name for {char!r}")
            glyph = mask[y0:y1, x0:x1]
            known = styles[style].get(char)
            if known is None:
                styles[style][char] = glyph
            elif ocr.similarity(glyph, known) < ocr.DIGIT_MIN_SCORE:
                raise CalibrationError(
                    f"{char!r} at {item['rect']} differs from the earlier crop of it: "
                    f"another rendering, or a wrong label"
                )
    return styles


def _expected_labels(layout_raw: dict) -> set[str]:
    ids = set()
    for group in layout_raw["anchors"].values():
        ids |= set(group)
    ids |= set(layout_raw["warehouses"])
    ids |= {r["id"] for r in layout_raw["price_rows"] if CYRILLIC.search(r["type"])}
    return ids


def build(spec_path, out_root=None) -> Report:
    spec_path = Path(spec_path)
    base = spec_path.parent
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    layout_raw = yaml.safe_load((base / spec["layout"]).read_text(encoding="utf-8"))
    out_root = Path(out_root) if out_root else base
    images = _open_images(spec, base)

    # Everything is prepared first, so that a bad spec leaves the current templates alone.
    digits = _collect_digits(spec, images)
    labels = {}
    for item in spec.get("labels", []):
        crop = _cut(images, item["image"], item["rect"])
        if not ocr.has_ink(crop):
            raise CalibrationError(f"label {item['id']}: the crop is blank")
        labels[item["id"]] = ocr.ink_mask(crop)

    name = layout_raw["name"]
    digits_dir = out_root / f"{name}-digits"
    labels_dir = out_root / f"{name}-labels"
    for directory in (digits_dir, labels_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    for style, sub in DIGIT_STYLES.items():
        if digits[style]:
            (digits_dir / sub).mkdir(exist_ok=True)
        for char, glyph in digits[style].items():
            ocr.save_mask(glyph, digits_dir / sub / f"{DIGIT_FILES[char]}.png")
    for label, mask in labels.items():
        ocr.save_mask(mask, labels_dir / f"{label}.png")

    wanted = list(DIGIT_FILES)
    return Report(
        present_digits={s: [c for c in wanted if c in digits[s]] for s in DIGIT_STYLES},
        missing_digits={s: [c for c in wanted if c not in digits[s]] for s in DIGIT_STYLES},
        labels=sorted(labels),
        missing_labels=sorted(_expected_labels(layout_raw) - set(labels)),
    )


def _default_window(title: str):
    from orderbot.screen.mac_window import MacRdpWindow

    return MacRdpWindow(title_contains=title)


def capture(out, window_factory=_default_window, env=None) -> int:
    from orderbot.screen.session import SessionLost

    if env is None:
        from orderbot.main import load_env_file

        env = dict(os.environ)
        load_env_file(".env", env)
    window = window_factory(env.get("RDP_WINDOW_TITLE_CONTAINS", ""))
    try:
        window.connect()
        img = window.screenshot()
    except SessionLost as e:
        print(f"capture failed: {e}")
        return 1
    finally:
        window.disconnect()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"saved {out}: {img.width}x{img.height} px")
    return 0


def main(argv=None, *, window_factory=_default_window, env=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "--capture":
        return capture(args[1], window_factory, env)
    if len(args) != 1 or args[0].startswith("-"):
        print(__doc__)
        return 2
    try:
        report = build(args[0])
    except CalibrationError as e:
        print(f"calibration failed: {e}")
        return 1
    print(report.text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
