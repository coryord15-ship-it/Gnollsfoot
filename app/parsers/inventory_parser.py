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
        items.append({"name": name, "id": item_id})
    return items
