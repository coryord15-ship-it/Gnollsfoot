"""
Quest pop-out overlays — individual always-on-top bubble windows.

There is NO separate "Quest Dock" hub window. Quests live in the main app's
Journal tab; up to MAX_BUBBLES may be popped out as standalone overlays.

Public surface used by main.py / MainWindow / Settings:
  OverlayManager (also aliased as JournalOverlay for older call sites)
    .pop_out(quest) / .dock(quest_id) / .is_popped(quest_id)
    .refresh_journal() / .update_zone(zone) / .update_watcher_status(status)
    .apply_typography() / .apply_opacity(alpha) / .close_all()
    .bubbles  (dict quest_id -> QuestBubble)
"""
from __future__ import annotations

import logging
import os
import sys
import threading

import customtkinter as ctk

from app.ui import theme

log = logging.getLogger(__name__)

MAX_BUBBLES = 5
# Magnetic snap: lock when edges/centers are within this many pixels.
SNAP_DISTANCE = 28
# Pull free of a cluster when dragged this far from the snap lock.
UNSNAP_DISTANCE = 48


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


def _cfg_opacity(app) -> float:
    try:
        a = float((app.config or {}).get("overlay_opacity", 0.92))
    except Exception:
        a = 0.92
    return max(0.4, min(1.0, a))


