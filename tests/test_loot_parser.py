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
