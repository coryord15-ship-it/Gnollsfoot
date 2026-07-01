"""
Silent background parsers for NPC encounters and vendor transactions.
Nothing here fires alerts — all data goes straight to the DB.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class NpcTargetEvent:
    npc_name: str


@dataclass
class NpcSlainEvent:
    npc_name: str


@dataclass
class VendorSellEvent:
    """You sold an item TO a vendor."""
    item_name: str
    merchant_name: str
    price_copper: int
    price_raw: str


@dataclass
class VendorBuyEvent:
    """You bought an item FROM a vendor."""
    item_name: str
    merchant_name: str
    quantity: int
    price_copper: int
    price_raw: str


@dataclass
class TurnInEvent:
    """You handed a quest item to an NPC (e.g. 'You have given Gnoll X.')."""
    item_name: str
    npc_name: str


@dataclass
class AutoSoldEvent:
    """EQL auto-sold a looted item to the bag-vendor:
    'You looted a <item> from a <mob>'s corpse and sold it for <price>.'
    A high community count of these = likely vendor trash. 'sold for free'
    (price_copper == 0, sold_for_free) usually means a quest/no-vendor item."""
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


class CombatParser:
    def __init__(self, patterns: dict):
        self._reload(patterns)

    def reload(self, patterns: dict):
        self._reload(patterns)

    def _reload(self, patterns: dict):
        def _c(key, fallback):
            return re.compile(patterns.get(key, fallback), re.IGNORECASE)

        self._npc_target = _c(
            "npc_target",
            r"Targeted \(NPC\): (?P<npc>.+)"
        )
        self._npc_slain = _c(
            "npc_slain",
            r"You have slain (?P<npc>.+?)!"
        )
        self._vendor_sell = _c(
            "vendor_sell",
            r"You receive (?P<price>.+?) from (?P<merchant>.+?) for the (?P<item>.+?)\(s\)\."
        )
        self._vendor_buy = _c(
            "vendor_buy",
            r"You purchased (?P<qty>\d+) (?P<item>.+?) from (?P<merchant>.+?) for\s+(?P<price>.+?)\."
        )
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

    def parse_npc_target(self, line: str) -> Optional[NpcTargetEvent]:
        m = self._npc_target.search(line)
        if m:
            return NpcTargetEvent(npc_name=m.group("npc").strip())
        return None

    def parse_npc_slain(self, line: str) -> Optional[NpcSlainEvent]:
        m = self._npc_slain.search(line)
        if m:
            return NpcSlainEvent(npc_name=m.group("npc").strip())
        return None

    def parse_vendor_sell(self, line: str) -> Optional[VendorSellEvent]:
        m = self._vendor_sell.search(line)
        if m:
            price_raw = m.group("price").strip()
            return VendorSellEvent(
                item_name=m.group("item").strip(),
                merchant_name=m.group("merchant").strip(),
                price_copper=_to_copper(price_raw),
                price_raw=price_raw,
            )
        return None

    def parse_vendor_buy(self, line: str) -> Optional[VendorBuyEvent]:
        m = self._vendor_buy.search(line)
        if m:
            price_raw = m.group("price").strip()
            return VendorBuyEvent(
                item_name=m.group("item").strip(),
                merchant_name=m.group("merchant").strip(),
                quantity=int(m.group("qty")),
                price_copper=_to_copper(price_raw),
                price_raw=price_raw,
            )
        return None

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
