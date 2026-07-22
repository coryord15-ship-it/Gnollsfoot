"""Regression test for the on_any_line dispatch bug (found 2026-07-21).

`LogWatcher._dispatch` used to call every `on_any_line` callback with ZERO arguments (a leftover
from when the hook was only a silence-timer ping). But the quest matcher registers a callback that
needs the raw line (to catch "You say, 'Hail, X'" / keyword replies) — so every single log line
threw a caught-and-logged TypeError, and hail/say quest-step matching silently never fired. This
pins the contract: on_any_line callbacks receive the line.
"""
from app.log_watcher import LogWatcher


def test_on_any_line_receives_the_raw_line():
    w = LogWatcher({})
    got = []
    w.on_any_line(lambda line: got.append(line))
    line = "[Tue Jul 21 20:00:00 2026] You say, 'Hail, Doug'"
    w._dispatch(line)
    assert got == [line]


def test_on_any_line_feeds_a_you_say_matcher():
    """The exact shape that was broken: a callback that parses 'You say' off the raw line."""
    import re
    w = LogWatcher({})
    seen = []

    def matcher(raw):
        m = re.search(r"You say,?\s*'(?P<text>.+?)'", raw or "")
        if m:
            seen.append(m.group("text"))

    w.on_any_line(matcher)
    w._dispatch("[Tue Jul 21 20:00:00 2026] You say, 'supplies'")
    assert seen == ["supplies"]
