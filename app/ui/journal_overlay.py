"""
Gnoll Guard overlay — minimal always-on-top HUD for your Quest Journal.

ONE view: your journaled quests as a live checklist. Each required item ticks off
(○ needed → ✓ looted → ✔ turned in) as you play, driven by the log watcher via
main.py callbacks. Quests for the zone you're in float to the top; other known
quests in that zone show at the bottom (web /api/quests/by-zone).

Rebuilt 2026-07-09 minimal: dropped the tab-view, the "Popular" tab (that's the
website's job), and the per-frame click-through poll — the fragile Windows
ex-style toggling that could flicker/vanish the window (and is the same family as
the old shell-freeze). Kept: always-on-top, opacity, drag-by-header, save-position.
No popups; this window IS the overlay.

Public API used by main.py / MainWindow (unchanged): refresh_journal(), update_zone(zone).
"""

import logging
import os
import sys
import threading
import urllib.parse

import customtkinter as ctk
from PIL import Image

from app.ui import theme

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

log = logging.getLogger(__name__)

API_BASE = "https://gnollguard.com"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


def _asset(*parts) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", *parts)


class JournalOverlay(ctk.CTkToplevel):
    """Detached always-on-top quest HUD, toggled from Settings → 'Enable Overlay Window'.
    A child of the main window; closing it just flips overlay_enabled off (the main app
    keeps running). One scrollable view — no tabs, no click-through timer."""

    def __init__(self, master, app_state):
        super().__init__(master, fg_color=theme.BG)
        self._app = app_state
        self.title("Gnoll Guard")

        self._current_zone = None
        self._quests: list = []        # the player's journaled quests
        self._zone_quests: list = []   # other known quests in the current zone
        self._rows: list = []          # rendered widgets (cleared on each render)

        # ── placement: spawn under the cursor (the active monitor), clamped to the
        # virtual desktop so a saved off-screen spot can never hide it. ──────────
        # Fixed LOGICAL open size — never restore a saved WxH. CustomTkinter multiplies
        # geometry() by the display's DPI scaling, while winfo_width() returns the ALREADY
        # scaled pixels; saving that and feeding it back re-scales every launch
        # (360→720→1080→3240…), which balloons the window on HiDPI. A fixed size breaks it.
        w, h = 360, 540
        self.minsize(300, 380)
        vx, vy = self.winfo_vrootx(), self.winfo_vrooty()
        vw, vh = self.winfo_vrootwidth(), self.winfo_vrootheight()
        px, py = self.winfo_pointerx(), self.winfo_pointery()
        gx = min(max(px - w // 2, vx), vx + vw - w)
        gy = min(max(py - 30, vy), vy + vh - h)
        self.geometry(f"{w}x{h}+{gx}+{gy}")

        try:
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.attributes("-alpha", float(self._app.config.get("overlay_opacity", 0.92)))
            if bool(self._app.config.get("overlay_borderless", False)):
                self.overrideredirect(True)   # frameless HUD; drag via the header
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        ico = _asset("icon.ico")
        try:
            if os.path.isfile(ico):
                self.iconbitmap(ico)
                self.after(300, lambda: self._safe(lambda: self.iconbitmap(ico)))
        except Exception:
            pass

        self._build()
        self.refresh_journal()
        self.after(200, self._surface)

    # ── small helpers ───────────────────────────────────────────────────────────
    def _safe(self, fn):
        try:
            fn()
        except Exception:
            pass

    def _surface(self):
        """Raise above the (possibly fullscreen) game; blink -topmost so Windows obeys."""
        def _do():
            self.deiconify()
            self.lift()
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.after(400, lambda: self._safe(lambda: self.attributes("-topmost", True)))
        self._safe(_do)

    def _save_geometry(self):
        # Persist POSITION only — never the size. Saving the DPI-scaled winfo size and
        # restoring it via geometry() compounds the scale each launch (see __init__).
        def _do():
            win = self._app.config.setdefault("window", {})
            win["overlay_x"], win["overlay_y"] = self.winfo_x(), self.winfo_y()
            self._app.save_config()
        self._safe(_do)

    # ── drag (via header) + resize (via footer grip) ─────────────────────────────
    def _drag_start(self, e):
        self._drag_off = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def _drag_move(self, e):
        off = getattr(self, "_drag_off", None)
        if off:
            self.geometry(f"+{e.x_root - off[0]}+{e.y_root - off[1]}")

    def _resize_start(self, e):
        self._resize_off = (e.x_root, e.y_root, self.winfo_width(), self.winfo_height())

    def _resize_move(self, e):
        o = getattr(self, "_resize_off", None)
        if not o:
            return
        sx, sy, sw, sh = o
        self.geometry(f"{max(300, sw + e.x_root - sx)}x{max(380, sh + e.y_root - sy)}")

    # ── layout ───────────────────────────────────────────────────────────────────
    def _build(self):
        # Header — title + refresh/close, and the drag grab-zone.
        hdr = ctk.CTkFrame(self, fg_color=theme.PANEL, height=38, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        try:
            img = ctk.CTkImage(Image.open(_asset("icon.png")), size=(18, 18))
            ctk.CTkLabel(hdr, image=img, text="").pack(side="left", padx=(theme.PAD, 4))
        except Exception:
            pass
        title = ctk.CTkLabel(hdr, text="Quest Journal", font=theme.FONT_SUBHEADER,
                             text_color=theme.GOLD)
        title.pack(side="left")
        for grab in (hdr, title):                      # drag from the header or its title
            grab.bind("<Button-1>", self._drag_start)
            grab.bind("<B1-Motion>", self._drag_move)
        ctk.CTkButton(hdr, text="✕", width=28, height=26, fg_color="transparent",
                      text_color=theme.TEXT_MUTED, hover_color=theme.DANGER,
                      font=theme.FONT_BODY, command=self._on_close).pack(side="right", padx=(0, 4))
        ctk.CTkButton(hdr, text="⟳", width=28, height=26, fg_color="transparent",
                      text_color=theme.TEXT_SECONDARY, hover_color=theme.PANEL_HOVER,
                      font=theme.FONT_BODY, command=self.refresh_journal).pack(side="right")

        # Zone indicator (updated by update_zone).
        self._zone_lbl = ctk.CTkLabel(self, text="", font=theme.FONT_BODY_SMALL,
                                      text_color=theme.TEXT_MUTED, anchor="w")
        self._zone_lbl.pack(fill="x", padx=theme.PAD, pady=(4, 0))

        # Body — the one scrollable quest list.
        self._scroll = ctk.CTkScrollableFrame(self, fg_color=theme.BG)
        self._scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(4, 0))

        # Footer — log-watcher light + status + resize grip.
        foot = ctk.CTkFrame(self, fg_color=theme.PANEL, height=24, corner_radius=0)
        foot.pack(fill="x")
        foot.pack_propagate(False)
        self._log_light = ctk.CTkLabel(foot, text="●", font=theme.FONT_BODY_SMALL,
                                       text_color=theme.STATUS_LOG_DISCONNECTED)
        self._log_light.pack(side="left", padx=(theme.PAD, 4))
        self._status_lbl = ctk.CTkLabel(foot, text="starting…", font=theme.FONT_BODY_SMALL,
                                        text_color=theme.TEXT_MUTED)
        self._status_lbl.pack(side="left")
        grip = ctk.CTkLabel(foot, text="◢", font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED)
        grip.pack(side="right", padx=theme.PAD)
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

    # ── data: journal ─────────────────────────────────────────────────────────
    def refresh_journal(self):
        """(Public — called by main.py on loot/turn-in and by the ⟳ button.)
        Reloads the player's journaled quests from Supabase and re-renders."""
        if not self._app.auth.is_logged_in:
            self._quests = []
            self._render(msg="Log in with Discord in the main window to use your Quest Journal.\n\n"
                             "Add quests at gnollguard.com/quests → “Add to Journal.”")
            return
        self._render(msg="Loading your journal…")

        def load():
            try:
                quests = self._app.supabase.get_journal() or []
            except Exception:
                log.debug("journal load failed", exc_info=True)
                quests = []
            self.after(0, lambda: self._set_quests(quests))
        threading.Thread(target=load, daemon=True).start()

    def _set_quests(self, quests):
        self._quests = quests
        # Keep the app-wide quest index in sync so loot ticks items off.
        try:
            from app import quest_progress
            self._app._journal_quests = quests
            self._app._quest_item_index = quest_progress.build_index(quests)
            self._app.quest_matcher.set_quests(quests)
        except Exception:
            pass
        self._render()

    # ── data: zone ──────────────────────────────────────────────────────────────
    def update_zone(self, zone: str):
        """(Public — called by main.py when the player enters a zone.)
        Floats matching journaled quests to the top and lists other known quests here."""
        self._current_zone = zone
        self._zone_lbl.configure(text=(f"📍 {zone}" if zone else ""))
        if not zone:
            self._zone_quests = []
            self._render()
            return

        def load():
            quests = []
            if requests is not None:
                try:
                    r = requests.get(API_BASE + "/api/quests/by-zone?zone=" + urllib.parse.quote(zone),
                                     timeout=12)
                    if r.ok:
                        quests = r.json().get("quests", [])
                except Exception:
                    log.debug("zone quest fetch failed", exc_info=True)
            self.after(0, lambda: self._set_zone_quests(quests))
        threading.Thread(target=load, daemon=True).start()

    def _set_zone_quests(self, quests):
        self._zone_quests = quests
        self._render()

    # ── render (the single view) ─────────────────────────────────────────────
    def _clear(self):
        for w in self._rows:
            self._safe(w.destroy)
        self._rows = []

    def _msg(self, text):
        lbl = ctk.CTkLabel(self._scroll, text=text, justify="left", wraplength=410,
                           font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY)
        lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
        self._rows.append(lbl)

    def _render(self, msg=None):
        self._clear()
        if msg:
            self._msg(msg)
            return
        zone = (self._current_zone or "").lower()
        if not self._quests:
            self._msg("No quests in your journal yet.\n\nBrowse gnollguard.com/quests and click "
                      "“Add to Journal” — they'll appear here and tick off as you play.")
        else:
            # zone-relevant journaled quests first
            ordered = sorted(self._quests,
                             key=lambda q: 0 if (zone and (q.get("zone") or "").lower() == zone) else 1)
            for q in ordered:
                self._render_quest(q, here=bool(zone) and (q.get("zone") or "").lower() == zone)
        # other known quests in this zone that aren't already journaled
        if self._current_zone and self._zone_quests:
            have = {q.get("id") for q in self._quests}
            extra = [q for q in self._zone_quests if q.get("id") not in have]
            if extra:
                hdr = ctk.CTkLabel(self._scroll, text=f"— More quests in {self._current_zone} —",
                                   font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED)
                hdr.pack(anchor="w", padx=theme.PAD, pady=(theme.PAD, 2))
                self._rows.append(hdr)
                for q in extra[:20]:
                    self._render_zone_row(q)

    def _render_quest(self, q, here=False):
        card = ctk.CTkFrame(self._scroll, fg_color=theme.PANEL, corner_radius=8,
                            border_width=(1 if here else 0), border_color=theme.GOLD)
        card.pack(fill="x", padx=theme.PAD, pady=4)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=theme.PAD, pady=(theme.PAD_SM, 0))
        ctk.CTkLabel(top, text=q.get("quest_name", "Quest"), font=theme.FONT_SUBHEADER,
                     text_color=theme.GOLD, anchor="w").pack(side="left")
        matcher = getattr(self._app, "quest_matcher", None)
        structured = [s for s in (q.get("steps") or []) if s.get("action_type")]
        if structured and matcher:
            done_n, total_n = matcher.progress(q)
            ctk.CTkLabel(top, text=f"  {done_n}/{total_n}", font=theme.FONT_BODY_SMALL,
                         text_color=theme.TEXT_SECONDARY).pack(side="left")
        ctk.CTkButton(top, text="🗑", width=26, height=22, fg_color="transparent",
                      text_color=theme.TEXT_MUTED, hover_color=theme.DANGER,
                      font=theme.FONT_BODY_SMALL,
                      command=lambda qq=q: self._delete_quest(qq)).pack(side="right")
        if q.get("zone"):
            ctk.CTkLabel(card, text=q["zone"], font=theme.FONT_BODY_SMALL,
                         text_color=theme.TEXT_SECONDARY, anchor="w").pack(anchor="w", padx=theme.PAD)
        prog = getattr(self._app, "_quest_progress", set())
        given = getattr(self._app, "_quest_given", set())
        for s in sorted(q.get("steps", []) or [], key=lambda s: s.get("step_order", 0)):
            num = s.get("step_order", "")
            is_done = bool(matcher and s.get("action_type")
                            and matcher.is_step_done(q.get("id"), num))
            if s.get("instruction"):
                mark = "✓ " if is_done else ""
                col = theme.GREEN if is_done else theme.TEXT_PRIMARY
                ctk.CTkLabel(card, text=f"{mark}{num}. {s['instruction']}", font=theme.FONT_BODY_SMALL,
                             text_color=col, anchor="w", justify="left",
                             wraplength=380).pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, 0))
            for it in (s.get("required_items") or []):
                low = it.lower()
                if low in given:
                    mark, col = "✔", theme.GREEN
                elif low in prog:
                    mark, col = "✓", theme.GOLD
                else:
                    mark, col = "○", theme.TEXT_SECONDARY
                ctk.CTkLabel(card, text=f"     {mark} {it}", font=theme.FONT_BODY_SMALL,
                             text_color=col, anchor="w").pack(anchor="w", padx=theme.PAD)
        if q.get("reward_items"):
            ctk.CTkLabel(card, text="Reward: " + ", ".join(q["reward_items"]),
                         font=theme.FONT_BODY_SMALL, text_color=theme.GOLD, anchor="w"
                         ).pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, theme.PAD_SM))
        else:
            ctk.CTkFrame(card, fg_color="transparent", height=theme.PAD_SM).pack()
        self._rows.append(card)

    def _render_zone_row(self, q):
        row = ctk.CTkFrame(self._scroll, fg_color=theme.PANEL, corner_radius=6)
        row.pack(fill="x", padx=theme.PAD, pady=2)
        ctk.CTkLabel(row, text=q.get("quest_name", "Quest"), font=theme.FONT_BODY_SMALL,
                     text_color=theme.TEXT_PRIMARY, anchor="w").pack(anchor="w", padx=theme.PAD, pady=(4, 0))
        giver = q.get("quest_giver_npc") or ""
        if giver:
            ctk.CTkLabel(row, text=f"   {giver}", font=theme.FONT_BODY_SMALL,
                         text_color=theme.TEXT_MUTED, anchor="w").pack(anchor="w", padx=theme.PAD, pady=(0, 4))
        self._rows.append(row)

    def _delete_quest(self, q):
        import tkinter.messagebox as _mb
        if not _mb.askyesno("Remove quest",
                            f"Remove “{q.get('quest_name', 'this quest')}” from your journal?",
                            parent=self):
            return
        qid = q.get("id")
        self._quests = [jq for jq in self._quests if jq.get("id") != qid]
        try:
            from app import quest_progress
            self._app._journal_quests = self._quests
            self._app._quest_item_index = quest_progress.build_index(self._quests)
            self._app.quest_matcher.set_quests(self._quests)
        except Exception:
            pass
        threading.Thread(target=lambda: self._safe(lambda: self._app.supabase.remove_quest(qid)),
                         daemon=True).start()
        self._render()

    # ── status hooks (safe if MainWindow forwards them; harmless otherwise) ─────
    def update_watcher_status(self, status: str):
        try:
            if status.startswith("watching"):
                c = theme.STATUS_LOG_WATCHING
            elif status.startswith("reading"):
                c = theme.STATUS_LOG_READING
            else:
                c = theme.STATUS_LOG_DISCONNECTED
            self._log_light.configure(text_color=c)
            self._status_lbl.configure(text=status)
        except Exception:
            pass

    def _refresh_auth_header(self):
        """Called on login/logout — just reload the journal for the new auth state."""
        self.refresh_journal()

    def update_sync_status(self, text: str):
        pass  # sync status is shown in the main window

    def show_update_banner(self, *a, **k):
        pass  # update banner lives in the main window

    def refresh_popular(self, *a, **k):
        pass  # legacy no-op — the Popular tab was removed in the minimal rebuild

    def _on_close(self):
        """Close just the overlay (main app keeps running); persist position + flip the setting off."""
        self._save_geometry()
        cfg = getattr(self._app, "config", {})
        cfg["overlay_enabled"] = False
        self._safe(self._app.save_config)
        mw = getattr(self._app, "main_window", None)
        if mw is not None and hasattr(mw, "_overlay"):
            mw._overlay = None
        try:
            self._app.overlay_window = None
        except Exception:
            pass
        self.destroy()
