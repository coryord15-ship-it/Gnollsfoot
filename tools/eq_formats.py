"""
EQ format profiles — keep all CLIENT / ERA-specific decode specifics in named profiles
so supporting EQL is a *profile swap*, not a code rewrite.

EQL uses a near-identical client, so the `eql` profile starts as a copy of `live` and must
be RE-VERIFIED against real EQL data (field offsets / struct layout can shift slightly).
Tools take a `--profile live|eql` flag.

SCOPE: only spell-file field positions live here. Any other client-format decode specs are
kept in a separate private owner toolkit, NOT in this public repo. Same swappable-profile
idea, separate home.
"""

# spells_us.txt field positions — validated on LIVE 2026-06-20
# (Complete Heal id13 -> CLR 39 / 400 mana / 10s cast; 16-class level block at 36..51)
_SPELL_FIELDS_LIVE = {
    "id": 0, "name": 1, "range": 4, "aoe": 5, "cast": 8, "recovery": 9,
    "recast": 10, "dur_formula": 11, "dur_value": 12, "mana": 14, "class_base": 36,
}

# max_level = highest class-scribe level we treat as a real player spell (the class
# block uses 254/255 as "can't use" sentinels). `live` keeps the classic-era 65 cap;
# `eql` ships the full retail spells_us.txt (levels to 120+), so we keep everything.
PROFILES = {
    "live": {"name": "live", "verified": True,  "max_level": 65,
             "spell_fields": dict(_SPELL_FIELDS_LIVE)},
    # EQL: VERIFIED 2026-06-27 against the real EverQuest Legends spells_us.txt —
    # identical 173-field layout, class block at 36..51 (confirmed on Minor Healing
    # id 200 -> CLR/PAL/RNG/DRU/SHM/BST L1, 10 mana, 1500ms cast).
    "eql":  {"name": "eql",  "verified": True,  "max_level": 125,
             "spell_fields": dict(_SPELL_FIELDS_LIVE)},
}

DEFAULT = "live"


def get_profile(name: str = DEFAULT) -> dict:
    return PROFILES.get((name or DEFAULT).lower(), PROFILES[DEFAULT])
