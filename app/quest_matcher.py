r"""quest_matcher.py — structured quest-step auto-completion engine (v1).

WHY THIS EXISTS
The Quest Journal's steps used to be raw text the player had to tick off by hand.
This module ticks them off itself, by watching the SAME parsed log events the app
already produces (see log_watcher.py / quest_sightings.py) and matching them
against each journaled quest's typed, structured `triggers`. See
GnollLoot-docs/devtool/QUEST_STEPS_PLAN.md for the full design — the "FINAL
revisions after Grok's sanity check" section is authoritative; this module
implements it.

DEPENDENCY-FREE BY DESIGN: no imports from app/main.py, app/ui/*, or the network.
That lets the hot log-watcher thread, the officer console, and a plain test
harness all import this module without pulling in Tk or Supabase.

WHAT THE LOG ACTUALLY GIVES US (do not assume more — see QUEST_STEPS_PLAN.md):
  - No continuous position stream. `/waypoint` is a navigation aid ONLY, never a
    completion trigger. There is no "reach these coordinates" trigger type.
  - No live inventory snapshot. `turn_in` completion keys on the NPC's
    reward/thank-you line, optionally gated on items previously LOOTED (tracked
    from loot lines) — never on "you gave away an item" (real EQL logs don't even
    emit that line; confirmed against a 14k-line beta log, see
    reference_eql_log_formats memory).
  - Reuse the app's own configured patterns (loot_triggers, zone_line,
    npc_dialogue, the you-say line) — this module receives events already parsed
    by those patterns; it does not invent its own regex over raw log lines.

TRIGGER SHAPE (typed, structured — matched by code; auditors never write regex):
    {"type": "hail", "npc": "Doug"}
    {"type": "player_line", "npc": "Old Doug", "phrase": "supplies"}   # was "say"
    {"type": "npc_line", "npc": "Old Doug", "contains": "old bones"}   # was "npc_dialog"
    {"type": "loot", "item": "Worn Boots", "qty": 1}                   # covers "gather" too
    {"type": "kill", "mob": "a gnoll pup", "qty": 3}
    {"type": "zone_enter", "zone": "the cave"}
    {"type": "turn_in", "npc": "Old Doug", "needs_items": ["Worn Boots"],
     "expected_reward_item": "Iron Key"}
    {"type": "reward", "npc": "Old Doug"}

A step's `triggers` is an ARRAY. `trigger_match` = "any" (default, OR — e.g. "loot
Worn Boots OR buy one from a vendor") or "all" (AND — every trigger in the array
must have independently matched before the step completes).

MATCH PRECEDENCE (most specific first, so a generic hail/dialogue match never
steals a line that a more specific loot/turn-in step should claim): within one
log event, only the highest-precedence group of matching triggers actually
completes steps this event — lower-precedence matches simply wait for another
line. turn_in/loot/reward > player_line > npc_line > hail.

CONVERSATION-SESSION GUARD (replaces a fixed proximity timer — the key upgrade
from Grok's sanity check): a hail, or any NPC line, opens an "active conversation"
with that NPC; it stays open until the player hails someone else or zones. A
`player_line` trigger with an `npc` field is only eligible while that NPC is the
active conversation partner. A generous stale cap (~5 min) is purely a backstop
against a context that never explicitly closes (alt-tab, AFK, a missed zone line).

STATE: local, per character (`progress_<charname>.json`), atomic writes
(temp+rename), a `version` + `last_log_hash` for corruption detection, and an
auto-backup of the previous file kept on every change that actually altered it.
Every step also has a manual override (`mark_done` / `mark_undone`) — logs drop
lines on zone crashes/lag, so this is the safety net that keeps a player unstuck
when the matcher misses something.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time

log = logging.getLogger(__name__)

# TODO(owner): CONFIRM IN-GAME before trusting the Copy /waypoint button — neither
# Grok nor Gemini nor Claude could verify the real `/loc` label or `/waypoint`
# argument order from the log alone. Run `/loc` in-game, compare the printed
# label order to this constant, and flip it if wrong. See QUEST_STEPS_PLAN.md
# "The one blocker to clear before coding".
WAYPOINT_AXIS_ORDER = "x y z"

STATE_VERSION = 1
CONVERSATION_STALE_SECONDS = 300  # ~5 min backstop; the real close is a hail/zone.

# Reused verbatim from quest_sightings.py's reward detector (single source of
# truth for "this line sounds like an NPC giving/thanking, not just talking").
_REWARD = re.compile(
    r"\b(you receive|hands you|gives you|here,? take|your faction (?:standing )?(?:has )?got|"
    r"thank you|my thanks|well done|as promised)\b", re.I)

_HAIL_RE = re.compile(r"^hail,?\s+(.+?)\.?$", re.I)

# Lower number = more specific = wins when multiple steps match the same event.
_PRECEDENCE = {
    "turn_in": 0, "loot": 0, "reward": 0, "kill": 0, "zone_enter": 0,
    "player_line": 1,
    "npc_line": 2,
    "hail": 3,
}


def classify_player_say(text: str) -> tuple[str, str]:
    """Split a "You say, '...'" payload into ("hail", npc) or ("line", text).

    Mirrors quest_sightings.on_player_say's own hail detection so the two
    modules never disagree about what counts as a hail."""
    t = (text or "").strip()
    m = _HAIL_RE.match(t)
    if m:
        return "hail", m.group(1).strip()
    return "line", t


def _step_key(quest_id, step_order) -> str:
    return f"{quest_id}:{step_order}"


def _norm(s) -> str:
    return (s or "").strip().lower()


def waypoint_command(entity: dict | None, axis_order: str = WAYPOINT_AXIS_ORDER) -> str | None:
    """Build a `/waypoint x y z` string from a tagged entity's loc, in whatever
    axis order the owner confirms in-game. None if the entity has no loc yet."""
    if not entity:
        return None
    coords = {"x": entity.get("loc_x"), "y": entity.get("loc_y"), "z": entity.get("loc_z")}
    if any(coords[a] is None for a in ("x", "y", "z")):
        return None
    ordered = [str(coords[a]) for a in axis_order.split()]
    return "/waypoint " + " ".join(ordered)


class ConversationState:
    """Tracks which NPC the player is "in conversation with" right now — the
    guard that lets `player_line` steps tell a real reply from ambient chatter."""

    def __init__(self):
        self.active_npc: str | None = None
        self.last_activity: float = 0.0

    def on_hail(self, npc: str, ts: float) -> None:
        self.active_npc = (npc or "").strip()
        self.last_activity = ts

    def on_npc_line(self, npc: str, ts: float) -> None:
        # Any NPC line (from the active NPC or a new one) opens/refreshes the
        # context for THAT npc — a new speaker switches the active conversation.
        self.active_npc = (npc or "").strip()
        self.last_activity = ts

    def on_zone(self) -> None:
        self.active_npc = None
        self.last_activity = 0.0

    def is_active(self, npc: str, ts: float) -> bool:
        if not self.active_npc:
            return False
        if _norm(self.active_npc) != _norm(npc):
            return False
        return (ts - self.last_activity) <= CONVERSATION_STALE_SECONDS


def _data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "GnollGuard")
    os.makedirs(folder, exist_ok=True)
    return folder


def state_path_for(charname: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", (charname or "unknown"))
    return os.path.join(_data_dir(), f"progress_{safe}.json")


class StepState:
    """Local per-character progress: which steps are done, plus the loot/kill
    counters and partial-trigger bookkeeping matching needs. Atomic writes
    (temp + rename) and an auto-backup of the last-known-good file on every
    real change, so a crash mid-write can never corrupt progress."""

    def __init__(self, path: str):
        self.path = path
        self.version = STATE_VERSION
        self.last_log_hash = ""
        self.completed_steps: set[str] = set()
        self.trigger_hits: set[str] = set()   # f"{quest}:{step}:{idx}"
        self.loot_counts: dict[str, int] = {}
        self.kill_counts: dict[str, int] = {}
        self._dirty = False

    @classmethod
    def load(cls, path: str) -> "StepState":
        st = cls(path)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            st.version = d.get("version", STATE_VERSION)
            st.last_log_hash = d.get("last_log_hash", "")
            st.completed_steps = set(d.get("completed_steps", []))
            st.trigger_hits = set(d.get("trigger_hits", []))
            st.loot_counts = dict(d.get("loot_counts", {}))
            st.kill_counts = dict(d.get("kill_counts", {}))
        except Exception:
            # Missing or corrupt — start clean rather than crash the watcher.
            # A prior backup is left on disk untouched for manual recovery.
            pass
        return st

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        payload = {
            "version": self.version,
            "last_log_hash": self.last_log_hash,
            "completed_steps": sorted(self.completed_steps),
            "trigger_hits": sorted(self.trigger_hits),
            "loot_counts": self.loot_counts,
            "kill_counts": self.kill_counts,
        }
        try:
            if os.path.exists(self.path):
                try:
                    shutil.copyfile(self.path, self.path + ".bak")
                except Exception:
                    pass
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            log.debug("quest step state save failed", exc_info=True)

    def mark_dirty(self) -> None:
        self._dirty = True


class QuestMatcher:
    """Feed it parsed log events; it ticks steps and persists progress.

    `quests` is the shape returned by SupabaseSync.get_journal(): a list of
    quest dicts, each with a "steps" list carrying the new structured columns
    (action_type, triggers, trigger_match, prerequisite_step_orders, items,
    target_entity_id / an embedded "entities" object, ...)."""

    def __init__(self, quests: list[dict], state: StepState):
        self.quests = quests or []
        self.state = state
        self.conversation = ConversationState()

    # ── journal wiring ──────────────────────────────────────────────────────
    def set_quests(self, quests: list[dict]) -> None:
        self.quests = quests or []

    def is_step_done(self, quest_id, step_order) -> bool:
        return _step_key(quest_id, step_order) in self.state.completed_steps

    def progress(self, quest: dict) -> tuple[int, int]:
        steps = quest.get("steps") or []
        done = sum(1 for s in steps if self.is_step_done(quest.get("id"), s.get("step_order")))
        return done, len(steps)

    def mark_done(self, quest_id, step_order) -> None:
        self.state.completed_steps.add(_step_key(quest_id, step_order))
        self.state.mark_dirty()
        self.state.save()

    def mark_undone(self, quest_id, step_order) -> None:
        self.state.completed_steps.discard(_step_key(quest_id, step_order))
        self.state.mark_dirty()
        self.state.save()

    def _known_npcs(self) -> set[str]:
        """NPC names referenced anywhere in an eligible step's triggers. A line
        from anyone else (a bark from the mob you're grinding, a stranger's
        ordinary zone chat that happens to parse as 'dialogue') never opens or
        steals the active conversation — the "scope to the active quest's
        NPCs only" guard from QUEST_STEPS_PLAN.md section 2."""
        names = set()
        for _q, s in self._eligible_steps():
            for t in (s.get("triggers") or []):
                npc = t.get("npc")
                if npc:
                    names.add(_norm(npc))
        return names

    def _eligible_steps(self):
        """(quest, step) pairs that are incomplete and whose prerequisites (if
        any) are already done. Default (no prerequisites) = eligible any order."""
        out = []
        for q in self.quests:
            qid = q.get("id")
            for s in (q.get("steps") or []):
                if self.is_step_done(qid, s.get("step_order")):
                    continue
                prereqs = s.get("prerequisite_step_orders") or []
                if prereqs and not all(
                        self.is_step_done(qid, p) for p in prereqs):
                    continue
                out.append((q, s))
        return out

    # ── the actual matching ──────────────────────────────────────────────────
    def _candidates(self, ttype: str, trig_matches) -> list[tuple[dict, dict, int, int]]:
        """trig_matches(trigger: dict, step: dict, quest: dict) -> bool.
        Returns (quest, step, trigger_index, precedence) for every eligible
        step whose trigger of this type matches."""
        out = []
        for q, s in self._eligible_steps():
            for i, trig in enumerate(s.get("triggers") or []):
                if trig.get("type") != ttype:
                    continue
                if trig_matches(trig, s, q):
                    out.append((q, s, i, _PRECEDENCE.get(ttype, 9)))
        return out

    def _resolve(self, candidates: list[tuple[dict, dict, int, int]]) -> list[dict]:
        """Apply match precedence (keep only the most-specific group this
        event), then apply each step's any/all trigger_match rule. Returns the
        steps that just transitioned to complete."""
        if not candidates:
            return []
        best_rank = min(c[3] for c in candidates)
        newly_done = []
        for q, s, i, rank in candidates:
            if rank != best_rank:
                continue
            qid = q.get("id")
            order = s.get("step_order")
            key = _step_key(qid, order)
            if key in self.state.completed_steps:
                continue
            mode = (s.get("trigger_match") or "any").lower()
            if mode == "all":
                self.state.trigger_hits.add(f"{key}:{i}")
                self.state.mark_dirty()
                total = len(s.get("triggers") or [])
                hit = sum(1 for j in range(total) if f"{key}:{j}" in self.state.trigger_hits)
                if hit < total:
                    continue
            self.state.completed_steps.add(key)
            self.state.mark_dirty()
            newly_done.append({"quest_id": qid, "quest_name": q.get("quest_name"),
                                "step_order": order, "instruction": s.get("instruction")})
        if newly_done:
            self.state.save()
        return newly_done

    # ── event entry points (call these from log_watcher callbacks) ──────────
    def on_hail(self, npc: str, ts: float | None = None) -> list[dict]:
        ts = ts or time.time()
        npc = (npc or "").strip()
        self.conversation.on_hail(npc, ts)
        cands = self._candidates(
            "hail", lambda t, s, q: _norm(t.get("npc")) == _norm(npc))
        return self._resolve(cands)

    def on_player_line(self, text: str, ts: float | None = None) -> list[dict]:
        ts = ts or time.time()
        text = (text or "").strip()
        low = text.lower()

        def _match(t, s, q):
            phrase = _norm(t.get("phrase"))
            if not phrase or phrase not in low:
                return False
            npc = t.get("npc")
            if npc and not self.conversation.is_active(npc, ts):
                return False
            return True

        return self._resolve(self._candidates("player_line", _match))

    def on_npc_line(self, npc: str, text: str, ts: float | None = None) -> list[dict]:
        ts = ts or time.time()
        npc = (npc or "").strip()
        text = (text or "").strip()
        low = text.lower()
        if _norm(npc) in self._known_npcs():
            self.conversation.on_npc_line(npc, ts)
        is_reward_line = bool(_REWARD.search(text))

        def _npc_matches(trig_npc):
            # An explicit npc on the trigger must match the speaker; otherwise
            # fall back to "this is the active conversation partner".
            if trig_npc:
                return _norm(trig_npc) == _norm(npc)
            return self.conversation.is_active(npc, ts)

        def _turn_in(t, s, q):
            if not is_reward_line or not _npc_matches(t.get("npc")):
                return False
            expected = t.get("expected_reward_item")
            if expected and _norm(expected) not in low:
                return False
            for item in (t.get("needs_items") or []):
                if self.state.loot_counts.get(_norm(item), 0) < 1:
                    return False
            return True

        def _reward(t, s, q):
            return is_reward_line and _npc_matches(t.get("npc"))

        def _npc_dialog(t, s, q):
            if not _npc_matches(t.get("npc")):
                return False
            contains = _norm(t.get("contains"))
            return bool(contains) and contains in low

        cands = (self._candidates("turn_in", _turn_in)
                 + self._candidates("reward", _reward)
                 + self._candidates("npc_line", _npc_dialog))
        return self._resolve(cands)

    def on_loot(self, item: str, ts: float | None = None) -> list[dict]:
        ts = ts or time.time()
        item = (item or "").strip()
        key = _norm(item)
        if not key:
            return []
        self.state.loot_counts[key] = self.state.loot_counts.get(key, 0) + 1
        self.state.mark_dirty()
        count = self.state.loot_counts[key]

        def _match(t, s, q):
            if _norm(t.get("item")) != key:
                return False
            return count >= int(t.get("qty", 1) or 1)

        return self._resolve(self._candidates("loot", _match))

    def on_kill(self, mob: str, ts: float | None = None) -> list[dict]:
        ts = ts or time.time()
        mob = (mob or "").strip()
        key = _norm(mob)
        if not key:
            return []
        self.state.kill_counts[key] = self.state.kill_counts.get(key, 0) + 1
        self.state.mark_dirty()
        count = self.state.kill_counts[key]

        def _match(t, s, q):
            if _norm(t.get("mob")) != key:
                return False
            return count >= int(t.get("qty", 1) or 1)

        return self._resolve(self._candidates("kill", _match))

    def on_zone(self, zone: str, ts: float | None = None) -> list[dict]:
        ts = ts or time.time()
        zone = (zone or "").strip()
        low = zone.lower()
        self.conversation.on_zone()

        def _match(t, s, q):
            target = _norm(t.get("zone"))
            return bool(target) and (target in low or low in target)

        return self._resolve(self._candidates("zone_enter", _match))
