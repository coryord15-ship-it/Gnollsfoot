#!/usr/bin/env python3
"""
Parse NPC / merchant data out of eqlwiki_dump.json into web-ready JSON.

Reads the raw wiki dump and, for every NPC / Named Mob / Merchant page, extracts:
  - zone, level, race, class, description
  - loot[]   — items the mob drops (name + optional rarity)
  - sells[]  — items a merchant sells (name + optional price)
  - is_merchant

Outputs (consumed by the website as static reference data):
  web/lib/npc_data.json    { npcName: { zone, level, race, class, description, loot, sells, is_merchant, url } }
  web/lib/zone_npcs.json   { zoneName: [npcName, ...] }      (regenerated — single source of truth)
  web/lib/zone_items.json  { zoneName: [itemName, ...] }     (aggregated from the zone's mobs + merchants)

Run AFTER (re-)harvesting:
    py -3.11 tools/parse_eqlwiki_npcs.py
"""

import json
import os
import re
from collections import defaultdict

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DUMP = os.path.join(ROOT, "eqlwiki_dump.json")
# Parsed reference data lives under tools/ (gitignored) — it's seeded into Supabase,
# not bundled into the website.
LIB = os.path.join(ROOT, "tools", "wikidata")

NPC_CATS = {"Category:NPCs", "Category:Named Mobs", "Category:Merchants"}


def get_field(wikitext: str, field: str) -> str | None:
    """Grab a template field value, from `| field =` up to the next top-level
    `| key =`, the closing `}}`, or end of text. Tolerates multi-line values
    (loot/merchant lists) and nested {{:Item}} / [[links]]."""
    m = re.search(
        r"\|\s*" + re.escape(field) + r"\s*=\s*(.*?)(?=\n\|\s*[a-z_0-9]+\s*=|\n\}\}|\Z)",
        wikitext,
        re.DOTALL | re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def strip_links(val: str | None) -> str | None:
    if not val:
        return None
    val = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", val)
    val = re.sub(r"<[^>]+>", "", val)         # strip stray html
    val = val.strip()
    return val or None


def extract_item_names(val: str | None) -> list[str]:
    """All item names referenced in a field, via {{:Item}} and [[Item]]."""
    if not val:
        return []
    names: list[str] = []
    for m in re.finditer(r"\{\{:\s*([^}|]+?)\s*\}\}", val):
        names.append(m.group(1).strip())
    for m in re.finditer(r"\[\[\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]", val):
        name = m.group(1).strip()
        if ":" in name:      # Category:, File:, Talk:, etc.
            continue
        names.append(name)
    # dedupe, preserve order
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def extract_loot(val: str | None) -> list[dict]:
    """Loot entries with optional rarity (drare span)."""
    if not val:
        return []
    out, seen = [], set()
    # Items with an adjacent rarity span
    for m in re.finditer(
        r"\{\{:\s*([^}|]+?)\s*\}\}\s*(?:<span[^>]*drare[^>]*>\s*\(([^)]+)\))?", val
    ):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "rarity": (m.group(2) or "").strip() or None})
    # Plain [[Item]] bullets (no rarity)
    for name in extract_item_names(val):
        if name not in seen:
            seen.add(name)
            out.append({"name": name, "rarity": None})
    return out


def extract_sells(val: str | None) -> list[dict]:
    """Merchant inventory with optional price (ddp span)."""
    if not val:
        return []
    out, seen = [], set()
    for m in re.finditer(
        r"\{\{:\s*([^}|]+?)\s*\}\}\s*(?:<span[^>]*ddp[^>]*>\s*\(([^)]+)\))?", val
    ):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "price": (m.group(2) or "").strip() or None})
    for name in extract_item_names(val):
        if name not in seen:
            seen.add(name)
            out.append({"name": name, "price": None})
    return out


def main():
    with open(DUMP, encoding="utf-8") as f:
        dump = json.load(f)
    pages = dump["pages"]

    npc_data: dict = {}
    zone_npcs: dict = defaultdict(list)
    zone_items: dict = defaultdict(set)

    no_text = 0
    for title, page in pages.items():
        cats = set(page.get("categories", []))
        if not (cats & NPC_CATS):
            continue

        wt = page.get("wikitext", "") or ""
        if len(wt.strip()) < 30:
            no_text += 1
            continue

        zone = strip_links(get_field(wt, "zone"))
        loot = extract_loot(get_field(wt, "known_loot"))
        sells = extract_sells(get_field(wt, "items_sold"))

        npc_data[title] = {
            "zone": zone,
            "level": strip_links(get_field(wt, "level")),
            "race": strip_links(get_field(wt, "race")),
            "class": strip_links(get_field(wt, "class")),
            "description": strip_links(get_field(wt, "description")),
            "loot": loot,
            "sells": sells,
            "is_merchant": bool(sells),
            "url": page.get("url"),
        }

        if zone:
            zone_npcs[zone].append(title)
            for it in loot:
                zone_items[zone].add(it["name"])
            for it in sells:
                zone_items[zone].add(it["name"])

    # Finalize
    zone_npcs_out = {z: sorted(set(n)) for z, n in zone_npcs.items()}
    zone_items_out = {z: sorted(items) for z, items in zone_items.items()}

    os.makedirs(LIB, exist_ok=True)
    with open(os.path.join(LIB, "npc_data.json"), "w", encoding="utf-8") as f:
        json.dump(npc_data, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(LIB, "zone_npcs.json"), "w", encoding="utf-8") as f:
        json.dump(zone_npcs_out, f, indent=2, ensure_ascii=False)
    with open(os.path.join(LIB, "zone_items.json"), "w", encoding="utf-8") as f:
        json.dump(zone_items_out, f, indent=2, ensure_ascii=False)

    # Stats
    merchants = sum(1 for v in npc_data.values() if v["is_merchant"])
    with_loot = sum(1 for v in npc_data.values() if v["loot"])
    print(f"Parsed NPCs:        {len(npc_data)}")
    print(f"  with loot:        {with_loot}")
    print(f"  merchants:        {merchants}")
    print(f"  skipped (no text):{no_text}")
    print(f"Zones with NPCs:    {len(zone_npcs_out)}")
    print(f"Zones with items:   {len(zone_items_out)}")
    print("Wrote npc_data.json, zone_npcs.json, zone_items.json -> web/lib/")


if __name__ == "__main__":
    main()
