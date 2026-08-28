"""Combat tab — live DPS, the last kill, and who helped.

Added 2026-08-22, completing the merge the owner asked for: the DPS parser lands in the
app rather than staying a separate tool. `app/parsers/combat_parser.py` arrived
first and sat unused; this is what consumes it.

HOW IT IS FED
    `LogWatcher.on_any_line` already delivers every raw line to any number of callbacks,
    and main.py wires one to `feed_line()` below. Nothing new tails the log — there is
    exactly one reader in the app and this rides it.

WHAT IT SHOWS THAT OTHER PARSERS DO NOT
    * charmed-pet damage credited to the CHARMER (633,828 damage was going to nobody)
    * membership by attacked-the-target, so a real player is not classified hostile
      because your own charmed pet clipped them
    * both DPS definitions side by side — encounter and active — rather than silently
      picking one and being wrong for half the audience

⚠ NEVER blocks the UI thread and never raises into it. A parser fault must degrade this
tab, not take the app down: every entry point is guarded.
"""
from __future__ import annotations

import logging

import customtkinter as ctk

from app.ui import theme

log = logging.getLogger(__name__)

# The main window is read and interacted with, not watched — it refreshes slowly and
# only when its data changed. A live HUD is the overlay's job, not this tab's.
REFRESH_MS = 4000


