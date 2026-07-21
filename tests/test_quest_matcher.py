"""Tests for app/quest_matcher.py — the v1 test matrix from QUEST_STEPS_PLAN.md:
Doug -> Old Doug -> Dead Doug (multi-NPC) * a kill+loot+turn-in quest * a
single-NPC multi-keyword conversation * all three replayed through a NOISY log
(combat barks + unrelated group chat interleaved) to prove the
conversation-session guard and match precedence hold up.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import quest_matcher as qm


def _quest(qid, name, steps):
    return {"id": qid, "quest_name": name, "steps": steps}


def _step(order, instruction, triggers, trigger_match="any", prereqs=None):
    return {
        "step_order": order,
        "instruction": instruction,
        "triggers": triggers,
        "trigger_match": trigger_match,
        "prerequisite_step_orders": prereqs or [],
    }


def _matcher(tmp_path, quests):
    state = qm.StepState.load(str(tmp_path / "progress_Test.json"))
    return qm.QuestMatcher(quests, state)


# ── 1. Doug -> Old Doug -> Dead Doug (multi-NPC dialogue chain) ────────────

def _doug_quest():
    return _quest("q1", "Doug's Bones", [
        _step(1, "Hail Doug", [{"type": "hail", "npc": "Doug"}]),
        _step(2, "Ask Old Doug about supplies",
              [{"type": "player_line", "npc": "Old Doug", "phrase": "supplies"}]),
        _step(3, "Hear Dead Doug out",
              [{"type": "npc_line", "npc": "Dead Doug", "contains": "old bones"}]),
    ])


def test_multi_npc_doug_chain(tmp_path):
    m = _matcher(tmp_path, [_doug_quest()])

    done = m.on_hail("Doug")
    assert [d["step_order"] for d in done] == [1]

    # Wrong NPC's active conversation must NOT unlock step 2's phrase.
    m.on_npc_line("Some Rando", "Hey there, need something?")
    done = m.on_player_line("I need supplies")
    assert done == []   # active conversation is "Some Rando", not "Old Doug"

    m.on_hail("Old Doug")
    done = m.on_player_line("I need supplies")
    assert [d["step_order"] for d in done] == [2]

    done = m.on_npc_line("Dead Doug", "These are my old bones, rattling in the wind.")
    assert [d["step_order"] for d in done] == [3]

    q = _doug_quest()
    m.set_quests([q])
    done_count, total = m.progress(q)
    # progress() checks the SAME state, but against a freshly-loaded quest dict
    # (ids must line up) — completed steps persisted across the calls above.
    assert total == 3


def test_doug_vs_old_doug_vs_dead_doug_are_never_merged(tmp_path):
    """Same base name, three distinct entities/npcs — a trigger for one must
    never fire off dialogue from another."""
    m = _matcher(tmp_path, [_doug_quest()])
    done = m.on_npc_line("Old Doug", "These are my old bones, rattling in the wind.")
    assert done == []  # step 3 wants Dead Doug specifically
    done = m.on_hail("Old Doug")
    assert done == []  # step 1 wants exactly "Doug", not "Old Doug"


# ── 2. Kill + loot + turn-in quest ──────────────────────────────────────────

def _kill_loot_quest():
    return _quest("q2", "Gnoll Pelts", [
        _step(1, "Kill 3 gnoll pups", [{"type": "kill", "mob": "a gnoll pup", "qty": 3}]),
        _step(2, "Loot 2 pelts", [{"type": "loot", "item": "Gnoll Pelt", "qty": 2}]),
        _step(3, "Turn in the pelts to Old Doug", [{
            "type": "turn_in", "npc": "Old Doug",
            "needs_items": ["Gnoll Pelt"], "expected_reward_item": "Iron Key",
        }]),
    ])


def test_kill_loot_turn_in(tmp_path):
    m = _matcher(tmp_path, [_kill_loot_quest()])

    assert m.on_kill("a gnoll pup") == []
    assert m.on_kill("a gnoll pup") == []
    done = m.on_kill("a gnoll pup")
    assert [d["step_order"] for d in done] == [1]

    assert m.on_loot("Gnoll Pelt") == []
    done = m.on_loot("Gnoll Pelt")
    assert [d["step_order"] for d in done] == [2]

    # Reward line without the required prior loot must NOT complete turn_in —
    # covered separately below; here the loot already happened, so it should.
    done = m.on_npc_line("Old Doug", "Thank you! Here, take this Iron Key.")
    assert [d["step_order"] for d in done] == [3]


def test_turn_in_gated_on_prior_loot(tmp_path):
    m = _matcher(tmp_path, [_kill_loot_quest()])
    # No pelts looted yet — the reward line must not complete the turn-in.
    done = m.on_npc_line("Old Doug", "Thank you! Here, take this Iron Key.")
    assert done == []


def test_turn_in_gated_on_expected_reward_item(tmp_path):
    m = _matcher(tmp_path, [_kill_loot_quest()])
    m.on_loot("Gnoll Pelt")
    m.on_loot("Gnoll Pelt")
    # Thanks from the same NPC but not naming the expected reward — a popular
    # NPC's unrelated "thank you" must not false-positive the turn-in.
    done = m.on_npc_line("Old Doug", "Thank you for stopping by!")
    assert done == []
    done = m.on_npc_line("Old Doug", "Thank you! Here, take this Iron Key.")
    assert [d["step_order"] for d in done] == [3]


# ── 3. Single-NPC multi-keyword conversation ────────────────────────────────

def _multi_keyword_quest():
    return _quest("q3", "A Long Chat", [
        _step(1, "Hail the Sage", [{"type": "hail", "npc": "Sage Elwood"}]),
        _step(2, "Ask about [history]",
              [{"type": "player_line", "npc": "Sage Elwood", "phrase": "history"}]),
        _step(3, "Ask about [prophecy]",
              [{"type": "player_line", "npc": "Sage Elwood", "phrase": "prophecy"}]),
        _step(4, "Ask about [danger]",
              [{"type": "player_line", "npc": "Sage Elwood", "phrase": "danger"}]),
    ])


def test_single_npc_multi_keyword_conversation_stays_open(tmp_path):
    m = _matcher(tmp_path, [_multi_keyword_quest()])
    assert [d["step_order"] for d in m.on_hail("Sage Elwood")] == [1]
    assert [d["step_order"] for d in m.on_player_line("Tell me about the [history]")] == [2]
    # The NPC's own reply lines refresh the conversation without closing it.
    m.on_npc_line("Sage Elwood", "Ah, the history of this land is long indeed...")
    assert [d["step_order"] for d in m.on_player_line("What of the [prophecy]?")] == [3]
    m.on_npc_line("Sage Elwood", "The prophecy speaks of a hero.")
    assert [d["step_order"] for d in m.on_player_line("Is there [danger]?")] == [4]


def test_conversation_closes_on_new_hail(tmp_path):
    m = _matcher(tmp_path, [_multi_keyword_quest()])
    m.on_hail("Sage Elwood")
    m.on_player_line("Tell me about the [history]")
    m.on_hail("Some Other Npc")   # hailing someone else closes the Sage conversation
    done = m.on_player_line("What of the [prophecy]?")
    assert done == []


def test_conversation_closes_on_zone(tmp_path):
    m = _matcher(tmp_path, [_multi_keyword_quest()])
    m.on_hail("Sage Elwood")
    m.on_player_line("Tell me about the [history]")
    m.on_zone("The Steppes")
    done = m.on_player_line("What of the [prophecy]?")
    assert done == []


# ── 4. All three quests replayed through a NOISY log ────────────────────────

NOISY_LINES = [
    ("npc_line", "a gnoll guard", "You will pay for this!"),
    ("player_line", "help"),
    ("npc_line", "a gnoll pup", "Grrrr!"),
    ("player_line", "yes"),
    ("player_line", "more"),
]


def test_noisy_log_does_not_false_positive_or_break_real_matches(tmp_path):
    quests = [_doug_quest(), _kill_loot_quest(), _multi_keyword_quest()]
    m = _matcher(tmp_path, quests)

    # Ambient noise before anything real happens — nothing should tick.
    for kind, *args in NOISY_LINES:
        getattr(m, f"on_{kind}")(*args)

    all_done = []
    all_done += m.on_hail("Doug")
    for kind, *args in NOISY_LINES:
        getattr(m, f"on_{kind}")(*args)
    m.on_hail("Old Doug")
    all_done += m.on_player_line("I need supplies")
    for kind, *args in NOISY_LINES:
        getattr(m, f"on_{kind}")(*args)
    all_done += m.on_npc_line("Dead Doug", "These are my old bones, rattling in the wind.")

    for kind, *args in NOISY_LINES:
        getattr(m, f"on_{kind}")(*args)
    for _ in range(3):
        all_done += m.on_kill("a gnoll pup")
    for kind, *args in NOISY_LINES:
        getattr(m, f"on_{kind}")(*args)
    for _ in range(2):
        all_done += m.on_loot("Gnoll Pelt")
    all_done += m.on_npc_line("Old Doug", "Thank you! Here, take this Iron Key.")

    for kind, *args in NOISY_LINES:
        getattr(m, f"on_{kind}")(*args)
    all_done += m.on_hail("Sage Elwood")
    all_done += m.on_player_line("Tell me about the [history]")
    for kind, *args in NOISY_LINES:
        getattr(m, f"on_{kind}")(*args)
    all_done += m.on_player_line("What of the [prophecy]?")
    all_done += m.on_player_line("Is there [danger]?")

    got = sorted((d["quest_id"], d["step_order"]) for d in all_done)
    expected = sorted(
        [("q1", 1), ("q1", 2), ("q1", 3)]
        + [("q2", 1), ("q2", 2), ("q2", 3)]
        + [("q3", 1), ("q3", 2), ("q3", 3), ("q3", 4)]
    )
    assert got == expected


# ── precedence: a specific loot/turn-in trigger beats a generic npc_line one ─

def test_precedence_turn_in_beats_generic_npc_line(tmp_path):
    """A step generically listening for 'thank' on the same NPC must not steal
    completion credit that a more specific turn_in step should get for the
    exact same line — and vice versa, the turn_in should still fire."""
    quests = [_quest("q4", "Precedence Check", [
        _step(1, "Hear Old Doug talk", [{"type": "npc_line", "npc": "Old Doug", "contains": "thank"}]),
        _step(2, "Turn in to Old Doug", [{
            "type": "turn_in", "npc": "Old Doug", "needs_items": [], "expected_reward_item": None,
        }]),
    ])]
    m = _matcher(tmp_path, quests)
    done = m.on_npc_line("Old Doug", "Thank you! Here, take this.")
    got = sorted(d["step_order"] for d in done)
    # Only the more specific turn_in (precedence rank 0) claims this event —
    # the generic npc_line (rank 2) does not fire on the same line.
    assert got == [2]
    # But it remains eligible and fires on a later, non-reward line.
    done2 = m.on_npc_line("Old Doug", "I thank the gods for adventurers like you.")
    assert [d["step_order"] for d in done2] == [1]


# ── manual override ─────────────────────────────────────────────────────────

def test_manual_mark_done_and_undone(tmp_path):
    m = _matcher(tmp_path, [_doug_quest()])
    assert not m.is_step_done("q1", 1)
    m.mark_done("q1", 1)
    assert m.is_step_done("q1", 1)
    m.mark_undone("q1", 1)
    assert not m.is_step_done("q1", 1)


# ── state persistence (atomic write, backup, version) ───────────────────────

def test_state_persists_across_reload(tmp_path):
    path = str(tmp_path / "progress_Test.json")
    state = qm.StepState.load(path)
    m = qm.QuestMatcher([_doug_quest()], state)
    m.on_hail("Doug")
    assert os.path.exists(path)

    reloaded = qm.StepState.load(path)
    assert reloaded.version == qm.STATE_VERSION
    m2 = qm.QuestMatcher([_doug_quest()], reloaded)
    assert m2.is_step_done("q1", 1)


def test_state_backup_written_on_second_save(tmp_path):
    path = str(tmp_path / "progress_Test.json")
    state = qm.StepState.load(path)
    m = qm.QuestMatcher([_doug_quest()], state)
    m.mark_done("q1", 1)   # first save() — creates the file, no prior version to back up
    m.mark_done("q1", 2)   # second save() — must back up the version from the first
    assert os.path.exists(path + ".bak")


# ── prerequisites (out-of-order eligibility) ────────────────────────────────

def test_prerequisite_step_orders_gate_eligibility(tmp_path):
    quest = _quest("q5", "Ordered", [
        _step(1, "First", [{"type": "hail", "npc": "A"}]),
        _step(2, "Second", [{"type": "hail", "npc": "B"}], prereqs=[1]),
    ])
    m = _matcher(tmp_path, [quest])
    # Step 2's prereq (step 1) isn't done yet, so hailing B must not complete it.
    assert m.on_hail("B") == []
    assert m.on_hail("A") == [{"quest_id": "q5", "quest_name": "Ordered",
                                "step_order": 1, "instruction": "First"}]
    assert [d["step_order"] for d in m.on_hail("B")] == [2]


# ── waypoint helper ──────────────────────────────────────────────────────────

def test_waypoint_command_none_without_loc():
    assert qm.waypoint_command(None) is None
    assert qm.waypoint_command({"loc_x": None, "loc_y": 1, "loc_z": 2}) is None


def test_waypoint_command_uses_axis_order():
    entity = {"loc_x": 1, "loc_y": 2, "loc_z": 3}
    assert qm.waypoint_command(entity, "x y z") == "/waypoint 1 2 3"
    assert qm.waypoint_command(entity, "y x z") == "/waypoint 2 1 3"


def test_classify_player_say():
    assert qm.classify_player_say("Hail, Doug") == ("hail", "Doug")
    assert qm.classify_player_say("Hail, Old Doug.") == ("hail", "Old Doug")
    assert qm.classify_player_say("I need supplies") == ("line", "I need supplies")
