"""
Gnoll Guard quest overlay — multi-window bubble HUD.

Replaces the fragile single-popup overlay with:
  • A dock hub (journal list + zone list)
  • Up to MAX_BUBBLES independent resizable "bubble" windows (one quest each)
  • Pop-out / dock controls per quest
  • Font family + scale from Settings (Helvetica available)

Public API (main.py / MainWindow — unchanged):
  refresh_journal(), update_zone(zone), update_watcher_status(status),
  apply_typography(), destroy via _on_close / toggle_overlay(False)
"""
from __future__ import annotations

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
MAX_BUBBLES = 5

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


def _asset(*parts) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", *parts)


def _cfg_font_family(app) -> str:
    return theme.resolve_font_family((app.config or {}).get("overlay_font_family") or "Segoe UI")


def _cfg_font_scale(app) -> float:
    try:
        s = float((app.config or {}).get("overlay_font_scale", 1.0))
    except Exception:
        s = 1.0
    return max(0.8, min(1.6, s))


def _fonts(app):
    """(subheader, body, small) tuples for current overlay typography."""
    return theme.scaled_fonts(_cfg_font_family(app), _cfg_font_scale(app))


# ═══════════════════════════════════════════════════════════════════════════════
# Single-quest bubble (undocked window)
# ═══════════════════════════════════════════════════════════════════════════════
class QuestBubble(ctk.CTkToplevel):
    """One always-on-top bubble for a single journaled quest. Resizable, draggable."""

    def __init__(self, master_hub: "JournalOverlay", app_state, quest: dict):
        super().__init__(master_hub, fg_color=theme.BG)
        self._hub = master_hub
        self._app = app_state
        self._quest = quest
        self._qid = quest.get("id")
        self.title(quest.get("quest_name") or "Quest")

        f_sub, f_body, f_sm = _fonts(app_state)
        self._f_sub, self._f_body, self._f_sm = f_sub, f_body, f_sm

        # Bubble look: soft rounded outer, gold edge when zone-relevant
        w, h = 320, 280
        self.minsize(240, 180)
        # cascade from hub
        try:
            hx, hy = master_hub.winfo_x(), master_hub.winfo_y()
            n = len(master_hub._bubbles)
            gx, gy = hx + 40 + n * 28, hy + 40 + n * 28
        except Exception:
            gx, gy = self.winfo_pointerx() - 100, self.winfo_pointery() - 40
        self.geometry(f"{w}x{h}+{gx}+{gy}")

        try:
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.attributes("-alpha", float(self._app.config.get("overlay_opacity", 0.92)))
            # Always frameless bubble; drag via header
            self.overrideredirect(True)
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._dock)
        self._build_shell()
        self.render()
        self.after(150, self._surface)

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            pass

    def _surface(self):
        def _do():
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
        self._safe(_do)

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
        self.geometry(f"{max(240, sw + e.x_root - sx)}x{max(180, sh + e.y_root - sy)}")

    def _build_shell(self):
        # Outer bubble ring
        self._shell = ctk.CTkFrame(self, fg_color=theme.BORDER, corner_radius=18)
        self._shell.pack(fill="both", expand=True, padx=2, pady=2)
        self._inner = ctk.CTkFrame(self._shell, fg_color=theme.PANEL, corner_radius=16)
        self._inner.pack(fill="both", expand=True, padx=2, pady=2)

        hdr = ctk.CTkFrame(self._inner, fg_color=theme.PANEL_HOVER, height=36, corner_radius=14)
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        hdr.pack_propagate(False)
        self._title = ctk.CTkLabel(hdr, text="", font=self._f_sub, text_color=theme.GOLD, anchor="w")
        self._title.pack(side="left", padx=(10, 4), fill="x", expand=True)
        for grab in (hdr, self._title):
            grab.bind("<Button-1>", self._drag_start)
            grab.bind("<B1-Motion>", self._drag_move)
        ctk.CTkButton(hdr, text="Dock", width=48, height=24, fg_color=theme.PANEL,
                      hover_color=theme.GOLD, text_color=theme.TEXT_PRIMARY, font=self._f_sm,
                      corner_radius=10, command=self._dock).pack(side="right", padx=(0, 4))
        ctk.CTkButton(hdr, text="✕", width=26, height=24, fg_color="transparent",
                      text_color=theme.TEXT_MUTED, hover_color=theme.DANGER, font=self._f_sm,
                      command=self._dock).pack(side="right")

        self._body = ctk.CTkScrollableFrame(self._inner, fg_color=theme.PANEL, corner_radius=10)
        self._body.pack(fill="both", expand=True, padx=8, pady=(2, 2))

        foot = ctk.CTkFrame(self._inner, fg_color=theme.PANEL_HOVER, height=22, corner_radius=12)
        foot.pack(fill="x", padx=6, pady=(0, 6))
        foot.pack_propagate(False)
        grip = ctk.CTkLabel(foot, text="◢ resize", font=self._f_sm, text_color=theme.TEXT_MUTED)
        grip.pack(side="right", padx=8)
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

    def set_quest(self, quest: dict):
        self._quest = quest
        self._qid = quest.get("id")
        self.render()

    def apply_typography(self):
        self._f_sub, self._f_body, self._f_sm = _fonts(self._app)
        try:
            self._title.configure(font=self._f_sub)
        except Exception:
            pass
        self.render()

    def render(self):
        for w in self._body.winfo_children():
            self._safe(w.destroy)
        q = self._quest or {}
        name = q.get("quest_name") or "Quest"
        try:
            self._title.configure(text=name)
            self.title(name)
        except Exception:
            pass
        zone = q.get("zone") or ""
        if zone:
            ctk.CTkLabel(self._body, text=zone, font=self._f_sm,
                         text_color=theme.TEXT_SECONDARY, anchor="w").pack(anchor="w", pady=(0, 4))
        matcher = getattr(self._app, "quest_matcher", None)
        if matcher and any(s.get("action_type") for s in (q.get("steps") or [])):
            try:
                done_n, total_n = matcher.progress(q)
                ctk.CTkLabel(self._body, text=f"Progress  {done_n}/{total_n}",
                             font=self._f_sm, text_color=theme.TEXT_SECONDARY,
                             anchor="w").pack(anchor="w", pady=(0, 6))
            except Exception:
                pass
        prog = getattr(self._app, "_quest_progress", set())
        given = getattr(self._app, "_quest_given", set())
        for s in sorted(q.get("steps") or [], key=lambda x: x.get("step_order", 0)):
            num = s.get("step_order", "")
            is_done = bool(matcher and s.get("action_type")
                           and matcher.is_step_done(q.get("id"), num))
            if s.get("instruction"):
                mark = "✓ " if is_done else ""
                col = theme.GREEN if is_done else theme.TEXT_PRIMARY
                ctk.CTkLabel(
                    self._body, text=f"{mark}{num}. {s['instruction']}",
                    font=self._f_body, text_color=col, anchor="w", justify="left",
                    wraplength=max(180, self.winfo_width() - 48),
                ).pack(anchor="w", pady=(2, 0))
            for it in (s.get("required_items") or []):
                low = it.lower()
                if low in given:
                    mark, col = "✔", theme.GREEN
                elif low in prog:
                    mark, col = "✓", theme.GOLD
                else:
                    mark, col = "○", theme.TEXT_SECONDARY
                ctk.CTkLabel(self._body, text=f"   {mark} {it}", font=self._f_sm,
                             text_color=col, anchor="w").pack(anchor="w")
        if q.get("reward_items"):
            ctk.CTkLabel(
                self._body, text="Reward: " + ", ".join(q["reward_items"]),
                font=self._f_sm, text_color=theme.GOLD, anchor="w",
            ).pack(anchor="w", pady=(8, 0))

    def _dock(self):
        """Return to hub (close this bubble)."""
        try:
            self._hub._on_bubble_closed(self._qid)
        except Exception:
            pass
        self._safe(self.destroy)


