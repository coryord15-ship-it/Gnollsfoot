"""
Quest progress tracking.

Tracks which *required items* of the player's journaled quests they've actually
looted, so the Quest Journal can tick them off (✓) and a quest alert can fire.

Two pieces, both kept deliberately dependency-free (no imports from main/UI) so
they can be shared by the hot loot path AND the journal renderer without circular
imports:

  * an in-memory index  item-name(lower) → [quest names]  rebuilt whenever the
    journal is fetched, used for a fast O(1) lookup on every loot line, and
  * a persisted set of looted required-item names (lowercased), saved to
    %APPDATA%/GnollGuard/quest_progress.json so progress survives restarts.
"""

import json
import logging
import os

log = logging.getLogger(__name__)


def _data_file(name: str) -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "GnollGuard")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def _progress_file() -> str:
    return _data_file("quest_progress.json")


def _given_file() -> str:
    return _data_file("quest_given.json")


def load_progress() -> set:
    """Looted required-item names (lowercased) from previous sessions."""
    try:
        with open(_progress_file()) as f:
            return {str(x).lower() for x in json.load(f)}
    except Exception:
        return set()


def save_progress(progress: set):
    try:
        with open(_progress_file(), "w") as f:
            json.dump(sorted(progress), f)
    except Exception:
        log.debug("Could not save quest progress", exc_info=True)


def _given_counts_file() -> str:
    return _data_file("quest_given_counts.json")


def load_given_counts() -> dict:
    """How MANY of each required item the player has turned in — {name_lower: int}.

    ⚠ `quest_given` (the set) CANNOT COUNT, and that is a real bug for repeatable quests.
    Once an item name enters it, EVERY step referencing that item shows a green tick
    forever, on every character, for every subsequent run. The owner hit this on
    2026-08-08 running SoulFire four times: after pass one the journal reported the whole
    quest complete and stopped being able to tell him what was left.

    Counts are additive and per-name. Kept in a SEPARATE file so an old client reading
    quest_given.json still works and a downgrade loses the counts, not the ticks.
    Migrated on first load: every name already in the set starts at 1.
    """
    try:
        with open(_given_counts_file()) as f:
            return {str(k).lower(): int(v) for k, v in json.load(f).items()}
    except Exception:
        # First run after the fix — seed from the legacy set so nothing regresses.
        return {name: 1 for name in load_given()}


def save_given_counts(counts: dict):
    try:
        with open(_given_counts_file(), "w") as f:
            json.dump({k: int(v) for k, v in sorted(counts.items())}, f)
    except Exception:
        log.debug("Could not save quest given-counts", exc_info=True)


def record_given(item_name: str, counts: dict, given: set) -> tuple[dict, set]:
    """Record ONE turn-in of `item_name`, keeping both the counter and the legacy set.

    Returns the updated (counts, given) so callers stay explicit about the write.
    """
    low = (item_name or "").strip().lower()
    if not low:
        return counts, given
    counts[low] = counts.get(low, 0) + 1
    given.add(low)
    return counts, given


def load_given() -> set:
    """Required-item names (lowercased) the player has TURNED IN to an NPC.

    ⚠ This is a SET — it records THAT an item was handed over, never HOW MANY TIMES.
    For anything repeatable use load_given_counts(). Kept for the ✔ markers and for
    backward compatibility with clients that predate the counter.
    """
    try:
        with open(_given_file()) as f:
            return {str(x).lower() for x in json.load(f)}
    except Exception:
        return set()


def save_given(given: set):
    try:
        with open(_given_file(), "w") as f:
            json.dump(sorted(given), f)
    except Exception:
        log.debug("Could not save quest turn-ins", exc_info=True)


def required_items(quest) -> set:
    """All required-item names (lowercased) across a quest's steps."""
    out = set()
    for step in (quest.get("steps") or []):
        for item in (step.get("required_items") or []):
            if item:
                out.add(item.lower())
    return out


def is_complete(quest, given: set) -> bool:
    """True if every required item of the quest has been turned in."""
    req = required_items(quest)
    return bool(req) and req.issubset({g.lower() for g in given})


def build_index(quests) -> dict:
    """Map item-name(lower) → [quest names] from the journaled quests' required
    items. `quests` is the list returned by SupabaseSync.get_journal()."""
    idx: dict = {}
    for q in quests or []:
        qn = q.get("quest_name", "Quest")
        for step in (q.get("steps") or []):
            for item in (step.get("required_items") or []):
                if item:
                    idx.setdefault(item.lower(), []).append(qn)
    return idx


def match(index: dict, item_name: str):
    """Quest name if this looted item is a required item in a journaled quest,
    else None."""
    hits = index.get((item_name or "").lower())
    return hits[0] if hits else None
