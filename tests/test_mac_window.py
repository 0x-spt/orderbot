"""MacRdpWindow on fakes of the window list and of subprocess. No real window is touched."""
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from orderbot.screen.mac_window import OWNER, MacRdpWindow
from orderbot.screen.session import SessionLost

PID = 4242


def win(wid=7, owner=OWNER, name="1С:Предприятие - rdp.example", layer=0,
        bounds=(100, 50, 1000, 700), pid=PID, alpha=1.0):
    x, y, w, h = bounds
    return {"kCGWindowNumber": wid, "kCGWindowOwnerName": owner, "kCGWindowOwnerPID": pid,
            "kCGWindowName": name, "kCGWindowLayer": layer, "kCGWindowAlpha": alpha,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


class Fakes:
    def __init__(self, windows, size=(2000, 1400), color="white"):
        self.windows = windows
        self.size = size
        self.color = color
        self.runs = []
        self.locked = False
        self.capture_allowed = True
        self.run_error = None
        self.write = True
        self.payload = None  # raw bytes to write instead of a PNG
        self.focused = PID

    def run(self, argv, **kw):
        self.runs.append((argv, kw))
        if self.run_error:
            raise self.run_error
        if self.payload is not None:
            Path(argv[-1]).write_bytes(self.payload)
        elif self.write:
            Image.new("RGB", self.size, self.color).save(argv[-1], "PNG")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def make(self, title_contains=""):
        return MacRdpWindow(title_contains=title_contains, list_windows=lambda: self.windows,
                            run=self.run, screen_locked=lambda: self.locked,
                            can_capture=lambda: self.capture_allowed,
                            focused_pid=lambda: self.focused, dock_edge="bottom")


def test_owner_is_microsoft_remote_desktop():
    assert OWNER == "Microsoft Remote Desktop"


def test_no_window_raises_session_lost():
    f = Fakes([win(owner="Finder")])
    with pytest.raises(SessionLost):
        f.make().connect()


def test_two_windows_raise_session_lost():
    f = Fakes([win(7, name="Connection Center"), win(8)])
    with pytest.raises(SessionLost) as e:
        f.make().connect()
    assert "RDP_WINDOW_TITLE_CONTAINS" in str(e.value)


def test_title_filter_picks_the_one_window():
    f = Fakes([win(7, name="Connection Center"), win(8, name="1С rdp.example")])
    w = f.make("rdp.example")
    w.connect()
    assert w.window_id == 8 and w.pid == PID


def test_other_layers_and_owners_are_ignored():
    f = Fakes([win(1, layer=25, name="menu"), win(2, owner="Safari"), win(7)])
    w = f.make()
    w.connect()
    assert w.window_id == 7


def test_title_filter_without_titles_is_session_lost():
    f = Fakes([win(7, name=None)])
    with pytest.raises(SessionLost):
        f.make("rdp").connect()


def test_no_screen_recording_permission_is_session_lost():
    f = Fakes([win()])
    f.capture_allowed = False
    with pytest.raises(SessionLost) as e:
        f.make().connect()
    assert "Запись экрана" in str(e.value)


def test_locked_screen_is_session_lost():
    f = Fakes([win()])
    f.locked = True
    with pytest.raises(SessionLost):
        f.make().connect()


def test_screenshot_uses_window_id_and_no_shadow():
    f = Fakes([win(7)])
    w = f.make()
    w.connect()
    img = w.screenshot()
    argv, kw = f.runs[0]
    assert argv[:5] == ["screencapture", "-l", "7", "-o", "-x"]
    assert len(argv) == 6 and argv[5].endswith(".png")
    assert kw.get("check") is True and kw.get("timeout")
    assert img.size == (2000, 1400) and img.mode == "RGB"
    assert not Path(argv[5]).exists()  # the temporary file is removed


def test_scale_is_image_pixels_per_point():
    f = Fakes([win(bounds=(100, 50, 1000, 700))], size=(2000, 1400))
    w = f.make()
    w.connect()
    w.screenshot()
    assert w.scale == 2.0
    assert w.bounds() == (100, 50, 1000, 700)


def test_screenshot_before_connect_is_session_lost():
    with pytest.raises(SessionLost):
        Fakes([win()]).make().screenshot()


def test_window_closed_after_connect_is_session_lost():
    f = Fakes([win(7)])
    w = f.make()
    w.connect()
    f.windows = []  # closed, minimised or on another desktop
    with pytest.raises(SessionLost):
        w.screenshot()
    assert f.runs == []


def test_window_replaced_after_connect_is_session_lost():
    f = Fakes([win(7)])
    w = f.make()
    w.connect()
    f.windows = [win(9)]
    with pytest.raises(SessionLost):
        w.screenshot()


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(1, "screencapture"),
    subprocess.TimeoutExpired("screencapture", 10),
    OSError("no screencapture"),
])
def test_screencapture_failure_is_session_lost(exc):
    f = Fakes([win()])
    f.run_error = exc
    w = f.make()
    w.connect()
    with pytest.raises(SessionLost) as e:
        w.screenshot()
    assert "Запись экрана" in str(e.value)


