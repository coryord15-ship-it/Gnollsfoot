#!/usr/bin/env python3
"""
Seed the EQL wiki reference data into Supabase (wiki_items, wiki_npcs,
wiki_npc_loot, wiki_merchant_items). Reads eqlwiki_dump.json, parses item
statsblocks + NPC/merchant data, and bulk-upserts via the Supabase REST API.

Item stat parsing reuses the same regex approach as the desktop app's
app/parsers/item_ocr.py (one EQ item-window format, parsed in one place).

Service-role key (NEVER commit / never paste in chat) is read from, in order:
  1. env  SUPABASE_SERVICE_KEY
  2. file tools/.supabase_service_key   (gitignored — paste the key there, one line)

Usage:
    py -3.11 tools/import_eqlwiki.py            # full import
    py -3.11 tools/import_eqlwiki.py --limit 50 # import only N items+npcs (smoke test)
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Missing: py -3.11 -m pip install requests")

# Reuse the NPC/loot/merchant extractors
sys.path.insert(0, os.path.dirname(__file__))
from parse_eqlwiki_npcs import get_field, strip_links, extract_loot, extract_sells  # noqa: E402

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DUMP = os.path.join(ROOT, "eqlwiki_dump.json")
DEFAULT_URL = "https://ratezylqpxgruyjscpbu.supabase.co"
BATCH = 500

NPC_CATS = {"Category:NPCs", "Category:Named Mobs", "Category:Merchants"}
ERA_CATS = ["Classic Era", "Kunark Era", "Velious Era", "Luclin Era", "Planes Era"]

# ── Item statsblock parsing (mirrors app/parsers/item_ocr.py) ─────────────────
_FLAG_TOKENS = ["MAGIC ITEM", "LORE ITEM", "NO DROP", "NODROP", "TEMPORARY",
                "QUEST ITEM", "ATTUNEABLE", "EXPENDABLE", "AUGMENTATION"]
_SLOT_RE = re.compile(r'Slot[:\s]+([A-Z][A-Z ,/]+)', re.IGNORECASE)
_AC_RE   = re.compile(r'\bAC[:\s]+(\d+)', re.IGNORECASE)
_HP_RE   = re.compile(r'\bHP[:\s]+\+?(-?\d+)', re.IGNORECASE)
_MANA_RE = re.compile(r'\bMANA[:\s]+\+?(-?\d+)', re.IGNORECASE)
_END_RE  = re.compile(r'\bEndurance[:\s]+\+?(-?\d+)', re.IGNORECASE)
_STAT_RE = re.compile(r'\b(STR|STA|AGI|DEX|WIS|INT|CHA)[:\s]+\+?(-?\d+)', re.IGNORECASE)
_DMG_RE  = re.compile(r'\bDMG[:\s]+(\d+)', re.IGNORECASE)
_DLY_RE  = re.compile(r'\bDLY[:\s]+(\d+)', re.IGNORECASE)
_WT_RE   = re.compile(r'WT[:\s]+([\d.]+)', re.IGNORECASE)
_SIZE_RE = re.compile(r'Size[:\s]+(\w+)', re.IGNORECASE)
_CLASS_RE = re.compile(r'Class[:\s]+([A-Z0-9 ,/]+)', re.IGNORECASE)
_RACE_RE  = re.compile(r'Race[:\s]+([A-Z0-9 ,/]+)', re.IGNORECASE)
_EFFECT_RE = re.compile(r'Effect[:\s]+(.+)', re.IGNORECASE)


def parse_statsblock(block: str) -> dict:
    """EQ item statsblock text -> {columns..., stats:{...}}. Tolerant of <br> and [[links]]."""
    empty = {"slot": None, "flags": None, "classes": None, "races": None,
             "weight": None, "size": None, "effect": None, "stats": {}}
    if not block:
        return dict(empty)
    txt = re.sub(r'<br\s*/?>', '\n', block, flags=re.IGNORECASE)

    flags = [f for f in _FLAG_TOKENS if re.search(re.escape(f), txt, re.IGNORECASE)]
    stats: dict = {}
    for rx, key in ((_AC_RE, "ac"), (_HP_RE, "hp"), (_MANA_RE, "mana"),
                    (_END_RE, "endurance"), (_DMG_RE, "damage"), (_DLY_RE, "delay")):
        m = rx.search(txt)
        if m:
            stats[key] = int(m.group(1))
    for m in _STAT_RE.finditer(txt):
        stats[m.group(1).lower()] = int(m.group(2))

    def grab(rx):
        m = rx.search(txt)
        return m.group(1).strip() if m else None

    slot = grab(_SLOT_RE)
    size = grab(_SIZE_RE)
    classes = grab(_CLASS_RE)
    races = grab(_RACE_RE)
    wt = grab(_WT_RE)
    effect = _EFFECT_RE.search(txt)
    effect_val = strip_links(effect.group(1).strip()) if effect else None

    return {
        "slot": slot.title() if slot else None,
        "flags": " ".join(flags) or None,
        "classes": classes,
        "races": races,
        "weight": float(wt) if wt else None,
        "size": size.title() if size else None,
        "effect": effect_val,
        "stats": stats,
    }


def era_of(cats: list[str]) -> str | None:
    plain = [c.replace("Category:", "") for c in cats]
    for e in ERA_CATS:
        if e in plain:
            return e.replace(" Era", "")
    return None


def build_rows(limit: int | None):
    with open(DUMP, encoding="utf-8") as f:
        pages = json.load(f)["pages"]

    items, npcs, loot, merch = [], [], [], []
    item_n = npc_n = 0

    for title, page in pages.items():
        cats = set(page.get("categories", []))
        wt = page.get("wikitext", "") or ""

        # Items
        if "Category:Items" in cats and len(wt.strip()) > 30:
            if limit is None or item_n < limit:
                sb = get_field(wt, "statsblock")
                parsed = parse_statsblock(sb or "")
                icon = get_field(wt, "lucy_img_ID")
                recipes = strip_links(get_field(wt, "recipes") or get_field(wt, "playercrafted"))
                items.append({
                    "name": title,
                    "slot": parsed["slot"],
                    "flags": parsed["flags"],
                    "classes": parsed["classes"],
                    "races": parsed["races"],
                    "weight": parsed["weight"],
                    "size": parsed["size"],
                    "effect": parsed["effect"],
                    "icon_id": int(icon) if icon and icon.strip().isdigit() else None,
                    "era": era_of(list(cats)),
                    "stats": parsed["stats"],
                    "recipes": recipes,
                    "raw_statsblock": (sb or "").strip()[:4000] or None,
                    "url": page.get("url"),
                })
                item_n += 1

        # NPCs / mobs / merchants
        if (cats & NPC_CATS) and len(wt.strip()) > 30:
            if limit is None or npc_n < limit:
                zone = strip_links(get_field(wt, "zone"))
                npc_loot = extract_loot(get_field(wt, "known_loot"))
                npc_sells = extract_sells(get_field(wt, "items_sold"))
                npcs.append({
                    "name": title,
                    "zone": zone,
                    "level": strip_links(get_field(wt, "level")),
                    "race": strip_links(get_field(wt, "race")),
                    "class": strip_links(get_field(wt, "class")),
                    "description": (strip_links(get_field(wt, "description")) or "")[:2000] or None,
                    "is_merchant": bool(npc_sells),
                    "url": page.get("url"),
                })
                for it in npc_loot:
                    loot.append({"npc_name": title, "item_name": it["name"], "rarity": it["rarity"]})
                for it in npc_sells:
                    merch.append({"npc_name": title, "item_name": it["name"], "price": it["price"]})
                npc_n += 1

    return items, npcs, loot, merch


# ── Supabase REST upsert ──────────────────────────────────────────────────────
def service_key() -> str:
    k = os.environ.get("SUPABASE_SERVICE_KEY")
    if k:
        return k.strip()
    path = os.path.join(ROOT, "tools", ".supabase_service_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    sys.exit("No service key. Set SUPABASE_SERVICE_KEY or create tools/.supabase_service_key")


def upsert(table: str, rows: list[dict], on_conflict: str, url: str, key: str):
    if not rows:
        print(f"  {table}: nothing to upsert")
        return
    endpoint = f"{url}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    ok = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        for attempt in range(4):
            r = requests.post(endpoint, headers=headers, data=json.dumps(chunk), timeout=60)
            if r.ok:
                ok += len(chunk)
                break
            if attempt == 3:
                print(f"  {table}: FAILED batch {i}-{i+len(chunk)} ({r.status_code}): {r.text[:300]}")
            else:
                time.sleep(2 ** attempt)
        print(f"  {table}: {ok}/{len(rows)}", end="\r", flush=True)
    print(f"  {table}: {ok}/{len(rows)} upserted")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap items+npcs (smoke test)")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", DEFAULT_URL).rstrip("/")
    key = service_key()

    print("Parsing dump...")
    items, npcs, loot, merch = build_rows(args.limit)
    print(f"  items={len(items)} npcs={len(npcs)} loot={len(loot)} merchant={len(merch)}")

    print("Upserting to Supabase...")
    upsert("wiki_items", items, "name", url, key)
    upsert("wiki_npcs", npcs, "name", url, key)
    upsert("wiki_npc_loot", loot, "npc_name,item_name", url, key)
    upsert("wiki_merchant_items", merch, "npc_name,item_name", url, key)
    print("Done.")


if __name__ == "__main__":
    main()
