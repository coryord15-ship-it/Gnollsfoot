#!/usr/bin/env python3
"""Generate build_data/{items,mobs,exaltations}.json for the build.

WHY THIS EXISTS
    `GnollGuard.spec` bundles these three snapshots ONLY IF they are on the build machine, and
    `build_data/` is gitignored because this repo is public. CI builds every release from a
    clean checkout, so it never had them -- meaning **no shipped release has ever contained
    them**, and every new user got "Reference data not found" on the Gear and Codex tabs.

    `datapaths.py` was written expecting the opposite ("the bundled copy keeps Gear and Codex
    working out of the box"), so the two files disagreed and nothing said so. Verified against
    the live 1.5.31 binary on 2026-09-01: items.json, mobs.json and exaltations.json all absent.

WHY IT IS SAFE TO RUN IN CI
    It reads with the PUBLISHABLE key, which is already hardcoded in `app/main.py` and ships
    inside every binary we release. This adds no exposure that the shipped app does not
    already have. 🔴 It must NEVER use the service_role key.

    The snapshots themselves end up inside a public download either way -- anyone can unpack a
    PyInstaller exe -- so bundling them publishes nothing that shipping the app did not.

USAGE
    py -3.11 tools/build_snapshots.py            # writes build_data/*.json
    py -3.11 tools/build_snapshots.py --check    # report only, write nothing
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "build_data")

URL = os.environ.get("SUPABASE_URL", "https://ratezylqpxgruyjscpbu.supabase.co")
KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_P8BT37b8iYnHHisNegOU6w_dqqP3dGB")

PAGE = 1000


def fetch(table: str, select: str, extra: str = "") -> list:
    """Every row of one table, paged. PostgREST caps a response, so never ask once."""
    rows, off = [], 0
    while True:
        url = f"{URL}/rest/v1/{table}?select={select}&limit={PAGE}&offset={off}{extra}"
        req = urllib.request.Request(url, headers={"apikey": KEY,
                                                   "Authorization": "Bearer " + KEY})
        try:
            batch = json.loads(urllib.request.urlopen(req, timeout=90).read().decode())
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "replace")
            raise SystemExit(f"{table}: HTTP {e.code} {body}")
        if not batch:
            break
        rows += batch
        off += PAGE
        if len(batch) < PAGE:
            break
    return rows


def key_of(name: str) -> str:
    """The lookup form the app uses. `extra_views` keys items.json by this exact shape."""
    return "".join(c for c in (name or "").lower() if c.isalnum() or c.isspace()).strip()


def build_items() -> dict:
    rows = fetch("wiki_items", "name,slot,classes,era,effect,flags,url")
    return {key_of(r["name"]): {"name": r.get("name") or "", "slot": r.get("slot") or "",
                                "classes": r.get("classes") or "", "era": r.get("era") or "",
                                "effect": r.get("effect") or "", "flags": r.get("flags") or "",
                                "url": r.get("url") or ""}
            for r in rows if r.get("name")}


def build_mobs() -> dict:
    """One entry per mob: measured rates, confirmed loot, and wiki-only claims.

    🔴 ALL THREE MATTER AND THEY ARE NOT THE SAME THING. A first cut of this built `wiki_only`
    alone and would have shipped a snapshot missing 786 mobs' CONFIRMED loot and 397 mobs'
    measured DROP RATES -- the two fields that make this database better than a wiki scrape,
    and the top of the evidence order for "can a player actually get this item".

    `wiki_only` deliberately excludes anything already confirmed, so the app can show the
    difference between "someone actually looted this" and "a wiki says so".
    """
    out: dict = {}

    def entry(name):
        k = key_of(name or "")
        return out.setdefault(k, {"rates": [], "confirmed": [], "wiki_only": []}) if k else None

    # Measured rates. `publishable` is the flag for a denominator we trust enough to show.
    for r in fetch("drop_rate_stats",
                   "item,mob,zone,corpses_with_item,kills,rate,publishable"):
        e = entry(r.get("mob"))
        if e is None or not r.get("item"):
            continue
        e["rates"].append({"item": r["item"], "rate": r.get("rate"),
                           "kills": r.get("kills"), "hits": r.get("corpses_with_item"),
                           "zone": r.get("zone") or "",
                           "pub": bool(r.get("publishable"))})

    # Confirmed loot -- someone actually looted it off that mob.
    for r in fetch("drop_reports", "item_name,drop_npc"):
        e = entry(r.get("drop_npc"))
        if e is None or not r.get("item_name"):
            continue
        if r["item_name"] not in e["confirmed"]:
            e["confirmed"].append(r["item_name"])

    # Wiki claims, minus anything already confirmed.
    for r in fetch("wiki_npc_loot", "npc_name,item_name"):
        e = entry(r.get("npc_name"))
        if e is None or not r.get("item_name"):
            continue
        it = r["item_name"]
        if it not in e["confirmed"] and it not in e["wiki_only"]:
            e["wiki_only"].append(it)
    return out


def build_exaltations() -> list:
    rows = fetch("wiki_items", "name,slot,classes,effect,item_category,era",
                 "&effect=not.is.null")
    out = []
    for r in rows:
        eff = (r.get("effect") or "").strip()
        if not eff:
            continue
        cat = (r.get("item_category") or "").lower()
        out.append({"name": r.get("name") or "", "slot": r.get("slot") or "",
                    "classes": r.get("classes") or "", "kind": "CLICK",
                    "effect": eff, "cat": cat, "equip": cat in ("weapon", "armor"),
                    "era": r.get("era") or ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    if "service_role" in KEY or KEY.startswith("sb_secret"):
        raise SystemExit("REFUSING: that looks like a service_role key. Publishable only.")

    built = {"items.json": build_items(), "mobs.json": build_mobs(),
             "exaltations.json": build_exaltations()}

    for name, data in built.items():
        print(f"  {name:<20} {len(data):>6} entries")
        # A snapshot that came back nearly empty is worse than none: it would ship as a build
        # that silently claims the database is tiny. Fail the build instead.
        if len(data) < 100:
            raise SystemExit(f"{name} has only {len(data)} entries — refusing to ship that")

    if args.check:
        print("  --check: nothing written")
        return 0

    os.makedirs(OUT, exist_ok=True)
    for name, data in built.items():
        p = os.path.join(OUT, name)
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        print(f"  wrote {p}  {os.path.getsize(p)/1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
