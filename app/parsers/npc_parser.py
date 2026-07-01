"""
NPC dialogue, /loc, and /who capture.
All patterns are configurable from settings.json.

/loc may not exist in EQL — if the pattern never matches, we fail gracefully
and leave loc_verified=False rather than raising an error.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DialogueEvent:
    npc_name: str
    text: str
    raw_line: str = ""


@dataclass
class LocEvent:
    x: float
    y: float
    z: float
    raw_line: str = ""


@dataclass
class WhoEvent:
    player: str
    guild: Optional[str] = None
    raw_line: str = ""


class NPCParser:
    def __init__(self, patterns: dict):
        self._reload(patterns)

    def _reload(self, patterns: dict):
        self._dialogue = re.compile(patterns.get("npc_dialogue", ""), re.IGNORECASE)
        # /loc capture — optional in EQL; compiled but may never match
        loc_pat = patterns.get("loc_output", "")
        self._loc = re.compile(loc_pat, re.IGNORECASE) if loc_pat else None
        self._who = re.compile(patterns.get("who_output", ""), re.IGNORECASE)

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

    def parse_loc(self, line: str) -> Optional[LocEvent]:
        if not self._loc:
            return None
        m = self._loc.search(line)
        if not m:
            return None
        g = m.groupdict()
        try:
            return LocEvent(x=float(g["x"]), y=float(g["y"]), z=float(g["z"]), raw_line=line)
        except (KeyError, ValueError):
            return None

    def parse_who(self, line: str) -> Optional[WhoEvent]:
        m = self._who.search(line)
        if not m:
            return None
        g = m.groupdict()
        return WhoEvent(
            player=g.get("player", "").strip(),
            guild=g.get("guild", "").strip() or None,
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