def test_empty_capture_is_session_lost():
    f = Fakes([win()])
    f.write = False
    w = f.make()
    w.connect()
    with pytest.raises(SessionLost) as e:
        w.screenshot()
    assert "Запись экрана" in str(e.value)


def test_black_capture_says_asleep_or_locked_or_permission():
    f = Fakes([win()], color="black")
    w = f.make()
    w.connect()
    with pytest.raises(SessionLost) as e:
        w.screenshot()
    assert "спит" in str(e.value) and "заблокирован" in str(e.value)
    assert "Запись экрана" in str(e.value)


def test_garbage_capture_is_session_lost():
    f = Fakes([win()])
    f.payload = b"not a png"
    w = f.make()
    w.connect()
    with pytest.raises(SessionLost):
        w.screenshot()


def test_connect_and_disconnect_forget_last_article():
    f = Fakes([win()])
    w = f.make()
    w.connect()
    w.last_article = "X"
    w.last_stock_crop = Image.new("RGB", (1, 1))
    w.disconnect()
    assert w.last_article is None and w.last_stock_crop is None
    w.last_article = "Y"
    w.connect()  # a person may have used 1C meanwhile
    assert w.last_article is None
    assert f.runs == []  # neither touches the window


def test_is_front_and_covered_at():
    f = Fakes([win(1, owner="Safari", bounds=(0, 0, 300, 300)), win(7)])
    w = f.make()
    w.connect()
    assert not w.is_front()
    f.windows = [win(5, owner="Dock", layer=20, bounds=(0, 600, 2000, 200)), win(7),
                 win(1, owner="Safari")]
    assert w.is_front()
    assert w.covered_at(500, 700)          # under the Dock
    assert not w.covered_at(500, 300)
    f.windows = [win(5, owner="Overlay", layer=20, bounds=(0, 0, 2000, 2000), alpha=0.0),
                 win(7)]
    assert not w.covered_at(500, 300)      # fully transparent windows are not counted


def test_full_screen_dock_window_covers_only_its_edge_strip():
    # The real Dock window spans the whole screen (seen live on the operator's Mac).
    f = Fakes([win(5, owner="Dock", layer=20, bounds=(0, 0, 1440, 900)),
               win(9, owner="Window Server", layer=2147483630, bounds=(300, 500, 28, 40)),
               win(7, bounds=(0, 30, 1440, 815))])
    f.windows[1]["kCGWindowName"] = "Cursor"
    w = f.make()
    w.dock_edge = "bottom"
    w.connect()
    assert not w.covered_at(700, 400)      # middle of the screen: not covered
    assert not w.covered_at(310, 510)      # the mouse cursor does not cover
    assert not w.covered_at(35, 480)       # tab near the left edge, Dock is at the bottom
    assert w.covered_at(700, 850)          # bottom strip where the Dock shows
    w.dock_edge = "left"
    assert w.covered_at(50, 400)
    assert not w.covered_at(700, 850)
    w.dock_edge = "right"
    assert w.covered_at(1400, 400)


