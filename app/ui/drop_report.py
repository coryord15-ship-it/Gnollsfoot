"""Report NEW item-off-mob sightings to the community database.

WHY THIS EXISTS: `submit_drop_report` was in the sync layer with NO CALLER. The app watched
the player loot things, already worked out which combinations the database had never recorded
(`CombatFeed._is_new`), and then reported none of it. Every one of the 5,277 rows in
`drop_reports` came from the website form -- nothing from anyone actually playing. This closes
that, which is the single biggest gap in "improve our db of drops".

🔴 NEW COMBINATIONS ONLY. Reporting every loot event would file thousands of duplicates of
things already known, drown the real signal and hit the endpoint's rate limit within a minute.
A drop is worth sending when the local snapshot has never seen that item off that mob.

⚠ NEVER REPORT DURING PRIMING. Startup replays megabytes of history through the same parser;
sending that would re-file the player's entire back catalogue on every launch -- the exact bug
that made inventory re-upload its whole dump each start. Only lines that arrive AFTER catch-up
count, which `CombatFeed.live_seen` already tracks.

PRIVACY: item name, mob name, zone. Game nouns only -- never a character name, never a chat
line, never a timestamp of play.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from app.ui import datapaths

log = logging.getLogger(__name__)

STATE_NAME = "drops_sent.json"

#: The endpoint burst-limits at 5/min, so a flush is capped well under that and the rest waits
#: for the next tick. Losing nothing, just slower.
MAX_PER_FLUSH = 4
FLUSH_MS = 5 * 60 * 1000

_lock = threading.Lock()
_pending: list[dict] = []


def _path() -> str:
    return os.path.join(datapaths.DATA_DIR, STATE_NAME)


def _load() -> set:
    """Combinations already sent, so a relaunch does not re-file them."""
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            return set(json.load(fh).get("sent") or [])
    except FileNotFoundError:
        return set()
    except Exception:
        log.exception("drop ledger unreadable; starting fresh")
        return set()


def _save(sent: set) -> None:
    try:
        os.makedirs(datapaths.DATA_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            # Bounded: this is a dedupe ledger, not an archive. The DB is the record.
            json.dump({"sent": sorted(sent)[-20000:]}, fh)
        os.replace(tmp, _path())
    except Exception:
        log.exception("could not write drop ledger")


def note(item: str, mob: str, zone: str, is_new: bool, live: bool) -> None:
    """Called from the parser thread when loot happens. Cheap: appends and returns."""
    if not (is_new and live and item and mob):
        return
    key = "%s|%s" % (item.strip().lower(), mob.strip().lower())
    with _lock:
        if any(p["_key"] == key for p in _pending):
            return
        _pending.append({"_key": key, "item_name": item.strip(),
                         "drop_npc": mob.strip(), "drop_zone": (zone or "").strip()})


def flush(app) -> int:
    """Send up to MAX_PER_FLUSH queued reports. Returns how many were accepted."""
    sb = getattr(app, "supabase", None)
    if not sb:
        return 0
    with _lock:
        if not _pending:
            return 0
        sent = _load()
        batch, keep = [], []
        for p in _pending:
            if p["_key"] in sent:
                continue                      # already filed in an earlier session
            (batch if len(batch) < MAX_PER_FLUSH else keep).append(p)
        _pending[:] = keep

    ok = 0
    for p in batch:
        try:
            if sb.submit_drop_report(p["item_name"], 0, p["drop_zone"], p["drop_npc"]):
                ok += 1
                sent.add(p["_key"])
            else:
                # Failed sends go back in the queue -- the ledger is only advanced for what
                # actually landed, so nothing is silently dropped.
                with _lock:
                    _pending.append(p)
        except Exception:
            log.debug("drop report failed", exc_info=True)
            with _lock:
                _pending.append(p)
    if ok:
        _save(sent)
        log.info("drop reports: %d sent, %d queued", ok, len(_pending))
    return ok


def start_timer(app) -> None:
    """Flush every 5 minutes. Re-arms even when a tick raises."""
    def tick():
        try:
            if getattr(app, "auth", None) and app.auth.is_logged_in:
                flush(app)
        except Exception:
            log.exception("drop report tick")
        finally:
            try:
                app.after(FLUSH_MS, tick)
            except Exception:
                pass
    try:
        app.after(90 * 1000, tick)     # after startup priming has settled
    except Exception:
        log.debug("drop report timer not armed", exc_info=True)
