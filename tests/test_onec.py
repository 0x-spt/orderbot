"""MacOneC on fakes: no real window, no real input."""
from decimal import Decimal

import pytest
from PIL import Image

from orderbot.domain import ItemRead
from orderbot.screen.onec import MacOneC
from orderbot.screen.reader import ReadUncertain, UnexpectedScreen
from orderbot.screen.session import SessionLost, ShuttingDown


class FakeWindow:
    instances = []

    def __init__(self, title_contains=""):
        self.title = title_contains
        self.connected = False
        self.disconnected = False
        self.last_article = None
        self.last_stock_crop = None
        self.frame = Image.new("RGB", (40, 40), "white")
        FakeWindow.instances.append(self)

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    def screenshot(self):
        return self.frame


class Parts:
    def __init__(self, tmp_path, check_ok=True, crop_ok=True):
        FakeWindow.instances = []
        self.gates, self.backends, self.guards, self.lookups = [], [], [], []
        self.check_ok, self.crop_ok, self.fail = check_ok, crop_ok, None
        self.gate_objs, self.gate_start_fails = [], False

        parts = self

        class Gate:
            def __init__(self, min_idle_s):
                self.min_idle_s = min_idle_s
                self.events = []
                parts.gate_objs.append(self)

            def start(self):
                self.events.append("start")
                if parts.gate_start_fails:
                    raise SessionLost("no tap")

            def stop(self):
                self.events.append("stop")

        def gate(min_idle_s):
            self.gates.append(min_idle_s)
            return Gate(min_idle_s)

        def backend(window, g, click_gap=None):
            self.backends.append((window, g))
            return type("B", (), {"press_return": lambda self: None})()

        def guard(b, layout, *, screenshot):
            self.guards.append((b, layout, screenshot))
            return FakeGuard()

        def lookup(article, session, g, layout):
            self.lookups.append((article, session, g, layout))
            session.last_article = article           # state that must survive between reads
            session.last_stock_frame = Image.new("RGB", (40, 40), "white")
            if self.fail:
                raise self.fail
            return ItemRead(article, {}, Decimal(1), Decimal(1), None)

        def check(frame, layout):
            if not self.check_ok:
                raise UnexpectedScreen("foreign window")

        def crop(frame, layout):
            if not self.crop_ok:
                raise ValueError("bad crop")
            return frame.crop((0, 0, 10, 10))

        self.onec = MacOneC("LAYOUT", "rdp", 45.0, tmp_path / "snaps",
                            window_factory=FakeWindow, gate_factory=gate,
                            backend_factory=backend, guard_factory=guard, lookup_fn=lookup,
                            check_fn=check, crop_fn=crop)


class FakeGuard:
    dismissed = 0

    def dismiss_task_notice(self):
        FakeGuard.dismissed += 1


def test_connect_closes_the_task_notice_before_checking_the_window(tmp_path):
    p = Parts(tmp_path)
    FakeGuard.dismissed = 0
    p.onec.connect()
    assert FakeGuard.dismissed == 1


