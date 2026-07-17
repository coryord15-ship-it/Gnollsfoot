"""
Silent background parser for the game-log event the app still cares about:
  - Quest turn-ins  ("You have given <npc> <item>.")  → ticks quest completion

Nothing here fires alerts on its own — the data feeds the Quest Journal. The pattern
is configurable from settings.json.

(Auto-sold vendor harvesting was removed 2026-07-09 — it was the cut "vendor prices"
scope; silent loot→item-DB contribution lives in the loot parser, not here.)
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class TurnInEvent:
    """You handed a quest item to an NPC (e.g. 'You have given Gnoll X.')."""
    item_name: str
    npc_name: str


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

    def parse_turn_in(self, line: str) -> Optional[TurnInEvent]:
        m = self._turn_in.search(line)
        if m:
            return TurnInEvent(
                item_name=m.group("item").strip(),
                npc_name=m.group("npc").strip(),
            )
        return None
