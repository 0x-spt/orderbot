"""The only code that asks for input into 1C (mac_input.py turns it into events).

The robot works in the operator's real 1C under their own login with write rights, so
what it may do is limited here and nowhere else: click the "x" button that empties the
search field, click into the field, type a search key, press Return, click the found row
once, switch between the «Остатки» and «Цены» tabs, and open the «Изображение» tab to
look at the item's picture (read-only, like the other two). One more click, never
asked for by the caller: the "x" of 1C's «Оповещение о задачах» task reminder when it covers
«Номенклатура», only on a frame recognised as that window. The public surface of Guard and
of the input backend (MacInputBackend) is exactly that, and tests pin it.

No modifier key is ever pressed (Remote Desktop drops synthetic Ctrl, Cmd and Shift, seen
live), so only characters typed without Shift on a US layout are allowed.
"""
import logging
import string
import subprocess
import time
from collections.abc import Callable
from typing import Protocol

from PIL import Image

from orderbot.screen.reader import Layout, Rect, UnexpectedScreen, check_screen, task_notice_shown
from orderbot.screen.session import SessionLost

log = logging.getLogger(__name__)

TABS = ("stock", "prices", "image")  # the three tab captions under the list
MAX_TEXT = 200
# 1C repaints the list right after the field changes (seen live: the frame before
# Return, taken just after the "x" click, missed the list header and the robot paused). A
# wrong frame is looked at again a few times before it stops the robot; nothing is sent
# until a frame passes.
VERIFY_TRIES = 5
VERIFY_PAUSE_S = 0.4
# 1C's task reminder over «Номенклатура» is closed by its "x" at most this many times per
# check; it takes a moment to go away.
NOTICE_CLOSES = 2
NOTICE_PAUSE_S = 1.0
# Typed without any modifier on a US layout. The 1C search is case-insensitive (seen live).
TYPABLE = frozenset(string.ascii_lowercase + string.digits + "-./")


class Backend(Protocol):
    def click(self, x: int, y: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press_return(self) -> None: ...


def validate_text(text: str) -> str:
    """Only characters that have an unshifted key. Anything else could be a key press in
    disguise, or need a modifier that never arrives in Windows."""
    if not isinstance(text, str) or not text:
        raise ValueError("text to type must be a non-empty string")
    if len(text) > MAX_TEXT:
        raise ValueError("text to type is too long")
    if not set(text) <= TYPABLE:
        raise ValueError("text to type has characters that cannot be typed without Shift")
    return text


class ActionRefused(Exception):
    """The Guard declines an action that is not allowed at this point."""


def _center(rect: Rect) -> tuple[int, int]:
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


class Guard:
    """Each backend call is preceded by a check of a fresh frame; a wrong screen raises
    UnexpectedScreen and nothing more is sent. Coordinates come only from the layout.
    After a lost session nothing more is sent either."""

    def __init__(self, backend: Backend, layout: Layout, *, screenshot: Callable[[], Image.Image],
                 sleep: Callable[[float], None] = time.sleep):
        self._sleep = sleep
        self._backend = backend
        self._layout = layout
        self._screenshot = screenshot
        self._row_armed = False
        self._dead = False

    def _guarded(self, fn, *args):
        if self._dead:
            raise SessionLost("the session was lost earlier")
        try:
            return fn(*args)
        except SessionLost:
            self._dead = True
            raise
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            self._dead = True
            raise SessionLost(f"{type(e).__name__}") from e

    def _verify(self, image_tab_ok: bool = False) -> None:
        attempt = closed = 0
        while True:
            img = self._guarded(self._screenshot)
            try:
                check_screen(img, self._layout, image_tab_ok=image_tab_ok)
                return
            except UnexpectedScreen as e:
                if closed < NOTICE_CLOSES and task_notice_shown(img, self._layout):
                    closed += 1
                    self._close_task_notice()
                    self._sleep(NOTICE_PAUSE_S)
                    continue
                attempt += 1
                if attempt == VERIFY_TRIES:
                    e.frame = img      # the 1C window as refused, for the debug snapshot
                    raise
            self._sleep(VERIFY_PAUSE_S)

    def dismiss_task_notice(self) -> None:
        """Close «Оповещение о задачах» if it is open now (at most NOTICE_CLOSES clicks).
        Used on connect, before the window check; anything else on screen is left alone."""
        for _ in range(NOTICE_CLOSES):
            if not task_notice_shown(self._guarded(self._screenshot), self._layout):
                return
            self._close_task_notice()
            self._sleep(NOTICE_PAUSE_S)

    def _close_task_notice(self) -> None:
        """One click on the "x" of «Оповещение о задачах»; the frame just taken showed it."""
        log.info("1C task reminder «Оповещение о задачах» covers the list: closing it")
        self._guarded(self._backend.click, *_center(self._layout.rects["notice_close"]))

    def _send(self, method: str, *args, image_tab_ok: bool = False) -> None:
        self._verify(image_tab_ok)
        self._guarded(getattr(self._backend, method), *args)

    def _click(self, rect: Rect, image_tab_ok: bool = False) -> None:
        self._send("click", *_center(rect), image_tab_ok=image_tab_ok)

    def clear_search(self) -> None:
        """One click on the "x" button at the right end of the search field."""
        self._row_armed = False
        self._click(self._layout.rects["search_clear"])

    def enter_article(self, text: str) -> None:
        """A click into the search field, then the text typed key by key."""
        validate_text(text)
        self._row_armed = False
        self._click(self._layout.rects["search_field"])
        self._send("type_text", text)

    def press_enter(self) -> None:
        self._send("press_return")
        self._row_armed = True

    def click_result_row(self, index: int = 0) -> None:
        """One click on one found row, once per Enter: a second call would be a double click.
        `index` picks the row when the search found several (the one whose article matches
        exactly); it must be a visible row of the results grid."""
        grid = self._layout.grids["results"]
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < grid.rows:
            raise ActionRefused(f"no such result row: {index!r}")
        if not self._row_armed:
            raise ActionRefused("the result row is clicked once after each Enter")
        self._row_armed = False
        self._click(grid.cell(index, "row"))

    def open_tab(self, name: str) -> None:
        if name not in TABS:
            raise ValueError(f"unknown tab {name!r}")
        # From the «Изображение» tab only the way back to «Остатки» is sent.
        self._click(self._layout.anchors["common"][f"tab_{name}"],
                    image_tab_ok=(name == "stock"))
