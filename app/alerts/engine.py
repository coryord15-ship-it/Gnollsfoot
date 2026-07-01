"""
Alert engine: routes events to the correct alert type and color,
then hands off to the alert window.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from app.ui import theme

log = logging.getLogger(__name__)


@dataclass
class Alert:
    title: str
    body: str
    color: str
    badge: str                     # 'Quest Item' | 'Quest Hint' | 'Turned In' ...
    source_url: str = ""
    alert_type: str = "quest"      # 'quest'
    item_name: str = ""
    npc_name: str = ""             # mob that dropped the item, from log parser


class AlertEngine:
    def __init__(self):
        self._listeners: List[Callable[[Alert], None]] = []

    def add_listener(self, fn: Callable[[Alert], None]):
        self._listeners.append(fn)

    def _emit(self, alert: Alert):
        log.info("Alert [%s] %s: %s", alert.badge, alert.title, alert.body[:60])
        for fn in self._listeners:
            try:
                fn(alert)
            except Exception:
                log.exception("Alert listener error")

    # ── Public fire methods ───────────────────────────────────────────────────

    def quest_hint(self, item_name: str, npc_name: str, hint: str, verified: bool):
        color = theme.ALERT_QUEST_VERIFIED if verified else theme.ALERT_QUEST_UNVERIFIED
        badge = "Quest Match" if verified else "Quest Hint"
        self._emit(Alert(
            title=f"Quest hint: {item_name}",
            body=(
                f"{npc_name} mentioned \"{hint}\". "
                f"You looted {item_name} — try turning it in to verify."
            ),
            color=color,
            badge=badge,
            alert_type="quest",
        ))

    def quest_item_obtained(self, item_name: str, quest_name: str, npc_name: str = ""):
        """A looted item matches a required item in one of the player's journaled
        quests. Fires even when generic loot alerts are off — quest items always
        notify — and lets the Quest Journal tick the item off."""
        self._emit(Alert(
            title=f"Quest item: {item_name}",
            body=f"Checked off in your journal for “{quest_name}”.",
            color=theme.ALERT_QUEST_VERIFIED,
            badge="Quest Item",
            alert_type="quest",
            item_name=item_name,
            npc_name=npc_name,
        ))

    def quest_item_turned_in(self, item_name: str, npc_name: str, complete: bool = False):
        """Player handed a quest item to an NPC. Shows 'You have given NPC ITEM'
        as a journal update; if this completed the quest, say so."""
        body = f"You have given {npc_name or 'an NPC'} {item_name}."
        if complete:
            body += "  Quest complete — removed from your journal."
        self._emit(Alert(
            title=f"Turned in: {item_name}",
            body=body,
            color=theme.ALERT_QUEST_VERIFIED,
            badge="Quest Complete" if complete else "Turned In",
            alert_type="quest",
            item_name=item_name,
            npc_name=npc_name,
        ))
