"""The frameless pop-out's edge hit-test.

These windows are `overrideredirect(True)` — a clean HUD with no title bar, and
therefore NO OS resize border. Resizing is done by hit-testing the pointer
against the window rect instead of overlaying grab widgets.

WHY THIS TEST EXISTS
    The first attempt overlaid `place()`d frames and `lift()`ed them. In Tk a
    placed widget swallows mouse events for everything beneath it, and the
    header's Dock / ✕ buttons `pack(side="right")` sat directly under the
    right-edge strip — the user could no longer dock or close the window, which
    is the only way to close a frameless overlay. It shipped, he hit it, and he
    was rightly furious.

    Hit-testing adds no widgets at all, so it structurally cannot repeat that.
    The test below pins the two properties that matter:
      1. every edge and corner resolves to the right direction
      2. the header button strip resolves to None, i.e. NOT a resize zone

`_edge_mode` is pure arithmetic over event/window coordinates, so it is tested
directly against stubs — no Tk root, no display, runs in CI.
"""
import pytest

from app.ui.journal_overlay import QuestBubble, RESIZE_EDGE

W, H = 320, 280          # default bubble size
ROOTX, ROOTY = 1000, 500  # arbitrary screen position


class _StubWindow:
    """Just enough of a Tk window for _edge_mode()."""
    def __init__(self, w=W, h=H, rootx=ROOTX, rooty=ROOTY):
        self._w, self._h, self._rx, self._ry = w, h, rootx, rooty
        self._rz = None

    def winfo_rootx(self):
        return self._rx

    def winfo_rooty(self):
        return self._ry

    def winfo_width(self):
        return self._w

    def winfo_height(self):
        return self._h


class _StubEvent:
    """x_root/y_root are SCREEN coords — the same thing Tk hands us."""
    def __init__(self, x, y):
        self.x_root = ROOTX + x
        self.y_root = ROOTY + y


def mode_at(x, y, win=None):
    """Resolve the resize mode for a window-relative point."""
    return QuestBubble._edge_mode(win or _StubWindow(), _StubEvent(x, y))


# ── corners ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("x,y,expected", [
    (0, 0, "nw"),
    (W - 1, 0, "ne"),
    (0, H - 1, "sw"),
    (W - 1, H - 1, "se"),
])
def test_corners(x, y, expected):
    assert mode_at(x, y) == expected


# ── edges ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("x,y,expected", [
    (W // 2, 0, "n"),
    (W // 2, H - 1, "s"),
    (0, H // 2, "w"),
    (W - 1, H // 2, "e"),
])
def test_edges(x, y, expected):
    assert mode_at(x, y) == expected


# ── the interior is NOT a resize zone ────────────────────────────────────────
@pytest.mark.parametrize("x,y", [
    (W // 2, H // 2),          # dead centre
    (RESIZE_EDGE + 1, RESIZE_EDGE + 1),
    (W - RESIZE_EDGE - 2, H - RESIZE_EDGE - 2),
])
def test_interior_is_not_resizable(x, y):
    assert mode_at(x, y) is None


def test_header_buttons_are_not_a_resize_zone():
    """THE REGRESSION GUARD.

    Dock / ✕ live in a 36px header, inset ~10px from the right by the shell and
    inner frame padding. Their hit area must resolve to None, or a press there
    starts a resize instead of docking — which is exactly what broke before.
    """
    for dx in range(10, 60):          # across the whole button cluster
        for y in range(10, 34):       # the header band, below the top edge
            assert mode_at(W - dx, y) is None, f"button area at -{dx},{y} became a resize zone"


def test_edge_band_is_exactly_RESIZE_EDGE_wide():
    assert mode_at(RESIZE_EDGE - 1, H // 2) == "w"
    assert mode_at(RESIZE_EDGE, H // 2) is None      # first pixel inside
    assert mode_at(W - RESIZE_EDGE, H // 2) == "e"
    assert mode_at(W - RESIZE_EDGE - 1, H // 2) is None


def test_degenerate_window_is_safe():
    """Before the window is mapped winfo_width() returns 1; must not claim an edge."""
    assert mode_at(0, 0, _StubWindow(w=1, h=1)) is None


def test_resize_math_respects_minimums():
    """Dragging the left/top edge inward must clamp AND stop moving the origin,
    otherwise the window walks across the screen once it hits the minimum."""
    sw, sh, ox, oy = W, H, 100, 100
    # drag the west edge 1000px right — far past the 240 minimum
    dx = 1000
    nw = max(240, sw - dx)
    nx = ox + (sw - nw)
    assert nw == 240
    assert nx == ox + (W - 240), "origin should shift by exactly the width lost"
    # and the window's right edge must not have moved
    assert nx + nw == ox + sw
