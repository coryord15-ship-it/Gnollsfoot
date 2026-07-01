#!/usr/bin/env python3
"""
parse_spells.py — parse EQ's spells_us.txt + spells_us_str.txt into clean JSON.

Field positions validated against known spells:
  Complete Heal (id 13)  -> CLR 39, 400 mana, 10000ms cast
  Minor Healing (id 200) -> CLR 1, 10 mana, 1500ms cast
The 16-class min-level block sits at fields 36..51 (254/255 = class can't use it).

Reusable for EQL: same file structure, different (classic-era) data — just point it
at the EQL install folder.

Usage:
  py -3.11 parse_spells.py ["<EQ folder>"] [out.json]
Defaults to the public Daybreak EverQuest install + tools/output/spells.json
"""
import sys, os, io, json

CLASSES = ["WAR", "CLR", "PAL", "RNG", "SHD", "DRU", "MNK", "BRD",
           "ROG", "SHM", "NEC", "WIZ", "MAG", "ENC", "BST", "BER"]

# Field positions come from a swappable format profile (eq_formats.py) so supporting
# EQL is a `--profile eql` swap, not a code change.
try:
    from eq_formats import get_profile
except ImportError:  # allow running the script in isolation
    def get_profile(_name=None):
        return {"name": "live", "spell_fields": {
            "id": 0, "name": 1, "range": 4, "aoe": 5, "cast": 8, "recovery": 9,
            "recast": 10, "dur_formula": 11, "dur_value": 12, "mana": 14,
            "class_base": 36}}


def parse_str(path):
    """spells_us_str.txt -> {id: {cast_on_you, cast_on_other, wear_off}}"""
    msgs = {}
    if not os.path.exists(path):
        return msgs
    with io.open(path, encoding="latin-1") as f:
        for line in f:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("^")
            if len(p) < 6 or not p[0].isdigit():
                continue
            sid, cast_me, cast_other, wear_off = int(p[0]), p[1], p[2], p[5]
            if cast_me or cast_other or wear_off:
                msgs[sid] = {"cast_on_you": cast_me,
                             "cast_on_other": cast_other,
                             "wear_off": wear_off}
    return msgs


def parse_spells(path, msgs, fields, max_level=65):
    cb = fields["class_base"]
    out = []
    with io.open(path, encoding="latin-1") as f:
        for line in f:
            p = line.rstrip("\n").split("^")
            if len(p) < cb + 16 or not p[fields["id"]].isdigit():
                continue

            def i(idx):
                try:
                    return int(p[idx])
                except (ValueError, IndexError):
                    return 0

            classes = {}
            for c in range(16):
                lvl = i(cb + c)
                if 1 <= lvl <= max_level:
                    classes[CLASSES[c]] = lvl

            sid = int(p[fields["id"]])
            rec = {
                "id": sid,
                "name": p[fields["name"]],
                "mana": i(fields["mana"]),
                "cast_time": i(fields["cast"]),
                "recast_time": i(fields["recast"]),
                "range": i(fields["range"]),
                "aoe_range": i(fields["aoe"]),
                "dur_formula": i(fields["dur_formula"]),
                "dur_value": i(fields["dur_value"]),
                "classes": classes,
                "player_usable": bool(classes),
            }
            m = msgs.get(sid)
            if m:
                rec["messages"] = m
            out.append(rec)
    return out


def main():
    args = list(sys.argv[1:])
    profile_name = "live"
    if "--profile" in args:
        idx = args.index("--profile")
        profile_name = args[idx + 1] if idx + 1 < len(args) else "live"
        del args[idx:idx + 2]

    eqdir = args[0] if len(args) > 0 else \
        r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest"
    out_path = args[1] if len(args) > 1 else \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "spells.json")

    prof = get_profile(profile_name)
    fields = prof["spell_fields"]

    max_level = prof.get("max_level", 65)
    msgs = parse_str(os.path.join(eqdir, "spells_us_str.txt"))
    spells = parse_spells(os.path.join(eqdir, "spells_us.txt"), msgs, fields, max_level)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(spells, f, ensure_ascii=False)

    pu = [s for s in spells if s["player_usable"]]
    verified = "" if prof.get("verified", True) else "  (UNVERIFIED - confirm on EQL data)"
    print(f"profile: {prof.get('name')}{verified}")
    print(f"parsed {len(spells)} spells | player-usable {len(pu)} | str messages {len(msgs)}")
    print(f"-> {out_path}  ({os.path.getsize(out_path) // 1024} KB)")
    for sid in (13, 200, 36, 210):
        s = next((x for x in spells if x["id"] == sid), None)
        if s:
            print(f"  [{s['id']}] {s['name']}: mana={s['mana']} cast={s['cast_time']}ms "
                  f"classes={s['classes']}")


if __name__ == "__main__":
    main()
