"""One live-combat state object, shared by every tab that needs it.

WHY THIS EXISTS
    The devkit build (BETATESTING) grew its own `Tail` thread that followed the log and fed
    `LiveCombat`. The shipped app already has exactly one log reader -- `LogWatcher` -- and
    `CombatView` already rides it. Porting the devkit tabs across as-is would have given the
    app TWO readers of the same file and TWO parsers that disagree.

    So this is the devkit's `Tail` with the threading removed: same attribute surface
    (`lc`, `loot`, `zone`, `live_seen`, `heal_zero`, `heal_any`) so the ported tabs work
    unchanged, but lines are pushed IN from the existing watcher rather than pulled by a
    thread of its own.

OFFLINE ONLY. Owner, 2026-08-25: *"i dont want any of it public we are working soly offline
mode."* Nothing here uploads, phones home, or reads anything but the game's own log file and
the local snapshots under %LOCALAPPDATA%/GnollGuard/data.
"""
from __future__ import annotations

import datetime
import logging
import re

from app.ui import datapaths

log = logging.getLogger(__name__)

RX_LOOT = re.compile(r"^--You have looted (?P<item>.+?) from (?P<mob>.+?)'s corpse\.--$")
RX_HEAL_ANY = re.compile(r"^(?P<src>.+?) (?:healed|heals) (?P<dst>.+?) for (?P<eff>\d+)")
RX_ZONE = re.compile(r"^You have entered (?P<z>.+?)\.$")


def log_dt(stamp: str):
    """EQ stamps '[Sat Aug 16 01:47:06 2026]'. Return a real datetime, or None.

    🔴 The loot list must carry WHEN THE LINE HAPPENED, not when it was parsed. Startup
    replays megabytes of history, so wall-clock time would date everything to "now" and the
    Loot tab -- which groups by day -- would file a week of drops under today.
    ⚠ The day is space-padded on single digits ("Aug  6"), so split on whitespace rather
    than trusting a fixed-width format string.
    """
    try:
        parts = (stamp or "").split()
        if len(parts) != 5:
            return None
        _, mon, day, clock, year = parts
        return datetime.datetime.strptime("%s %s %s %s" % (mon, day, clock, year),
                                          "%b %d %H:%M:%S %Y")
    except Exception:
        return None


def _norm_mob(s: str) -> str:
    return re.sub(r"^(a|an|the)\s+", "", (s or "").strip().lower())


def clean_item(s: str) -> str:
    """Strip the leading article the LOG writes but the DATABASE does not store.

    🔴 The log says "a Mote of Major Potential"; the export stores "Mote of Major
    Potential". Comparing them raw made every single loot read as NEW -- 22 of 22 on the
    first run. Every name crossing the log/database boundary goes through here.
    """
    s = (s or "").strip()
    s = re.sub(r"^(?:a|an|the)\s+", "", s, flags=re.I)
    return s.strip()


class CombatFeed:
    """Live combat, loot and healing state for the whole app."""

    def __init__(self):
        self.lc = None
        self.loot: list[dict] = []          # newest first
        self.zone = ""
        # 🔴 Priming reads history, so `LiveCombat.current()` returns the last fight in the
        # file -- possibly hours old. Calling that "IN COMBAT" is a lie the moment the app
        # opens. Nothing counts as live until a line arrives AFTER we caught up.
        self.live_seen = False
        self.heal_zero = {"you": 0, "others": 0}
        self.heal_any = {"you": 0, "others": 0}
        # Newest LOG timestamp seen, in the parser's own epoch. A benchmark run has to be
        # bounded in LOG time, not wall-clock: priming replays history, and the two clocks
        # are not the same thing.
        self.last_ts = 0.0
        self._ts = None
        self._parse_ts = None
        self._ready = False
        self._mobs = {}

    # ── setup ───────────────────────────────────────────────────────────────
    def start(self, log_path: str = "") -> bool:
        """Build the parser and teach it the player's name. Safe to call twice."""
        if self._ready:
            return True
        try:
            from app.parsers.combat_parser import (LiveCombat, TS, parse_ts,
                                                   set_player_name, player_name_from_log)
            # Without this the player splits into two actors ("You" and "Morbid") and
            # lifetap self-sustain books as group healing -- measured at 91% of the total.
            try:
                who = player_name_from_log(log_path or "")
                if who:
                    set_player_name(who)
            except Exception:
                log.debug("player name not resolved", exc_info=True)
            self.lc = LiveCombat()
            self._ts, self._parse_ts = TS, parse_ts
            self._mobs = datapaths.load("mobs.json", {})
            self._ready = True
            return True
        except Exception:
            log.exception("combat feed unavailable")
            return False

    # ── ingest ──────────────────────────────────────────────────────────────
    def feed_line(self, line: str, live: bool = True):
        """Called from LogWatcher's thread. Parse only -- never touch widgets here."""
        if not self._ready:
            return
        try:
            m = self._ts.match(line or "")
            if not m:
                return
            body, stamp = m.group("body"), m.group("ts")
            ts = self._parse_ts(stamp)
            if ts > self.last_ts:
                self.last_ts = ts
            self.lc.feed(body, ts)
            if live:
                self.live_seen = True

            z = RX_ZONE.match(body)
            if z:
                self.zone = z.group("z")

            h = RX_HEAL_ANY.match(body)
            if h:
                who = "you" if h.group("src").strip().lower() in ("you", "morbid") else "others"
                self.heal_any[who] += 1
                if int(h.group("eff")) == 0:
                    self.heal_zero[who] += 1

            l = RX_LOOT.match(body)
            if l:
                item, mob = clean_item(l.group("item")), l.group("mob")
                # 'when' is what the Loot tab groups by; it must be the log's own time,
                # not now. 'stamp' is kept raw for anything that wants the original text.
                self.loot.insert(0, {"item": item, "mob": mob, "stamp": stamp,
                                     "when": log_dt(stamp),
                                     "new": self._is_new(item, mob)})
                del self.loot[80:]
        except Exception:
            log.exception("combat feed line")

    def _is_new(self, item: str, mob: str) -> bool:
        """Has anyone recorded this item off this mob before?

        ⚠ Compares against the LOCAL snapshot, so "NEW" means new to that export -- not
        proof nobody on earth has seen it. Said plainly in the UI rather than implied.
        """
        rec = self._mobs.get(_norm_mob(mob))
        if not rec:
            return True
        want = clean_item(item).lower()
        known = {clean_item(x).lower() for x in rec.get("confirmed", [])}
        known |= {clean_item(x).lower() for x in rec.get("wiki_only", [])}
        return want not in known
