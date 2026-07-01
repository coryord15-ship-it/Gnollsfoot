"""
SQLAlchemy models. Schema matches spec exactly.
All writes must go through a background thread (never block the log watcher).
"""

import logging
from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, create_engine, event, text
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
import os

log = logging.getLogger(__name__)

DB_PATH = os.path.join(
    os.path.expanduser("~"), "Documents", "GnollGuard", "gnollloot.db"
)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("name", "item_level", name="uq_items_name_level"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    item_level = Column(Integer, nullable=False, default=0)  # 0=base, 1-5=enhancement tiers
    description = Column(Text)
    drop_mob = Column(Text)
    drop_zone = Column(Text)
    drop_time_of_day = Column(Text)        # 'day' | 'night' | 'unknown'
    quest_linked = Column(Boolean, default=False)
    quest_npc_id = Column(Integer, ForeignKey("npcs.id"))
    quest_reward = Column(Text)
    verified = Column(Boolean, default=False)
    source_url = Column(Text)
    submitted_by = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    quest_npc = relationship("NPC", back_populates="quest_items")


class NPC(Base):
    __tablename__ = "npcs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    zone = Column(Text)
    loc_x = Column(Float)
    loc_y = Column(Float)
    loc_z = Column(Float)
    # /loc may not exist in EQL — always check this flag before treating coords as valid
    loc_verified = Column(Boolean, default=False)
    verified = Column(Boolean, default=False)
    source_url = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    dialogue = relationship("NPCDialogue", back_populates="npc", order_by="NPCDialogue.sequence_order")
    quest_items = relationship("Item", back_populates="quest_npc")


class NPCDialogue(Base):
    __tablename__ = "npc_dialogue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    npc_id = Column(Integer, ForeignKey("npcs.id"), nullable=False)
    dialogue_text = Column(Text, nullable=False)
    sequence_order = Column(Integer)       # order preserved for multi-step quest chains
    item_hints = Column(Text)              # JSON array of extracted hint strings
    created_at = Column(DateTime, default=datetime.utcnow)

    npc = relationship("NPC", back_populates="dialogue")


class LootEvent(Base):
    """Local session history — never synced to community db."""
    __tablename__ = "loot_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_name = Column(Text, nullable=False)
    character_name = Column(Text)
    zone = Column(Text)
    game_time = Column(Text)
    real_timestamp = Column(DateTime, default=datetime.utcnow)


class CorrectionRequest(Base):
    __tablename__ = "correction_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_type = Column(Text)             # 'item' | 'npc'
    target_id = Column(Integer)
    submitted_by = Column(Text)
    correction_text = Column(Text)
    status = Column(Text, default="pending")  # 'pending' | 'approved' | 'rejected'
    created_at = Column(DateTime, default=datetime.utcnow)


class VendorPrice(Base):
    """Silent vendor economy data — every buy/sell transaction is logged here."""
    __tablename__ = "vendor_prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_name = Column(Text, nullable=False)
    merchant_name = Column(Text, nullable=False)
    # 'sell' = you sold TO the vendor; 'buy' = you bought FROM the vendor
    transaction_type = Column(Text, nullable=False)
    price_copper = Column(Integer, nullable=False)
    price_raw = Column(Text)
    quantity = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)


def _migrate_schema(engine):
    """Apply column-level migrations that create_all() skips on existing tables."""
    with engine.connect() as conn:
        try:
            cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(items)")).fetchall()
            ]
            if "item_level" not in cols:
                conn.execute(text(
                    "ALTER TABLE items ADD COLUMN item_level INTEGER NOT NULL DEFAULT 0"
                ))
                conn.commit()
                log.info("DB migration: added items.item_level column")
        except Exception as exc:
            log.warning("DB migration failed: %s", exc)


def create_db_engine(db_path: str = DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # check_same_thread=False: the log watcher runs on a background thread and
    # calls get_item(); WAL mode + _write_lock in queries.py keep this safe.
    engine = create_engine(
        f"sqlite:///{db_path}", echo=False,
        connect_args={"check_same_thread": False},
    )
    # WAL mode: allows concurrent reads while a background thread writes
    @event.listens_for(engine, "connect")
    def set_wal(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    _migrate_schema(engine)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
