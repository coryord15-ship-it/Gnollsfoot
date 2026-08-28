"""Where things actually drop, at what level, and how fast -- from your own logs.

Owner, 2026-08-28: *"i want to start watching other peoples logs for mote drops and parsing
where they were around the level they were ... so we can get a better idea of where to farm
motes"* and *"we can apply the same logic to drops of any kind ... but i want to focus on
motes for the most part."*

🔴 RATE, NEVER COUNT. A raw drop COUNT measures how long you stood somewhere, not how good
the spot is. That mistake has already been made on this project once -- sources were ranked
by count until the owner caught it. Every figure here is per-hour, and `hours` ships
alongside so a reader can see when a rate is built on nothing:

    The Castle of Mistmoore 4 (Refined)   20.9h  194 motes   9.3/hr   <- trustworthy
    The Plane of Fear 1 (Awakened)         1.5h    8 motes   5.2/hr   <- one lucky run

⚠️ THE INSTANCE IS PART OF THE ZONE. Measured on real logs: "The Castle of Mistmoore 4
(Refined)" yields 9.3 motes/hour while plain "The Castle of Mistmoore" yields 1.0 over a
comparable 11 hours. Same castle, 9x apart. Collapsing instance names would average that into
a number describing neither.

TIME IS MEASURED ZONE-ENTRY TO ZONE-CHANGE, and any gap beyond `MAX_SESSION` is discarded as
a logout rather than counted as play. That means the hours are "time between zone changes",
which slightly UNDER-counts a long camp and never over-counts an overnight idle. Stated here
because a farming rate is only as honest as its denominator.

PRIVACY: this produces aggregates -- zone, level band, item, count, hours. Never a log line,
never a character name, never a timestamp. Owner's standing rule is that reading chat and
logs is fine but a player's name must never leave the machine.
"""
from __future__ import annotations

import collections
import io
import logging
import os
import re

log = logging.getLogger(__name__)

RX_LEVEL = re.compile(r"^You have gained a level! Welcome to level (?P<lvl>\d+)!")
RX_ZONE = re.compile(r"^You have entered (?P<zone>.+?)\.$")
RX_LOOT = re.compile(r"^--You have looted (?P<item>.+?) from (?P<mob>.+?)'s corpse\.--$")
RX_SLAIN = re.compile(r"^You have slain (?P<mob>.+?)!")

#: A gap longer than this is a logout, not a camp.
MAX_SESSION = 6 * 3600

#: Level bands rather than exact levels. With a small contributor pool, "the level 37 shaman
#: in Befallen on Tuesday" identifies a person; a band answers the same farming question and
#: does not.
def band(lvl):
    if not lvl:
        return "?"
    lo = (int(lvl) // 10) * 10
    return "%d-%d" % (max(1, lo), lo + 9)


def _clean_item(s: str) -> str:
    """The log writes "a Mote of Major Potential"; the database stores it without the
    article. Every name crossing that boundary goes through here or nothing matches."""
    return re.sub(r"^(?:a|an|the)\s+", "", (s or "").strip(), flags=re.I).strip()


class FarmStats:
    """Aggregate loot-per-hour by zone and level band.

    `item_filter` narrows what is counted -- motes by default, since that is the immediate
    question, but the same machinery answers it for any drop.
    """

    def __init__(self, item_filter=r"\bMote\b"):
        self._filter = re.compile(item_filter, re.I) if item_filter else None
        # (zone, band) -> {"secs", "kills", "items": Counter, "mobs": Counter}
        self.cells: dict = collections.defaultdict(
            lambda: {"secs": 0.0, "kills": 0,
                     "items": collections.Counter(), "mobs": collections.Counter()})
        self._lvl = None
        self._zone = None
        self._since = 0.0

    # ── ingest ──────────────────────────────────────────────────────────────
    def feed(self, body: str, ts: float):
        m = RX_LEVEL.match(body)
        if m:
            # 🔴 Close the interval BEFORE the level changes. Time is bucketed by level band,
            # so a ding mid-camp must split the clock -- otherwise every hour of a long camp
            # lands in whichever band you happened to be in when you finally zoned out, while
            # the drops sat in the band you actually earned them in. Measured: without this,
            # Mistmoore 4 read 23.1 motes/hr over 6.8h; the true figure is 9.3 over 20.9h.
            self._close(ts)
            self._lvl = int(m.group("lvl"))
            self._since = ts
            return

        m = RX_ZONE.match(body)
        if m:
            self._close(ts)
            self._zone, self._since = m.group("zone"), ts
            return

        if not self._zone:
            return
        cell = self.cells[(self._zone, band(self._lvl))]

        if RX_SLAIN.match(body):
            cell["kills"] += 1
            return

        m = RX_LOOT.match(body)
        if m:
            item = _clean_item(m.group("item"))
            if self._filter is None or self._filter.search(item):
                cell["items"][item] += 1
                cell["mobs"][m.group("mob")] += 1

    def _close(self, ts: float):
        if self._zone and self._since:
            d = ts - self._since
            if 0 < d < MAX_SESSION:
                self.cells[(self._zone, band(self._lvl))]["secs"] += d

    def feed_file(self, path: str, ts_re, parse_ts) -> int:
        n = 0
        try:
            with io.open(path, "rb") as fh:
                for raw in fh:
                    m = ts_re.match(raw.decode("utf-8", "replace").rstrip())
                    if not m:
                        continue
                    self.feed(m.group("body"), parse_ts(m.group("ts")))
                    n += 1
        except Exception:
            log.debug("could not read %s", path, exc_info=True)
        return n

    # ── output ──────────────────────────────────────────────────────────────
    def rows(self, min_hours: float = 0.0) -> list:
        """One row per (zone, band). Sorted by RATE, with hours kept visible."""
        out = []
        for (zone, lvl), c in self.cells.items():
            total = sum(c["items"].values())
            hours = c["secs"] / 3600.0
            if not total and hours < min_hours:
                continue
            if hours < min_hours:
                continue
            out.append({
                "zone": zone,
                "level_band": lvl,
                "hours": round(hours, 2),
                "kills": c["kills"],
                "drops": total,
                "per_hour": round(total / hours, 2) if hours > 0.01 else 0.0,
                "by_item": dict(c["items"].most_common()),
                "top_mobs": dict(c["mobs"].most_common(5)),
            })
        return sorted(out, key=lambda r: -r["per_hour"])

    def submission(self, min_hours: float = 0.25) -> list:
        """Aggregate rows safe to share: no names, no timestamps, no log text.

        `min_hours` drops slivers that would publish a meaningless rate -- a single kill in a
        zone is not evidence about that zone, and pooling many of them would create confident
        nonsense at scale.
        """
        return [{
            "zone": r["zone"],
            "level_band": r["level_band"],
            "hours": r["hours"],
            "kills": r["kills"],
            "drops": r["drops"],
            "by_item": r["by_item"],
        } for r in self.rows(min_hours=min_hours) if r["drops"]]
