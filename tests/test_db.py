import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base
from app.db.queries import (
    get_item, upsert_item, log_loot_event,
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


def test_loot_event_logged(session):
    from app.db.models import LootEvent
    log_loot_event(session, "Blue Rose", character_name="Coryo")
    log_loot_event(session, "Gnoll Fang")
    history = session.query(LootEvent).all()
    assert len(history) == 2
    names = [h.item_name for h in history]
    assert "Blue Rose" in names
