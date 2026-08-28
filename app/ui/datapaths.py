"""Where the local reference snapshots live -- deliberately OUTSIDE this repository.

🔴 THIS REPO (`Gnollsfoot`) IS PUBLIC. The item/mob/exaltation snapshots are exports of the
project database: 8,057 items, 4,822 mobs, 1,386 exaltation donors. Dropping them into the
source tree would put the whole dataset one `git add` away from being published, and the
project's own rule is that `git add -A` must never be used here precisely because the tree
carries unrelated work.

So they live under %LOCALAPPDATA%/GnollGuard/data and are found at runtime. A missing file is
a SILENT no-op returning the caller's default -- the tabs that use them degrade to "no data"
rather than crashing the app, and nothing here ever downloads a replacement.

Owner, 2026-08-25: *"i dont want any of it public we are working soly offline mode."*
"""
from __future__ import annotations

import json
import logging
import os
import sys

log = logging.getLogger(__name__)

#: Overridable for testing; normally %LOCALAPPDATA%\GnollGuard\data
DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "GnollGuard", "data")

_cache: dict[str, object] = {}


def _bundled_dir() -> str:
    """Where PyInstaller unpacked our bundled snapshots, or "" when running from source.

    sys._MEIPASS is the temp folder a one-file build extracts into. It is READ-ONLY for our
    purposes and is deleted on exit, so it is a fallback source only -- never a write target.
    """
    base = getattr(sys, "_MEIPASS", "")
    if not base:
        return ""
    d = os.path.join(base, "data")
    return d if os.path.isdir(d) else ""


def path(name: str) -> str:
    """Prefer a user-updated snapshot; fall back to the one bundled in the build.

    A user who refreshes their data drops newer files in %LOCALAPPDATA% and those win. A
    fresh install has none, so the bundled copy keeps Gear and Codex working out of the box
    instead of showing two empty tabs with no explanation.
    """
    local = os.path.join(DATA_DIR, name)
    if os.path.exists(local):
        return local
    bundled = _bundled_dir()
    if bundled:
        cand = os.path.join(bundled, name)
        if os.path.exists(cand):
            return cand
    return local


def available(name: str) -> bool:
    return os.path.exists(path(name))


def load(name: str, default):
    """Read a snapshot, or hand back `default` if it is not there.

    Cached: `items.json` is 4 MB and several tabs want it. Re-reading it per redraw was
    never going to be acceptable on a UI thread.
    """
    if name in _cache:
        return _cache[name]
    p = path(name)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            val = json.load(fh)
    except FileNotFoundError:
        log.info("snapshot not present: %s", p)
        val = default
    except Exception:
        log.exception("snapshot unreadable: %s", p)
        val = default
    _cache[name] = val
    return val


def missing() -> list[str]:
    """Which snapshots the data-backed tabs need but cannot find."""
    return [n for n in ("items.json", "mobs.json", "exaltations.json") if not available(n)]
