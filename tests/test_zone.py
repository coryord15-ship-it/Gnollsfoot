"""Zone detection, pinned to real EQL beta lines (Noss filtered logs, 2026-06).
EQL appends a difficulty suffix "<N> (<Label>)" to zone names — we strip it."""
from app.log_watcher import LogWatcher


def _lw():
    return LogWatcher({})


def test_entered_strips_difficulty():
    lw = _lw()
    got = []
    lw.on_zone(lambda z: got.append(z))
    lw._dispatch("[Wed Jun 10 12:01:42 2026] You have entered The Plane of Hate 4 (Refined).")
    assert got == ["The Plane of Hate"]
    assert lw._current_difficulty == "4"


def test_currently_in_status_line():
    lw = _lw()
    got = []
    lw.on_zone(lambda z: got.append(z))
    lw._dispatch("[Tue Jun 09 14:35:22 2026] You are currently in: The Ruins of Old Guk 0 (Normal)")
    assert got == ["The Ruins of Old Guk"]
    assert lw._current_difficulty == "0"


def test_area_message_ignored():
    lw = _lw()
    got = []
    lw.on_zone(lambda z: got.append(z))
    lw._dispatch("[Tue Jun 09 14:35:22 2026] You have entered an area where levitation does not function.")
    assert got == []


def test_fires_only_on_clean_zone_change():
    lw = _lw()
    got = []
    lw.on_zone(lambda z: got.append(z))
    lw._dispatch("[Wed Jun 10 12:01:42 2026] You have entered The Plane of Hate 4 (Refined).")
    # Same zone, different difficulty -> no second fire, but difficulty updates.
    lw._dispatch("[Wed Jun 10 12:02:00 2026] You are currently in: The Plane of Hate 2 (Adaptive)")
    assert got == ["The Plane of Hate"]
    assert lw._current_difficulty == "2"
