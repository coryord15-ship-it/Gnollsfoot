"""Load fights from the log ARCHIVES so any mob ever killed can be drilled into.

Owner, 2026-08-25: *"will there be away for me to go back and look into the drill downs of
mobs ive killed before?"*

The live parser keeps `MAX_FIGHTS = 300` and trims the rest — deliberately, since it runs
for hours and must not grow without bound. Everything older is still on disk, so this
re-reads it on demand into a SEPARATE parser that the live one never sees.

🔴 LOG FILES ROTATE TO `.bak`, AND NOT ALWAYS INTO THE SAME FOLDER. A plain `eqlog_*.txt`
glob misses the overwhelming majority of a long-running player's history — measured at 62 of
2,151,301 lines. Always go through `app.log_discovery`, and treat `.bak` files as the
archive, never as junk.

⚠ Runs on a WORKER THREAD. Parsing a day out of 2.1M lines takes seconds, and doing it on
the UI thread would freeze the window — which is the same class of problem as the refresh
storm the owner already complained about.
"""
from __future__ import annotations

import datetime
import logging
import os
import threading

log = logging.getLogger(__name__)

def _log_files():
    """Every log file including rotated archives, newest first.

    `log_discovery` searches the EverQuest install and this app's archive folders, so this
    works on any machine with no external tooling.
    """
    try:
        from app.log_discovery import log_files as _lf
        return _lf()
    except Exception:
        log.debug("log discovery failed", exc_info=True)
        return []


def available_days(limit_files: int = 40) -> list:
    """Which calendar days have combat, cheaply — first and last stamp per file.

    Reading only the head and tail of each file keeps this near-instant; a day that only
    appears in the middle of a very long file can still be typed in manually.
    """
    from app.parsers.combat_parser import TS, parse_ts
    days = set()
    for p in _log_files()[:limit_files]:
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as fh:
                head = fh.readline().decode("utf-8", "replace")
                fh.seek(max(0, size - 4096))
                tail = fh.read().decode("utf-8", "replace").splitlines()
            for line in [head] + tail[-3:]:
                m = TS.match(line.rstrip())
                if not m:
                    continue
                t = parse_ts(m.group("ts"))
                if t:
                    days.add(datetime.date.fromordinal(int(t // 86400) + 719163))
        except Exception:
            continue
    return sorted(days, reverse=True)


def load_day(day: datetime.date, on_done, on_progress=None):
    """Parse every fight on `day` into a fresh parser, off the UI thread.

    `on_done(parser_or_None, note)` is called from the WORKER thread — the caller is
    responsible for bouncing back to the UI thread (tkinter `after`) before touching widgets.
    """
    def work():
        try:
            from app.parsers.combat_parser import (LiveCombat, TS, parse_ts,
                                                   set_player_name, player_name_from_log)
            files = _log_files()
            if not files:
                on_done(None, "no log files found")
                return
            try:
                who = player_name_from_log(files[0])
                if who:
                    set_player_name(who)
            except Exception:
                pass

            lc = LiveCombat()
            # This parser is for BROWSING history, so the 300-fight trim that protects the
            # live one would silently throw away most of the day. Raise it here only.
            lc.MAX_FIGHTS = 100000

            lo = (day.toordinal() - 719163) * 86400
            hi = lo + 86400
            scanned = matched = 0
            for p in files:
                try:
                    with open(p, "rb") as fh:
                        for raw in fh:
                            scanned += 1
                            if scanned % 250000 == 0 and on_progress:
                                on_progress(scanned, matched)
                            line = raw.decode("utf-8", "replace").rstrip()
                            m = TS.match(line)
                            if not m:
                                continue
                            t = parse_ts(m.group("ts"))
                            if t < lo or t >= hi:
                                continue
                            matched += 1
                            lc.feed(m.group("body"), t)
                except Exception:
                    continue
            note = ("%s — %d fights from %s lines"
                    % (day.strftime("%a %b %d %Y"), len(lc.fights), format(matched, ",")))
            on_done(lc if lc.fights else None,
                    note if lc.fights else "%s — no combat found" % day.strftime("%a %b %d"))
        except Exception as exc:
            log.exception("history load failed")
            on_done(None, "failed: %s" % exc)

    threading.Thread(target=work, daemon=True).start()