def test_connect_builds_parts_and_checks_window(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    w = FakeWindow.instances[0]
    assert w.connected and w.title == "rdp"
    assert p.gates == [45.0]
    assert p.backends[0][0] is w
    assert p.guards[0][1] == "LAYOUT"


def test_connect_refuses_foreign_screen_and_leaves_nothing_half_connected(tmp_path):
    p = Parts(tmp_path, check_ok=False)
    with pytest.raises(UnexpectedScreen):
        p.onec.connect()
    assert FakeWindow.instances[0].disconnected
    with pytest.raises(SessionLost):
        p.onec.read("A1")


def test_failed_lookup_still_gets_a_snapshot_on_the_error(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    p.fail = ReadUncertain("no")
    with pytest.raises(ReadUncertain) as ei:
        p.onec.read("A1")
    assert ei.value.snapshot_path and Image.open(ei.value.snapshot_path).size == (10, 20)


def test_stop_request_blocks_new_input_and_reads(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    gated = p.guards[0][0]
    p.onec.request_stop()
    with pytest.raises(ShuttingDown):
        gated.press_return()
    with pytest.raises(ShuttingDown):
        p.onec.read("A1")


def test_stop_and_wait_waits_for_the_running_input_call(tmp_path):
    import threading
    import time
    p = Parts(tmp_path)
    p.onec.connect()
    release, inside = threading.Event(), threading.Event()

    def slow():
        inside.set()
        release.wait(2)

    t = threading.Thread(target=lambda: p.onec._act(slow))
    t.start()
    inside.wait(2)
    threading.Timer(0.2, release.set).start()
    t0 = time.monotonic()
    assert p.onec.stop_and_wait(timeout=2)
    assert time.monotonic() - t0 >= 0.15
    t.join()


def test_read_before_connect_is_session_lost(tmp_path):
    with pytest.raises(SessionLost):
        Parts(tmp_path).onec.read("A1")


def test_lookup_state_persists_across_reads(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    p.onec.read("A1")
    p.onec.read("A2")
    assert p.lookups[0][1] is p.lookups[1][1]            # the same session object
    assert p.lookups[1][1].last_article == "A2"
    assert len(FakeWindow.instances) == 1


def test_read_saves_only_the_cropped_tables(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    item = p.onec.read("A/1 (x)")
    assert item.snapshot_path
    with Image.open(item.snapshot_path) as im:
        assert im.size == (10, 20)   # stock crop above the current screen's crop
    assert "/" not in item.snapshot_path.split("snaps/")[1]


def test_failed_crop_gives_no_image_but_the_read_stays(tmp_path):
    p = Parts(tmp_path, crop_ok=False)
    p.onec.connect()
    item = p.onec.read("A1")
    assert item.snapshot_path is None and item.article == "A1"


def test_disconnect_drops_everything_without_rdp_action(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    p.onec.disconnect()
    assert FakeWindow.instances[0].disconnected
    with pytest.raises(SessionLost):
        p.onec.read("A1")
    p.onec.connect()                                       # a new connection: new parts
    assert len(FakeWindow.instances) == 2
    assert len(p.gates) == 2


def test_stale_stock_error_snapshot_has_no_stock_image(tmp_path):
    """lookup raised before accepting a stock frame: only the current screen is attached."""
    p = Parts(tmp_path)
    p.onec.connect()
    p.fail = ReadUncertain("the stock table is still the previous article's")
    window = FakeWindow.instances[0]
    window.last_stock_frame = Image.new("RGB", (40, 40), "black")   # the previous article's

    def lookup(article, session, g, layout):
        raise p.fail                                           # never sets a new stock frame

    p.onec._lookup = lookup
    with pytest.raises(ReadUncertain) as ei:
        p.onec.read("A1")
    assert Image.open(ei.value.snapshot_path).size == (10, 10)   # one crop, no stock image


def test_stopping_raises_shutting_down_not_session_lost(tmp_path):
    from orderbot.screen.session import ShuttingDown
    p = Parts(tmp_path)
    p.onec.connect()
    p.onec.request_stop()
    with pytest.raises(ShuttingDown):
        p.onec.read("A1")
    with pytest.raises(ShuttingDown):
        p.onec.connect()


def test_input_tap_starts_on_connect_and_stops_on_disconnect(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    assert [g.events for g in p.gate_objs] == [["start"]]
    p.onec.connect()            # a reconnect stops the old tap before starting a new one
    assert [g.events for g in p.gate_objs] == [["start", "stop"], ["start"]]
    p.onec.disconnect()
    p.onec.disconnect()
    assert [g.events for g in p.gate_objs] == [["start", "stop"], ["start", "stop"]]


def test_no_input_tap_fails_connect_and_leaves_nothing(tmp_path):
    p = Parts(tmp_path)
    p.gate_start_fails = True
    with pytest.raises(SessionLost):
        p.onec.connect()
    assert p.gate_objs[0].events == ["start", "stop"]
    assert FakeWindow.instances[0].disconnected
    assert p.backends == []


def test_failed_window_check_stops_the_tap(tmp_path):
    p = Parts(tmp_path, check_ok=False)
    with pytest.raises(UnexpectedScreen):
        p.onec.connect()
    assert p.gate_objs[0].events == ["start", "stop"]


# --- the 1C picture ------------------------------------------------


def test_read_saves_the_1c_picture_next_to_the_snapshot(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()

    def lookup(article, session, g, layout):
        session.last_stock_frame = Image.new("RGB", (40, 40), "white")
        session.last_prices_frame = Image.new("RGB", (40, 40), "black")
        session.last_image_crop = Image.new("RGB", (30, 20), "red")
        return ItemRead(article, {}, Decimal(1), Decimal(1), None)

    p.onec._lookup = lookup
    window = FakeWindow.instances[0]
    window.frame = Image.new("RGB", (40, 40), "blue")   # the «Изображение» tab is open now
    item = p.onec.read("A/1")
    assert item.photo_1c and item.photo_1c.startswith(str(tmp_path / "snaps"))
    with Image.open(item.photo_1c) as im:
        assert im.size == (30, 20) and im.convert("RGB").getpixel((1, 1)) == (255, 0, 0)
    with Image.open(item.snapshot_path) as im:
        # stock crop above the PRICES frame read, not above the picture now on screen
        assert im.size == (10, 20)
        assert im.convert("RGB").getpixel((5, 15)) == (0, 0, 0)


def test_picture_of_an_earlier_read_is_never_attached(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    window = FakeWindow.instances[0]
    window.last_image_crop = Image.new("RGB", (30, 20), "red")   # left by the previous article
    window.last_prices_frame = Image.new("RGB", (40, 40), "black")
    item = p.onec.read("A1")                    # this lookup reads no picture
    assert item.photo_1c is None
    with Image.open(item.snapshot_path) as im:  # nor the previous article's prices frame
        assert im.convert("RGB").getpixel((5, 15)) == (255, 255, 255)


def test_picture_that_cannot_be_saved_leaves_the_read(tmp_path, monkeypatch):
    p = Parts(tmp_path)
    p.onec.connect()

    class Bad:
        def save(self, *a, **k):
            raise OSError("disk full")

    def lookup(article, session, g, layout):
        session.last_stock_frame = Image.new("RGB", (40, 40), "white")
        session.last_image_crop = Bad()
        return ItemRead(article, {}, Decimal(1), Decimal(1), None)

    p.onec._lookup = lookup
    item = p.onec.read("A1")
    assert item.photo_1c is None and item.snapshot_path


# --- 1C left on «Изображение» --------------------------------------


@pytest.mark.parametrize("left, shown, expected", [
    (True, None, True), (False, "image", True), (False, "stock", False), (None, None, False)])
def test_unexpected_screen_says_when_the_image_tab_is_left(tmp_path, left, shown, expected):
    p = Parts(tmp_path)
    p.onec._tab = lambda frame, layout: shown
    p.onec.connect()
    window = FakeWindow.instances[0]
    if left is not None:
        window.image_tab_left = left
    p.fail = UnexpectedScreen("tables_tail")
    with pytest.raises(UnexpectedScreen) as ei:
        p.onec.read("A1")
    assert getattr(ei.value, "image_tab", False) is expected


def test_failed_connect_check_says_when_the_image_tab_is_shown(tmp_path):
    p = Parts(tmp_path, check_ok=False)
    p.onec._tab = lambda frame, layout: "image"
    with pytest.raises(UnexpectedScreen) as ei:
        p.onec.connect()
    assert ei.value.image_tab is True


def test_tab_check_failure_does_not_hide_the_error(tmp_path):
    p = Parts(tmp_path)

    def boom(frame, layout):
        raise ValueError("no")

    p.onec._tab = boom
    p.onec.connect()
    p.fail = UnexpectedScreen("x")
    with pytest.raises(UnexpectedScreen) as ei:
        p.onec.read("A1")
    assert not getattr(ei.value, "image_tab", False)


def test_connect_accepts_a_window_left_on_the_image_tab(tmp_path):
    """Connecting sends no input; the lookup then clicks «Остатки» first."""
    from pathlib import Path as P

    from orderbot.screen.reader import Layout
    root = P(__file__).parent.parent
    layout = Layout.load(root / "layouts" / "production.yaml")
    image = Image.open(root / "layouts" / "shots" / "live-image-tab.png")
    onec = MacOneC(layout, "rdp", 45.0, tmp_path / "snaps", window_factory=FakeWindow,
                   gate_factory=lambda s: type("G", (), {"start": lambda self: None,
                                                         "stop": lambda self: None})(),
                   backend_factory=lambda w, g, click_gap=None: object(),
                   guard_factory=lambda b, l, *, screenshot: FakeGuard())
    class W(FakeWindow):
        def __init__(self, title_contains=""):
            super().__init__(title_contains)
            self.frame = image

    onec._window_factory = W
    onec.connect()
    assert onec._window is not None


def test_refused_frame_is_kept_whole_for_diagnosis(tmp_path):
    from PIL import Image
    p = Parts(tmp_path)
    p.onec.connect()
    e = UnexpectedScreen("anchor grid_header not found")
    e.frame = Image.new("RGB", (40, 30), "white")
    p.fail = e
    with pytest.raises(UnexpectedScreen):
        p.onec.read("DF620-WHI")
    saved = list((tmp_path / "snaps").glob("*-DF620-WHI-refused.png"))
    assert len(saved) == 1 and Image.open(saved[0]).size == (40, 30)


def test_refusal_without_a_frame_saves_nothing(tmp_path):
    p = Parts(tmp_path)
    p.onec.connect()
    p.fail = UnexpectedScreen("x")
    with pytest.raises(UnexpectedScreen):
        p.onec.read("A1")
    assert not list((tmp_path / "snaps").glob("*-refused.png"))
