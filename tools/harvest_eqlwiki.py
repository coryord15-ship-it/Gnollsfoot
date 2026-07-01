#!/usr/bin/env python3
"""
EQL Wiki full harvester — pulls every article from eqlwiki.com via the
MediaWiki API and writes them to eqlwiki_dump.json.

The dump is the raw source of truth; downstream importers (items, quests,
NPCs, tradeskills, etc.) read from it rather than hitting the wiki again.

Output structure:
{
  "meta": { "fetched_at": "...", "base_url": "...", "page_count": N },
  "pages": {
    "Page Title": {
      "pageid": 12345,
      "title": "Page Title",
      "url": "https://eqlwiki.com/index.php/Page_Title",
      "categories": ["Category:Items", ...],
      "wikitext": "{{Itempage|...}}"
    },
    ...
  }
}

Usage:
    py -3.11 tools/harvest_eqlwiki.py
    py -3.11 tools/harvest_eqlwiki.py --resume   # continue a partial dump
    py -3.11 tools/harvest_eqlwiki.py --ns 0 14  # specific namespaces (default: 0)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing: py -3.11 -m pip install requests")

API = "https://eqlwiki.com/api.php"
BASE = "https://eqlwiki.com/index.php/"
OUT_FILE = os.path.join(os.path.dirname(__file__), "..", "eqlwiki_dump.json")
OUT_FILE = os.path.normpath(OUT_FILE)

HEADERS = {"User-Agent": "GnollGuard-wiki-harvester/1.0 (coryord15@gmail.com)"}
BATCH = 50      # pages per API request (max 50 with revisions)
PAUSE = 0.5     # seconds between requests — polite


def fetch_batch(session: requests.Session, params: dict) -> dict:
    for attempt in range(5):
        try:
            r = session.get(API, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            wait = 2 ** attempt
            print(f"  retrying in {wait}s ({e})", flush=True)
            time.sleep(wait)
    sys.exit("Too many failures — aborting.")


def harvest(namespaces: list[int], resume: bool) -> dict:
    # Load existing dump if resuming
    existing: dict = {}
    if resume and os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        existing = data.get("pages", {})
        print(f"Resuming — {len(existing)} pages already in dump.", flush=True)

    session = requests.Session()
    pages: dict = dict(existing)

    for ns in namespaces:
        print(f"\n=== Namespace {ns} ===", flush=True)
        cont: dict | None = {}   # empty dict = start from beginning; None = done

        while cont is not None:
            params = {
                "action": "query",
                "generator": "allpages",
                "gapnamespace": ns,
                "gaplimit": BATCH,
                "prop": "revisions|categories",
                "rvprop": "content",
                "rvslots": "main",
                "cllimit": 50,
                "format": "json",
                "formatversion": "2",
            }
            params.update(cont)

            data = fetch_batch(session, params)

            query_pages = data.get("query", {}).get("pages", [])
            for p in query_pages:
                title = p["title"]

                # Already had it from a PRIOR resumed dump? Skip.
                if resume and title in existing:
                    continue

                # Extract wikitext (may be absent on a clcontinue re-emission)
                wikitext = ""
                revs = p.get("revisions", [])
                if revs:
                    slot = revs[0].get("slots", {}).get("main", {})
                    wikitext = slot.get("content", "")

                # Extract categories (may be partial — arrives across clcontinue batches)
                cats = [c["title"] for c in p.get("categories", [])]

                # MERGE, never clobber. The MediaWiki generator+multiprop continuation
                # re-emits the same page across clcontinue batches WITHOUT revision
                # content, so a naive `pages[title] = {...}` wipes the wikitext we
                # already captured. Merge: keep non-empty wikitext, union categories.
                cur = pages.get(title)
                if cur is None:
                    pages[title] = {
                        "pageid": p["pageid"],
                        "title": title,
                        "url": BASE + title.replace(" ", "_"),
                        "categories": cats,
                        "wikitext": wikitext,
                    }
                else:
                    if wikitext and not cur["wikitext"]:
                        cur["wikitext"] = wikitext
                    seen = set(cur["categories"])
                    for c in cats:
                        if c not in seen:
                            cur["categories"].append(c)
                            seen.add(c)

            fetched = len(query_pages)
            total = len(pages)
            print(f"  fetched {fetched} pages (total so far: {total})", flush=True)

            cont_data = data.get("continue")
            if cont_data:
                cont = cont_data
            else:
                cont = None

            time.sleep(PAUSE)

    return pages


def main():
    parser = argparse.ArgumentParser(description="Harvest eqlwiki.com to JSON.")
    parser.add_argument("--ns", nargs="+", type=int, default=[0],
                        help="Namespaces to fetch (default: 0 = main articles). "
                             "Use 14 for categories.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip pages already in the dump file.")
    args = parser.parse_args()

    print(f"Harvesting eqlwiki.com (namespaces: {args.ns}) -> {OUT_FILE}", flush=True)

    pages = harvest(args.ns, args.resume)

    dump = {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "base_url": "https://eqlwiki.com",
            "namespaces": args.ns,
            "page_count": len(pages),
        },
        "pages": pages,
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(pages)} pages written to {OUT_FILE}")


if __name__ == "__main__":
    main()