# ═══════════════════════════════════════════════════════════════════════════════
# Hub / dock — primary overlay window
# ═══════════════════════════════════════════════════════════════════════════════
class JournalOverlay(ctk.CTkToplevel):
    """Dock hub: journal list with pop-out bubbles (max MAX_BUBBLES). Always-on-top."""

    def __init__(self, master, app_state):
        super().__init__(master, fg_color=theme.BG)
        self._app = app_state
        self.title("Gnoll Guard — Quest Dock")

        self._current_zone = None
        self._quests: list = []
        self._zone_quests: list = []
        self._rows: list = []
        self._bubbles: dict = {}  # quest_id -> QuestBubble

        f_sub, f_body, f_sm = _fonts(app_state)
        self._f_sub, self._f_body, self._f_sm = f_sub, f_body, f_sm

        w, h = 380, 560
        self.minsize(300, 360)
        vx, vy = self.winfo_vrootx(), self.winfo_vrooty()
        vw, vh = self.winfo_vrootwidth(), self.winfo_vrootheight()
        # Prefer saved position; else under cursor
        win = (self._app.config or {}).get("window") or {}
        sx, sy = win.get("overlay_x"), win.get("overlay_y")
        if sx is not None and sy is not None:
            gx, gy = int(sx), int(sy)
        else:
            px, py = self.winfo_pointerx(), self.winfo_pointery()
            gx = min(max(px - w // 2, vx), vx + vw - w)
            gy = min(max(py - 30, vy), vy + vh - h)
        self.geometry(f"{w}x{h}+{gx}+{gy}")

        try:
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.attributes("-alpha", float(self._app.config.get("overlay_opacity", 0.92)))
            if bool(self._app.config.get("overlay_borderless", False)):
                self.overrideredirect(True)
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

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            pass

    def _surface(self):
        def _do():
            self.deiconify()
            self.lift()
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.after(400, lambda: self._safe(lambda: self.attributes("-topmost", True)))
        self._safe(_do)

    def _save_geometry(self):
        def _do():
            win = self._app.config.setdefault("window", {})
            win["overlay_x"], win["overlay_y"] = self.winfo_x(), self.winfo_y()
            self._app.save_config()
        self._safe(_do)

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
        self.geometry(f"{max(300, sw + e.x_root - sx)}x{max(360, sh + e.y_root - sy)}")

    # ── layout ────────────────────────────────────────────────────────────────
    def _build(self):
        # Bubble-style outer frame for the hub too
        shell = ctk.CTkFrame(self, fg_color=theme.BORDER, corner_radius=14)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        body = ctk.CTkFrame(shell, fg_color=theme.BG, corner_radius=12)
        body.pack(fill="both", expand=True, padx=2, pady=2)
        self._root_body = body

        hdr = ctk.CTkFrame(body, fg_color=theme.PANEL, height=40, corner_radius=10)
        hdr.pack(fill="x", padx=6, pady=(6, 2))
        hdr.pack_propagate(False)
        try:
            img = ctk.CTkImage(Image.open(_asset("icon.png")), size=(18, 18))
            ctk.CTkLabel(hdr, image=img, text="").pack(side="left", padx=(10, 4))
        except Exception:
            pass
        self._hdr_title = ctk.CTkLabel(
            hdr, text="Quest Dock", font=self._f_sub, text_color=theme.GOLD)
        self._hdr_title.pack(side="left")
        for grab in (hdr, self._hdr_title):
            grab.bind("<Button-1>", self._drag_start)
            grab.bind("<B1-Motion>", self._drag_move)
        ctk.CTkButton(hdr, text="✕", width=28, height=26, fg_color="transparent",
                      text_color=theme.TEXT_MUTED, hover_color=theme.DANGER,
                      font=self._f_body, command=self._on_close).pack(side="right", padx=(0, 6))
        ctk.CTkButton(hdr, text="⟳", width=28, height=26, fg_color="transparent",
                      text_color=theme.TEXT_SECONDARY, hover_color=theme.PANEL_HOVER,
                      font=self._f_body, command=self.refresh_journal).pack(side="right")

        self._zone_lbl = ctk.CTkLabel(body, text="", font=self._f_sm,
                                      text_color=theme.TEXT_MUTED, anchor="w")
        self._zone_lbl.pack(fill="x", padx=12, pady=(4, 0))

        self._hint = ctk.CTkLabel(
            body,
            text=f"Pop out up to {MAX_BUBBLES} quests as bubble windows · Dock to return",
            font=self._f_sm, text_color=theme.TEXT_MUTED, anchor="w",
        )
        self._hint.pack(fill="x", padx=12, pady=(0, 4))

        self._scroll = ctk.CTkScrollableFrame(body, fg_color=theme.BG, corner_radius=8)
        self._scroll.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        foot = ctk.CTkFrame(body, fg_color=theme.PANEL, height=26, corner_radius=10)
        foot.pack(fill="x", padx=6, pady=(0, 6))
        foot.pack_propagate(False)
        self._log_light = ctk.CTkLabel(foot, text="●", font=self._f_sm,
                                       text_color=theme.STATUS_LOG_DISCONNECTED)
        self._log_light.pack(side="left", padx=(10, 4))
        self._status_lbl = ctk.CTkLabel(foot, text="starting…", font=self._f_sm,
                                        text_color=theme.TEXT_MUTED)
        self._status_lbl.pack(side="left")
        self._bubble_count_lbl = ctk.CTkLabel(
            foot, text="0 bubbles", font=self._f_sm, text_color=theme.TEXT_MUTED)
        self._bubble_count_lbl.pack(side="right", padx=(0, 8))
        grip = ctk.CTkLabel(foot, text="◢", font=self._f_sm, text_color=theme.TEXT_MUTED)
        grip.pack(side="right", padx=6)
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

    # ── typography ────────────────────────────────────────────────────────────
    def apply_typography(self):
        """Re-read config font family/scale and rebuild fonts on hub + bubbles."""
        self._f_sub, self._f_body, self._f_sm = _fonts(self._app)
        try:
            self._hdr_title.configure(font=self._f_sub)
            self._zone_lbl.configure(font=self._f_sm)
            self._hint.configure(font=self._f_sm)
            self._status_lbl.configure(font=self._f_sm)
            self._bubble_count_lbl.configure(font=self._f_sm)
        except Exception:
            pass
        for b in list(self._bubbles.values()):
            try:
                if b.winfo_exists():
                    b.apply_typography()
            except Exception:
                pass
        self._render()

    # ── data: journal ─────────────────────────────────────────────────────────
    def refresh_journal(self):
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
        try:
            from app import quest_progress
            self._app._journal_quests = quests
            self._app._quest_item_index = quest_progress.build_index(quests)
            self._app.quest_matcher.set_quests(quests)
        except Exception:
            pass
        # Refresh open bubbles with updated quest data; drop dead ids
        by_id = {q.get("id"): q for q in quests}
        for qid, bub in list(self._bubbles.items()):
            if qid not in by_id:
                self._on_bubble_closed(qid)
                try:
                    if bub.winfo_exists():
                        bub.destroy()
                except Exception:
                    pass
            else:
                try:
                    if bub.winfo_exists():
                        bub.set_quest(by_id[qid])
                except Exception:
                    pass
        self._render()

    def update_zone(self, zone: str):
        self._current_zone = zone
        try:
            self._zone_lbl.configure(text=(f"📍 {zone}" if zone else ""))
        except Exception:
            pass
        if not zone:
            self._zone_quests = []
            self._render()
            return

        def load():
            quests = []
            if requests is not None:
                try:
                    r = requests.get(
                        API_BASE + "/api/quests/by-zone?zone=" + urllib.parse.quote(zone),
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

    # ── bubbles ───────────────────────────────────────────────────────────────
    def _pop_out(self, quest: dict):
        qid = quest.get("id")
        if not qid:
            return
        existing = self._bubbles.get(qid)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing._surface()
                    return
            except Exception:
                pass
            self._bubbles.pop(qid, None)
        if len(self._bubbles) >= MAX_BUBBLES:
            self._status_lbl.configure(
                text=f"Max {MAX_BUBBLES} bubbles open — dock one first")
            return
        try:
            bub = QuestBubble(self, self._app, quest)
            self._bubbles[qid] = bub
        except Exception:
            log.exception("pop-out bubble failed")
            return
        self._update_bubble_count()
        self._render()

    def _on_bubble_closed(self, qid):
        self._bubbles.pop(qid, None)
        self._update_bubble_count()
        try:
            if self.winfo_exists():
                self._render()
        except Exception:
            pass

    def _update_bubble_count(self):
        n = len(self._bubbles)
        try:
            self._bubble_count_lbl.configure(text=f"{n}/{MAX_BUBBLES} bubbles")
        except Exception:
            pass

    def _is_popped(self, qid) -> bool:
        b = self._bubbles.get(qid)
        if not b:
            return False
        try:
            return bool(b.winfo_exists())
        except Exception:
            return False

    # ── render ────────────────────────────────────────────────────────────────
    def _clear(self):
        for w in self._rows:
            self._safe(w.destroy)
        self._rows = []

    def _msg(self, text):
        lbl = ctk.CTkLabel(self._scroll, text=text, justify="left", wraplength=420,
                           font=self._f_body, text_color=theme.TEXT_SECONDARY)
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
                      "“Add to Journal” — they'll appear here. Pop one out as a bubble to track it "
                      "while you play.")
        else:
            ordered = sorted(
                self._quests,
                key=lambda q: 0 if (zone and (q.get("zone") or "").lower() == zone) else 1,
            )
            for q in ordered:
                self._render_quest_row(
                    q, here=bool(zone) and (q.get("zone") or "").lower() == zone)
        if self._current_zone and self._zone_quests:
            have = {q.get("id") for q in self._quests}
            extra = [q for q in self._zone_quests if q.get("id") not in have]
            if extra:
                hdr = ctk.CTkLabel(
                    self._scroll, text=f"— More quests in {self._current_zone} —",
                    font=self._f_sm, text_color=theme.TEXT_MUTED)
                hdr.pack(anchor="w", padx=theme.PAD, pady=(theme.PAD, 2))
                self._rows.append(hdr)
                for q in extra[:20]:
                    self._render_zone_row(q)
        self._update_bubble_count()

    def _render_quest_row(self, q, here=False):
        qid = q.get("id")
        popped = self._is_popped(qid)
        card = ctk.CTkFrame(
            self._scroll, fg_color=theme.PANEL, corner_radius=14,
            border_width=(2 if here else 1),
            border_color=theme.GOLD if here else theme.BORDER,
        )
        card.pack(fill="x", padx=theme.PAD, pady=5)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=theme.PAD, pady=(theme.PAD_SM, 0))
        ctk.CTkLabel(
            top, text=q.get("quest_name", "Quest"), font=self._f_sub,
            text_color=theme.GOLD, anchor="w",
        ).pack(side="left", fill="x", expand=True)
        matcher = getattr(self._app, "quest_matcher", None)
        structured = [s for s in (q.get("steps") or []) if s.get("action_type")]
        if structured and matcher:
            try:
                done_n, total_n = matcher.progress(q)
                ctk.CTkLabel(top, text=f"  {done_n}/{total_n}", font=self._f_sm,
                             text_color=theme.TEXT_SECONDARY).pack(side="left")
            except Exception:
                pass
        # Pop out / Dock
        if popped:
            ctk.CTkButton(
                top, text="Dock", width=52, height=24, fg_color=theme.PANEL_HOVER,
                hover_color=theme.GOLD, text_color=theme.TEXT_PRIMARY, font=self._f_sm,
                corner_radius=10,
                command=lambda i=qid: self._dock_quest(i),
            ).pack(side="right", padx=(4, 0))
        else:
            ctk.CTkButton(
                top, text="Pop out", width=64, height=24, fg_color=theme.PANEL_HOVER,
                hover_color=theme.GOLD, text_color=theme.TEXT_PRIMARY, font=self._f_sm,
                corner_radius=10,
                command=lambda qq=q: self._pop_out(qq),
            ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            top, text="🗑", width=26, height=22, fg_color="transparent",
            text_color=theme.TEXT_MUTED, hover_color=theme.DANGER, font=self._f_sm,
            command=lambda qq=q: self._delete_quest(qq),
        ).pack(side="right")
        if q.get("zone"):
            ctk.CTkLabel(card, text=q["zone"], font=self._f_sm,
                         text_color=theme.TEXT_SECONDARY, anchor="w"
                         ).pack(anchor="w", padx=theme.PAD)
        # Compact step preview (first 4) when not popped — full detail is in bubble
        if not popped:
            prog = getattr(self._app, "_quest_progress", set())
            given = getattr(self._app, "_quest_given", set())
            steps = sorted(q.get("steps") or [], key=lambda s: s.get("step_order", 0))
            for s in steps[:5]:
                num = s.get("step_order", "")
                is_done = bool(matcher and s.get("action_type")
                               and matcher.is_step_done(q.get("id"), num))
                if s.get("instruction"):
                    mark = "✓ " if is_done else ""
                    col = theme.GREEN if is_done else theme.TEXT_PRIMARY
                    ctk.CTkLabel(
                        card, text=f"{mark}{num}. {s['instruction']}",
                        font=self._f_sm, text_color=col, anchor="w", justify="left",
                        wraplength=400,
                    ).pack(anchor="w", padx=theme.PAD, pady=(2, 0))
                for it in (s.get("required_items") or [])[:3]:
                    low = it.lower()
                    if low in given:
                        mark, col = "✔", theme.GREEN
                    elif low in prog:
                        mark, col = "✓", theme.GOLD
                    else:
                        mark, col = "○", theme.TEXT_SECONDARY
                    ctk.CTkLabel(card, text=f"     {mark} {it}", font=self._f_sm,
                                 text_color=col, anchor="w").pack(anchor="w", padx=theme.PAD)
            if len(steps) > 5:
                ctk.CTkLabel(card, text=f"  … +{len(steps) - 5} more steps (pop out for full list)",
                             font=self._f_sm, text_color=theme.TEXT_MUTED, anchor="w"
                             ).pack(anchor="w", padx=theme.PAD, pady=(0, 4))
        else:
            ctk.CTkLabel(card, text="  ● open in bubble window", font=self._f_sm,
                         text_color=theme.GOLD, anchor="w"
                         ).pack(anchor="w", padx=theme.PAD, pady=(2, 6))
        if q.get("reward_items") and not popped:
            ctk.CTkLabel(card, text="Reward: " + ", ".join(q["reward_items"]),
                         font=self._f_sm, text_color=theme.GOLD, anchor="w"
                         ).pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, theme.PAD_SM))
        else:
            ctk.CTkFrame(card, fg_color="transparent", height=theme.PAD_SM).pack()
        self._rows.append(card)

    def _dock_quest(self, qid):
        bub = self._bubbles.get(qid)
        if bub is not None:
            try:
                if bub.winfo_exists():
                    bub.destroy()
            except Exception:
                pass
        self._on_bubble_closed(qid)

    def _render_zone_row(self, q):
        row = ctk.CTkFrame(self._scroll, fg_color=theme.PANEL, corner_radius=10)
        row.pack(fill="x", padx=theme.PAD, pady=2)
        ctk.CTkLabel(row, text=q.get("quest_name", "Quest"), font=self._f_sm,
                     text_color=theme.TEXT_PRIMARY, anchor="w"
                     ).pack(anchor="w", padx=theme.PAD, pady=(4, 0))
        giver = q.get("quest_giver_npc") or ""
        if giver:
            ctk.CTkLabel(row, text=f"   {giver}", font=self._f_sm,
                         text_color=theme.TEXT_MUTED, anchor="w"
                         ).pack(anchor="w", padx=theme.PAD, pady=(0, 4))
        self._rows.append(row)

    def _delete_quest(self, q):
        import tkinter.messagebox as _mb
        if not _mb.askyesno(
            "Remove quest",
            f"Remove “{q.get('quest_name', 'this quest')}” from your journal?",
            parent=self,
        ):
            return
        qid = q.get("id")
        self._dock_quest(qid)
        self._quests = [jq for jq in self._quests if jq.get("id") != qid]
        try:
            from app import quest_progress
            self._app._journal_quests = self._quests
            self._app._quest_item_index = quest_progress.build_index(self._quests)
            self._app.quest_matcher.set_quests(self._quests)
        except Exception:
            pass
        threading.Thread(
            target=lambda: self._safe(lambda: self._app.supabase.remove_quest(qid)),
            daemon=True,
        ).start()
        self._render()

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
        self.refresh_journal()

    def update_sync_status(self, text: str):
        pass

    def show_update_banner(self, *a, **k):
        pass

    def refresh_popular(self, *a, **k):
        pass

    def _on_close(self):
        """Close hub + all bubbles; persist hub position; disable overlay setting."""
        for qid, bub in list(self._bubbles.items()):
            try:
                if bub.winfo_exists():
                    bub.destroy()
            except Exception:
                pass
        self._bubbles.clear()
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
