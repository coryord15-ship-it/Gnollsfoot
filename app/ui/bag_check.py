"""Bag Check — which quests can you hand in RIGHT NOW, from what is in your bags.

THE PROBLEM IT SOLVES
    Plane of Sky is a key chain -- one mob per island drops the key to the next -- and every
    class test there wants a Wind Rune plus two or three drops. People end up hauling a bag of
    runes and fangs with no idea which of them completes anything. The quest data already knows
    every turn-in (`quest_steps.required_items`), and the app already reads
    `<Char>-Inventory.txt`. Nothing had ever joined the two.

HOW IT WORKS
    Type `/outputfile inventory` in game, press Check My Bags, and every quest whose turn-in
    items you are carrying is listed:

        READY    -- you have all of them, shown in green
        PARTIAL  -- you have some, and the missing ones are named

    It reads the newest `*-Inventory.txt` off your own disk. No packets, no hooking, nothing
    leaves the machine -- the game's own dump file, same as the rest of the journal.

🔴 MATCHING IS FOLDED, because the two sides spell things differently
    Your inventory writes what you are carrying; the quest data writes what the NPC asks for.
    Both are lowercased, with the `+N` tier suffix and any leading stack quantity stripped --
    otherwise a "Wind Rune Beza +1" in your bag never matches the "Wind Rune Beza" a quest
    wants, and "2 Bone Chips" never matches "Bone Chips". Old app builds emitted that quantity
    prefix for weeks, so folding it is not optional.

⚠ ONLY steps that actually list items count. A quest whose steps we have not verified
   contributes nothing rather than a guess: an invented turn-in sends someone across Norrath
   for nothing, which is worse than an honest gap. A quest with no recorded requirements is
   never shown as "ready" -- absence of data is not completion.
"""

import logging
import os
import re
import threading

import customtkinter as ctk

from app.parsers.inventory_parser import parse_inventory
from app.ui import extra_views as EV
from app.ui import theme

log = logging.getLogger(__name__)

card, lab, wrap = EV.card, EV.lab, EV.wrap
GOLD, T1, T2, T3 = EV.GOLD, EV.T1, EV.T2, EV.T3
F_SMALL, F_BODY = EV.F_SMALL, EV.F_BODY

#: green for a completed set. Distinct from GOLD, which already means "yours".
READY = "#4CAF50"

#: Wiki class names -> the abbreviations the class pickers use. Same map as spell_quests.
CLASS_ABBR = {
    "Warrior": "WAR", "Cleric": "CLR", "Paladin": "PAL", "Ranger": "RNG",
    "Shadowknight": "SHD", "Shadow Knight": "SHD", "Druid": "DRU", "Monk": "MNK",
    "Bard": "BRD", "Rogue": "ROG", "Shaman": "SHM", "Necromancer": "NEC",
    "Wizard": "WIZ", "Magician": "MAG", "Enchanter": "ENC", "Beastlord": "BST",
    "Berserker": "BER",
}

_QTY = re.compile(r"^\s*\d+\s+")
_TIER = re.compile(r"\s*\+\d+\s*$")


def fold(name: str) -> str:
    """Inventory and quest data spell the same item differently. Fold both the same way."""
    return _TIER.sub("", _QTY.sub("", (name or "").strip())).strip().lower()


def carried() -> tuple:
    """-> (folded item names, the file they came from, how many items were read)."""
    try:
        from app import log_discovery
        files = log_discovery.inventory_files()
    except Exception:
        log.exception("inventory discovery failed")
        return set(), None, 0
    if not files:
        return set(), None, 0
    path = files[0]
    try:
        with open(path, "r", encoding="latin-1", errors="replace") as fh:
            items = parse_inventory(fh.read())
    except OSError:
        log.exception("could not read %s", path)
        return set(), path, 0
    return {fold(i["name"]) for i in items if i.get("name")}, path, len(items)


def fetch_quests(app) -> list:
    """Quests with their required items attached. Blocking -- never call on the UI thread."""
    try:
        sup = getattr(app, "supabase", None)
        client = getattr(sup, "_client", None) if sup else None
        if client is None:
            return []
        qs = client.table("quests").select(
            "id,quest_name,zone,quest_giver_npc,char_class,min_level").execute()
        quests = {q["id"]: q for q in (qs.data or [])}
        st = client.table("quest_steps").select(
            "quest_id,step_order,required_items,deliver_to_npc").execute()
        for s in (st.data or []):
            q = quests.get(s.get("quest_id"))
            if q is not None:
                q.setdefault("steps", []).append(s)
        return list(quests.values())
    except Exception:
        log.exception("bag check quest fetch failed")
        return []


