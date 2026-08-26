"""Devkit tabs ported into the shipped app: Healing, Loot, Gear, Codex, and the overlay.

Owner, 2026-08-25: *"can you make a journal app plus that dps healing stuff we was working on
into 1 app"* -- so the two builds become one. The app already had Journal and Combat; these
are the four tabs plus the overlay that only existed in the devkit build.

WHAT CHANGED IN THE PORT, and why:
  * The devkit owned a `Tail` THREAD that followed the log itself. The app already has exactly
    one reader (`LogWatcher`), so every `app.tail` reference here now resolves to the shared
    `CombatFeed` -- one reader, one parser, no disagreement between tabs.
  * `load()` reads from %LOCALAPPDATA%/GnollGuard/data instead of the script folder. THIS REPO
    IS PUBLIC; the snapshots must not sit in the source tree. See `datapaths`.
  * Colours and fonts are taken from `theme` where they line up, so the tabs match the rest of
    the app rather than carrying the devkit's palette.

OFFLINE ONLY -- nothing in this module opens a socket. It reads the game's own log file and
the local snapshots, and that is all.
"""
from __future__ import annotations

import collections
import datetime
import logging
import os
import re
import sys
import time

import customtkinter as ctk

from app.ui import datapaths, theme
from app.ui.combat_feed import clean_item as _feed_clean_item  # noqa: F401

log = logging.getLogger(__name__)

# Devkit palette names mapped onto the app theme so the ported bodies need no edits.
BG, PANEL, HOVER, BORDER = theme.BG, theme.PANEL, theme.PANEL_HOVER, theme.PANEL_HOVER
GOLD, T1, T2, T3 = theme.GOLD, theme.TEXT_PRIMARY, theme.TEXT_SECONDARY, theme.TEXT_MUTED
OK, WARN, BAD = theme.GREEN, theme.GOLD, theme.DANGER
INFO, VIOLET = theme.TEXT_SECONDARY, theme.GOLD
F_HEAD, F_SUB = theme.FONT_HEADER, theme.FONT_SUBHEADER
F_BODY, F_SMALL, F_MONO = theme.FONT_BODY, theme.FONT_BODY_SMALL, theme.FONT_MONO
F_BIG = ("Consolas", 30, "bold")

CLASSES = ["WAR", "CLR", "PAL", "RNG", "SHD", "DRU", "MNK", "BRD",
           "ROG", "SHM", "NEC", "WIZ", "MAG", "ENC", "BST", "BER"]
# Slot weights for the class picker: first pick counts 3x, second 2x, third 1x -- the same
# 3:2:1 the site's BiS uses, so the app and the site rank a multiclass identically.
POSITION_WEIGHTS = (3, 2, 1)
# Mutable on purpose: the class selector rewrites this in place and the Codex reads it.
MY_CLASSES = ["PAL", "MNK", "ENC"]

ERAS = ["Classic only", "+ Kunark", "+ Velious", "All (incl. unreleased)"]
ERA_ALLOWS = {ERAS[0]: {"Classic"}, ERAS[1]: {"Classic", "Kunark"},
              ERAS[2]: {"Classic", "Kunark", "Velious"},
              ERAS[3]: {"Classic", "Kunark", "Velious", ""}}
ERA_COLOR = {"Classic": OK, "Kunark": WARN, "Velious": INFO, "": T3}
KIND_COLOR = {"CLICK": INFO, "PROC": BAD, "FOCUS": OK, "FOCUS?": OK, "WORN": VIOLET}

# The devkit reached sideways into these two trees. The app IS the app tree, and the devkit
# is private and must never be a runtime dependency of a shipped build -- so APP_ROOT points
# at ourselves and DEVTOOL is optional: if it is absent the affected helper degrades rather
# than raising. Never make the shipped app require devtool/ to start.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEVTOOL = os.environ.get("GNOLLGUARD_DEVTOOL", r"C:/Users/coryo/GnollLoot-docs/devtool")

AC_TO_HP = 7.5   # owner's armour:HP exchange rate; same constant the site's BiS uses


def load(name, default):
    """Snapshot loader -- reads from the external data dir, never from this repo."""
    return datapaths.load(name, default)


ITEMS = load("items.json", {})
CATALOG = load("exaltations.json", [])
MOBS = load("mobs.json", {})

