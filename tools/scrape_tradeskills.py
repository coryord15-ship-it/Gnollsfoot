#!/usr/bin/env python3
"""
Tradeskill recipe scraper — EARLY / first pass.

Gathers classic tradeskill recipes (result name, trivial level, container, and
components) from the Project 1999 wiki, writes them to JSON for review, and — if a
Supabase service key is present in the environment — upserts them into the
`tradeskill_recipes` table that the website's /tradeskills page reads from.

Why "early": P99 recipe pages are human-edited and not perfectly uniform, so the
table parser is intentionally best-effort. It pulls what it can and prints a count
of rows it couldn't confidently parse, so you eyeball the JSON before loading. As we
learn each page's quirks we tighten the parser (or add per-skill handlers).

Usage:
    py -3.11 -m pip install requests beautifulsoup4
    py -3.11 tools/scrape_tradeskills.py                 # all known skills -> tradeskills_scraped.json
    py -3.11 tools/scrape_tradeskills.py Baking Brewing   # only these skills

To upsert into Supabase after review, set these env vars first, then re-run:
    SUPABASE_URL=https://<project>.supabase.co
    SUPABASE_SERVICE_KEY=<service_role key>   # never commit this
"""

import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: py -3.11 -m pip install requests beautifulsoup4")

WIKI = "https://wiki.project1999.com/"
OUT_FILE = "tradeskills_scraped.json"

# tradeskill -> wiki page slug. Extend as we add skills/sources.
SKILL_PAGES = {
    "Baking": "Baking",
    "Blacksmithing": "Blacksmithing",
    "Brewing": "Brewing",
    "Fletching": "Fletching",
    "Jewelcraft": "Jewelcraft",
    "Pottery": "Pottery",
    "Tailoring": "Tailoring",
    "Tinkering": "Tinkering",
}

_TRIVIAL_RE = re.compile(r"trivial[^0-9]*(\d{1,3})", re.IGNORECASE)
_COUNT_RE = re.compile(r"^\s*(\d+)\s*[x×]\s*(.+)$", re.IGNORECASE)


def fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": "GnollGuard-tradeskill-scraper/0.1"}, timeout=20)
    resp.raise_for_status()
    return resp.text


def _component(text: str) -> dict:
    """Turn 'Water Flask' or '2x Short Beer' into {name, count}."""
    text = text.strip().strip("·-•").strip()
    m = _COUNT_RE.match(text)
    if m:
        return {"name": m.group(2).strip(), "count": int(m.group(1))}
    return {"name": text, "count": 1}


def parse_recipes(html: str, tradeskill: str, source_url: str) -> tuple[list, int]:
    """Best-effort parse of P99 recipe tables. Returns (recipes, skipped_count).

    P99 recipe tables generally have one row per recipe with cells for the result,
    a trivial number, and the ingredient list. We read each row's cells, treat a
    short numeric cell as the trivial, the first link/bold cell as the result, and
    the remaining item-link cells as components. Rows we can't read are counted and
    skipped (not guessed)."""
    from bs4 import BeautifulSoup  # imported here so --help works without the dep

    soup = BeautifulSoup(html, "html.parser")
    recipes, skipped = [], 0

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]

            # Trivial: a cell that's just a number, or "Trivial: NN" anywhere.
            trivial = None
            for t in texts:
                if t.isdigit() and 1 <= int(t) <= 400:
                    trivial = int(t)
                    break
            if trivial is None:
                m = _TRIVIAL_RE.search(" ".join(texts))
                if m:
                    trivial = int(m.group(1))

            # Result name: first cell that links to an item page and isn't a number.
            name = None
            for c, t in zip(cells, texts):
                if t and not t.isdigit() and c.find("a"):
                    name = t
                    break
            if not name:
                name = next((t for t in texts if t and not t.isdigit()), None)

            # Components: item-link cells after the name, minus the name itself.
            comps = []
            for c, t in zip(cells, texts):
                if not t or t == name or t.isdigit():
                    continue
                if c.find("a"):
                    for piece in re.split(r",|\n", t):
                        piece = piece.strip()
                        if piece:
                            comps.append(_component(piece))

            if name and (comps or trivial is not None):
                recipes.append({
                    "name": name,
                    "tradeskill": tradeskill,
                    "trivial": trivial,
                    "container": None,          # filled in by review; varies by page
                    "components": comps,
                    "source": source_url,
                })
            else:
                skipped += 1

    return recipes, skipped


def upsert_supabase(recipes: list):
    """Optional: push to Supabase if the service key is configured."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("  (no SUPABASE_URL / SUPABASE_SERVICE_KEY — skipping upload)")
        return
    endpoint = f"{url}/rest/v1/tradeskill_recipes?on_conflict=name,tradeskill"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    resp = requests.post(endpoint, headers=headers, data=json.dumps(recipes), timeout=30)
    if resp.ok:
        print(f"  Uploaded {len(recipes)} recipes to Supabase.")
    else:
        print(f"  Upload failed ({resp.status_code}): {resp.text[:200]}")


def main(argv):
    wanted = [a for a in argv if a in SKILL_PAGES] or list(SKILL_PAGES)
    all_recipes = []
    for skill in wanted:
        url = WIKI + SKILL_PAGES[skill]
        print(f"Scraping {skill} <- {url}")
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  fetch failed: {e}")
            continue
        recipes, skipped = parse_recipes(html, skill, url)
        print(f"  parsed {len(recipes)} recipes ({skipped} rows skipped)")
        all_recipes.extend(recipes)
        time.sleep(1)  # be polite to the wiki

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_recipes, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(all_recipes)} recipes -> {OUT_FILE}  (review before loading)")
    upsert_supabase(all_recipes)


if __name__ == "__main__":
    main(sys.argv[1:])
