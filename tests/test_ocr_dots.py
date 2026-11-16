from orderbot.screen.ocr import _without_trailing_dots

TEXT = ([[19, 25], [272, 24], [272, 74], [20, 75]], "NX-STP281.", 0.99)


def box(x0, x1):
    return [[x0, 58], [x1, 58], [x1, 69], [x0, 69]]


def test_dot_box_at_the_right_end_is_folded():
    assert _without_trailing_dots([TEXT, (box(265, 278), "。", 0.7)]) == [
        (TEXT[0], "NX-STP281…", 0.99)]


def test_dot_box_inside_the_text_is_not_folded():
    found = [TEXT, (box(100, 110), ".", 0.7)]
    assert _without_trailing_dots(found) == found


def test_second_text_box_is_not_folded():
    found = [TEXT, (box(280, 320), "AB", 0.9)]
    assert _without_trailing_dots(found) == found


def test_single_box_untouched():
    assert _without_trailing_dots([TEXT]) == [TEXT]
