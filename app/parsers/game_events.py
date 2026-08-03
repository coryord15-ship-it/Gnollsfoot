"""
Silent background parser for game-log events that feed the databases:
  - Quest turn-ins   ("You offered 1 <item> to <npc>.")   → quest hand-in evidence
  - Tradeskill combines ("You have fashioned…" / "You lacked the skills…")

Nothing here fires alerts — the data feeds the Quest Journal and the item/recipe DBs.
Patterns stay configurable from settings.json.

⚠ THE TURN-IN PATTERN WAS WRONG FROM THE START (fixed 2026-07-30).
It matched "You have given <npc> <item>." — a format EQL does not emit. The real line is
"You offered 1 <item> to <npc>." A census of 955,894 real log lines found 110 hand-ins,
of which this parser had matched ZERO. The old comment even said the format was
"unconfirmed"; it was never checked against a log.

⚠ AN OFFER IS NOT A TURN-IN. The NPC's reply within the next few lines says which:
    rejected : "<NPC> says, 'I have no need for this, <Player>. You can have it back.'"
    partial  : "You must turn in all quest items at once in order to complete this quest."
               → RIGHT NPC, incomplete set. Positive evidence, not a refusal.
    accepted : "You complete the trade with <NPC>." and no refusal line
Without this, one item looks like it belongs to five different NPCs when four refused it
(real example: Master Crushbone Cell Key).

⚠ A REFUSAL IS NOT PROOF THE NPC IS WRONG. The same item/NPC pair can be accepted once
and refused later — quest not started, or already completed. Treat verdicts as weighted
evidence, never as a single-shot fact.

⚠ TRADESKILL COMPONENTS ARE NOT LOGGED. Checked ±6 lines around every combine: only the
result and a skill-up. A combine tells you WHAT a recipe produces, never what went in.
The success line also carries a "something new:" prefix and UPPER-CASES the product,
while the failure line does not — compare case-insensitively or you double-count.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class TurnInEvent:
    """An item offered to an NPC. `verdict` says whether they took it."""
    item_name: str
    npc_name: str
    verdict: str = "unknown"      # accepted | rejected | partial | unknown


@dataclass
class CraftEvent:
    """A tradeskill combine. `success` False means 'you lacked the skills'."""
    item_name: str
    success: bool


class GameEventParser:
    def __init__(self, patterns: dict):
        self._reload(patterns)

    def reload(self, patterns: dict):
        self._reload(patterns)

    def _reload(self, patterns: dict):
        def _c(key, fallback):
            return re.compile(patterns.get(key, fallback), re.IGNORECASE)

        # Verified against real EQ Legends logs 2026-07-30 (110 occurrences).
        self._turn_in = _c(
            "quest_turn_in",
            r"You offered \d+ (?P<item>.+?) to (?P<npc>.+?)\.\s*$",
        )
        # 187 successes / 113 failures in the same census.
        self._craft_ok = _c(
            "tradeskill_success",
            r"You have fashioned the items together to create "
            r"(?:something new:\s*)?(?:an? )?(?P<item>.+?)\.\s*$",
        )
        self._craft_fail = _c(
            "tradeskill_failure",
            r"You lacked the skills to fashion (?:an? )?(?P<item>.+?)\.\s*$",
        )
        # Verdict markers, checked against the lines FOLLOWING an offer.
        self._reject = re.compile(r"says,\s*'I have no need for this,[^']*'", re.I)
        self._partial = re.compile(r"You must turn in all quest items at once", re.I)
        self._traded = re.compile(r"You complete the trade with (?P<npc>.+?)\.\s*$", re.I)

    # ── turn-ins ──────────────────────────────────────────────────────────────
    def parse_turn_in(self, line: str) -> Optional[TurnInEvent]:
        m = self._turn_in.search(line)
        if not m:
            return None
        return TurnInEvent(item_name=m.group("item").strip(),
                           npc_name=m.group("npc").strip())

    def classify_turn_in(self, offer: TurnInEvent, following: list[str]) -> TurnInEvent:
        """Set `verdict` from the lines that came after the offer.

        Caller passes the next few raw lines. Stops early at another offer so two
        back-to-back hand-ins cannot borrow each other's verdict.
        """
        for ln in following:
            if self._turn_in.search(ln):
                break
            if self._reject.search(ln):
                offer.verdict = "rejected"
                return offer
            if self._partial.search(ln):
                offer.verdict = "partial"
                return offer
            t = self._traded.search(ln)
            if t and t.group("npc").strip().lower() == offer.npc_name.lower():
                offer.verdict = "accepted"
                return offer
        return offer

    # ── tradeskills ───────────────────────────────────────────────────────────
    def parse_craft(self, line: str) -> Optional[CraftEvent]:
        m = self._craft_ok.search(line)
        if m:
            return CraftEvent(item_name=m.group("item").strip(), success=True)
        m = self._craft_fail.search(line)
        if m:
            return CraftEvent(item_name=m.group("item").strip(), success=False)
        return None
