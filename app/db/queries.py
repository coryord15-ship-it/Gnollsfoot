"""
Common DB read/write operations. All writes are designed to be called from a
background thread — callers must NOT call these from the main/UI thread.
"""

import json
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import CorrectionRequest, Item, LootEvent, NPC, NPCDialogue, VendorPrice

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


# ── NPCs ─────────────────────────────────────────────────────────────────────

def get_npc(session: Session, name: str) -> Optional[NPC]:
    return session.query(NPC).filter(NPC.name.ilike(name)).first()


def upsert_npc(session: Session, name: str, **kwargs) -> tuple[NPC, bool]:
    """Returns (npc, is_new)."""
    with _write_lock:
        npc = session.query(NPC).filter(NPC.name.ilike(name)).first()
        is_new = npc is None
        if is_new:
            npc = NPC(name=name, **kwargs)
            session.add(npc)
        else:
            for k, v in kwargs.items():
                setattr(npc, k, v)
            npc.updated_at = datetime.utcnow()
        session.commit()
        session.refresh(npc)
    return npc, is_new


def set_npc_location(session: Session, npc_id: int, x: float, y: float, z: float):
    with _write_lock:
        npc = session.get(NPC, npc_id)
        if npc:
            npc.loc_x, npc.loc_y, npc.loc_z = x, y, z
            # Only mark verified if the /loc actually fired — EQL may not support it
            npc.loc_verified = True
            npc.updated_at = datetime.utcnow()
            session.commit()


# ── NPC Dialogue ─────────────────────────────────────────────────────────────

def add_dialogue(
    session: Session,
    npc_id: int,
    text: str,
    hints: Optional[list[str]] = None,
) -> NPCDialogue:
    with _write_lock:
        last = (
            session.query(NPCDialogue)
            .filter(NPCDialogue.npc_id == npc_id)
            .order_by(NPCDialogue.sequence_order.desc())
            .first()
        )
        next_seq = (last.sequence_order or 0) + 1 if last else 1
        line = NPCDialogue(
            npc_id=npc_id,
            dialogue_text=text,
            sequence_order=next_seq,
            item_hints=json.dumps(hints) if hints else None,
        )
        session.add(line)
        session.commit()
        session.refresh(line)
    return line


def get_all_dialogue(session: Session) -> list:
    """All captured NPC dialogue, flattened for the community sync push."""
    rows = (
        session.query(NPCDialogue, NPC)
        .join(NPC, NPCDialogue.npc_id == NPC.id)
        .order_by(NPC.name, NPCDialogue.sequence_order)
        .all()
    )
    out = []
    for d, npc in rows:
        hints = None
        if d.item_hints:
            try:
                hints = json.loads(d.item_hints)
            except Exception:
                hints = None
        out.append({
            "npc_name": npc.name,
            "zone": npc.zone,
            "dialogue_text": d.dialogue_text,
            "sequence_order": d.sequence_order,
            "item_hints": hints,
        })
    return out


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


def get_loot_history(session: Session, limit: int = 100) -> list[LootEvent]:
    return (
        session.query(LootEvent)
        .order_by(LootEvent.real_timestamp.desc())
        .limit(limit)
        .all()
    )


# ── Vendor Prices ────────────────────────────────────────────────────────────

def log_vendor_price(
    session: Session,
    item_name: str,
    merchant_name: str,
    transaction_type: str,
    price_copper: int,
    price_raw: str,
    quantity: int = 1,
):
    with _write_lock:
        row = VendorPrice(
            item_name=item_name,
            merchant_name=merchant_name,
            transaction_type=transaction_type,
            price_copper=price_copper,
            price_raw=price_raw,
            quantity=quantity,
        )
        session.add(row)
        session.commit()


def get_vendor_price_range(
    session: Session,
    item_name: str,
    transaction_type: str,
) -> Optional[tuple[int, int, int]]:
    """
    Returns (min_copper, max_copper, sample_count) for the given item + direction.
    Returns None if no data exists yet.
    """
    from sqlalchemy import func
    row = (
        session.query(
            func.min(VendorPrice.price_copper),
            func.max(VendorPrice.price_copper),
            func.count(VendorPrice.id),
        )
        .filter(
            VendorPrice.item_name.ilike(item_name),
            VendorPrice.transaction_type == transaction_type,
        )
        .first()
    )
    if row and row[2]:
        return (row[0], row[1], row[2])
    return None


# ── Corrections ──────────────────────────────────────────────────────────────

def submit_correction(
    session: Session,
    target_type: str,
    target_id: int,
    submitted_by: str,
    correction_text: str,
):
    with _write_lock:
        req = CorrectionRequest(
            target_type=target_type,
            target_id=target_id,
            submitted_by=submitted_by,
            correction_text=correction_text,
        )
        session.add(req)
        session.commit()
