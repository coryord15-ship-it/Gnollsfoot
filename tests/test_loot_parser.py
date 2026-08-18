import pytest
from app.parsers.loot_parser import LootParser

PATTERNS = [
    r"--You have looted a (?P<item>.+?)\.--",
    r"You receive (?P<item>.+?) from (?P<npc>.+?)\.",
]


@pytest.fixture
def parser():
    return LootParser(PATTERNS)


def test_looted_pattern(parser):
    line = "[Mon Jun 16 12:00:00 2026] --You have looted a Gnoll Fang.--"
    result = parser.parse(line)
    assert result is not None
    assert result.item_name == "Gnoll Fang"


def test_receive_pattern_with_npc(parser):
    line = "[Mon Jun 16 12:00:01 2026] You receive Blue Rose from Festering Gnoll."
    result = parser.parse(line)
    assert result is not None
    assert result.item_name == "Blue Rose"
    assert result.npc_name == "Festering Gnoll"


def test_no_match(parser):
    line = "[Mon Jun 16 12:00:02 2026] You say, 'Hello'"
    assert parser.parse(line) is None


def test_empty_line(parser):
    assert parser.parse("") is None


def test_hot_reload(parser):
    parser.reload([r"Obtained: (?P<item>.+)"])
    line = "Obtained: Magic Sword"
    result = parser.parse(line)
    assert result is not None
    assert result.item_name == "Magic Sword"


# ── stack counts, tiers and coin ────────────────────────────────────────────
# Added 2026-08-17 with the fix for "2 Bone Chips" being stored as an item NAME.
# These use the REAL shipped patterns from config/settings.json, not the simplified
# ones above -- the coin bug below only reproduces against the real ones, because they
# capture the leading count into (?P<qty>...) and the simplified patterns do not.

REAL_PATTERNS = [
    r"--You have looted (?:a |an |(?P<qty>\d+) )?(?P<item>.+?) from (?:a |an )?"
    r"(?P<npc>[^']+?)'s corpse\.--",
    r"--You have looted (?:a |an |(?P<qty>\d+) )?(?P<item>.+?)\.--",
]


@pytest.fixture
def real_parser():
    return LootParser(REAL_PATTERNS)


def test_stack_count_is_not_part_of_the_name(real_parser):
    """'2 Bone Chips' is two Bone Chips. Storing it as a name orphans the row."""
    r = real_parser.parse("--You have looted 2 Bone Chips from a gnoll pup's corpse.--")
    assert r is not None
    assert r.item_name == "Bone Chips"
    assert r.quantity == 2


def test_tier_suffix_is_not_part_of_the_name(real_parser):
    """'Bronze Long Sword +4' is a known item at tier 4, not a new item."""
    r = real_parser.parse("--You have looted a Bronze Long Sword +4 from a kobold's corpse.--")
    assert r is not None
    assert r.item_name == "Bronze Long Sword"
    assert r.tier == 4


def test_coin_is_rejected_even_when_the_pattern_eats_the_count(real_parser):
    """REGRESSION: the pattern captures the leading '1 ' into qty, so the coin guard
    is handed 'platinum 4 gold 6 silver' with no leading digit. It used to require
    one, and the app filed currency as an item."""
    assert real_parser.parse("--You have looted 1 platinum 4 gold 6 silver.--") is None
    assert real_parser.parse("--You have looted 7 copper.--") is None


def test_coin_guard_does_not_eat_real_items(real_parser):
    """The guard must not swallow items that merely mention a metal."""
    for line, name in [
        ("--You have looted a Gold Ring from a bandit's corpse.--", "Gold Ring"),
        ("--You have looted 2 Golden Ale from a dwarf's corpse.--", "Golden Ale"),
    ]:
        r = real_parser.parse(line)
        assert r is not None, line
        assert r.item_name == name


def test_defaults_when_there_is_no_count_or_tier(real_parser):
    r = real_parser.parse("--You have looted a Rat Ears from a large field rat's corpse.--")
    assert r is not None
    assert r.item_name == "Rat Ears"
    assert r.quantity == 1
    assert r.tier == 0
