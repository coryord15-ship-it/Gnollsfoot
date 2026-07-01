import pytest
from app.parsers.npc_parser import NPCParser, extract_item_hints

PATTERNS = {
    "npc_dialogue": r"(?P<npc>[\w ]+) says(?:, '| ')(?P<text>.+?)'?$",
    "loc_output": r"Your Location is (?P<x>-?[\d.]+), (?P<y>-?[\d.]+), (?P<z>-?[\d.]+)",
    "who_output": r"(?P<player>[\w]+)\s+<(?P<guild>.+?)>",
}


@pytest.fixture
def parser():
    return NPCParser(PATTERNS)


def test_dialogue_parse(parser):
    line = "[Mon Jun 16 12:00:00 2026] Elder Gnoll says, 'Bring me a golden flower.'"
    result = parser.parse_dialogue(line)
    assert result is not None
    assert result.npc_name == "Elder Gnoll"
    assert "golden flower" in result.text


def test_loc_parse(parser):
    line = "[Mon Jun 16 12:00:01 2026] Your Location is -123.4, 456.7, 89.0"
    result = parser.parse_loc(line)
    assert result is not None
    assert result.x == pytest.approx(-123.4)
    assert result.y == pytest.approx(456.7)
    assert result.z == pytest.approx(89.0)


def test_loc_graceful_if_no_pattern():
    """When /loc is not supported by EQL, the parser silently returns None."""
    parser = NPCParser({**PATTERNS, "loc_output": ""})
    result = parser.parse_loc("Your Location is 1, 2, 3")
    assert result is None


def test_extract_hints():
    hints = extract_item_hints("Bring me a golden flower of crimson hue.")
    assert "golden flower" in hints
    assert "crimson hue" in hints


def test_extract_hints_empty():
    assert extract_item_hints("Hello adventurer!") == []
