"""Spell Quests — the quests that grant a spell, matched against what you already know.

WHY THIS IS ITS OWN SECTION
    Spell acquisition was invisible in this app. The `quests` table had one spell quest in it
    (the Magician epic); everything else lived in `wiki_items` as a `Spell: X` scroll with no
    link back to a quest. So "what spell quests can my class do" could not be answered from the
    journal at all, and asking it returned an empty list that looked like an answer.

    Owner, 2026-09-01: *"we might not have that quest on file but there is quests for spells
    that we dont have you better go look"*. He was right -- twelve of them are listed on the
    Legends wiki's own Class Race Quest List, including the two that matter to him: Tashania
    (ENC 41, the Coin of Tash quest) and Divine Might (PAL 45, The Bones of Darak Lightforge).

WHAT MAKES IT USEFUL RATHER THAN A LIST
    It cross-references your OWN spellbook dump, so a quest you have already completed shows as
    done rather than as homework. That needs `known_spells()`, not `spellbook_files()` --
    a Legends spellbook dump is CHARACTER-wide even though its filename names one class.

🔴 Rows the wiki lists but whose steps we have not recorded say so. They are not padded with
    plausible-looking steps: a guessed turn-in sends someone across Norrath for nothing.
"""
from __future__ import annotations

import logging
import threading

import customtkinter as ctk

from app.parsers import spellbook
from app.ui import extra_views as EV
from app.ui import theme

log = logging.getLogger(__name__)

card, lab, wrap = EV.card, EV.lab, EV.wrap
GOLD, T1, T2, T3 = EV.GOLD, EV.T1, EV.T2, EV.T3
F_SMALL, F_BODY = EV.F_SMALL, EV.F_BODY

#: Wiki class names -> the abbreviations the spell file and class pickers use.
CLASS_ABBR = {
    "Warrior": "WAR", "Cleric": "CLR", "Paladin": "PAL", "Ranger": "RNG",
    "Shadowknight": "SHD", "Shadow Knight": "SHD", "Druid": "DRU", "Monk": "MNK",
    "Bard": "BRD", "Rogue": "ROG", "Shaman": "SHM", "Necromancer": "NEC",
    "Wizard": "WIZ", "Magician": "MAG", "Enchanter": "ENC", "Beastlord": "BST",
    "Berserker": "BER",
}

_CACHE: dict = {"rows": None, "known": None}


def _known(app) -> set:
    if _CACHE["known"] is None:
        cfg = getattr(app, "config", None) or getattr(app, "settings", None)
        try:
            _CACHE["known"] = spellbook.known_spells(cfg)
        except Exception:
            log.exception("could not read the spellbook dump")
            _CACHE["known"] = set()
    return _CACHE["known"]


def fetch_rows(app) -> list:
    """Spell quests from the public API. Blocking — never call from the UI thread."""
    try:
        sup = getattr(app, "supabase", None)
        client = getattr(sup, "_client", None) if sup else None
        if client is None:
            return []
        res = (client.table("quests")
               .select("quest_name,quest_giver_npc,zone,reward_item,min_level,"
                       "char_class,description")
               .eq("category", "spell").execute())
        return list(res.data or [])
    except Exception:
        log.exception("spell quest fetch failed")
        return []


def _spell_name(reward: str) -> str:
    """`Spell: Tashania` -> `tashania`, for matching against the spellbook."""
    r = (reward or "").strip()
    for p in ("spell:", "tome of", "words of"):
        if r.lower().startswith(p):
            r = r[len(p):].strip()
            break
    return r.lower()


def render(body, app, rows, my_classes=None) -> None:
    """Draw the rows into `body`.

    Deliberately separate from the fetch so it can be tested WITHOUT threading. The first cut
    had only `build()`, and an exception inside the worker's `after()` callback was swallowed
    by a bare `except` -- which looked exactly like "there are no spell quests".
    """
    for w in body.winfo_children():
        w.destroy()
    if not rows:
        lab(body, "Could not load spell quests.", F_BODY, T3).pack(padx=12, pady=14)
        return

    known = _known(app)
    mine = {c for c in (my_classes or EV.MY_CLASSES) if c}

    def sort_key(r):
        ab = CLASS_ABBR.get((r.get("char_class") or "").strip(), "")
        return (0 if ab in mine else 1, r.get("min_level") or 99, r.get("char_class") or "")

    for r in sorted(rows, key=sort_key):
        cls = (r.get("char_class") or "?").strip()
        is_mine = CLASS_ABBR.get(cls, "") in mine
        have = _spell_name(r.get("reward_item") or "") in known

        box = card(body)
        box.pack(fill="x", pady=3)
        top = ctk.CTkFrame(box, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(9, 1))
        lab(top, cls, F_SMALL, GOLD if is_mine else T3).pack(side="left")
        lab(top, r.get("quest_name") or "?", F_BODY, T1).pack(side="left", padx=(8, 0))
        if have:
            lab(top, "✓ you know this", F_SMALL, GOLD).pack(side="right")
        elif is_mine:
            lab(top, "YOUR CLASS", F_SMALL, GOLD).pack(side="right")

        bits = [r.get("reward_item") or "?"]
        if r.get("min_level"):
            bits.append("level %s" % r["min_level"])
        if r.get("quest_giver_npc"):
            bits.append(r["quest_giver_npc"])
        if r.get("zone"):
            bits.append(r["zone"])
        lab(box, " · ".join(bits), F_SMALL, T3).pack(fill="x", padx=12)

        desc = (r.get("description") or "").strip()
        # 🔴 Say plainly when the steps are not recorded. A guessed turn-in is worse
        # than a gap -- it sends someone across Norrath for nothing.
        if "not yet recorded" in desc.lower() or len(desc) < 80:
            wrap(box, "Steps not recorded yet — the Legends wiki lists this quest but we "
                      "have not verified how it runs. If you do it, tell us.",
                 T3).pack(fill="x", padx=12, pady=(2, 10))
        else:
            wrap(box, desc, T2).pack(fill="x", padx=12, pady=(2, 10))


def build(parent, app, my_classes=None) -> None:
    """Fetch on a worker thread, then render. Never blocks the UI thread."""
    for w in parent.winfo_children():
        w.destroy()

    head = card(parent)
    head.pack(fill="x", pady=(0, 6))
    lab(head, "SPELL QUESTS", F_SMALL, GOLD).pack(fill="x", padx=12, pady=(10, 2))
    wrap(head, "Spells you have to earn rather than buy. Checked against your own spellbook, "
               "so anything you already know is marked. Yours are listed first.",
         T2).pack(fill="x", padx=12, pady=(0, 10))

    body = ctk.CTkFrame(parent, fg_color="transparent")
    body.pack(fill="both", expand=True)
    lab(body, "Loading spell quests…", F_BODY, T3).pack(padx=12, pady=14)

    def work():
        rows = fetch_rows(app)

        def paint():
            try:
                render(body, app, rows, my_classes)
            except Exception:
                # 🔴 Never swallow this silently. A render fault used to present as "no spell
                # quests", which is indistinguishable from the data genuinely being empty.
                log.exception("spell quest render failed")
                for w in body.winfo_children():
                    w.destroy()
                lab(body, "Spell quests hit an error — see the log.",
                    F_BODY, theme.DANGER).pack(padx=12, pady=14)
        try:
            body.after(0, paint)
        except Exception:
            log.exception("could not schedule the spell quest render")

    threading.Thread(target=work, daemon=True).start()
