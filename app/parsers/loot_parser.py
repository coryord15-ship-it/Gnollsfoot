"""
Loot detection. All patterns are loaded from config — never hardcoded here.
EQL's exact log format is unknown until launch (June 16); the user can edit
patterns in settings.json live during beta and they take effect immediately.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class LootEvent:
    item_name: str          # BASE name only — no stack count, no +N tier
    npc_name: Optional[str] = None
    raw_line: str = ""
    quantity: int = 1       # "2 Bone Chips" -> name "Bone Chips", quantity 2
    tier: int = 0           # "Bronze Long Sword +4" -> name "...Sword", tier 4


# Looted coin ("1 platinum 4 gold 6 silver", "1 copper", ...) matches the loot
# trigger but is money, not an item — drop it so it never clutters the Items list.
# 🔴 THE LEADING COUNT IS OPTIONAL HERE, AND THAT IS THE WHOLE POINT.
# The configured pattern is `(?:a |an |(?P<qty>\d+) )?(?P<item>.+?)\.` — so on
# "You have looted 1 platinum 4 gold 6 silver." the regex eats "1 " into `qty` and hands
# this check the string "platinum 4 gold 6 silver", with no leading digit left. The
# original version of this pattern REQUIRED a leading digit, so it failed to match and
# the app recorded an item literally named "platinum 4 gold 6 silver".
# Making the first count optional catches it either way.
_COIN_RE = re.compile(
    r"^\s*(?:\d[\d,]*\s*)?(?:platinum|gold|silver|copper)\b"
    r"(?:[\s,]*\d[\d,]*\s*(?:platinum|gold|silver|copper)\b)*\s*$",
    re.IGNORECASE,
)


# 🔴 A LOOT LINE CARRIES THREE THINGS AND THE NAME IS ONLY ONE OF THEM.
#
#   "You have looted 2 Bone Chips"        -> two Bone Chips, NOT an item called "2 Bone Chips"
#   "You have looted a Bronze Long Sword +4" -> a known item at tier 4, NOT a new item
#
# Leaving either in the name orphans the row: it matches nothing in any table, so it never
# contributes to drop data and it breaks quest-item matching for stackable turn-ins, which
# is most of them. It reported 362 "new discoveries" when the real number was 20.
#
# The same bug in a different place made one vendor price look like two — a stack of 2 sold
# for double, so "Bone Chips" appeared to have prices [11, 22] until the quantity was
# divided out. A leading integer is DATA; code that treats it as part of the name corrupts
# whatever it touches downstream.
#
# Downstream tooling had been normalising around this after the fact. Here is the actual
# bug, so both values are kept instead of being thrown away.
_QTY_RE = re.compile(r"^\s*(\d[\d,]*)\s+(?=\S)")
_TIER_RE = re.compile(r"\s*\+(\d+)\s*$")


def split_item_name(raw: str) -> tuple[str, int, int]:
    """'2 Bone Chips +3' -> ('Bone Chips', 2, 3). Always returns a usable base name."""
    s = " ".join((raw or "").split()).strip()
    qty = 1
    m = _QTY_RE.match(s)
    if m:
        rest = s[m.end():].strip()
        if rest:                      # never strip the number if nothing is left
            try:
                qty = int(m.group(1).replace(",", "")) or 1
            except ValueError:
                qty = 1
            s = rest
    tier = 0
    t = _TIER_RE.search(s)
    if t:
        base = _TIER_RE.sub("", s).strip()
        if base:                      # "+4" alone is not an item
            tier = int(t.group(1))
            s = base
    return s, qty, tier


class LootParser:
    def __init__(self, patterns: list[str]):
        """
        patterns: list of regex strings from config["log_patterns"]["loot_triggers"].
        Each pattern must capture a named group 'item'; optionally 'npc'.
        """
        self._compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    def reload(self, patterns: list[str]):
        """Hot-reload patterns without restarting the watcher."""
        self._compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    def parse(self, line: str) -> Optional[LootEvent]:
        for pattern in self._compiled:
            m = pattern.search(line)
            if m:
                groups = m.groupdict()
                item_name = groups.get("item", "").strip()
                # ⚠ The coin test must run BEFORE the quantity strip, or "1 platinum"
                # becomes the item "platinum" with quantity 1.
                if not item_name or _COIN_RE.match(item_name):
                    return None  # empty or looted coin — not an item
                base, qty, tier = split_item_name(item_name)
                if not base:
                    return None
                # The configured patterns ALREADY capture the stack count as (?P<qty>\d+)
                # — and this parser used to discard it, which is the actual leak. Prefer
                # the pattern's own group; split_item_name() stays as the safety net for
                # any pattern that does not have one (a user can edit these live in
                # settings.json during beta, so one without a qty group is expected).
                g_qty = (groups.get("qty") or "").strip()
                if g_qty.isdigit() and int(g_qty) > 0:
                    qty = int(g_qty)
                return LootEvent(
                    item_name=base,
                    npc_name=groups.get("npc", "").strip() or None,
                    raw_line=line,
                    quantity=qty,
                    tier=tier,
                )
        return None
