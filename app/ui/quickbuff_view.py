"""Quick Buff planner — which buffs to load in your gems, for up to three classes.

Quick Buff is an AA that fires every buff you have memorised. The question it creates is which
buffs to put in the gems, and the answer is not "the best N buffs" -- buffs that write the same
effect BLOCK each other, so a naive best-N list hands you six overlapping stat buffs and three
wasted gems.

So this optimises for COVERAGE: each pick is scored on what it adds beyond what is already
chosen, which is why a second haste is worth nothing here and the first mana-regen is worth a
lot. Everything it knows comes from the game's own `spells_us.txt` (see `parsers/spellbook.py`)
-- no network, and it always matches the patch you are running.

🔴 ONE level selector, not three. Owner, 2026-08-31: *"you only need 1 level selector if you
have all 3 classes equipped then they would all be the same level"*. EverQuest Legends is
MULTICLASS -- the three slots are one character's three classes, not three players -- so they
share the character's level. (The Plane of Sky class tests are the unlock for this: Holwin's
turn-in "flags Monk as a primary class".) Consequence: a buff is available if ANY equipped
class reaches it at the character's level.

🔴 What counts as a buff, per the owner 2026-08-31: *"buffs are things that improve your stats
for a period of time heal over times dont count but regen does"*. That is exactly the duration
split -- a regen BUFF runs for many minutes, a heal-over-time does not -- so heals fall out on
their own rather than by guessing intent from a spell's name.
"""
from __future__ import annotations

import logging

import customtkinter as ctk

from app.parsers import spellbook
from app.ui import extra_views as EV
from app.ui import theme

log = logging.getLogger(__name__)

card, lab, wrap = EV.card, EV.lab, EV.wrap
GOLD, T1, T2, T3 = EV.GOLD, EV.T1, EV.T2, EV.T3
F_SMALL, F_BODY = EV.F_SMALL, EV.F_BODY

#: The gems the owner dedicates to buffs. Quick Buff fires everything memorised, but 1-7 and 9
#: are wanted for spells you actually cast, so six is the working default.
DEFAULT_SLOTS = 6
SLOT_GEMS = (8, 10, 11, 12, 13, 14)

_CACHE = {"spells": None, "known": None}


def _known(app):
    """The character's own spellbook dumps, {CLASS: {names}}. Cached per session."""
    if _CACHE["known"] is None:
        cfg = getattr(app, "config", None) or getattr(app, "settings", None)
        try:
            _CACHE["known"] = spellbook.spellbook_files(cfg)
        except Exception:
            log.exception("could not read spellbook dumps")
            _CACHE["known"] = {}
    return _CACHE["known"]


def _spells(app):
    """Parse the client file once per session — it is 74k rows and does not change while running."""
    if _CACHE["spells"] is None:
        cfg = getattr(app, "config", None) or getattr(app, "settings", None)
        try:
            _CACHE["spells"] = spellbook.load(cfg)
        except Exception:
            log.exception("could not load the spell file")
            _CACHE["spells"] = []
    return _CACHE["spells"]