def test_activate_goes_to_the_owner_pid_and_scale_needs_a_screenshot():
    f = Fakes([win(7)])
    called = []
    w = MacRdpWindow(list_windows=lambda: f.windows, run=f.run, screen_locked=lambda: False,
                     can_capture=lambda: True, activate_app=called.append)
    w.connect()
    assert w.scale is None and w.captured_bounds is None
    w.activate()
    assert called == [PID]
    w.screenshot()
    assert w.captured_bounds == (100, 50, 1000, 700)


def test_topmost_window_ours_but_other_app_focused_is_not_front():
    """The operator clicked the desktop: Finder has focus with no window in front of ours."""
    f = Fakes([win(7), win(1, owner="Safari")])
    w = f.make()
    w.connect()
    assert w.is_front()
    f.focused = 301  # Finder or Spotlight
    assert not w.is_front()
    f.focused = None  # AX does not say
    assert not w.is_front()


def test_key_overlay():
    f = Fakes([win(7)])
    w = f.make()
    w.connect()
    menu = win(2, owner="Window Server", name="Menubar", layer=24, bounds=(0, 0, 2000, 33))
    status = win(3, owner="Telegram", layer=25, bounds=(1500, 0, 30, 33))
    dock = win(4, owner="Dock", layer=20, bounds=(0, 1300, 2000, 100))
    f.windows = [menu, status, dock, win(7)]
    assert not w.key_overlay()
    f.windows = [win(5, owner="Spotlight", layer=101, bounds=(600, 200, 800, 60)), win(7)]
    assert w.key_overlay()
    f.windows = [win(5, owner="NotificationCenter", layer=23, bounds=(1600, 40, 380, 90)),
                 win(7)]
    assert w.key_overlay()
    f.windows = [win(6, owner="Window Server", layer=25, bounds=(0, 0, 2000, 2000)), win(7)]
    assert w.key_overlay()  # a layer-25 window larger than a menu bar strip
    f.windows = [win(6, owner="Overlay", layer=30, bounds=(0, 0, 2000, 2000), alpha=0.0),
                 win(7)]
    assert not w.key_overlay()


@pytest.mark.parametrize("part", ["list_windows", "screen_locked", "can_capture",
                                  "focused_pid", "activate_app"])
def test_missing_pyobjc_is_session_lost(part):
    f = Fakes([win(7)])

    def boom(*a):
        raise ImportError("No module named 'Quartz'")

    kw = dict(list_windows=lambda: f.windows, run=f.run, screen_locked=lambda: False,
              can_capture=lambda: True, focused_pid=lambda: PID, activate_app=lambda pid: None)
    w = MacRdpWindow(**kw)
    w.connect()
    kw[part] = boom
    w = MacRdpWindow(**kw)
    with pytest.raises(SessionLost):
        w.connect()
        w.activate()
        w.is_front()


def test_mouse_cursor_and_menu_extras_are_not_key_overlays():
    # Window list seen live on the operator's Mac with nothing over the 1c window.
    f = Fakes([win(9, owner="Window Server", layer=2147483630, bounds=(305, 526, 28, 40)),
               win(3, owner="Пункт управления", layer=25, bounds=(1300, 0, 140, 30)),
               win(4, owner="Window Server", layer=24, bounds=(0, 0, 1440, 30)),
               win(5, owner="Dock", layer=20, bounds=(0, 0, 1440, 900)),
               win(7, bounds=(0, 30, 1440, 815))])
    f.windows[0]["kCGWindowName"] = "Cursor"
    w = f.make()
    w.connect()
    assert not w.key_overlay()
    f.windows[0]["kCGWindowName"] = "Spotlight"   # anything else up there still counts
    assert w.key_overlay()


def test_recording_indicator_in_the_menu_bar_is_not_a_key_overlay():
    # macOS shows this while the robot captures the window (seen live).
    f = Fakes([win(8, owner="Window Server", layer=2147483630, bounds=(1276, 1, 29, 28)),
               win(7, bounds=(0, 30, 1440, 815))])
    f.windows[0]["kCGWindowName"] = "StatusIndicator"
    w = f.make()
    w.connect()
    assert not w.key_overlay()
    f.windows[0]["kCGWindowBounds"] = {"X": 500, "Y": 300, "Width": 400, "Height": 60}
    assert w.key_overlay()              # the same kind of window over the screen still counts
