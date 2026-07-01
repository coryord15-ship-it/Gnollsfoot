"""
JSONL training export — manual trigger only, never automatic.
Exports ONLY verified items + verified quest chains (unverified data poisons training sets).
Format is LayerEight-compatible instruction/response pairs.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Item


DEFAULT_EXPORT_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "GnollGuard", "exports"
)


def export_jsonl(session: Session, export_dir: str = DEFAULT_EXPORT_DIR) -> str:
    """
    Writes verified data to a timestamped .jsonl file.
    Returns the path to the created file.
    """
    Path(export_dir).mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(export_dir, f"gnollloot_training_{timestamp}.jsonl")

    verified_items = (
        session.query(Item)
        .filter(Item.verified.is_(True))
        .order_by(Item.name)
        .all()
    )

    records_written = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for item in verified_items:
            instruction = f"What is the {item.name} in EverQuest Legends?"
            parts = []
            if item.description:
                parts.append(item.description)
            if item.drop_mob and item.drop_zone:
                parts.append(f"It drops from {item.drop_mob} in {item.drop_zone}.")
            elif item.drop_mob:
                parts.append(f"It drops from {item.drop_mob}.")
            if item.drop_time_of_day and item.drop_time_of_day != "unknown":
                parts.append(f"It only drops during {item.drop_time_of_day}time.")
            if item.quest_linked and item.quest_npc and item.quest_reward:
                parts.append(
                    f"It is used in a quest given by {item.quest_npc.name}. "
                    f"Reward: {item.quest_reward}."
                )

            if not parts:
                continue  # skip items with no useful content

            record = {
                "instruction": instruction,
                "response": " ".join(parts),
            }
            f.write(json.dumps(record) + "\n")
            records_written += 1

    return out_path, records_written
