"""One live-combat state object, shared by every tab that needs it.

WHY THIS EXISTS
    The app has exactly ONE log reader (`LogWatcher`). Every tab that needs combat state
    reads THIS object, which the watcher pushes lines into. Giving each tab its own tailer
    would mean several readers of one file and several parsers that disagree about the same
    fight, so there is deliberately only one.

READS THE LOG, NOTHING ELSE. No packet capture, no hooking, no memory reading, no input
simulation -- the game's own log file and the local snapshots under
%LOCALAPPDATA%/GnollGuard/data, and that is the whole input surface. Permanent.

⚠ This module still opens NO sockets. Sharing pooled farming rates is a separate, explicit
step in `app/sync/supabase.py` that the user turns on -- parsing and uploading are kept apart
on purpose so that reading a log can never imply sending one. (The "solely offline" rule this
file used to cite was scoped to the closed beta; owner clarified 2026-08-28 that the shipped
app is live for everyone. The parse/upload separation is NOT part of that relaxation.)
"""
from __future__ import annotations

import datetime
import logging
import os
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
        #: The player's own character name, lowercased, resolved from the log at start().
        #: 🔴 This used to be the literal string "morbid". EQ writes self-heals under the
        #: character's NAME as often as under "You", so a hardcoded name meant every user who
        #: was not the author had their own heals bucketed as healing OTHERS -- and so did the
        #: author's own second character. Never hardcode the player.
        self.me = ""
        #: Drop rates by zone and level band. Rides the SAME reader as combat -- a second
        #: tailer would disagree with this one about where you were standing.
        self.farm = None
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
                    self.me = who.strip().lower()
            except Exception:
                log.debug("player name not resolved", exc_info=True)
            self.lc = LiveCombat()
            try:
                from app.farm_stats import FarmStats
                self.farm = FarmStats()          # motes by default
            except Exception:
                log.debug("farm stats unavailable", exc_info=True)
            self._ts, self._parse_ts = TS, parse_ts
            self._mobs = datapaths.load("mobs.json", {})
            self._ready = True
            return True
        except Exception:
            log.exception("combat feed unavailable")
            return False

    def seed_charm_state(self, log_path: str, mb: int = 40) -> int:
        """Pre-scan far back for CHARM/PET lines only, before damage priming.

        🔴 Owner, 2026-08-25: the pet showed as its own row instead of tiering under him,
        because the app's `charmed` set did not contain it. Damage priming reads the last
        4 MB — but a charm cast, or the pet last answering a command, can easily be older
        than that. A pet charmed an hour ago and never spoken to since is then invisible as
        a pet, and its damage renders as an independent combatant.

        This walks a much larger window and feeds ONLY the lines that establish charm state.
        It is cheap because it matches against three narrow patterns and never touches the
        damage path — 40 MB scans in well under a second and costs no fight state.

        ⚠ Deliberately runs BEFORE damage priming: `_owner_of` is evaluated at SHAPING time,
        not at ingest, so ordering is not strictly required — but seeding first means a
        `current()` call during startup already has the right owner.
        """
        if not self._ready or not log_path or not os.path.exists(log_path):
            return 0
        try:
            from app.parsers.combat_parser import (RX_CHARM_BREAK, RX_CHARM_FAIL,
                                                   RX_CHARM_LAND, RX_PET_SPEAK, RX_YOU_CAST)
        except Exception:
            return 0
        pats = (RX_PET_SPEAK, RX_CHARM_LAND, RX_CHARM_BREAK, RX_CHARM_FAIL, RX_YOU_CAST)
        n = 0
        try:
            size = os.path.getsize(log_path)
            with open(log_path, "rb") as fh:
                fh.seek(max(0, size - mb * 1024 * 1024))
                fh.readline()
                for raw in fh:
                    line = raw.decode("utf-8", "replace").rstrip()
                    m = self._ts.match(line)
                    if not m:
                        continue
                    body = m.group("body")
                    if not any(p.match(body) for p in pats):
                        continue
                    self.lc.feed(body, self._parse_ts(m.group("ts")))
                    n += 1
        except Exception:
            log.exception("charm seed scan")
        log.info("charm seed: %d lines, %d pets known", n, len(getattr(self.lc, "charmed", {})))
        return n

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
            if self.farm is not None:
                self.farm.feed(body, ts)
            if live:
                self.live_seen = True

            z = RX_ZONE.match(body)
            if z:
                self.zone = z.group("z")

            h = RX_HEAL_ANY.match(body)
            if h:
                src = h.group("src").strip().lower()
                who = "you" if src == "you" or (self.me and src == self.me) else "others"
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
