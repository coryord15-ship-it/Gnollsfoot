"""
Silent background parsers for the two game-log events the app still cares about:
  - Quest turn-ins  ("You have given <npc> <item>.")  → ticks quest completion
  - Auto-sold loot  ("You looted a <item> from a <mob>'s corpse ...") → item/drop data

Nothing here fires alerts on its own — the data feeds the Quest Journal and the
Item Database. All patterns are configurable from settings.json.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class TurnInEvent:
    """You handed a quest item to an NPC (e.g. 'You have given Gnoll X.')."""
    item_name: str
    npc_name: str


@dataclass
class AutoSoldEvent:
    """EQL auto-sold a looted item to the bag-vendor:
    'You looted a <item> from a <mob>'s corpse and sold it for <price>.'
    'sold for free' (sold_for_free) usually means a quest/no-vendor item, which is
    a useful signal for the Item Database."""
    item_name: str        # full looted name, e.g. "Crushbone Belt +1"
    base_name: str        # tier stripped, e.g. "Crushbone Belt"
    tier: int             # the +N (0 if none)
    quantity: int
    npc_name: str
    price_copper: int
    price_raw: str
    sold_for_free: bool


def split_tier(name: str):
    """'Crushbone Belt +1' -> ('Crushbone Belt', 1); 'Foo' -> ('Foo', 0)."""
    m = re.match(r"^(?P<base>.+?)\s+\+(?P<tier>\d+)\s*$", name or "")
    if m:
        return m.group("base").strip(), int(m.group("tier"))
    return (name or "").strip(), 0


def _to_copper(price_str: str) -> int:
    """Convert '238 platinum 9 silver 5 copper' → total copper value."""
    rates = {"platinum": 1000, "gold": 100, "silver": 10, "copper": 1}
    total = 0
    for m in re.finditer(r'(\d+)\s+(platinum|gold|silver|copper)', price_str, re.IGNORECASE):
        total += int(m.group(1)) * rates[m.group(2).lower()]
    return total


class GameEventParser:
    def __init__(self, patterns: dict):
        self._reload(patterns)

    def reload(self, patterns: dict):
        self._reload(patterns)

    def _reload(self, patterns: dict):
        def _c(key, fallback):
            return re.compile(patterns.get(key, fallback), re.IGNORECASE)

        # Quest turn-in: 'You have given <npc> <item>.' / 'You give your <item> to <npc>.'
        # NOTE: EQL's exact turn-in log format is unconfirmed — this is configurable
        # in settings.json (log_patterns.quest_turn_in) and may need adjusting.
        self._turn_in = _c(
            "quest_turn_in",
            r"You (?:have given|give)(?: your)? (?P<item>.+?) to (?P<npc>.+?)[.!]"
        )
        # Auto-sold loot: 'You looted a <item> from a <mob>'s corpse and sold it
        # for <price>.' (EQL auto-vendors trash on loot). price can be "free".
        self._auto_sold = _c(
            "auto_sold",
            r"You looted (?:a |an |(?P<qty>\d+) )?(?P<item>.+?) from (?:a |an )?(?P<npc>.+?)'s corpse and sold it for (?P<price>.+?)\."
        )

    def parse_turn_in(self, line: str) -> Optional[TurnInEvent]:
        m = self._turn_in.search(line)
        if m:
            return TurnInEvent(
                item_name=m.group("item").strip(),
                npc_name=m.group("npc").strip(),
            )
        return None

    def parse_auto_sold(self, line: str) -> Optional[AutoSoldEvent]:
        m = self._auto_sold.search(line)
        if not m:
            return None
        name = m.group("item").strip()
        base, tier = split_tier(name)
        price_raw = m.group("price").strip()
        free = price_raw.lower() == "free"
        qty = m.groupdict().get("qty")
        return AutoSoldEvent(
            item_name=name,
            base_name=base,
            tier=tier,
            quantity=int(qty) if qty else 1,
            npc_name=m.group("npc").strip(),
            price_copper=0 if free else _to_copper(price_raw),
            price_raw=price_raw,
            sold_for_free=free,
        )