def evaluate(quests, bag, my_classes=None) -> list:
    """-> [{quest, have, missing, ready, npc, mine}] for every quest we hold ANY item for.

    🔴 DO NOT GATE "READY" ON CLASS. I did, and it was wrong: the owner, 2026-09-04 --
    *"i didnt ask you to filter it by class... the sky quests are also how we unlock other
    classes as primary classes."* Legends characters equip up to THREE classes, and the Plane
    of Sky class tests are the mechanism that flags a new one. So a Bard test is not useless to
    a PAL/MNK/ENC character -- completing it is how Bard becomes available. Filtering on the
    classes you already play hides precisely the quests worth doing.

    The class is still shown, because it tells you what the test unlocks. It is not a filter,
    and it never downgrades a completed set."""
    mine = {c for c in (my_classes if my_classes is not None else EV.MY_CLASSES) if c}
    out = []
    for q in quests:
        need, npc = [], None
        for s in (q.get("steps") or []):
            for it in (s.get("required_items") or []):
                nm = it if isinstance(it, str) else (it or {}).get("item_name")
                if nm:
                    need.append(nm)
                    npc = npc or s.get("deliver_to_npc")
        if not need:
            continue
        folded = {}
        for n in need:
            folded.setdefault(fold(n), n)
        have = sorted(o for f, o in folded.items() if f in bag)
        if not have:
            continue
        missing = sorted(o for f, o in folded.items() if f not in bag)
        cls = (q.get("char_class") or "").strip()
        # `mine` is INFORMATION ONLY -- used to sort your current classes first, never to
        # decide readiness. A test for a class you do not play yet is how you unlock it.
        is_mine = (not cls) or CLASS_ABBR.get(cls, cls.upper()[:3]) in mine
        out.append({"quest": q, "have": have, "missing": missing,
                    "ready": not missing, "mine": is_mine, "npc": npc})
    out.sort(key=lambda r: (not r["ready"], len(r["missing"]), not r["mine"],
                            (r["quest"].get("quest_name") or "").lower()))
    return out


def add_to_journal(app, rows) -> int:
    """Put every matched quest into the journal. -> how many were newly added.

    Only quests we actually hold an item for, which `evaluate` has already filtered to. Uses
    the existing `add_quest` upsert, so re-pressing the button is harmless -- an upsert on a
    quest already tracked changes nothing and is not counted.
    """
    sup = getattr(app, "supabase", None)
    if sup is None or not getattr(getattr(app, "auth", None), "is_logged_in", False):
        return 0                      # not signed in: nothing to add to
    try:
        # get_journal() returns the quest rows themselves, so the id is `id`, not `quest_id`.
        have = {q.get("id") for q in (sup.get_journal() or [])}
    except Exception:
        log.exception("could not read the journal; adding without a duplicate check")
        have = set()
    added = 0
    for r in rows:
        qid = (r.get("quest") or {}).get("id")
        if qid is None or qid in have:
            continue
        try:
            if sup.add_quest(qid):
                added += 1
        except Exception:
            log.exception("could not add quest %s to the journal", qid)
    return added


# ── UI ────────────────────────────────────────────────────────────────────────────────────

