"""When and whether farming rates are sent, and the ledger that stops double-counting.

WHY A LEDGER AT ALL
    `FarmStats` counts CUMULATIVELY over the whole log history. Sending that table twice
    inserts the same hours twice, and the pooled reader sums hours across rows. The per-hour
    rate survives it (hours and drops inflate together, which is why nobody notices), but the
    hours column is what tells a reader how much evidence is behind a number -- and a repeat
    sender's experience gets weighted twice in the pooled average. So we send DELTAS, and the
    baseline that defines "since when" is persisted here.

    🔴 The baseline is written ONLY after a send actually succeeded. Writing it on a failure
    would discard that window permanently: the data never reaches the pool and nothing ever
    reports an error.

WHY A SLOW TIMER AND NOT PER-DROP
    Sending on every loot line would be one request per mote per player. At a few thousand
    users that is a sustained write flood for data that nobody reads in real time -- and it
    would file one row per drop, destroying the "one row = one contributor's time in one cell"
    invariant the pooled maths depends on. Farming rates are a slow-moving statistic; a few
    times a day is not a compromise, it is the correct resolution.

    Actual arithmetic, so the choice is not vibes: 3,000 users sending once every 6 hours is
    12,000 requests/day, about 0.14/second. Once a day is 0.035/second. Per-drop, assuming a
    mote every 10 minutes for 3,000 concurrent players, is ~5/second sustained AND turns one
    tidy row into hundreds of fragments.

OPT-IN. Nothing is sent unless the user turned it on. Off is the default.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from app.ui import datapaths

log = logging.getLogger(__name__)

STATE_NAME = "farm_share.json"

#: Six hours. Long enough that the write volume is irrelevant at any user count we will
#: plausibly reach, short enough that a session's farming is not stranded for a day.
INTERVAL_SECS = 6 * 3600

#: Never publish a rate built on less than this in one cell. A single kill in a zone is not
#: evidence about that zone, and thousands of such slivers would be confident nonsense.
MIN_HOURS = 0.25

_lock = threading.Lock()


def _state_path() -> str:
    return os.path.join(datapaths.DATA_DIR, STATE_NAME)


def load_state() -> dict:
    """Read the ledger. Deliberately NOT `datapaths.load`, which caches for snapshots."""
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            return st
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("farm share state unreadable; starting fresh")
    return {"enabled": False, "baseline": {}, "last_sent": 0.0}


def save_state(st: dict) -> None:
    try:
        os.makedirs(datapaths.DATA_DIR, exist_ok=True)
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, _state_path())      # atomic: a crash must not leave a half ledger
    except Exception:
        log.exception("could not write farm share state")


def enabled() -> bool:
    return bool(load_state().get("enabled"))


def set_enabled(on: bool) -> None:
    with _lock:
        st = load_state()
        st["enabled"] = bool(on)
        save_state(st)
    log.info("farm sharing %s", "ENABLED" if on else "disabled")


def pending(farm) -> list:
    """What WOULD be sent right now: the increment since the last successful send."""
    if farm is None:
        return []
    try:
        return farm.submission(min_hours=MIN_HOURS,
                               baseline=load_state().get("baseline") or {})
    except Exception:
        log.exception("could not compute pending farm rows")
        return []


def send_now(app, force: bool = False) -> tuple[bool, str]:
    """Send the delta. Returns (sent, human-readable reason).

    `force` is the manual button: it ignores the interval but still honours the ledger, so
    pressing it twice in a row sends nothing the second time rather than duplicating.
    """
    farm = getattr(app.tail, "farm", None) if getattr(app, "tail", None) else None
    if farm is None:
        return False, "no farming data yet"

    with _lock:
        st = load_state()
        if not force and not st.get("enabled"):
            return False, "sharing is off"
        if not force and (time.time() - float(st.get("last_sent") or 0)) < INTERVAL_SECS:
            return False, "already sent recently"

        rows = farm.submission(min_hours=MIN_HOURS, baseline=st.get("baseline") or {})
        if not rows:
            return False, "nothing new to share"

        sb = getattr(app, "supabase", None)
        if not sb:
            return False, "not connected"
        try:
            ok = bool(sb.submit_farm_report(rows))
        except Exception:
            log.exception("farm report send")
            ok = False
        if not ok:
            # Baseline deliberately untouched -- this window retries on the next tick.
            return False, "send failed, will retry"

        st["baseline"] = farm.snapshot()
        st["last_sent"] = time.time()
        save_state(st)
    return True, "shared %d zones" % len(rows)


def start_timer(app) -> None:
    """Check every 15 minutes whether a send is due. Cheap: usually a dict read and a return.

    Uses Tk's `after` rather than a thread so it cannot race the parser, and re-arms itself
    even when a tick raises -- a broken send must not silently stop all future ones.
    """
    def tick():
        try:
            if enabled():
                sent, why = send_now(app)
                if sent:
                    log.info("farm share: %s", why)
        except Exception:
            log.exception("farm share tick")
        finally:
            try:
                app.after(15 * 60 * 1000, tick)
            except Exception:
                pass

    try:
        # First check a minute in, so startup priming has finished counting before we look.
        app.after(60 * 1000, tick)
    except Exception:
        log.debug("could not arm farm share timer", exc_info=True)
