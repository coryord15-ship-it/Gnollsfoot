import pytest
from app.parsers.npc_parser import NPCParser, extract_item_hints

PATTERNS = {
    "npc_dialogue": r"(?P<npc>[\w ]+) says(?:, '| ')(?P<text>.+?)'?$",
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


def test_extract_hints():
    hints = extract_item_hints("Bring me a golden flower of crimson hue.")
    assert "golden flower" in hints
    assert "crimson hue" in hints


def test_extract_hints_empty():
    assert extract_item_hints("Hello adventurer!") == []