class CombatView(ctk.CTkFrame):
    def __init__(self, master, app=None):
        super().__init__(master, fg_color=theme.BG, corner_radius=0)
        self._app = app
        self._lc = None
        self._ts = None
        self._parse_ts = None
        self._live_seen = False
        self._ready = False
        self._last_sig = None
        self._shared = None

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.pack(fill="both", expand=True, padx=6, pady=6)

        self._head = ctk.CTkFrame(self._scroll, fg_color=theme.PANEL_HOVER, corner_radius=9)
        self._head.pack(fill="x", padx=6, pady=(4, 6))
        self._status = ctk.CTkLabel(
            self._head, text="starting the combat parser...", font=theme.FONT_BODY,
            text_color=theme.TEXT_SECONDARY, anchor="w", justify="left")
        self._status.pack(fill="x", padx=12, pady=10)

        self._body = ctk.CTkFrame(self._scroll, fg_color="transparent")
        self._body.pack(fill="both", expand=True)

        self._boot()
        self.after(REFRESH_MS, self._tick)

    # ── wiring ──────────────────────────────────────────────────────────────
    def _boot(self):
        try:
            from app.parsers.combat_parser import (LiveCombat, TS, parse_ts,
                                                    set_player_name, player_name_from_log)
            # 🔴 Teach the parser which character IS "You" before a single line lands.
            # EQ names the player outright in some lines ("You healed Morbid for 42 hit
            # points by Lifedraw"), and without this that reads as healing a DIFFERENT
            # actor: it splits the player in two AND books lifetap self-sustain as group
            # healing. Measured on a 12 MB slice — 91% of "healing" was self-sustain.
            try:
                lw = getattr(self._app, "log_watcher", None)
                path = lw.log_path() if lw is not None else ""
                who = player_name_from_log(path or "")
                if who:
                    set_player_name(who)
            except Exception:
                log.debug("player name not resolved", exc_info=True)
            # Prefer the app-wide CombatFeed if it exists: the Tools tabs (Healing, Loot)
            # read the same object, and two parsers over one log would drift apart and
            # report different numbers for the same fight. Fall back to a private parser
            # only if the shared one could not be built.
            shared = getattr(self._app, "combat_feed", None)
            if shared is not None and getattr(shared, "lc", None) is not None:
                self._lc = shared.lc
                self._shared = shared
            else:
                self._lc = LiveCombat()
                self._shared = None
            self._ts, self._parse_ts = TS, parse_ts
            self._ready = True
            self._status.configure(text="watching for combat")
        except Exception:
            log.exception("combat parser unavailable")
            self._status.configure(text="combat parser unavailable — see the log",
                                   text_color=theme.DANGER)

    def feed_line(self, line: str):
        """Called from LogWatcher's thread. Parse only; never touch widgets here."""
        if not self._ready:
            return
        try:
            # When the shared feed owns the parser it has ALREADY consumed this line in
            # main.py. Feeding it again here would double every hit and double the DPS.
            if self._shared is not None:
                self._live_seen = bool(getattr(self._shared, "live_seen", False))
                return
            m = self._ts.match(line or "")
            if m:
                self._lc.feed(m.group("body"), self._parse_ts(m.group("ts")))
                self._live_seen = True
        except Exception:
            log.exception("combat feed")

    # ── render ──────────────────────────────────────────────────────────────
    def _signature(self):
        """Cheap fingerprint of what _render draws from."""
        lc = self._lc
        if lc is None:
            return None
        cur = getattr(lc, "attacking", None)
        return (len(lc.fights), self._live_seen,
                round(cur.end, 1) if cur is not None else 0,
                round(sum(a.damage for a in cur.actors.values()), 0) if cur is not None else 0)

    def _tick(self):
        """Redraw only when the data moved, and never while we are in the background.

        🔴 Owner, 2026-08-23, about an earlier build carrying this same view: "the front
        page of the app keeps refreshing so much i cant move the window or anything the
        only thing that should refresh that often is the layover live view."

        `_render` destroys and rebuilds the entire widget tree. Doing that every 1.5s
        makes the window hard to drag or scroll and burns CPU redrawing tabs sitting
        behind EverQuest. Both gates below are cheap; the overlay is unaffected.
        """
        try:
            sig = self._signature()
            if sig != self._last_sig and self._visible():
                self._last_sig = sig
                self._render()
        except Exception:
            log.exception("combat render")
        self.after(REFRESH_MS, self._tick)

    def _visible(self) -> bool:
        try:
            return bool(self.winfo_ismapped()) and self.winfo_toplevel().focus_displayof() is not None
        except Exception:
            return True

    def _clear(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _card(self, parent):
        c = ctk.CTkFrame(parent, fg_color=theme.PANEL_HOVER, corner_radius=9)
        c.pack(fill="x", padx=6, pady=2)
        return c

    def _lab(self, parent, text, font=None, color=None, **kw):
        return ctk.CTkLabel(parent, text=text, font=font or theme.FONT_BODY,
                            text_color=color or theme.TEXT_PRIMARY,
                            anchor="w", justify="left", **kw)

    def _bar(self, parent, frac, color):
        t = ctk.CTkFrame(parent, fg_color=theme.PANEL, corner_radius=3, height=5)
        t.pack(fill="x", pady=(4, 0))
        t.pack_propagate(False)
        f = ctk.CTkFrame(t, fg_color=color, corner_radius=3)
        f.place(relx=0, rely=0, relwidth=max(0.01, min(1.0, frac)), relheight=1)

    def _render(self):
        if not self._ready:
            return
        lc = self._lc
        sess = lc.session(limit=12)
        # ⚠ Only claim a live fight once a line has actually arrived. Without this the
        # parser's "current fight" is whatever it last saw, which reads as IN COMBAT
        # forever after the fighting stops.
        cur = lc.current() if self._live_seen else None
        kill = lc.last_kill()
        self._status.configure(
            text=(f"{sess['zone'] or 'unknown zone'}   ·   {sess['count']} fights   ·   "
                  f"{sess['kills']} kills   ·   best {sess['best_dps']:,} dps"),
            text_color=theme.TEXT_PRIMARY)

        show = cur or kill
        self._clear()
        if not show:
            self._lab(self._body, "no combat yet", color=theme.TEXT_MUTED)\
                .pack(padx=12, pady=12)
            return

        hero = self._card(self._body)
        self._lab(hero, "IN COMBAT" if cur else "LAST KILL", theme.FONT_BODY_SMALL,
                  theme.GREEN if cur else theme.GOLD).pack(fill="x", padx=12, pady=(9, 1))
        self._lab(hero, show["mob"], theme.FONT_SUBHEADER).pack(fill="x", padx=12)
        self._lab(hero, f"{show['duration']:.0f}s  ·  {show['total']:,} damage  ·  "
                        f"{show['raid_dps']:,} raid dps",
                  theme.FONT_BODY_SMALL, theme.TEXT_MUTED).pack(fill="x", padx=12, pady=(1, 10))

        self._lab(self._body, "WHO IS ON IT" if cur else "WHO HELPED KILL IT",
                  theme.FONT_BODY_SMALL, theme.TEXT_MUTED).pack(fill="x", padx=8, pady=(10, 3))
        peak = max((r["damage"] for r in show["rows"]), default=1)
        for r in show["rows"]:
            c = self._card(self._body)
            line = ctk.CTkFrame(c, fg_color="transparent")
            line.pack(fill="x", padx=11, pady=(8, 0))
            col = theme.GOLD if r["is_me"] else theme.TEXT_PRIMARY
            self._lab(line, r["name"], color=col).pack(side="left")
            self._lab(line, f"{r['encounter_dps']:,} dps", theme.FONT_MONO, col).pack(side="right")
            sub = f"{r['damage']:,} dmg · {r['share']*100:.0f}%"
            if r["pet_damage"]:
                sub += f"   (own {r['own_damage']:,} + pet {r['pet_damage']:,})"
            if r.get("heal_others"):
                sub += f"   ·  healed {r['heal_others']:,}"
            if r.get("heal_self"):
                sub += f"   ·  self {r['heal_self']:,}"
            self._lab(c, sub, theme.FONT_BODY_SMALL, theme.TEXT_MUTED).pack(fill="x", padx=11)
            holder = ctk.CTkFrame(c, fg_color="transparent")
            holder.pack(fill="x", padx=11, pady=(0, 9))
            self._bar(holder, r["damage"] / peak, col)
            for p in r["pets"]:
                self._lab(c, f"     {p['name']}   {p['damage']:,}"
                             f"{'  charmed' if p['charmed'] else '  summoned'}",
                          theme.FONT_BODY_SMALL, theme.TEXT_MUTED)\
                    .pack(fill="x", padx=11, pady=(0, 6))

        if sess["fights"]:
            self._lab(self._body, "SESSION", theme.FONT_BODY_SMALL, theme.TEXT_MUTED)\
                .pack(fill="x", padx=8, pady=(12, 3))
            for f in sess["fights"][:10]:
                c = self._card(self._body)
                row = ctk.CTkFrame(c, fg_color="transparent")
                row.pack(fill="x", padx=11, pady=6)
                col = theme.TEXT_PRIMARY if f["killed"] else theme.TEXT_MUTED
                self._lab(row, f["mob"], theme.FONT_BODY_SMALL, col).pack(side="left")
                self._lab(row, f"{f['my_dps']:,} dps", theme.FONT_MONO, col).pack(side="right")
                if not f["killed"]:
                    self._lab(row, "no kill", theme.FONT_BODY_SMALL, theme.TEXT_MUTED)\
                        .pack(side="right", padx=8)