# ═══════════════════════════════════════════════════════════════════════════════
# Single-quest bubble (standalone overlay window)
# ═══════════════════════════════════════════════════════════════════════════════
class QuestBubble(ctk.CTkToplevel):
    """One always-on-top bubble for a single journaled quest. Resizable, draggable,
    magnetically snappable to other bubbles."""

    def __init__(self, master, manager: "OverlayManager", app_state, quest: dict,
                 geometry: str | None = None):
        super().__init__(master, fg_color=theme.BG)
        self._manager = manager
        self._app = app_state
        self._quest = quest
        self._qid = quest.get("id")
        self.title(quest.get("quest_name") or "Quest")

        # Snap group: set of quest_ids locked together (includes self).
        self._group: set = {self._qid}
        # Relative offsets of other group members when we start a group drag.
        self._group_offsets: dict = {}
        self._dragging = False
        self._drag_off = None
        self._resize_off = None

        f_sub, f_body, f_sm = _fonts(app_state)
        self._f_sub, self._f_body, self._f_sm = f_sub, f_body, f_sm

        w, h = 320, 280
        self.minsize(240, 180)
        if geometry:
            self.geometry(geometry)
        else:
            try:
                n = len(manager.bubbles)
                px, py = self.winfo_pointerx(), self.winfo_pointery()
                gx = px - 80 + n * 28
                gy = py - 40 + n * 28
            except Exception:
                gx, gy = 200 + len(manager.bubbles) * 30, 200 + len(manager.bubbles) * 30
            self.geometry(f"{w}x{h}+{gx}+{gy}")

        try:
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.attributes("-alpha", _cfg_opacity(self._app))
            # Frameless HUD; drag via header, resize via footer grip.
            self.overrideredirect(True)
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._dock)
        ico = _asset("icon.ico")
        try:
            if os.path.isfile(ico):
                self.iconbitmap(ico)
        except Exception:
            pass

        self._build_shell()
        self.render()
        self.after(150, self._surface)

    # ── helpers ───────────────────────────────────────────────────────────────
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

    def apply_opacity(self, alpha: float | None = None):
        a = _cfg_opacity(self._app) if alpha is None else max(0.4, min(1.0, float(alpha)))
        self._safe(lambda: self.attributes("-alpha", a))

    def apply_typography(self):
        self._f_sub, self._f_body, self._f_sm = _fonts(self._app)
        try:
            self._title.configure(font=self._f_sub)
        except Exception:
            pass
        self.render()

    def set_quest(self, quest: dict):
        self._quest = quest
        self._qid = quest.get("id")
        self.render()

    def rect(self):
        """Return (x, y, w, h) in screen coords."""
        try:
            return self.winfo_x(), self.winfo_y(), self.winfo_width(), self.winfo_height()
        except Exception:
            return 0, 0, 320, 280

    # ── drag + magnetic snap ──────────────────────────────────────────────────
    def _break_from_group(self):
        """Leave any snap cluster; peers stay grouped without us."""
        peers = set(self._group) - {self._qid}
        self._group = {self._qid}
        self._group_offsets = {}
        if not peers:
            return
        remaining = set(peers)
        for qid in peers:
            other = self._manager.bubbles.get(qid)
            if other is None:
                continue
            other._group = set(remaining)

    def _drag_start(self, e):
        self._dragging = True
        self._drag_off = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())
        self._group_offsets = {}
        # Shift+drag (or Control+drag) breaks this bubble off the cluster and
        # moves it alone — the "drag away to unsnap" gesture.
        try:
            state = int(getattr(e, "state", 0) or 0)
        except Exception:
            state = 0
        # Tk state bitmasks: Shift=0x0001, Control=0x0004
        if state & 0x0005:
            self._break_from_group()
            self._move_group = False
            return
        self._move_group = len(self._group) > 1
        if not self._move_group:
            return
        mx, my = self.winfo_x(), self.winfo_y()
        for qid in list(self._group):
            if qid == self._qid:
                continue
            other = self._manager.bubbles.get(qid)
            if other is None:
                continue
            try:
                if other.winfo_exists():
                    self._group_offsets[qid] = (other.winfo_x() - mx, other.winfo_y() - my)
            except Exception:
                pass

    def _drag_move(self, e):
        off = self._drag_off
        if not off:
            return
        nx = e.x_root - off[0]
        ny = e.y_root - off[1]
        self.geometry(f"+{nx}+{ny}")

        # Move the rest of the locked group with us (cluster drag).
        if getattr(self, "_move_group", False):
            for qid, (dx, dy) in list(self._group_offsets.items()):
                other = self._manager.bubbles.get(qid)
                if other is None:
                    continue
                try:
                    if other.winfo_exists():
                        other.geometry(f"+{nx + dx}+{ny + dy}")
                except Exception:
                    pass
        else:
            # Solo drag: magnetic snap preview toward nearby bubbles
            self._maybe_snap(nx, ny)

    def _drag_end(self, e=None):
        self._dragging = False
        self._drag_off = None
        self._group_offsets = {}
        self._move_group = False
        # Final snap pass on release for a clean lock (solo or after break-free).
        try:
            self._maybe_snap(self.winfo_x(), self.winfo_y(), force=True)
        except Exception:
            pass

    def _edge_gap(self, a: "QuestBubble", b: "QuestBubble") -> float:
        """Minimum distance between two window rectangles (0 if overlapping)."""
        ax, ay, aw, ah = a.rect()
        bx, by, bw, bh = b.rect()
        dx = max(ax - (bx + bw), bx - (ax + aw), 0)
        dy = max(ay - (by + bh), by - (ay + ah), 0)
        if dx == 0 and dy == 0:
            return 0.0
        return (dx * dx + dy * dy) ** 0.5

    def _maybe_snap(self, x: int, y: int, force: bool = False):
        """Magnetically lock this bubble (and its group) to a nearby non-group bubble.

        A candidate only wins if after snapping the two windows nearly touch
        (edge gap ≤ SNAP_DISTANCE) — pure edge-alignment while far away is ignored.
        """
        me_w = max(1, self.winfo_width())
        me_h = max(1, self.winfo_height())
        best = None  # (score, target_bubble, snap_x, snap_y)

        for qid, other in list(self._manager.bubbles.items()):
            if qid == self._qid or qid in self._group:
                continue
            try:
                if not other.winfo_exists():
                    continue
            except Exception:
                continue
            ox, oy, ow, oh = other.rect()
            ow, oh = max(1, ow), max(1, oh)

            # Only consider targets already near us (cheap reject).
            if self._edge_gap_at(x, y, me_w, me_h, ox, oy, ow, oh) > SNAP_DISTANCE * 3:
                continue

            candidates = [
                # Stack / abut
                (abs(y - (oy + oh)) + abs(x - ox) * 0.25, x, oy + oh),          # below, keep x
                (abs(y - (oy + oh)) + abs((x + me_w / 2) - (ox + ow / 2)) * 0.25,
                 int(ox + ow / 2 - me_w / 2), oy + oh),                          # below, center x
                (abs((y + me_h) - oy) + abs(x - ox) * 0.25, x, oy - me_h),      # above
                (abs(x - (ox + ow)) + abs(y - oy) * 0.25, ox + ow, y),          # right
                (abs(x - (ox + ow)) + abs((y + me_h / 2) - (oy + oh / 2)) * 0.25,
                 ox + ow, int(oy + oh / 2 - me_h / 2)),                          # right, center y
                (abs((x + me_w) - ox) + abs(y - oy) * 0.25, ox - me_w, y),      # left
                # Align edges while already adjacent
                (abs(x - ox) + abs(y - oy) * 0.15, ox, y),
                (abs(y - oy) + abs(x - ox) * 0.15, x, oy),
            ]

            for score, sx, sy in candidates:
                sx, sy = int(sx), int(sy)
                gap_after = self._edge_gap_at(sx, sy, me_w, me_h, ox, oy, ow, oh)
                # Must nearly touch after the snap (or slightly overlap → gap 0).
                if gap_after > SNAP_DISTANCE:
                    continue
                # Prefer lower score (closer to ideal abutment).
                if best is None or score < best[0]:
                    best = (score, other, sx, sy)

        if best is None:
            return
        score, other, sx, sy = best
        if not force and score > SNAP_DISTANCE * 2:
            return

        # Apply snap position to self + group
        self.geometry(f"+{sx}+{sy}")
        for qid, (odx, ody) in list(self._group_offsets.items()):
            o = self._manager.bubbles.get(qid)
            if o is None:
                continue
            try:
                if o.winfo_exists():
                    o.geometry(f"+{sx + odx}+{sy + ody}")
            except Exception:
                pass
        # Merge groups
        self._merge_group_with(other)

    @staticmethod
    def _edge_gap_at(ax, ay, aw, ah, bx, by, bw, bh) -> float:
        dx = max(ax - (bx + bw), bx - (ax + aw), 0)
        dy = max(ay - (by + bh), by - (ay + ah), 0)
        if dx == 0 and dy == 0:
            return 0.0
        return (dx * dx + dy * dy) ** 0.5

    def _merge_group_with(self, other: "QuestBubble"):
        merged = set(self._group) | set(other._group)
        for qid in merged:
            b = self._manager.bubbles.get(qid)
            if b is not None:
                b._group = set(merged)
        # Refresh group offsets relative to self for continued drag
        mx, my = self.winfo_x(), self.winfo_y()
        self._group_offsets = {}
        for qid in merged:
            if qid == self._qid:
                continue
            b = self._manager.bubbles.get(qid)
            if b is None:
                continue
            try:
                if b.winfo_exists():
                    self._group_offsets[qid] = (b.winfo_x() - mx, b.winfo_y() - my)
            except Exception:
                pass

    def _resize_start(self, e):
        self._resize_off = (e.x_root, e.y_root, self.winfo_width(), self.winfo_height())

    def _resize_move(self, e):
        o = self._resize_off
        if not o:
            return
        sx, sy, sw, sh = o
        self.geometry(f"{max(240, sw + e.x_root - sx)}x{max(180, sh + e.y_root - sy)}")

    # ── shell / render ────────────────────────────────────────────────────────
    def _build_shell(self):
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
            grab.bind("<ButtonRelease-1>", self._drag_end)
        ctk.CTkButton(
            hdr, text="Dock", width=48, height=24, fg_color=theme.PANEL,
            hover_color=theme.GOLD, text_color=theme.TEXT_PRIMARY, font=self._f_sm,
            corner_radius=10, command=self._dock,
        ).pack(side="right", padx=(0, 4))
        ctk.CTkButton(
            hdr, text="✕", width=26, height=24, fg_color="transparent",
            text_color=theme.TEXT_MUTED, hover_color=theme.DANGER, font=self._f_sm,
            command=self._dock,
        ).pack(side="right")

        self._body = ctk.CTkScrollableFrame(self._inner, fg_color=theme.PANEL, corner_radius=10)
        self._body.pack(fill="both", expand=True, padx=8, pady=(2, 2))

        foot = ctk.CTkFrame(self._inner, fg_color=theme.PANEL_HOVER, height=22, corner_radius=12)
        foot.pack(fill="x", padx=6, pady=(0, 6))
        foot.pack_propagate(False)
        grip = ctk.CTkLabel(foot, text="◢ resize", font=self._f_sm, text_color=theme.TEXT_MUTED)
        grip.pack(side="right", padx=8)
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

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
            ctk.CTkLabel(
                self._body, text=zone, font=self._f_sm,
                text_color=theme.TEXT_SECONDARY, anchor="w",
            ).pack(anchor="w", pady=(0, 4))
        matcher = getattr(self._app, "quest_matcher", None)
        if matcher and any(s.get("action_type") for s in (q.get("steps") or [])):
            try:
                done_n, total_n = matcher.progress(q)
                ctk.CTkLabel(
                    self._body, text=f"Progress  {done_n}/{total_n}",
                    font=self._f_sm, text_color=theme.TEXT_SECONDARY, anchor="w",
                ).pack(anchor="w", pady=(0, 6))
            except Exception:
                pass
        prog = getattr(self._app, "_quest_progress", set())
        given = getattr(self._app, "_quest_given", set())
        wrap = max(180, self.winfo_width() - 48)
        for s in sorted(q.get("steps") or [], key=lambda x: x.get("step_order", 0)):
            num = s.get("step_order", "")
            is_done = bool(
                matcher and s.get("action_type") and matcher.is_step_done(q.get("id"), num)
            )
            if s.get("instruction"):
                mark = "✓ " if is_done else ""
                col = theme.GREEN if is_done else theme.TEXT_PRIMARY
                ctk.CTkLabel(
                    self._body, text=f"{mark}{num}. {s['instruction']}",
                    font=self._f_body, text_color=col, anchor="w", justify="left",
                    wraplength=wrap,
                ).pack(anchor="w", pady=(2, 0))
            for it in (s.get("required_items") or []):
                low = it.lower()
                if low in given:
                    mark, col = "✔", theme.GREEN
                elif low in prog:
                    mark, col = "✓", theme.GOLD
                else:
                    mark, col = "○", theme.TEXT_SECONDARY
                ctk.CTkLabel(
                    self._body, text=f"   {mark} {it}", font=self._f_sm,
                    text_color=col, anchor="w",
                ).pack(anchor="w")
        if q.get("reward_items"):
            ctk.CTkLabel(
                self._body, text="Reward: " + ", ".join(q["reward_items"]),
                font=self._f_sm, text_color=theme.GOLD, anchor="w",
            ).pack(anchor="w", pady=(8, 0))

    def _dock(self):
        """Close this bubble and restore it to the main Journal tab."""
        try:
            self._manager.dock(self._qid)
        except Exception:
            log.debug("dock failed", exc_info=True)
            self._safe(self.destroy)


