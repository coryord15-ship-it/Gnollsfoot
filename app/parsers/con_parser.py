"""/consider prints the mob's LEVEL. Harvest it from the log.

    [Thu Aug 20 21:39:11 2026] A decrepit warder scowls at you, ready to attack --
                               looks like it would wipe the floor with you! (Lvl: 52)

Measured across 2.94M lines of our own logs: 399 distinct mobs carry a level this way, and
200 of them were not in `mobs.json` at all. Free, measured, no wiki and no scraping.

🔴 MOBS SPAWN IN LEVEL RANGES -- NEVER AVERAGE THEM.
    orc centurion   cons at 4, 5, 7, 8, 9, 10
    rock golem      cons at 49, 50, 51, 52, 53
    earth elemental cons at 15, 16, 22, 23   <- probably TWO spawns sharing one name
An average produces a level no mob in the game actually is, which is the same class of error
as inventing a drop rate. We store min, max and the observation count, and a wide spread is
reported as a flag rather than quietly smoothed away.

⚠ ONLY THE NUMBER IS A FACT. The con VERB ("scowls", "regards you indifferently", "judges you
amiably") is a difficulty-and-faction signal RELATIVE TO YOUR OWN LEVEL -- the same mob cons
differently to a different character, and differently to the same character ten levels later.
Parse `(Lvl: N)` and discard everything else.

⚠ A con proves nothing about player-vs-NPC: you can consider anyone. This is a level source,
never an identity source.
"""
from __future__ import annotations

import re

#: Every con verb we have actually observed, plus the ones EQ is documented to use. The verb
#: is not trusted for anything -- it only anchors where the mob's name ends.
CON_VERBS = (
    "scowls at you, ready to attack",
    "glares at you threateningly",
    "glowers at you dubiously",
    "looks your way apprehensively",
    "regards you indifferently",
    "judges you amiably",
    "kindly considers you",
    "regards you as an ally",
    "looks upon you warmly",
    "considers you a friend",
)

RX_CON = re.compile(
    r"^(?P<mob>.+?) (?:" + "|".join(re.escape(v) for v in CON_VERBS) + r")\b"
    r".*?\(Lvl:\s*(?P<lvl>\d+)\)")

#: The client writes the rare marker into the name itself. Keep it as a flag, strip it from
#: the key, or "glyphed ghoul" and "glyphed ghoul - a rare creature -" become two mobs.
RX_RARE = re.compile(r"\s*-\s*a rare creature\s*-\s*", re.I)

#: A spread wider than this in one name almost certainly means two different spawns share it,
#: not one mob with a huge range. Flagged for a human, never merged silently.
WIDE_SPREAD = 6


def norm_mob(s: str) -> str:
    """Lowercase, article-stripped, rare-marker removed -- the key `mobs.json` uses."""
    s = RX_RARE.sub(" ", s or "").strip()
    return re.sub(r"^(a|an|the)\s+", "", s.strip().lower()).strip()


class ConLevels:
    """Accumulate observed levels per mob."""

    def __init__(self):
        self.seen: dict[str, dict[int, int]] = {}
        self.rare: set[str] = set()

    def feed(self, body: str) -> bool:
        m = RX_CON.match(body or "")
        if not m:
            return False
        raw = m.group("mob")
        key = norm_mob(raw)
        if not key:
            return False
        if RX_RARE.search(raw):
            self.rare.add(key)
        self.seen.setdefault(key, {})
        lvl = int(m.group("lvl"))
        self.seen[key][lvl] = self.seen[key].get(lvl, 0) + 1
        return True

    def rows(self) -> list:
        out = []
        for mob, counts in self.seen.items():
            lvls = sorted(counts)
            spread = lvls[-1] - lvls[0]
            out.append({
                "mob": mob,
                "level_min": lvls[0],
                "level_max": lvls[-1],
                # The most-observed level, for callers that must show ONE number. Not a mean.
                "level_common": max(counts.items(), key=lambda kv: kv[1])[0],
                "observations": sum(counts.values()),
                "rare": mob in self.rare,
                # A human should look at these before they are trusted as one mob.
                "suspect_two_spawns": spread >= WIDE_SPREAD,
            })
        return sorted(out, key=lambda r: -r["observations"])