def pet_label(name):
    """Display name for a pet row.

    The parser appends " (your pet)" only to disambiguate a charmed pet whose name COLLIDES
    with the mob being fought (`an ice giant` pet vs `an ice giant` target). Once the row is
    indented beneath its owner the suffix is redundant, and at overlay width it truncated to
    the memorable "an ice giant (yo".
    """
    for suffix in (" (your pet)", " (unknown pet)"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def loose_name(x):
    """Match item names across the log/DB boundary despite punctuation drift.

    🔴 Owner, 2026-08-23: "what do you mean slime blood of cazic-thule +5 isnt in our db?
    it better be im waring it." It was — stored as "Slime Blood of Cazic Thule" with a
    SPACE while the game writes a HYPHEN. Same for "Djarn's" vs "Djarns", and
    "Engineer's". Reporting a worn item as unknown is worse than useless: it makes the
    tool look broken on the exact gear the player is staring at.

    items.json is keyed by this form, so every lookup must go through it.
    """
    x = (x or "").lower().replace("-", " ").replace("`", " ").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", x).split())


def norm_mob(s):
    s = (s or "").strip().lower()
    for a in ("a ", "an ", "the "):
        if s.startswith(a):
            s = s[len(a):]
    return s


def clean_item(s):
    """Strip the leading article the LOG adds but the DATABASE does not carry.

    🔴 SMOKE-TEST BUG, 2026-08-22. The log writes "--You have looted a Mote of Major
    Potential from ...--" while wiki_items stores "Mote of Major Potential". Comparing
    the raw strings made EVERY loot read NEW — 22 of 22 on the first run — which is both
    useless and a lie, since the badge is supposed to mean "nobody has recorded this".
    """
    s = (s or "").strip()
    low = s.lower()
    for a in ("a ", "an ", "the "):
        if low.startswith(a):
            return s[len(a):].strip()
    return s


def per_tier(base):
    return max(1.0, 0.10 * float(base or 0))


def log_dt(stamp: str):
    """EQ stamps '[Sat Aug 16 01:47:06 2026]'. Return a real datetime, or None.

    🔴 The loot list originally recorded time.time() — the moment the line was PARSED.
    For the 4 MB of history read at startup that is simply wrong: everything looted days
    ago showed as happening now. The log carries the truth; use it.
    ⚠ The day is space-padded on single digits ("Aug  6"), so split on whitespace rather
    than trusting a fixed-width format string.
    """
    try:
        parts = (stamp or "").split()
        if len(parts) != 5:
            return None
        _, mon, day, clock, year = parts
        return datetime.datetime.strptime(f"{mon} {day} {clock} {year}", "%b %d %H:%M:%S %Y")
    except Exception:
        return None


def card(p, **k):
    return ctk.CTkFrame(p, fg_color=HOVER, corner_radius=9, **k)


def lab(p, text, font=F_BODY, color=T1, **k):
    return ctk.CTkLabel(p, text=text, font=font, text_color=color,
                        anchor="w", justify="left", **k)


def wrap(p, text, color=T2, width=620):
    return ctk.CTkLabel(p, text=text, font=F_SMALL, text_color=color,
                        wraplength=width, justify="left", anchor="w")


def rule(p):
    ctk.CTkFrame(p, fg_color=BORDER, height=1).pack(fill="x", pady=(2, 8))


def section(p, title):
    lab(p, title.upper(), F_SMALL, T3).pack(fill="x", padx=2, pady=(10, 3))
    rule(p)


def bar(p, frac, color, height=6):
    t = ctk.CTkFrame(p, fg_color=PANEL, corner_radius=3, height=height)
    t.pack(fill="x", pady=(5, 0))
    t.pack_propagate(False)
    f = ctk.CTkFrame(t, fg_color=color, corner_radius=3)
    f.place(relx=0, rely=0, relwidth=max(0.01, min(1.0, frac)), relheight=1)
    return t


def clear(frame):
    for w in frame.winfo_children():
        w.destroy()


class Overlay(ctk.CTkToplevel):
    """Always-on-top HUD.

    🔴 INVARIANT: never steals focus, no popups. No focus_force, no grab_set, and
    -topmost is set on THIS window only — never stamped on a parent handle, which is
    the mistake that once froze the whole machine.
    """

    def __init__(self, master, tail):
        super().__init__(master)
        self.tail = tail
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.92)
        self.geometry("360x430+40+40")
        self.configure(fg_color=BORDER)
        shell = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=16)
        shell.pack(fill="both", expand=True, padx=2, pady=2)

        hdr = ctk.CTkFrame(shell, fg_color=HOVER, corner_radius=14, height=36)
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        hdr.pack_propagate(False)
        self.dot = ctk.CTkFrame(hdr, fg_color=OK, width=7, height=7, corner_radius=4)
        self.dot.pack(side="left", padx=(10, 6))
        self.title_lbl = lab(hdr, "waiting", F_SUB, GOLD)
        self.title_lbl.pack(side="left")
        ctk.CTkButton(hdr, text="x", width=22, height=22, corner_radius=6,
                      fg_color=HOVER, hover_color=BORDER, text_color=T3,
                      font=F_SMALL, command=self.destroy).pack(side="right", padx=8)

        top = ctk.CTkFrame(shell, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(8, 2))
        self.dps = lab(top, "--", F_BIG, T1)
        self.dps.pack(side="left")
        lab(top, "  your dps", F_SMALL, T2).pack(side="left", pady=(10, 0))
        self.elapsed = lab(top, "", F_MONO, T2)
        self.elapsed.pack(side="right", pady=(12, 0))
        # 🔴 Owner, 2026-08-23: "there is no way im doing only 36 damage per second."
        # He was reading ENCOUNTER dps — damage over the whole time the mob was up,
        # including every second he was not swinging (running in, medding, out of
        # range). Measured on his own log: he is actually swinging for about 10% of the
        # wall-clock he spends in fights, so the two numbers differ by a lot and only
        # one of them answers "how hard do I hit".
        # Show BOTH. Encounter dps stays the headline because that is the number that
        # kills the mob and the number other parsers print; "while swinging" is what he
        # was looking for. Never replace one with the other — a meter that quietly
        # switches to the flattering definition is how parsers lie.
        self.swing = lab(top, "", F_SMALL, T3)
        self.swing.pack(side="right", padx=10, pady=(12, 0))

        self.rows = ctk.CTkFrame(shell, fg_color="transparent")
        self.rows.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # drag by the header — an overlay you cannot move is an overlay in the way
        for w in (hdr, self.title_lbl):
            w.bind("<Button-1>", self._grab)
            w.bind("<B1-Motion>", self._drag)
        self._off = (0, 0)
        self.refresh()

    def _grab(self, e):
        self._off = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag(self, e):
        self.geometry(f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")

    def refresh(self):
        try:
            lc = self.tail.lc
            livegate = bool(self.tail and self.tail.live_seen)
            f = ((lc.current() if livegate else None) or lc.last_kill()) if lc else None
            if f:
                live = livegate and bool(lc.current())
                self.title_lbl.configure(text=f["mob"][:26])
                self.dot.configure(fg_color=OK if live else T3)
                me = next((r for r in f["rows"] if r["is_me"]), None)
                self.dps.configure(text=str(me["encounter_dps"]) if me else "--")
                act = (me or {}).get("active_dps") or 0
                enc = (me or {}).get("encounter_dps") or 0
                self.swing.configure(
                    text=f"{act:,} while swinging" if act and act != enc else "")
                self.elapsed.configure(text=f"{f['duration']:.0f}s")
                clear(self.rows)
                peak = max((r["damage"] for r in f["rows"]), default=1)
                for r in f["rows"][:6]:
                    c = ctk.CTkFrame(self.rows, fg_color="transparent")
                    c.pack(fill="x", pady=1)
                    line = ctk.CTkFrame(c, fg_color="transparent")
                    line.pack(fill="x")
                    col = GOLD if r["is_me"] else T1
                    lab(line, r["name"][:20], F_SMALL, col).pack(side="left")
                    lab(line, f"{r['encounter_dps']:,}", F_MONO, col).pack(side="right")
                    bar(c, r["damage"] / peak, col, height=4)
                    # Tiered under the owner rather than a "+pet" tag crowding the dps
                    # figure. Space is tight here, so one indented line per pet and no bar.
                    for p in (r.get("pets") or [])[:2]:
                        pl = ctk.CTkFrame(c, fg_color="transparent")
                        pl.pack(fill="x", padx=(14, 0))
                        lab(pl, "└ " + pet_label(p["name"])[:18], F_SMALL, T3).pack(side="left")
                        lab(pl, f"{p['damage']:,}", F_SMALL, T3).pack(side="right")
        except Exception:
            pass
        self.after(1000, self.refresh)


def tab_loot(tab, app):
    head = card(tab)
    head.pack(fill="x", padx=10, pady=(10, 4))
    lab(head, "LOOT + POOLED INTEL", F_SMALL, GOLD).pack(fill="x", padx=12, pady=(10, 2))
    wrap(head, "Everything you loot, newest first, grouped by the day it happened. NEW means the database has never recorded "
               "that item off that mob — that is the moment your log adds something no wiki "
               "has. Below each kill, what 24 other people's kills say the mob drops.",
         T2).pack(fill="x", padx=12, pady=(0, 11))
    body = ctk.CTkFrame(tab, fg_color="transparent")
    body.pack(fill="both", expand=True)

    def redraw():
        clear(body)
        loot = app.tail.loot if app.tail else []
        if not loot:
            lab(body, "nothing looted yet", F_BODY, T3).pack(padx=12, pady=12)
        # Grouped by the DAY IT HAPPENED, newest first, using the log's own stamp.
        # Owner asked for "looted today" plus dates — one heading per day answers both,
        # and makes it obvious when the list is showing history rather than this sitting.
        today = datetime.date.today()
        buckets = {}
        for l in loot:
            d = l["when"].date() if l.get("when") else None
            buckets.setdefault(d, []).append(l)
        order = sorted([d for d in buckets if d], reverse=True) + ([None] if None in buckets else [])
        for d in order:
            if d == today:
                head = f"looted today — {len(buckets[d])}"
            elif d is None:
                head = f"undated — {len(buckets[d])}"
            elif (today - d).days == 1:
                head = f"yesterday, {d:%b %d} — {len(buckets[d])}"
            else:
                head = f"{d:%a %b %d} — {len(buckets[d])}"
            section(body, head)
            for l in buckets[d][:40]:
                c = card(body)
                c.pack(fill="x", padx=10, pady=2)
                line = ctk.CTkFrame(c, fg_color="transparent")
                line.pack(fill="x", padx=11, pady=(7, 0))
                lab(line, l["item"], F_BODY, T1).pack(side="left")
                if l["new"]:
                    lab(line, "NEW", F_SMALL, GOLD).pack(side="right")
                when = f"{l['when']:%H:%M}" if l.get("when") else "--:--"
                lab(line, when, F_MONO, T3).pack(side="right", padx=10)
                lab(c, "from " + l["mob"], F_SMALL, T3).pack(fill="x", padx=11, pady=(0, 8))

        # pooled intel for the most recent mob — the thing a local-only tool cannot know
        lc = app.tail.lc if app.tail else None
        mob = None
        if lc:
            f = lc.current() or lc.last_kill()
            mob = f["mob"] if f else None
        if mob:
            rec = MOBS.get(norm_mob(mob))
            section(body, f"what everyone knows about {mob}")
            if not rec or not rec.get("rates"):
                c = card(body)
                c.pack(fill="x", padx=10, pady=2)
                wrap(c, f"No pooled drop rates for {mob} yet. Only 397 of 4,822 mobs have "
                        f"enough kills across all contributors. Yours are helping.", T3)\
                    .pack(fill="x", padx=12, pady=10)
            else:
                for r in rec["rates"][:12]:
                    c = card(body)
                    c.pack(fill="x", padx=10, pady=2)
                    line = ctk.CTkFrame(c, fg_color="transparent")
                    line.pack(fill="x", padx=11, pady=(8, 0))
                    lab(line, r["item"] or "?", F_BODY, T1).pack(side="left")
                    pct = (r["rate"] or 0) * 100
                    lab(line, f"{pct:.1f}%", F_MONO, GOLD if r["pub"] else T3).pack(side="right")
                    lab(c, f"{r['hits'] or 0} drops in {r['kills'] or 0} kills"
                           + ("" if r["pub"] else "   low confidence"),
                        F_SMALL, T3).pack(fill="x", padx=11, pady=(1, 9))

    app.redraw_loot = redraw


def tab_healing(tab, app):
    """Healing, rewritten 2026-08-25 to answer the two questions the owner actually asks.

    He dropped the per-second framing outright: *"i dont like the heals per second lets just
    highlight who we healed and what our over all Healing average between all fights per
    day."* So HPS, casts/min and secs-between are gone from the display -- the parser still
    computes them, nothing reads them here.

    What is left is deliberately two things:
      WHO YOU HEALED   aggregated across every parsed fight, biggest first
      PER DAY          total healing divided by fights that day -- an average per fight,
                       which is comparable across a long night and a short one

    Self-sustain (lifetap) is kept OUT of both and shown separately. Folding it in would put
    a lifetapping paladin at the top of his own healing chart, which is exactly the flattery
    `heal_others` exists to prevent -- measured at 91% of his total on 2026-08-23.
    """
    head = card(tab)
    head.pack(fill="x", padx=10, pady=(10, 4))
    st = lab(head, "waiting for healing...", F_BODY, T2)
    st.pack(fill="x", padx=12, pady=(10, 2))
    wrap(head, "Who you healed, and your average healing per fight on each day. Healing to "
               "OTHERS only - lifetap self-sustain is listed on its own, because counting it "
               "would top the chart with your own hits.", T3)        .pack(fill="x", padx=12, pady=(0, 10))
    body = ctk.CTkFrame(tab, fg_color="transparent")
    body.pack(fill="both", expand=True)

    def day_of(fight):
        """Calendar date of a fight, from the parser's date-aware timestamps."""
        try:
            return datetime.date.fromordinal(int(fight.start // 86400) + 719163)
        except Exception:
            return None

    def redraw():
        lc = app.tail.lc if app.tail else None
        if not lc:
            return
        clear(body)

        targets = {}      # who -> [healed, casts, best, overheal]
        per_day = {}      # date -> [healed_to_others, fight_count]
        self_heal = 0
        for fi in lc.fights:
            d = day_of(fi)
            if d is not None:
                per_day.setdefault(d, [0, 0])[1] += 1
            me = fi.actors.get("You")
            if me is None:
                continue
            self_heal += me.heal_self
            # 🔴 BOTH numbers come from the LEDGER, never from `heal_others`.
            # `heal_others` is a RESIDUAL: heal_effective minus heal_self. Measured
            # 2026-08-25 on a 4 MB slice, the ledger recorded all 4,693 healing against
            # target "You" while heal_self counted only 3,613 -- so heal_others reported
            # 1,080 healing "to others" on a session where he healed nobody at all. Some
            # self-heal line shape is not being counted as self, and the residual quietly
            # became a phantom group-healing figure.
            # Summing the ledger instead makes the per-day total and the who-you-healed
            # list agree BY CONSTRUCTION: they are the same rows, added up two ways.
            for t, (eff, att, casts, best) in me.heal_out.items():
                if t == "You" or not eff:
                    continue          # self-sustain is counted separately, never as a target
                if d is not None:
                    per_day[d][0] += eff
                row = targets.setdefault(t, [0, 0, 0, 0])
                row[0] += eff
                row[1] += casts
                row[2] = max(row[2], best)
                row[3] += max(0, att - eff)

        total = sum(v[0] for v in targets.values())
        st.configure(
            text=("%s   %s   %s healed to others across %d fights"
                  % (app.tail.zone or "unknown zone",
                     chr(183), format(total, ",")
                     if total else "no group healing yet", len(lc.fights))),
            text_color=T1)

        if not targets and not self_heal:
            lab(body, "no healing recorded yet", F_BODY, T3).pack(padx=12, pady=12)
            return

        # ── per day ─────────────────────────────────────────────────────────
        if per_day:
            section(body, "your healing per day - average per fight")
            today = datetime.date.today()
            days = sorted(per_day, reverse=True)
            peak = max((v[0] / max(1, v[1])) for v in per_day.values()) or 1
            for d in days:
                healed, fights = per_day[d]
                avg = healed / max(1, fights)
                label = ("today" if d == today
                         else "yesterday" if (today - d).days == 1
                         else "%s" % d.strftime("%a %b %d"))
                c = card(body)
                row = ctk.CTkFrame(c, fg_color="transparent")
                row.pack(fill="x", padx=11, pady=(8, 0))
                lab(row, label, F_BODY, T1 if healed else T3).pack(side="left")
                lab(row, "%s avg / fight" % format(int(avg), ","), F_MONO,
                    GOLD if healed else T3).pack(side="right")
                lab(c, "%s healed over %d fight%s"
                       % (format(healed, ","), fights, "" if fights == 1 else "s"),
                    F_SMALL, T3).pack(fill="x", padx=11)
                holder = ctk.CTkFrame(c, fg_color="transparent")
                holder.pack(fill="x", padx=11, pady=(0, 9))
                bar(holder, avg / peak, GOLD if healed else BORDER, height=5)

        # ── who ─────────────────────────────────────────────────────────────
        if targets:
            section(body, "who you healed")
            order = sorted(targets.items(), key=lambda kv: -kv[1][0])
            peak = order[0][1][0] or 1
            for name, (healed, casts, best, over) in order:
                c = card(body)
                row = ctk.CTkFrame(c, fg_color="transparent")
                row.pack(fill="x", padx=11, pady=(8, 0))
                lab(row, name, F_BODY, T1).pack(side="left")
                lab(row, format(healed, ","), F_MONO, GOLD).pack(side="right")
                sub = "%d cast%s   %s   biggest %s" % (
                    casts, "" if casts == 1 else "s", chr(183), format(best, ","))
                if over:
                    sub += "   %s %d%% overheal" % (chr(183),
                                                    round(100 * over / max(1, healed + over)))
                lab(c, sub, F_SMALL, T3).pack(fill="x", padx=11)
                holder = ctk.CTkFrame(c, fg_color="transparent")
                holder.pack(fill="x", padx=11, pady=(0, 9))
                bar(holder, healed / peak, GOLD, height=5)

        if self_heal:
            section(body, "your own sustain")
            c = card(body)
            lab(c, "%s self-healed (lifetap and self-casts)" % format(self_heal, ","),
                F_BODY, T2).pack(fill="x", padx=12, pady=9)

    app.redraw_healing = redraw

def _slot_key(s):
    """'Fingers2' -> 'finger'. Strip the 1/2 suffix EQ adds to paired slots.

    ⚠ The inventory dump says "Fingers", the item DB says "Finger". Without the singular
    mapping both ring slots silently matched nothing and dropped out of the comparison.
    """
    k = "".join(ch for ch in (s or "") if not ch.isdigit()).strip().lower()
    return {"fingers": "finger", "ears": "ear", "wrists": "wrist"}.get(k, k)


def _item_sources():
    """item name (lower) -> [(npc, zone)], reversed out of the pooled mob table."""
    idx = {}
    for mob, rec in MOBS.items():
        for it in rec.get("confirmed", []) + rec.get("wiki_only", []):
            idx.setdefault(it.lower(), []).append(mob)
    return idx


SOURCES = _item_sources()


def acquire_line(it):
    """How you actually GET this item, in one line.

    Preference order is evidence order: a drop WE have confirmed beats a wiki listing,
    which beats a bare Quest flag, which beats admitting we do not know. Never claim a
    source we do not have — "no recorded source" is a real and useful answer.
    """
    srcs = it.get("sources") or []
    conf = [x for x in srcs if x.get("confirmed")]
    pick = conf or srcs
    if pick:
        bits = []
        for x in pick[:2]:
            t = x["npc"]
            if x.get("zone"):
                t += f"  ·  {x['zone']}"
            if x.get("rarity"):
                t += f"  ({x['rarity']})"
            bits.append(t)
        tag = "CONFIRMED DROP" if conf else "drops from"
        return tag, "   |   ".join(bits)
    flags = (it.get("flags") or "").lower()
    if "quest" in flags:
        return "QUEST ITEM", "flagged Quest on the item — quest not recorded in our data"
    return "no recorded source", "may be a quest reward or crafted"


def _best_for_slot(era_ok=("Classic",)):
    """Best AC+HP/7.5 item per slot, filtered to the played classes and era.

    Multi-slot items ("Shoulders Arms Wrist") are indexed under every slot they fit,
    which is correct and is why one cord can be the best answer in three places.

    🔴 CONFIRMED CLASSIC ONLY by default, and that is a correction. The first version
    also admitted unknown-era items, and EVERY upgrade it recommended came back era=''
    — Breastplate of the Righteous (the owner corrected me on that one before: "Righteous
    is out of era dude"), History of the Di`zok (Kunark), Bile Etched Obsidian Choker
    (Fear/Hate revamp). 2,343 of 7,929 equippable items have no era at all, and that pile
    is exactly where out-of-era gear hides, so letting it in made the tool point at things
    he cannot get.

    Unknown-era items are still surfaced, but SEPARATELY and labelled unverified — they
    are also where legitimate craftables live, which is why they are not simply dropped.
    """
    picked = [c for c in MY_CLASSES if c]

    def usable(it):
        cls = (it.get("classes") or "").upper()
        if "ALL" in cls and "EXCEPT" not in cls:
            return True
        return any(c in cls for c in picked)

    best = {}
    for it in ITEMS.values():
        if not usable(it):
            continue
        if (it.get("era") or "") not in era_ok:
            continue
        score = (it.get("ac") or 0) + (it.get("hp") or 0) / AC_TO_HP
        if score <= 0:
            continue
        for part in (it.get("slot") or "").lower().split():
            cur = best.get(part)
            if not cur or score > cur[0]:
                best[part] = (score, it)
    return best


def tab_gear(tab, app):
    """Where a mote is WORTH spending — which is not the same as where it buys most AC.

    🔴 Owner, 2026-08-23: "idk if i like this its tellling me to put motes in useless gear."
    Correct, and the first version deserved it. Ranking purely by gain-per-tier (10% of
    base AC) recommends whatever has the highest base, with no idea whether the piece is
    worth keeping. On his own loadout that meant a Hardened Bark Breastplate scoring 16.0
    against a 55.3 available for the same slot — motes poured into gear he will replace.

    So the tab now answers the question in the right order:
        1. which slots should you UPGRADE (a mote here is wasted)
        2. of what is left, where does a mote buy the most
    """
    head = card(tab)
    head.pack(fill="x", padx=10, pady=(10, 4))
    st = lab(head, "", F_SMALL, T2)
    st.pack(fill="x", padx=12, pady=(10, 2))
    warn = wrap(head, "", WARN)
    warn.pack(fill="x", padx=12, pady=(0, 11))
    body = ctk.CTkFrame(tab, fg_color="transparent")
    body.pack(fill="both", expand=True)

    def redraw():
        clear(body)
        # Re-resolve on every draw. /outputfile inventory is something the player runs
        # WHILE the app is open, so reading it once at startup guarantees a stale answer
        # exactly when it matters most.
        app.inventory_path = find_inventory()
        app.equipped = parse_equipped(app.inventory_path)
        inv = app.inventory_path
        if not inv:
            st.configure(text="no /outputfile inventory found")
            warn.configure(text="Type  /outputfile inventory  in game, then reopen this tab.")
            return
        age_d = (time.time() - os.path.getmtime(inv)) / 86400.0
        st.configure(text=f"{os.path.basename(inv)}  ·  {age_d:.0f} days old  ·  "
                          f"{' / '.join([c for c in MY_CLASSES if c])}  ·  "
                          f"comparing against Classic-era gear only")
        warn.configure(text=(f"THIS DUMP IS {age_d:.0f} DAYS OLD and almost certainly not your "
                             f"current gear. Re-run  /outputfile inventory  before trusting any "
                             f"of this.") if age_d > 1 else "")

        best = _best_for_slot()                       # confirmed Classic
        maybe = _best_for_slot(era_ok=("",))           # era unknown - unverified
        keepers, upgrades = [], []
        for e in app.equipped:
            if not e["known"]:
                continue
            b = e["base"]
            mine = (b.get("ac") or 0) + (b.get("hp") or 0) / AC_TO_HP
            cand = best.get(_slot_key(e["slot"]))
            if cand and loose_name(cand[1]["name"]) != loose_name(e["name"]) and cand[0] > mine * 1.25:
                upgrades.append((cand[0] - mine, e, cand))
            elif b.get("ac"):
                keepers.append((per_tier(b["ac"]), e, cand))
        upgrades.sort(key=lambda x: -x[0])
        keepers.sort(key=lambda x: -x[0])

        # ── 1. don't tier these ─────────────────────────────────────────────
        if upgrades:
            section(body, f"upgrade these first — a mote here is wasted ({len(upgrades)})")
            for gap, e, (bscore, bit) in upgrades[:14]:
                mine = bscore - gap
                c = card(body)
                c.pack(fill="x", padx=10, pady=2)
                line = ctk.CTkFrame(c, fg_color="transparent")
                line.pack(fill="x", padx=11, pady=(8, 0))
                lab(line, e["slot"].upper(), F_SMALL, T3, width=74).pack(side="left")
                lab(line, e["name"], F_BODY, T2).pack(side="left")
                lab(line, f"+{gap:.0f} available", F_MONO, WARN).pack(side="right")
                arrow = ctk.CTkFrame(c, fg_color="transparent")
                arrow.pack(fill="x", padx=11, pady=(2, 0))
                lab(arrow, "        get instead:", F_SMALL, T3).pack(side="left")
                lab(arrow, bit["name"], F_BODY, GOLD).pack(side="left", padx=6)
                tag, where = acquire_line(bit)
                lab(c, "        " + tag, F_SMALL,
                    OK if tag == "CONFIRMED DROP" else (
                        INFO if tag == "QUEST ITEM" else T3)).pack(fill="x", padx=11, pady=(2, 0))
                wrap(c, "        " + where, T2, width=560).pack(fill="x", padx=11, pady=(0, 4))
                # an unknown-era item may be better still, or may not exist on Legends yet.
                # Show it, never rank it, and say which it is.
                mb = maybe.get(_slot_key(e["slot"]))
                if mb and mb[0] > bscore * 1.1:
                    lab(c, f"        unverified era: {mb[1]['name']} would be higher "
                           f"(+{mb[0] - mine:.0f}) — may not be obtainable yet",
                        F_SMALL, WARN).pack(fill="x", padx=11, pady=(0, 9))
                else:
                    ctk.CTkFrame(c, fg_color="transparent", height=5).pack()

        # ── 2. now the motes ────────────────────────────────────────────────
        section(body, f"worth tiering — you are already at or near the best ({len(keepers)})")
        if not keepers:
            c = card(body)
            c.pack(fill="x", padx=10, pady=2)
            wrap(c, "Nothing here is worth a mote yet — every equipped slot has a clearly "
                    "better option above. Upgrade first, tier after. Spending motes now buys "
                    "AC on gear you are about to replace.", T2).pack(fill="x", padx=12, pady=10)
        else:
            top = keepers[0]
            hero = ctk.CTkFrame(body, fg_color=HOVER, corner_radius=10,
                                border_width=1, border_color=GOLD)
            hero.pack(fill="x", padx=10, pady=(2, 6))
            lab(hero, "NEXT MOTE GOES HERE", F_SMALL, GOLD).pack(fill="x", padx=14, pady=(11, 2))
            lab(hero, top[1]["name"], F_SUB, T1).pack(fill="x", padx=14)
            row = ctk.CTkFrame(hero, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(6, 12))
            lab(row, f"+{top[0]:.1f}", F_BIG, GOLD).pack(side="left")
            lab(row, f"  AC per tier  ·  {top[1]['slot']}  ·  currently +{top[1]['tier']}",
                F_BODY, T2).pack(side="left", padx=(8, 0))
            peak = top[0]
            for gain, e, cand in keepers:
                c = card(body)
                c.pack(fill="x", padx=10, pady=2)
                line = ctk.CTkFrame(c, fg_color="transparent")
                line.pack(fill="x", padx=11, pady=(8, 0))
                lab(line, e["slot"].upper(), F_SMALL, T3, width=74).pack(side="left")
                lab(line, e["name"], F_BODY, T1).pack(side="left")
                lab(line, f"+{gain:.1f}/tier", F_MONO, GOLD).pack(side="right")
                lab(c, f"        +{e['tier']}  ·  base {e['base'].get('ac', 0)} AC",
                    F_SMALL, T3).pack(fill="x", padx=11)
                h = ctk.CTkFrame(c, fg_color="transparent")
                h.pack(fill="x", padx=11, pady=(0, 6))
                bar(h, gain / peak, GOLD)
                # 🔴 Owner, 2026-08-23: even a keeper should name the BiS and say how to
                # get it. "At or near the best" is not the best, and a tier chart that
                # hides the target is telling you to settle without saying so.
                if cand:
                    bit = cand[1]
                    same = loose_name(bit["name"]) == loose_name(e["name"])
                    row2 = ctk.CTkFrame(c, fg_color=PANEL, corner_radius=7)
                    row2.pack(fill="x", padx=11, pady=(4, 9))
                    top2 = ctk.CTkFrame(row2, fg_color="transparent")
                    top2.pack(fill="x", padx=10, pady=(7, 0))
                    lab(top2, "BEST IN SLOT" if not same else "THIS IS BEST IN SLOT",
                        F_SMALL, OK if same else GOLD).pack(side="left")
                    if not same:
                        lab(top2, bit["name"], F_BODY, T1).pack(side="left", padx=8)
                        gapv = cand[0] - ((e["base"].get("ac") or 0)
                                          + (e["base"].get("hp") or 0) / AC_TO_HP)
                        lab(top2, f"+{gapv:.0f}", F_MONO, WARN).pack(side="right")
                    if not same:
                        tag, where = acquire_line(bit)
                        lab(row2, "   " + tag, F_SMALL,
                            OK if tag == "CONFIRMED DROP" else (
                                INFO if tag == "QUEST ITEM" else T3)).pack(fill="x", padx=10, pady=(3, 0))
                        wrap(row2, "   " + where, T2, width=560).pack(fill="x", padx=10, pady=(0, 8))
                        stats = []
                        if bit.get("ac"):   stats.append(f"AC {bit['ac']}")
                        if bit.get("hp"):   stats.append(f"HP {bit['hp']}")
                        if bit.get("mana"): stats.append(f"mana {bit['mana']}")
                        if bit.get("effect"): stats.append(bit["effect"][:44])
                        if stats:
                            lab(row2, "   " + "  ·  ".join(stats), F_SMALL, T3)                                .pack(fill="x", padx=10, pady=(0, 8))
                    else:
                        lab(row2, "   nothing in Classic beats it — tier away",
                            F_SMALL, T3).pack(fill="x", padx=10, pady=(2, 8))

        missing = [e for e in app.equipped if not e["known"]]
        if missing:
            section(body, f"{len(missing)} equipped items are not in our database")
            for e in missing:
                c = card(body)
                c.pack(fill="x", padx=10, pady=2)
                lab(c, e["display"], F_BODY, T1).pack(fill="x", padx=11, pady=(8, 0))
                lab(c, e["slot"] + "  ·  a gap in wiki_items, not in your gear",
                    F_SMALL, T3).pack(fill="x", padx=11, pady=(1, 9))

    app.redraw_gear = redraw


def tab_codex(tab, app):
    # 🔴 Owner, 2026-08-22: "you put a lot of potions in this codex did you mean too?"
    # No. The catalog was every item with a non-null effect, and potions have effects
    # because they are clickies with charges — but an exaltation is pulled OUT of
    # equipment, so a potion can never be a donor. `equip` is item_category in
    # (weapon, armor), which agrees exactly with slot-presence: 1,074 real donors,
    # 312 consumables. Defaults to donors only; the toggle shows the rest.
    state = {"kind": "ALL", "era": "Classic only", "mine": True, "q": "", "unk": True,
             "equip_only": True}
    ctrl = ctk.CTkFrame(tab, fg_color="transparent")
    ctrl.pack(fill="x", padx=10, pady=(10, 2))

    def mk_menu(parent, values, width):
        return ctk.CTkOptionMenu(parent, width=width, height=28, values=values,
                                 fg_color=HOVER, button_color=BORDER, button_hover_color=GOLD,
                                 text_color=T1, dropdown_fg_color=PANEL, dropdown_text_color=T1,
                                 dropdown_hover_color=BORDER, font=F_SMALL,
                                 dropdown_font=F_SMALL, corner_radius=7)

    kind_m = mk_menu(ctrl, ["ALL", "CLICK", "PROC", "WORN", "FOCUS?"], 104)
    kind_m.pack(side="left")
    era_m = mk_menu(ctrl, ERAS, 168)
    era_m.pack(side="left", padx=6)
    search = ctk.CTkEntry(ctrl, height=28, width=190, placeholder_text="search",
                          fg_color=HOVER, border_color=BORDER, text_color=T1,
                          font=F_SMALL, corner_radius=7)
    search.pack(side="left")
    ctrl2 = ctk.CTkFrame(tab, fg_color="transparent")
    ctrl2.pack(fill="x", padx=10, pady=(4, 0))
    mine_b = ctk.CTkCheckBox(ctrl2, text="usable by my classes", font=F_SMALL, text_color=T2,
                             fg_color=GOLD, hover_color=GOLD, checkmark_color=PANEL,
                             border_color=BORDER, checkbox_width=16, checkbox_height=16)
    mine_b.select()
    mine_b.pack(side="left")
    unk_b = ctk.CTkCheckBox(ctrl2, text="include unknown era", font=F_SMALL, text_color=T2,
                            fg_color=GOLD, hover_color=GOLD, checkmark_color=PANEL,
                            border_color=BORDER, checkbox_width=16, checkbox_height=16)
    unk_b.select()
    unk_b.pack(side="left", padx=10)
    eq_b = ctk.CTkCheckBox(ctrl2, text="equipment only (real donors)", font=F_SMALL, text_color=T2,
                           fg_color=GOLD, hover_color=GOLD, checkmark_color=PANEL,
                           border_color=BORDER, checkbox_width=16, checkbox_height=16)
    eq_b.select()
    eq_b.pack(side="left", padx=10)
    count = lab(tab, "", F_SMALL, T3)
    count.pack(fill="x", padx=12, pady=(6, 2))
    rule(tab)
    body = ctk.CTkFrame(tab, fg_color="transparent")
    body.pack(fill="both", expand=True)

    def usable(r):
        picked = [c for c in MY_CLASSES if c]
        cls = (r.get("classes") or "").upper()
        if not picked:
            return True
        if "ALL" in cls and "EXCEPT" not in cls:
            return True
        return any(c in cls for c in picked)

    def redraw(_=None):
        state.update(kind=kind_m.get(), era=era_m.get(), q=search.get(),
                     mine=bool(mine_b.get()), unk=bool(unk_b.get()),
                     equip_only=bool(eq_b.get()))
        rows = []
        for r in CATALOG:
            if state["equip_only"] and not r.get("equip", True):
                continue
            if state["kind"] != "ALL" and r["kind"] != state["kind"]:
                continue
            era = r.get("era") or ""
            # "All" means all — it must not be second-guessed by the unknown checkbox,
            # or the label lies about what it does.
            show_all = state["era"] == ERAS[3]
            if era == "":
                if not (state["unk"] or show_all):
                    continue
            elif not show_all and era not in ERA_ALLOWS[state["era"]]:
                continue
            if state["mine"] and not usable(r):
                continue
            q = state["q"].strip().lower()
            if q and q not in r["name"].lower() and q not in (r.get("effect") or "").lower():
                continue
            rows.append(r)
        clear(body)
        pool = sum(1 for r in CATALOG if r.get("equip", True)) if state["equip_only"] else len(CATALOG)
        count.configure(text=f"{len(rows):,} of {pool:,}  ·  showing "
                             f"{min(len(rows), 120)}  ·  "
                             f"{sum(1 for r in rows if r['sources']):,} with a known source")
        for r in rows[:120]:
            c = card(body)
            c.pack(fill="x", padx=10, pady=2)
            line = ctk.CTkFrame(c, fg_color="transparent")
            line.pack(fill="x", padx=11, pady=(8, 0))
            lab(line, r["name"], F_BODY, T1).pack(side="left")
            era = r.get("era") or ""
            lab(line, (era or "?") + ("." if r.get("era_how") == "zone" else ""),
                F_SMALL, ERA_COLOR.get(era, T3)).pack(side="right", padx=(8, 0))
            lab(line, r["kind"], F_SMALL, KIND_COLOR.get(r["kind"], T3)).pack(side="right")
            if not r.get("equip", True):
                lab(line, r.get("cat") or "not equipment", F_SMALL, T3).pack(side="right", padx=6)
            lab(c, r["effect"], F_SMALL, GOLD).pack(fill="x", padx=11, pady=(2, 0))
            if r["sources"]:
                for s in r["sources"][:2]:
                    lab(c, "   from " + s["npc"] + ("  ·  " + s["zone"] if s["zone"] else ""),
                        F_SMALL, T3).pack(fill="x", padx=11)
            else:
                lab(c, "   no drop source recorded — may be quest or craft",
                    F_SMALL, T3).pack(fill="x", padx=11)
            lab(c, r.get("classes") or "", F_SMALL, T3).pack(fill="x", padx=11, pady=(1, 9))

    for w in (kind_m, era_m, mine_b, unk_b, eq_b):
        w.configure(command=redraw)
    search.bind("<KeyRelease>", redraw)
    kind_m.set("ALL")
    era_m.set("Classic only")
    redraw()
    app.redraw_codex = redraw


def find_inventory():
    import glob
    cands = []
    try:
        sys.path.insert(0, DEVTOOL)
        from eq_logs import search_dirs
        roots = set()
        for d in search_dirs():
            roots.add(d)
            roots.add(os.path.dirname(d.rstrip("\\/")))
        for d in roots:
            cands += [p for p in glob.glob(os.path.join(d, "*.txt"))
                      if "inventory" in os.path.basename(p).lower()]
    except Exception:
        pass
    return max(cands, key=os.path.getmtime) if cands else None


def parse_equipped(path):
    if not path:
        return []
    try:
        sys.path.insert(0, APP_ROOT)
        from app.parsers.inventory_parser import parse_equipment
        with open(path, encoding="utf-8", errors="replace") as fh:
            rows = parse_equipment(fh.read())
    except Exception:
        return []
    out = []
    for r in rows:
        raw = r.get("name") or ""
        m = re.search(r"\s\+(\d+)\s*$", raw)
        base_name = re.sub(r"\s\+\d+\s*$", "", raw).strip()
        info = ITEMS.get(loose_name(base_name))
        out.append({"slot": r.get("slot") or "", "name": base_name, "display": raw,
                    "tier": int(m.group(1)) if m else 0,
                    "base": info, "known": info is not None})
    return out


def tab_combat(tab, app):
    """Combat readout with a browsable history and real per-actor detail.

    Owner, 2026-08-22: "doesnt have a history but it shows like the last mob. kinda plain
    no real details to it" and "it should have like melee damage x spell damage x and like
    hit chance ... how offten i hit sucessfully".

    So: the session list is CLICKABLE (pick any fight, it becomes the readout), and every
    actor row expands to melee/spell/dot, hit chance, crit rate, best and average hit, and
    what they actually swung and cast.
    """
    state = {"pick": None, "open": None}      # pick = index into session fights

    head = card(tab)
    head.pack(fill="x", padx=10, pady=(10, 4))
    st = lab(head, "starting...", F_BODY, T2)
    st.pack(fill="x", padx=12, pady=10)
    body = ctk.CTkFrame(tab, fg_color="transparent")
    body.pack(fill="both", expand=True)

    def detail_rows(r):
        """Every number we can defend, and nothing we cannot."""
        acc = "not measured" if r.get("accuracy") is None else f"{r['accuracy']*100:.0f}%"
        # 🔴 Owner, 2026-08-25: *"what happen to the dps showing me my break down like melee
        # damage from skill damage from proc damage from spell damage and such?"* and
        # *"also pet damage"*. It only ever showed melee/spell/dot -- three buckets for six
        # questions. dmg_melee silently contained special attacks and archery; dmg_spell
        # silently contained item procs. On his own log that hid the single biggest fact
        # about his damage: procs are ~20% of it and Smiting Strike alone is 44,226.
        # Zero rows are dropped rather than printed -- a category that did nothing this
        # fight is noise, but a category that did something must never be missing.
        parts = [("melee", r.get("melee", 0)), ("skill", r.get("skill", 0)),
                 ("ranged", r.get("ranged", 0)), ("proc", r.get("proc", 0)),
                 ("spell", r.get("spell", 0)), ("dot", r.get("dot", 0)),
                 ("pet", r.get("pet_damage", 0))]
        out = [(k, f"{v:,}") for k, v in parts if v]
        out += [("hit chance", acc), ("crit", f"{r.get('crit_rate',0)*100:.0f}%"),
                ("swings", f"{r['hits'] + r['misses']:,}")]
        out += [("best hit", f"{r.get('best_hit',0):,}"), ("avg hit", f"{r.get('avg_hit',0):.0f}"),
                ("taken", f"{r.get('taken',0):,}")]
        if r.get("heal_effective"):
            # heal_others, never heal_effective — the raw total includes lifetap
            # self-sustain and makes a DPS read as a healer (91% of it, measured).
            out += [("healed others", f"{r.get('heal_others', 0):,}"),
                    ("self-sustain", f"{r.get('heal_self', 0):,}"),
                    ("overheal", f"{r.get('overheal', 0):,}")]
        return out

    def redraw():
        lc = app.tail.lc if app.tail else None
        if not lc:
            return
        live = bool(app.tail and app.tail.live_seen)
        sess = lc.session(limit=40)
        fights = sess["fights"]
        st.configure(
            text=f"{app.tail.zone or 'unknown zone'}   ·   {sess['count']} fights   ·   "
                 f"{sess['kills']} kills   ·   best {sess['best_dps']:,} dps"
                 + ("" if live else "   ·   history only, waiting for a live line"),
            text_color=T1)

        # which fight are we showing? a pinned pick, else the live one, else the last kill
        show, pinned = None, state["pick"]
        if pinned is not None and pinned < len(lc.fights):
            show = lc._shape(list(reversed(lc.fights))[pinned])
        if show is None:
            show = (lc.current() if live else None) or lc.last_kill()

        clear(body)
        if not show:
            lab(body, "no fight yet — go hit something", F_BODY, T3).pack(padx=12, pady=12)
            return

        hero = card(body)
        hero.pack(fill="x", padx=10, pady=(6, 4))
        tagrow = ctk.CTkFrame(hero, fg_color="transparent")
        tagrow.pack(fill="x", padx=12, pady=(10, 1))
        tag = "PINNED" if pinned is not None else ("IN COMBAT" if live and lc.current() else "LAST KILL")
        lab(tagrow, tag, F_SMALL, WARN if pinned is not None else (OK if tag == "IN COMBAT" else GOLD))\
            .pack(side="left")
        if pinned is not None:
            b = ctk.CTkButton(tagrow, text="back to live", width=86, height=20, corner_radius=6,
                              fg_color=PANEL, hover_color=BORDER, text_color=T2, font=F_SMALL,
                              command=lambda: (state.update(pick=None), redraw()))
            b.pack(side="right")
        lab(hero, show["mob"], F_SUB, T1).pack(fill="x", padx=12)
        lab(hero, f"{show['duration']:.0f}s  ·  {show['total']:,} damage  ·  "
                  f"{show['raid_dps']:,} raid dps", F_SMALL, T3).pack(fill="x", padx=12, pady=(1, 11))

        section(body, "who helped kill it — click a name for detail")
        peak = max((r["damage"] for r in show["rows"]), default=1)
        for i, r in enumerate(show["rows"]):
            c = card(body)
            c.pack(fill="x", padx=10, pady=2)
            opened = state["open"] == i

            def toggle(idx=i):
                state["open"] = None if state["open"] == idx else idx
                redraw()

            line = ctk.CTkFrame(c, fg_color="transparent")
            line.pack(fill="x", padx=11, pady=(8, 0))
            col = GOLD if r["is_me"] else T1
            nm = lab(line, r["name"], F_BODY, col)
            nm.pack(side="left")
            lab(line, f"{r['encounter_dps']:,} dps", F_MONO, col).pack(side="right")
            for w in (c, line, nm):
                w.bind("<Button-1>", lambda e, f=toggle: f())
            sub = f"{r['damage']:,} dmg · {r['share']*100:.0f}%"
            if r["pet_damage"]:
                sub += f"   (own {r['own_damage']:,} + pet {r['pet_damage']:,})"
            if r.get("heal_others"):
                sub += f"   ·  healed {r['heal_others']:,}"
            if r.get("heal_self"):
                sub += f"   ·  self {r['heal_self']:,}"
            lab(c, sub, F_SMALL, T3).pack(fill="x", padx=11)
            h = ctk.CTkFrame(c, fg_color="transparent")
            h.pack(fill="x", padx=11, pady=(0, 9))
            bar(h, r["damage"] / peak, col)

            # 🔴 Owner, 2026-08-25: *"would you make the pet a but smaller and kinda teired
            # off under me in the dps window?"*
            # A pet is not a peer combatant -- its damage is ALREADY inside the owner's
            # total (see _owner_of: charmed-pet damage is the charmer's, 633,828 of it was
            # going uncredited). Rendering it as its own top-level row would double-count it
            # to the eye even though the arithmetic is right. So: indented, smaller type,
            # thinner bar, and scaled against the OWNER's damage rather than the encounter
            # peak -- the useful question is "how much of my damage is the pet", not "how
            # does my pet rank against the raid".
            for p in (r.get("pets") or []):
                pc = ctk.CTkFrame(c, fg_color="transparent")
                pc.pack(fill="x", padx=(30, 11), pady=(0, 5))
                pl = ctk.CTkFrame(pc, fg_color="transparent")
                pl.pack(fill="x")
                lab(pl, "└ " + pet_label(p["name"]), F_SMALL, T3).pack(side="left")
                share = (p["damage"] / r["damage"] * 100) if r.get("damage") else 0
                lab(pl, f"{p['damage']:,}   {share:.0f}% of yours"
                    if r.get("is_me") else f"{p['damage']:,}   {share:.0f}%",
                    F_SMALL, T3).pack(side="right")
                lab(pc, "charmed" if p["charmed"] else "summoned", F_SMALL, T3)                    .pack(fill="x")
                pb = ctk.CTkFrame(pc, fg_color="transparent")
                pb.pack(fill="x", pady=(2, 0))
                bar(pb, (p["damage"] / r["damage"]) if r.get("damage") else 0, T3, height=3)

            if opened:
                grid = ctk.CTkFrame(c, fg_color=PANEL, corner_radius=7)
                grid.pack(fill="x", padx=11, pady=(0, 9))
                cells = detail_rows(r)
                for n, (k, v) in enumerate(cells):
                    cell = ctk.CTkFrame(grid, fg_color="transparent")
                    cell.grid(row=n // 3, column=n % 3, sticky="w", padx=10, pady=5)
                    lab(cell, v, F_MONO, T1).pack(anchor="w")
                    lab(cell, k.upper(), F_SMALL, T3).pack(anchor="w")
                for cnum in range(3):
                    grid.grid_columnconfigure(cnum, weight=1, uniform="d")
                if r.get("verbs"):
                    lab(c, "attacks:  " + ",  ".join(f"{v} x{n}" for v, n in r["verbs"][:6]),
                        F_SMALL, T2).pack(fill="x", padx=11, pady=(0, 3))
                if r.get("spells"):
                    lab(c, "spells:   " + ",  ".join(f"{v} x{n}" for v, n in r["spells"][:6]),
                        F_SMALL, T2).pack(fill="x", padx=11, pady=(0, 9))

        # ── browsable history ────────────────────────────────────────────────
        section(body, f"history — {len(fights)} fights, click to pin one")
        for idx, f in enumerate(fights[:30]):
            c = ctk.CTkFrame(body, fg_color=HOVER if idx != pinned else "#2A2415",
                             corner_radius=8)
            c.pack(fill="x", padx=10, pady=1)
            row = ctk.CTkFrame(c, fg_color="transparent")
            row.pack(fill="x", padx=11, pady=6)
            col = T1 if f["killed"] else T3

            def pin(ix=idx):
                state.update(pick=None if state["pick"] == ix else ix, open=None)
                redraw()

            a = lab(row, f["mob"], F_SMALL, col)
            a.pack(side="left")
            b_ = lab(row, f"{f['my_dps']:,} dps", F_MONO, col)
            b_.pack(side="right")
            d = lab(row, f"{f['duration']:.0f}s", F_SMALL, T3)
            d.pack(side="right", padx=10)
            if not f["killed"]:
                lab(row, "no kill", F_SMALL, T3).pack(side="right", padx=6)
            for w in (c, row, a, b_, d):
                w.bind("<Button-1>", lambda e, fn=pin: fn())

    app.redraw_combat = redraw
