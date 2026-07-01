import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base
from app.db.queries import (
    get_item, upsert_item, upsert_npc, add_dialogue, log_loot_event, get_loot_history,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_upsert_item_create(session):
    item = upsert_item(session, {"name": "Blue Rose", "verified": False})
    assert item.id is not None
    assert item.name == "Blue Rose"


def test_upsert_item_update(session):
    upsert_item(session, {"name": "Blue Rose", "description": "A blue flower."})
    upsert_item(session, {"name": "Blue Rose", "description": "Updated description.", "verified": True})
    found = get_item(session, "Blue Rose")
    assert found.description == "Updated description."
    assert found.verified is True


def test_get_item_case_insensitive(session):
    upsert_item(session, {"name": "Gnoll Fang"})
    assert get_item(session, "gnoll fang") is not None
    assert get_item(session, "GNOLL FANG") is not None


def test_upsert_npc_new_flag(session):
    _, is_new = upsert_npc(session, "Elder Gnoll")
    assert is_new is True
    _, is_new2 = upsert_npc(session, "Elder Gnoll")
    assert is_new2 is False


def test_add_dialogue_sequence(session):
    npc, _ = upsert_npc(session, "Elder Gnoll")
    add_dialogue(session, npc.id, "Bring me a blue flower.")
    add_dialogue(session, npc.id, "Return when you have it.")
    lines = session.query(__import__('app.db.models', fromlist=['NPCDialogue']).NPCDialogue).all()
    assert lines[0].sequence_order == 1
    assert lines[1].sequence_order == 2


def test_loot_event_history(session):
    log_loot_event(session, "Blue Rose", character_name="Coryo")
    log_loot_event(session, "Gnoll Fang")
    history = get_loot_history(session, limit=10)
    assert len(history) == 2
    names = [h.item_name for h in history]
    assert "Blue Rose" in names
