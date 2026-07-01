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
    item_name: str
    npc_name: Optional[str] = None
    raw_line: str = ""


# Looted coin ("1 platinum 4 gold 6 silver", "1 copper", ...) matches the loot
# trigger but is money, not an item — drop it so it never clutters the Items list.
_COIN_RE = re.compile(
    r"^\s*\d[\d,]*\s*(?:platinum|gold|silver|copper)\b"
    r"(?:[\s,]*\d[\d,]*\s*(?:platinum|gold|silver|copper)\b)*\s*$",
    re.IGNORECASE,
)


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
                if not item_name or _COIN_RE.match(item_name):
                    return None  # empty or looted coin — not an item
                return LootEvent(
                    item_name=item_name,
                    npc_name=groups.get("npc", "").strip() or None,
                    raw_line=line,
                )
        return None
