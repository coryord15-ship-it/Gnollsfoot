"""
Common DB read/write operations. All writes are designed to be called from a
background thread — callers must NOT call these from the main/UI thread.
"""

import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Item, LootEvent

_write_lock = threading.Lock()


# ── Items ────────────────────────────────────────────────────────────────────

def get_item(session: Session, name: str, item_level: int = 0) -> Optional[Item]:
    return (
        session.query(Item)
        .filter(Item.name.ilike(name), Item.item_level == item_level)
        .first()
    )


def upsert_item(session: Session, data: dict) -> Item:
    with _write_lock:
        level = data.get("item_level", 0)
        item = (
            session.query(Item)
            .filter(Item.name.ilike(data["name"]), Item.item_level == level)
            .first()
        )
        if item:
            for k, v in data.items():
                if k != "id":
                    setattr(item, k, v)
            item.updated_at = datetime.utcnow()
        else:
            item = Item(**data)
            session.add(item)
        session.commit()
        session.refresh(item)
    return item


def verify_item(session: Session, name: str, item_level: int = 0) -> Optional[Item]:
    """Mark an item as verified. Returns the item, or None if not found."""
    with _write_lock:
        item = (
            session.query(Item)
            .filter(Item.name.ilike(name), Item.item_level == item_level)
            .first()
        )
        if item:
            item.verified = True
            item.updated_at = datetime.utcnow()
            session.commit()
            session.refresh(item)
    return item


def get_items(session: Session, verified_only: bool = False) -> list[Item]:
    q = session.query(Item)
    if verified_only:
        q = q.filter(Item.verified.is_(True))
    return q.order_by(Item.name, Item.item_level).all()

list_items = get_items  # alias used by main_window.py


def delete_item(session: Session, name: str, item_level: int = 0) -> bool:
    """Remove an item from local queue (e.g. it's now in the community DB)."""
    with _write_lock:
        item = (
            session.query(Item)
            .filter(Item.name.ilike(name), Item.item_level == item_level)
            .first()
        )
        if item:
            session.delete(item)
            session.commit()
            return True
    return False


def prune_loot_events(session: Session, keep_hours: int = 24):
    """Keep only recent loot events needed for quest-hint matching."""
    from datetime import timedelta
    with _write_lock:
        cutoff = datetime.utcnow() - timedelta(hours=keep_hours)
        try:
            session.query(LootEvent).filter(LootEvent.real_timestamp < cutoff).delete()
            session.commit()
        except Exception:
            session.rollback()


def purge_coin_items(session: Session) -> int:
    """Remove looted-coin entries ('1 platinum 4 gold ...') that slipped into the
    items table before the coin filter existed."""
    import re
    coin = re.compile(r"^\s*\d[\d,]*\s*(?:platinum|gold|silver|copper)\b", re.IGNORECASE)
    deleted = 0
    with _write_lock:
        for it in session.query(Item).all():
            if it.name and coin.match(it.name):
                session.delete(it)
                deleted += 1
        if deleted:
            session.commit()
    return deleted


# ── Loot Events ──────────────────────────────────────────────────────────────

def log_loot_event(
    session: Session,
    item_name: str,
    character_name: Optional[str] = None,
    zone: Optional[str] = None,
    game_time: Optional[str] = None,
):
    with _write_lock:
        evt = LootEvent(
            item_name=item_name,
            character_name=character_name,
            zone=zone,
            game_time=game_time,
        )
        session.add(evt)
        session.commit()


