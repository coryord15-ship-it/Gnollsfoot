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
        # ⚠ The fallback used to be "" — an EMPTY REGEX, which matches every line at
        # position 0 with no groups. Any install whose settings.json lacked
        # `npc_dialogue` would therefore treat EVERY log line as dialogue, and because
        # log_watcher._dispatch returns after a dialogue match, everything below that
        # point (turn-ins, crafts) became unreachable. Ship a real default. (2026-07-30)
        self._dialogue = re.compile(
            patterns.get("npc_dialogue")
            or r"(?P<npc>[\w ]+) says(?:, '| ')(?P<text>.+?)'?$",
            re.IGNORECASE)

    def reload(self, patterns: dict):
        self._reload(patterns)

    def parse_dialogue(self, line: str) -> Optional[DialogueEvent]:
        m = self._dialogue.search(line)
        if not m:
            return None
        g = m.groupdict()
        npc = (g.get("npc") or "").strip()
        text = (g.get("text") or "").strip()
        # ⚠ A dying mob emits "Orc legionnaire's corpse says, 'You shall have all the
        # Crushbone orc legions on your tail!'". The name pattern is [\w ]+, which stops
        # at the apostrophe, so it captured the NPC as literally "s corpse" — which became
        # the single most common "NPC" in the whole log (2026-07-30 audit) and inflated the
        # distinct-NPC count to 415. Death taunts are combat flavour, never quest dialogue.
        if not npc or npc.lower().endswith("corpse") or "' corpse says" in line \
                or "'s corpse says" in line:
            return None
        # A bare "s"/"S" left over from any other possessive is garbage, not a name.
        if len(npc) < 2:
            return None
        return DialogueEvent(npc_name=npc, text=text, raw_line=line)


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