def tab_quickbuff(tab, app):
    """Builder matching the other Tools tabs: (tab_frame, section) -> sets section._qb_refresh."""
    # Default to the classes the rest of the app already knows are his.
    default_classes = list(EV.MY_CLASSES)[:3] or ["CLR", "ENC", "SHM"]
    while len(default_classes) < 3:
        default_classes.append("-")
    state = {"classes": default_classes, "level": spellbook.LEVEL_CAP,
             "slots": DEFAULT_SLOTS, "order": list(spellbook.DEFAULT_ORDER),
             # 🔴 A pin is a JUDGEMENT, so it has to be visible and removable. Pinning Yaulp
             # costs the paladin 55 AC — Yaulp II writes AC, which blocks Armor of Faith
             # (AC +85) from ever taking a gem. A hardcoded pin would have quietly made every
             # plan worse with nothing on screen to say why.
             "pins": list(spellbook.ALWAYS_CAST)}

    head = card(tab)
    head.pack(fill="x", padx=10, pady=(10, 4))
    lab(head, "QUICK BUFF PLANNER", F_SMALL, GOLD).pack(fill="x", padx=12, pady=(10, 2))
    wrap(head, "Quick Buff fires every buff you have memorised, so the only question is which "
               "ones to load. Buffs that write the same effect block each other — this picks a "
               "set that all holds at once, preferring a new kind of benefit over more of one "
               "you already have. Read from the game's own spell file, so it matches your patch.",
         T2).pack(fill="x", padx=12, pady=(0, 11))

    # ── pickers ─────────────────────────────────────────────────────────────
    ctrl = ctk.CTkFrame(tab, fg_color="transparent")
    ctrl.pack(fill="x", padx=10, pady=(0, 6))

    menus = []
    for i in range(3):
        box = ctk.CTkFrame(ctrl, fg_color="transparent")
        box.pack(side="left", padx=(0, 10))
        lab(box, "CLASS %d" % (i + 1), F_SMALL, T3).pack(anchor="w")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack()
        m = ctk.CTkOptionMenu(
            row, width=92, height=28, values=["-"] + EV.CLASSES,
            fg_color=theme.PANEL, button_color=theme.PANEL_HOVER,
            button_hover_color=GOLD, text_color=T1,
            dropdown_fg_color=theme.PANEL, dropdown_text_color=T1,
            dropdown_hover_color=theme.PANEL_HOVER, font=F_SMALL,
            dropdown_font=F_SMALL, corner_radius=7,
            command=lambda _v, _i=i: _changed())
        m.set(state["classes"][i] if i < len(state["classes"]) else "-")
        m.pack(side="left")
        menus.append(m)

    # One level: the classes are all on the SAME character, so they share it.
    lbox = ctk.CTkFrame(ctrl, fg_color="transparent")
    lbox.pack(side="left", padx=(6, 14))
    lab(lbox, "LEVEL", F_SMALL, T3).pack(anchor="w")
    level_menu = ctk.CTkOptionMenu(
        lbox, width=68, height=28, values=[str(n) for n in range(1, spellbook.LEVEL_CAP + 1)],
        fg_color=theme.PANEL, button_color=theme.PANEL_HOVER,
        button_hover_color=GOLD, text_color=T1,
        dropdown_fg_color=theme.PANEL, dropdown_text_color=T1,
        dropdown_hover_color=theme.PANEL_HOVER, font=F_SMALL,
        dropdown_font=F_SMALL, corner_radius=7,
        command=lambda _v: _changed())
    level_menu.set(str(state["level"]))
    level_menu.pack()

    sbox = ctk.CTkFrame(ctrl, fg_color="transparent")
    sbox.pack(side="left")
    lab(sbox, "GEMS", F_SMALL, T3).pack(anchor="w")
    slot_menu = ctk.CTkOptionMenu(
        sbox, width=64, height=28, values=[str(n) for n in range(1, 15)],
        fg_color=theme.PANEL, button_color=theme.PANEL_HOVER,
        button_hover_color=GOLD, text_color=T1,
        dropdown_fg_color=theme.PANEL, dropdown_text_color=T1,
        dropdown_hover_color=theme.PANEL_HOVER, font=F_SMALL,
        dropdown_font=F_SMALL, corner_radius=7,
        command=lambda _v: _changed())
    slot_menu.set(str(DEFAULT_SLOTS))
    slot_menu.pack()

    split = ctk.CTkFrame(tab, fg_color="transparent")
    split.pack(fill="both", expand=True, padx=10, pady=(0, 8))

    # ── priority order ──────────────────────────────────────────────────────
    # 🔴 Ordered, not weighted. Owner: *"why would i care about resist magic if i didnt includr
    # an hp buff first kinda thing"*. The planner walks this top-down, so what sits at the top
    # gets a gem first and the bottom only gets one if gems are left.
    side = ctk.CTkFrame(split, fg_color=theme.PANEL, corner_radius=9, width=214)
    side.pack(side="left", fill="y", padx=(0, 8))
    side.pack_propagate(False)
    lab(side, "PRIORITY", F_SMALL, GOLD).pack(fill="x", padx=12, pady=(10, 1))
    wrap(side, "Top of the list gets a gem first.", T3, width=190).pack(
        fill="x", padx=12, pady=(0, 6))
    prio_box = ctk.CTkScrollableFrame(side, fg_color="transparent")
    prio_box.pack(fill="both", expand=True, padx=4, pady=(0, 8))

    # ── always-cast pins ────────────────────────────────────────────────────
    pin_wrap = ctk.CTkFrame(side, fg_color="transparent")
    pin_wrap.pack(fill="x", padx=4, pady=(0, 8))
    lab(pin_wrap, "ALWAYS CAST", F_SMALL, GOLD).pack(fill="x", padx=8, pady=(4, 1))
    wrap(pin_wrap, "Takes a gem before anything is ranked — and blocks whatever else "
                   "writes the same stat.", T3, width=190).pack(fill="x", padx=8, pady=(0, 4))
    pin_box = ctk.CTkFrame(pin_wrap, fg_color="transparent")
    pin_box.pack(fill="x")

    def _unpin(stem):
        if stem in state["pins"]:
            state["pins"].remove(stem)
            _draw_pins()
            _changed()

    def _repin(stem):
        if stem not in state["pins"]:
            state["pins"].append(stem)
            _draw_pins()
            _changed()

    def _draw_pins():
        for w in pin_box.winfo_children():
            w.destroy()
        for stem in spellbook.ALWAYS_CAST:
            on = stem in state["pins"]
            r = ctk.CTkFrame(pin_box, fg_color="transparent")
            r.pack(fill="x", pady=1)
            lab(r, stem.title(), F_SMALL, T1 if on else T3).pack(side="left", padx=(8, 0))
            ctk.CTkButton(r, text="×" if on else "+", width=22, height=20,
                          corner_radius=5, fg_color=theme.PANEL_HOVER,
                          hover_color=GOLD, text_color=T2, font=F_SMALL,
                          command=(lambda s=stem: _unpin(s)) if on
                          else (lambda s=stem: _repin(s))).pack(side="right", padx=1)

    def _move(name, delta):
        o = state["order"]
        i = o.index(name)
        j = max(0, min(len(o) - 1, i + delta))
        if i != j:
            o[i], o[j] = o[j], o[i]
            _draw_priorities()
            _changed()

    def _draw_priorities():
        for w in prio_box.winfo_children():
            w.destroy()
        for i, name in enumerate(state["order"]):
            r = ctk.CTkFrame(prio_box, fg_color="transparent")
            r.pack(fill="x", pady=1)
            lab(r, "%2d" % (i + 1), F_SMALL, T3, width=20).pack(side="left")
            lab(r, name, F_SMALL, T1).pack(side="left", padx=(2, 0))
            for txt, d in (("▼", 1), ("▲", -1)):
                ctk.CTkButton(r, text=txt, width=22, height=20, corner_radius=5,
                              fg_color=theme.PANEL_HOVER, hover_color=GOLD,
                              text_color=T2, font=F_SMALL,
                              command=lambda n=name, dd=d: _move(n, dd)).pack(
                    side="right", padx=1)

    body = ctk.CTkScrollableFrame(split, fg_color="transparent")
    body.pack(side="left", fill="both", expand=True)

    def _picks():
        """[(class, level)] -- one shared level, because it is one character."""
        try:
            lvl = int(level_menu.get())
        except ValueError:
            lvl = 60
        return [(m.get(), lvl) for m in menus if m.get() and m.get() != "-"]

    def _render():
        for w in body.winfo_children():
            w.destroy()

        spells = _spells(app)
        if not spells:
            box = card(body)
            box.pack(fill="x", pady=6)
            lab(box, "Spell file not found", F_BODY, theme.DANGER).pack(
                fill="x", padx=12, pady=(10, 2))
            wrap(box, "Looked for spells_us.txt in your EverQuest Legends folder. The planner "
                      "reads the game's own spell file — nothing else — so it cannot suggest "
                      "anything without it.", T2).pack(fill="x", padx=12, pady=(0, 10))
            return

        if not spellbook.verify(spells):
            box = card(body)
            box.pack(fill="x", pady=(0, 8))
            lab(box, "Spell file does not look the way we expect", F_BODY, theme.DANGER).pack(
                fill="x", padx=12, pady=(10, 2))
            wrap(box, "A patch can change the layout of spells_us.txt. Rather than recommend "
                      "buffs from a file we may be misreading, this is stopping here. Please "
                      "report it — the fix is quick.", T2).pack(fill="x", padx=12, pady=(0, 10))
            return

        picks = _picks()
        if not picks:
            lab(body, "Pick at least one class above.", F_BODY, T2).pack(padx=12, pady=14)
            return

        try:
            slots = int(slot_menu.get())
        except ValueError:
            slots = DEFAULT_SLOTS
        top_level = max(l for _, l in picks)

        known = _known(app)
        cands = spellbook.castable_buffs(spells, picks, known=known)
        chosen, rejected = spellbook.optimise(cands, slots, top_level, state["order"],
                                              tuple(state["pins"]))

        head2 = ctk.CTkFrame(body, fg_color="transparent")
        head2.pack(fill="x", pady=(2, 6))
        lab(head2, "%d worth loading, from %d you can actually cast"
                   % (len(chosen), len(cands)), F_SMALL, T3).pack(side="left", padx=4)

        # Say plainly which classes were checked against the character's own spellbook. Without
        # this the tool cannot be trusted: the spell FILE lists every spell EverQuest has ever
        # had, and recommending one the player has no way to get is worse than recommending
        # nothing. This is how "Austerity" (a level-55 buff, on a level-50 server) got offered.
        picked = [c for c, _ in picks]
        verified = [c for c in picked if c in known]
        guessed = [c for c in picked if c not in known]
        if verified:
            lab(head2, "· %s from your spellbook" % "/".join(verified),
                F_SMALL, GOLD).pack(side="left", padx=(8, 0))
        if guessed:
            lab(head2, "· %s NOT verified (no spellbook dump)" % "/".join(guessed),
                F_SMALL, theme.DANGER).pack(side="left", padx=(8, 0))

        if len(chosen) < slots:
            note0 = card(body)
            note0.pack(fill="x", pady=(0, 6))
            wrap(note0, "Only %d of your %d gems are worth filling. The rest would hold buffs "
                        "too weak or too short to be worth re-firing — an empty gem is better "
                        "than a bad one." % (len(chosen), slots), T3).pack(
                fill="x", padx=12, pady=9)

        for i, r in enumerate(chosen):
            s = r["spell"]
            gem = SLOT_GEMS[i] if slots == DEFAULT_SLOTS and i < len(SLOT_GEMS) else i + 1
            box = card(body)
            box.pack(fill="x", pady=3)

            top = ctk.CTkFrame(box, fg_color="transparent")
            top.pack(fill="x", padx=12, pady=(9, 1))
            lab(top, "Gem %-2d" % gem, F_SMALL, GOLD).pack(side="left")
            lab(top, s.name, F_BODY, T1).pack(side="left", padx=(8, 0))
            # Which priority earned this gem -- the reason it is here at all.
            why = r.get("for_priority", "")
            if why and why != "extra":
                lab(top, why, F_SMALL, T3).pack(side="right")
            if s.is_song:
                lab(top, "SONG", F_SMALL, T3).pack(side="left", padx=(8, 0))

            meta = "%s L%d · %s · %s · %s" % (
                r["caster"], r["req_level"],
                ("%d mana" % s.mana) if s.mana else "no mana",
                _dur(s, top_level),
                spellbook.TARGETS.get(s.target, "?"))
            lab(box, meta, F_SMALL, T3).pack(fill="x", padx=12)
            lab(box, spellbook.summary(r), F_SMALL, T2).pack(fill="x", padx=12, pady=(1, 10))

        if rejected:
            gap = card(body)
            gap.pack(fill="x", pady=(10, 3))
            lab(gap, "PASSED OVER", F_SMALL, GOLD).pack(fill="x", padx=12, pady=(9, 1))
            wrap(gap, "Strong buffs that did not make it, and why. An overlap means the game "
                      "would block one of the two — the slot would be wasted, not doubled.",
                 T3).pack(fill="x", padx=12, pady=(0, 8))
            for r in rejected[:10]:
                line = ctk.CTkFrame(gap, fg_color="transparent")
                line.pack(fill="x", padx=12, pady=1)
                lab(line, r["spell"].name, F_SMALL, T2).pack(side="left")
                lab(line, r["because"], F_SMALL, T3).pack(side="left", padx=(8, 0))
            ctk.CTkFrame(gap, fg_color="transparent", height=6).pack()

        note = card(body)
        note.pack(fill="x", pady=(10, 4))
        if guessed:
            gw = card(body)
            gw.pack(fill="x", pady=(4, 0))
            wrap(gw, "No spellbook dump found for %s, so those picks come from the client's "
                     "spell list rather than from what you actually know. The client writes "
                     "<character>-<CLASS>-Spellbook.txt when you log in on that class — do that "
                     "once and this tab will only offer spells you really have."
                 % "/".join(guessed), T3).pack(fill="x", padx=12, pady=9)

        wrap(note, "How it decides: it walks your PRIORITY list top-down and, for each line "
                   "not already covered, takes the strongest buff that delivers it and still "
                   "holds alongside everything above it. So resists only get a gem once the "
                   "things above them are handled — reorder the list and the plan changes. "
                   "Buffs that write the same effect really do block each other, which is why "
                   "one is picked and the others are listed as passed over.",
             T3).pack(fill="x", padx=12, pady=10)

    def _changed(*_a):
        try:
            _render()
        except Exception:
            log.exception("quick buff render failed")

    section = app if hasattr(app, "_built") else None
    if section is not None:
        section._qb_refresh = _render
    _draw_priorities()
    _draw_pins()
    _render()


def _dur(s, level: int) -> str:
    """Durations run from a couple of minutes to permanent; one format does not fit all."""
    t = s.ticks(level)
    if t >= 60000:
        return "permanent"
    mins = t * 6 / 60.0
    if mins >= 60:
        return "%.1f hr" % (mins / 60.0)
    if mins >= 1:
        return "%d min" % round(mins)
    return "%d sec" % (t * 6)
