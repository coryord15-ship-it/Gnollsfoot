"""Auto-sold loot parsing, pinned to real EQL beta log lines (Marnos, 2026-06-27/28)."""
from app.parsers.combat_parser import CombatParser, split_tier


def test_split_tier():
    assert split_tier("Crushbone Belt +1") == ("Crushbone Belt", 1)
    assert split_tier("Fiery Avenger +7") == ("Fiery Avenger", 7)
    assert split_tier("Gold Ring") == ("Gold Ring", 0)


def test_parse_auto_sold_real_lines():
    p = CombatParser({})

    e = p.parse_auto_sold(
        "You looted an Undead Froglok Tongue from a wan ghoul knight's corpse "
        "and sold it for 5 silver and 8 copper."
    )
    assert e is not None
    assert e.base_name == "Undead Froglok Tongue"
    assert e.tier == 0
    assert e.price_copper == 58  # 5*10 + 8
    assert e.sold_for_free is False

    # tiered name + named (article-less) mob
    e2 = p.parse_auto_sold(
        "You looted a Crushbone Belt +1 from orc centurion's corpse and sold it for 2 silver and 9 copper."
    )
    assert e2 is not None and e2.base_name == "Crushbone Belt" and e2.tier == 1

    # "sold it for free" => quest / no-vendor item
    e3 = p.parse_auto_sold(
        "You looted a Sealed Note from The Prophet's corpse and sold it for free."
    )
    assert e3 is not None and e3.sold_for_free is True and e3.price_copper == 0

    # quantity prefix
    e4 = p.parse_auto_sold(
        "You looted 2 Spider Silk from a heart spider's corpse and sold it for 2 gold, 8 silver and 6 copper."
    )
    assert e4 is not None and e4.quantity == 2 and e4.base_name == "Spider Silk"

    # the KEPT-loot format (--...--) must NOT be treated as auto-sold
    assert p.parse_auto_sold(
        "--You have looted a Crushbone Belt from orc centurion's corpse.--"
    ) is None
