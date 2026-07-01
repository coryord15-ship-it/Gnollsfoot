#!/usr/bin/env python3
"""
import_spells.py — load tools/output/spells.json into the Supabase `spells` table.

Modes:
  py -3.11 import_spells.py                # bulk upsert via REST (needs env vars below)
  py -3.11 import_spells.py --sql [N]      # print SQL for the first N player-usable spells (by id)
  py -3.11 import_spells.py --all          # include NPC/AA/disc spells too (default: player-usable only)

REST mode env:
  SUPABASE_URL                (default: https://ratezylqpxgruyjscpbu.supabase.co)
  SUPABASE_SERVICE_ROLE_KEY   (required; owner-only — never commit / paste)
"""
import sys, os, json, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON = os.path.join(HERE, "output", "spells.json")
DEFAULT_URL = "https://ratezylqpxgruyjscpbu.supabase.co"

COLS = ["id", "name", "mana", "cast_time", "recast_time", "range", "aoe_range",
        "dur_formula", "dur_value", "classes", "player_usable",
        "cast_on_you", "cast_on_other", "wear_off"]


def load(path, all_spells=False):
    with open(path, encoding="utf-8") as f:
        spells = json.load(f)
    if not all_spells:
        spells = [s for s in spells if s.get("player_usable")]
    rows = []
    for s in spells:
        m = s.get("messages", {})
        rows.append({
            "id": s["id"], "name": s["name"], "mana": s["mana"],
            "cast_time": s["cast_time"], "recast_time": s["recast_time"],
            "range": s["range"], "aoe_range": s["aoe_range"],
            "dur_formula": s["dur_formula"], "dur_value": s["dur_value"],
            "classes": s["classes"], "player_usable": s.get("player_usable", False),
            "cast_on_you": m.get("cast_on_you") or None,
            "cast_on_other": m.get("cast_on_other") or None,
            "wear_off": m.get("wear_off") or None,
        })
    return rows


def _sql(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def emit_sql(rows, n):
    rows = sorted(rows, key=lambda r: r["id"])[:n]
    vals = []
    for r in rows:
        cells = []
        for c in COLS:
            if c == "classes":
                cells.append(_sql(json.dumps(r[c], ensure_ascii=False)) + "::jsonb")
            else:
                cells.append(_sql(r[c]))
        vals.append("(" + ",".join(cells) + ")")
    print(f"insert into public.spells ({','.join(COLS)}) values")
    print(",\n".join(vals))
    print("on conflict (id) do update set " +
          ", ".join(f"{c}=excluded.{c}" for c in COLS if c != "id") + ";")


def rest_upsert(rows, url, key):
    endpoint = url.rstrip("/") + "/rest/v1/spells"
    headers = {"apikey": key, "Authorization": "Bearer " + key,
               "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    B = 500
    for i in range(0, len(rows), B):
        batch = rows[i:i + B]
        req = urllib.request.Request(endpoint, data=json.dumps(batch).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            print(f"  upserted {i + len(batch)}/{len(rows)} (HTTP {resp.status})")


def main():
    args = sys.argv[1:]
    rows = load(DEFAULT_JSON, all_spells="--all" in args)
    if "--sql" in args:
        i = args.index("--sql")
        n = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 50
        emit_sql(rows, n)
        return
    url = os.environ.get("SUPABASE_URL", DEFAULT_URL)
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        sys.exit("Set SUPABASE_SERVICE_ROLE_KEY (owner-only; never commit).")
    print(f"importing {len(rows)} spells -> {url}")
    rest_upsert(rows, url, key)
    print("done.")


if __name__ == "__main__":
    main()
