"""
NPC dialogue capture + lightweight item-hint extraction.

This is used only to spot quest-item hints in NPC speech (e.g. an NPC asking for
a "red feather") and fuzzy-match them against recently looted items. No NPC
location/mapping is tracked. The dialogue pattern is configurable in settings.json.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class DialogueEvent:
    npc_name: str
    text: str
    raw_line: str = ""


class NPCParser:
    def __init__(self, patterns: dict):
        self._reload(patterns)

    def _reload(self, patterns: dict):
        self._dialogue = re.compile(patterns.get("npc_dialogue", ""), re.IGNORECASE)

    def reload(self, patterns: dict):
        self._reload(patterns)

    def parse_dialogue(self, line: str) -> Optional[DialogueEvent]:
        m = self._dialogue.search(line)
        if not m:
            return None
        g = m.groupdict()
        return DialogueEvent(
            npc_name=g.get("npc", "").strip(),
            text=g.get("text", "").strip(),
            raw_line=line,
        )


# ── Hint extraction ───────────────────────────────────────────────────────────

_COLOR_WORDS = {
    "red", "blue", "green", "gold", "golden", "silver", "black", "white",
    "purple", "crimson", "azure", "emerald", "amber", "dark", "bright", "glowing",
}
_MATERIAL_WORDS = {
    "iron", "steel", "bone", "cloth", "leather", "wood", "crystal", "gem",
    "stone", "silk", "scales", "feather", "fang", "claw", "flower", "root", "herb",
}


def extract_item_hints(text: str) -> list[str]:
    """
    Lightweight NLP: find color+noun or material+noun phrases in dialogue.
    Used to fuzzy-match NPC speech against known/recently looted items.
    Not a full parser — good enough to catch explicit item hints from quest NPCs.
    """
    words = re.findall(r"\b\w+\b", text.lower())
    hints = []
    for i, word in enumerate(words):
        if word in _COLOR_WORDS or word in _MATERIAL_WORDS:
            if i + 1 < len(words):
                hints.append(f"{word} {words[i + 1]}")
    return list(dict.fromkeys(hints))  # deduplicate, preserve order
