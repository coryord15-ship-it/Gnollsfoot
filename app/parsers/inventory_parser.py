"""
Parse an EQ `/outputfile inventory` dump (<Char>-Inventory.txt).

The file is tab-separated with a header row:
    Location\tName\tID\tCount\tSlots

We pull out (name, id) for real items, skipping the header, empty slots,
and augment-slot placeholders (which have ID 0). De-dupes by name.
"""

import logging

log = logging.getLogger(__name__)


# Equipped-gear slots: the Location is exactly the slot name (augments are
# "<Slot>-SlotN", which we skip here). Ear/Wrist/Fingers each appear twice.
_EQUIP_SLOTS = {
    "Charm", "Ear", "Head", "Face", "Neck", "Shoulders", "Arms", "Back", "Wrist",
    "Range", "Hands", "Primary", "Secondary", "Fingers", "Chest", "Legs", "Feet",
    "Waist", "Ammo", "Power Source",
}
_DUAL_SLOTS = {"Ear", "Wrist", "Fingers"}


def parse_equipment(text: str) -> list[dict]:
    """Return the EQUIPPED loadout as [{"slot": str, "name": str, "id": int}].

    Only bare equip-slot rows (Location == slot name); augment rows ("Ear-Slot1")
    are skipped. The two-of-a-kind slots are indexed: Ear -> Ear1/Ear2, etc."""
    out: list[dict] = []
    dual_count: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        loc, name, raw_id = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if loc not in _EQUIP_SLOTS:
            continue
        if not name or name.lower() == "empty":
            continue
        try:
            item_id = int(raw_id)
        except ValueError:
            continue
        if item_id <= 0:
            continue
        slot = loc
        if loc in _DUAL_SLOTS:
            n = dual_count.get(loc, 0) + 1
            dual_count[loc] = n
            slot = f"{loc}{n}"        # Ear1, Ear2, Wrist1, …
        out.append({"slot": slot, "name": name, "id": item_id})
    return out


def parse_inventory(text: str) -> list[dict]:
    """Return a list of {"name": str, "id": int} from inventory dump text."""
    items: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name = parts[1].strip()
        raw_id = parts[2].strip()
        low = name.lower()
        if not name or low in ("name", "empty"):
            continue
        try:
            item_id = int(raw_id)
        except ValueError:
            continue
        if item_id <= 0 or low in seen:
            continue
        seen.add(low)
        # Count and location were being thrown away. Both matter now that this also reads
        # Currencies.txt: a quest wants "3 wind runes", not "a wind rune", and the location
        # is what tells an exaltation apart from a bag item.
        loc = parts[0].strip()
        try:
            count = int(parts[3].strip()) if len(parts) > 3 else 1
        except ValueError:
            count = 1
        items.append({"name": name, "id": item_id,
                      "count": max(1, count), "location": loc})
    return items


def parse_exaltations(text: str) -> list[dict]:
    """Which exaltation sits in which slot of which equipped item.

    🔴 THIS IS THE FIVE-SLOT STRUCTURE THE CATALOGUE COULD NOT REPRESENT. Currencies.txt
    states it outright, one row per slot:

        Primary-Slot7   Idol of the Underking (Exaltation)   14762  1  10
        Primary-Slot8   SoulFire (Exaltation)                 5504  1  10
        Fingers-Slot7   Djarn's Amethyst Ring (Exaltation)   10366  1  10

    That last one explains a long-standing puzzle: Djarn's Amethyst Ring procced 7,585 times
    while being absent from items.json -- it is not worn, it is SLOTTED.

    ⚠ Slot NUMBERS are not slot MEANINGS. The card shows Ornamentation / Focus / Click /
    Worn / Proc, and which numeric slot maps to which is NOT stated in this file. Record the
    number; do not invent the label.
    """
    out = []
    for line in text.splitlines():
        parts = line.split("	")
        if len(parts) < 3:
            continue
        loc, name = parts[0].strip(), parts[1].strip()
        if "(Exaltation)" not in name or "-Slot" not in loc:
            continue
        base, _, slot = loc.partition("-Slot")
        try:
            item_id = int(parts[2].strip())
        except ValueError:
            continue
        out.append({"equipped_slot": base.strip(),
                    "exalt_slot": slot.strip(),
                    "name": name.replace("(Exaltation)", "").strip(),
                    "id": item_id})
    return out