# ═══════════════════════════════════════════════════════════════════════════════
# Manager — owns all popped-out bubbles (no hub UI)
# ═══════════════════════════════════════════════════════════════════════════════
class OverlayManager:
    """Owns up to MAX_BUBBLES quest bubble windows. Not a window itself.

    Kept as a plain object so MainWindow can hold one for the app lifetime.
    """

    def __init__(self, master, app_state):
        self._master = master
        self._app = app_state
        self._bubbles: dict = {}  # quest_id -> QuestBubble
        self._on_change = None  # optional callback when pop/dock changes

    @property
    def bubbles(self) -> dict:
        return self._bubbles

    def set_on_change(self, fn):
        """fn() called after pop-out or dock so the Journal tab can re-render."""
        self._on_change = fn

    def _notify(self):
        cb = self._on_change
        if cb:
            try:
                cb()
            except Exception:
                log.debug("overlay on_change failed", exc_info=True)

    def is_popped(self, qid) -> bool:
        b = self._bubbles.get(qid)
        if not b:
            return False
        try:
            return bool(b.winfo_exists())
        except Exception:
            return False

    def popped_ids(self) -> set:
        out = set()
        for qid, b in list(self._bubbles.items()):
            try:
                if b.winfo_exists():
                    out.add(qid)
                else:
                    self._bubbles.pop(qid, None)
            except Exception:
                self._bubbles.pop(qid, None)
        return out

    def pop_out(self, quest: dict) -> bool:
        """Open a bubble for this quest. Returns True if open (or already open)."""
        qid = quest.get("id")
        if not qid:
            return False
        existing = self._bubbles.get(qid)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing._surface()
                    return True
            except Exception:
                pass
            self._bubbles.pop(qid, None)
        # Prune dead entries first
        self.popped_ids()
        if len(self._bubbles) >= MAX_BUBBLES:
            log.info("Max %s pop-out windows open", MAX_BUBBLES)
            return False
        try:
            # Ensure tk default root is the main window (splash can clear it).
            import tkinter as tk
            if tk._default_root is None and self._master is not None:
                tk._default_root = self._master
            bub = QuestBubble(self._master, self, self._app, quest)
            self._bubbles[qid] = bub
        except Exception:
            log.exception("pop-out bubble failed")
            return False
        self._notify()
        return True

    def dock(self, qid) -> None:
        """Close the bubble for qid and return it to the Journal tab state."""
        bub = self._bubbles.pop(qid, None)
        if bub is not None:
            # Remove from any snap groups
            for peer_id in list(getattr(bub, "_group", set()) or set()):
                peer = self._bubbles.get(peer_id)
                if peer is not None:
                    peer._group.discard(qid)
            try:
                if bub.winfo_exists():
                    bub.destroy()
            except Exception:
                pass
        self._notify()

    def close_all(self):
        for qid in list(self._bubbles.keys()):
            self.dock(qid)

    def refresh_journal(self):
        """Reload quest data for open bubbles from Supabase journal (background)."""
        if not self._app.auth.is_logged_in:
            return

        def load():
            try:
                quests = self._app.supabase.get_journal() or []
            except Exception:
                log.debug("journal load for bubbles failed", exc_info=True)
                quests = []
            master = self._master
            if master is None:
                return
            try:
                master.after(0, lambda: self._apply_quests(quests))
            except Exception:
                pass

        threading.Thread(target=load, daemon=True, name="OverlayJournalRefresh").start()

    def _apply_quests(self, quests: list):
        by_id = {q.get("id"): q for q in (quests or [])}
        try:
            from app import quest_progress
            self._app._journal_quests = quests or []
            self._app._quest_item_index = quest_progress.build_index(quests or [])
            self._app.quest_matcher.set_quests(quests or [])
        except Exception:
            pass
        for qid, bub in list(self._bubbles.items()):
            if qid not in by_id:
                self.dock(qid)
                continue
            try:
                if bub.winfo_exists():
                    bub.set_quest(by_id[qid])
            except Exception:
                pass
        self._notify()

    def update_zone(self, zone: str):
        """Zone changes do not need a hub; bubbles just re-render progress if needed."""
        for bub in list(self._bubbles.values()):
            try:
                if bub.winfo_exists():
                    bub.render()
            except Exception:
                pass

    def update_watcher_status(self, status: str):
        # No hub status bar anymore; no-op kept for main.py compatibility.
        pass

    def apply_typography(self):
        for bub in list(self._bubbles.values()):
            try:
                if bub.winfo_exists():
                    bub.apply_typography()
            except Exception:
                pass

    def apply_opacity(self, alpha: float | None = None):
        a = _cfg_opacity(self._app) if alpha is None else max(0.4, min(1.0, float(alpha)))
        for bub in list(self._bubbles.values()):
            try:
                if bub.winfo_exists():
                    bub.apply_opacity(a)
            except Exception:
                pass

    # Compatibility shims used by older main_window / settings code
    def winfo_exists(self):
        return True

    def deiconify(self):
        for bub in list(self._bubbles.values()):
            try:
                if bub.winfo_exists():
                    bub.deiconify()
                    bub.lift()
            except Exception:
                pass

    def lift(self):
        self.deiconify()

    def destroy(self):
        self.close_all()


# Back-compat alias: old code imported JournalOverlay as the dock window.
# Now it is the manager (no standalone container window).
JournalOverlay = OverlayManager