def render(body, rows, path, n_items, added=0) -> None:
    """Draw the results. Separate from the fetch so it can be tested without threading."""
    for w in body.winfo_children():
        w.destroy()

    if path is None:
        lab(body, "No inventory file found yet.", F_BODY, T3).pack(padx=12, pady=(14, 2))
        wrap(body, "Put what you want checked into your bags, then type  /outputfile inventory "
                   "in game and press Check My Bags again.", T3).pack(fill="x", padx=12,
                                                                      pady=(0, 14))
        return

    ready = [r for r in rows if r["ready"]]
    head = card(body)
    head.pack(fill="x", pady=(0, 6))
    lab(head, "%d READY TO TURN IN" % len(ready), F_SMALL,
        READY if ready else T3).pack(fill="x", padx=12, pady=(10, 2))
    wrap(head, "%d partly done · %d items read from %s"
               % (len(rows) - len(ready), n_items, os.path.basename(path)),
         T3).pack(fill="x", padx=12, pady=(0, 2))
    lab(head, ("added %d new quest%s to your journal" % (added, "" if added == 1 else "s"))
              if added else "nothing new to add — these are already in your journal",
        F_SMALL, GOLD if added else T3).pack(fill="x", padx=12, pady=(0, 10))

    if not rows:
        wrap(body, "Nothing in your bags matches a quest we have turn-in items recorded for. "
                   "That can simply mean the steps are not verified yet — we only match "
                   "against steps we have actually confirmed.", T3).pack(fill="x", padx=12,
                                                                         pady=14)
        return

    for r in rows:
        q = r["quest"]
        box = card(body)
        box.pack(fill="x", pady=3)

        top = ctk.CTkFrame(box, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(9, 1))
        lab(top, q.get("quest_name") or "?", F_BODY, READY if r["ready"] else T1).pack(
            side="left")
        if r["ready"]:
            tag, col = "READY", READY
        else:
            tag, col = "%d missing" % len(r["missing"]), T3
        lab(top, tag, F_SMALL, col).pack(side="right")

        bits = []
        if q.get("zone"):
            bits.append(q["zone"])
        who = r.get("npc") or q.get("quest_giver_npc")
        if who:
            bits.append("turn in to %s" % who)
        if q.get("char_class"):
            # Sky class tests are the multiclass unlock, so name what it opens up rather
            # than treating the class as a restriction.
            bits.append(q["char_class"] if r.get("mine", True)
                        else "unlocks %s" % q["char_class"])
        if bits:
            lab(box, " · ".join(bits), F_SMALL, T3).pack(fill="x", padx=12)

        wrap(box, "have: " + ", ".join(r["have"]), READY).pack(fill="x", padx=12, pady=(4, 0))
        if r["missing"]:
            wrap(box, "need: " + ", ".join(r["missing"]), T2).pack(fill="x", padx=12,
                                                                   pady=(0, 10))
        else:
            lab(box, "", F_SMALL, T3).pack(pady=(0, 6))


def build(parent, app) -> None:
    """Bag Check sub-tab. Reference data plus your own dump file -- no login needed."""
    for w in parent.winfo_children():
        w.destroy()

    head = card(parent)
    head.pack(fill="x", pady=(0, 6))
    lab(head, "BAG CHECK", F_SMALL, GOLD).pack(fill="x", padx=12, pady=(10, 2))
    wrap(head, "Load your bags with anything you might be carrying for a quest, then type  "
               "/outputfile inventory  in game and press the button. Every quest you hold "
               "turn-in items for is ADDED TO YOUR JOURNAL, and the ones you can finish right "
               "now are green. Works for any quest we have turn-in items recorded for, not "
               "just Plane of Sky. The file is read off your own disk — nothing is sent.",
         T2).pack(fill="x", padx=12, pady=(0, 8))

    body = ctk.CTkFrame(parent, fg_color="transparent")

    def run():
        btn.configure(state="disabled", text="Reading…")
        for w in body.winfo_children():
            w.destroy()
        lab(body, "Reading your inventory…", F_BODY, T3).pack(padx=12, pady=14)

        def work():
            added = 0
            try:
                bag, path, n = carried()
                rows = evaluate(fetch_quests(app), bag) if bag else []
                # 🔴 THE POINT OF THE BUTTON. Owner, 2026-09-04: *"all i asked you to do was
                # look at the items find the quest the items belong to add them to journal"*.
                # The first cut only DISPLAYED matches, which is not what was asked for.
                added = add_to_journal(app, rows)
            except Exception:
                log.exception("bag check failed")
                bag, path, n, rows = set(), None, 0, []

            def paint():
                try:
                    render(body, rows, path, n, added)
                except Exception:
                    # Never swallow this. A render fault presenting as "nothing matched" is
                    # indistinguishable from genuinely having nothing -- the exact trap the
                    # spell-quest tab hit.
                    log.exception("bag check render failed")
                    for w in body.winfo_children():
                        w.destroy()
                    lab(body, "Bag check hit an error — see the log.",
                        F_BODY, theme.DANGER).pack(padx=12, pady=14)
                finally:
                    btn.configure(state="normal", text="Check My Bags")
            try:
                body.after(0, paint)
            except Exception:
                # 🔴 RESTORE THE BUTTON HERE TOO. The re-enable lives inside paint(), so if
                # scheduling paint() fails the button stays disabled reading "Reading…" and the
                # tab is dead until the app restarts. Caught by building this tab off-screen;
                # py_compile and a render-only test both pass with the bug present.
                log.exception("could not schedule the bag check render")
                try:
                    btn.configure(state="normal", text="Check My Bags")
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    btn = ctk.CTkButton(head, text="Check My Bags", width=150, command=run)
    btn.pack(padx=12, pady=(0, 10), anchor="w")

    body.pack(fill="both", expand=True)
