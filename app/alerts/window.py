"""
Floating alert window — pure tkinter only.

CustomTkinter widgets require a CTkToplevel parent to render correctly;
placing them on a plain tk.Toplevel results in a blank window. Using
standard tk widgets avoids this entirely and is guaranteed to work.
"""

import logging
import threading
import webbrowser
from queue import Empty, Queue
from typing import Optional
from urllib.parse import quote as _url_quote
import tkinter as tk

import customtkinter as ctk   # kept only for type-compatibility with start()

from app.alerts.engine import Alert
from app.ui import theme

log = logging.getLogger(__name__)


VOTE_REASONS = {
    "keep":    ["Upgrade for me", "Best in slot", "For an alt", "Quest item"],
    "sell":    ["Worth good plat", "Wrong class for me", "Already have better", "Price is dropping"],
    "tribute": ["Low stats", "Nobody uses it", "Can't sell it"],
}

_ITEM_H = 284
_STD_H  = 170
_W      = theme.ALERT_WIDTH   # 344

# Map theme hex → tk-compatible (they're the same, tk accepts hex fine)
_BG    = theme.PANEL          # "#1A1416"
_BG2   = theme.BG             # "#0D0A0B"
_HOVER = theme.PANEL_HOVER    # "#221C1E"
_MUTED = theme.TEXT_MUTED     # "#5E4E4E"
_SEC   = theme.TEXT_SECONDARY # "#9E8E7E"
_PRI   = theme.TEXT_PRIMARY   # "#E8E0D0"
_GOLD  = theme.GOLD           # "#C8960C"
_BORDER= theme.BORDER         # "#2E2428"

_FONT_TITLE  = ("Georgia",    13, "bold")
_FONT_BODY   = ("Segoe UI",    9)
_FONT_SMALL  = ("Segoe UI",    8)
_FONT_BADGE  = ("Segoe UI",    9, "bold")
_FONT_CLOSE  = ("Segoe UI",   13, "bold")


class AlertWindow:
    def __init__(self, config: dict, supabase=None,
                 on_position_save: Optional[callable] = None,
                 on_submit_screenshot: Optional[callable] = None,
                 is_admin: Optional[callable] = None):
        self._config               = config
        self._supabase             = supabase
        self._on_position_save     = on_position_save
        self._on_submit_screenshot = on_submit_screenshot
        self._is_admin             = is_admin
        self._duration        = config.get("alert_duration_seconds", 15)
        self._queue: Queue[Alert] = Queue()
        self._root            = None
        self._current_alert   = None
        self._dismiss_timer   = None
        self._pending: list[Alert] = []
        self.last_zone: str   = ""

    def push(self, alert: Alert):
        self._queue.put(alert)

    def start(self, root):
        self._root = root
        self._poll()

    def _poll(self):
        try:
            while True:
                self._pending.append(self._queue.get_nowait())
        except Empty:
            pass
        if self._pending and not self._current_alert:
            self._show(self._pending.pop(0))
        self._root.after(100, self._poll)

    # ── Window ───────────────────────────────────────────────────────────────

    def _show(self, alert: Alert):
        h = _ITEM_H if alert.alert_type == "item" else _STD_H

        win = tk.Toplevel(self._root)
        win.configure(bg=_BG)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.resizable(False, False)

        cfg  = self._config.get("window", {})
        ax, ay = cfg.get("alert_x"), cfg.get("alert_y")
        if ax is not None and ay is not None:
            win.geometry(f"{_W}x{h}+{int(ax)}+{int(ay)}")
        else:
            sw = win.winfo_screenwidth()
            win.geometry(f"{_W}x{h}+{sw - _W - 20}+40")

        self._current_alert = win
        self._build(win, alert)
        self._make_draggable(win)

        if self._config.get("audio_enabled", True):
            threading.Thread(target=self._play_sound, args=(alert.badge,),
                             daemon=True).start()

        self._dismiss_timer = win.after(
            int(self._duration * 1000), lambda: self._dismiss(win)
        )

    def _build(self, win: tk.Toplevel, alert: Alert):
        PAD   = 12
        color = alert.color
        W     = _W

        # Colored top border
        tk.Frame(win, bg=color, height=3).place(x=0, y=0, width=W)

        # ✕ dismiss — top right
        btn_x = tk.Button(
            win, text="✕", bg=_BG, fg=_MUTED,
            font=_FONT_CLOSE, bd=0, relief="flat",
            activebackground=_HOVER, activeforeground=_PRI,
            cursor="hand2",
            command=lambda: self._dismiss(win),
        )
        btn_x.place(x=W - 32, y=4, width=28, height=26)

        # Badge
        tk.Label(win, text=alert.badge, bg=_BG, fg=color,
                 font=_FONT_BADGE).place(x=PAD, y=9)

        # Title
        tk.Label(
            win, text=alert.title, bg=_BG, fg=_PRI,
            font=_FONT_TITLE, anchor="w", justify="left",
            wraplength=W - PAD * 2 - 36,
        ).place(x=PAD, y=28, width=W - PAD * 2 - 36)

        # Body
        body = alert.body[:130] + "…" if len(alert.body) > 130 else alert.body
        tk.Label(
            win, text=body, bg=_BG, fg=_SEC,
            font=_FONT_BODY, anchor="nw", justify="left",
            wraplength=W - PAD * 2,
        ).place(x=PAD, y=68, width=W - PAD * 2, height=56)

        # Submit Screenshot area removed — OCR/screenshot capture deprecated
        if False:
            _params = [f"item={_url_quote(alert.item_name)}"]
            if getattr(alert, "npc_name", ""):
                _params.append(f"mob={_url_quote(alert.npc_name)}")
            _generic = ("No info yet", "Researching", "Community-verified", "Item looted")
            if alert.body and not any(g in alert.body for g in _generic):
                _params.append(f"notes={_url_quote(alert.body[:300])}")
            _fallback_url = "https://gnollguard.com/submit?" + "&".join(_params)

            # Container frame so we can swap content after clicking
            sub_frame = tk.Frame(win, bg=_BG)
            sub_frame.place(x=PAD, y=126, width=W - PAD * 2, height=54)

            def _show_capture_ui(frame=sub_frame, a=alert, fb=_fallback_url):
                for w in frame.winfo_children():
                    w.destroy()
                # Cancel auto-dismiss so popup stays open
                if self._dismiss_timer:
                    try: win.after_cancel(self._dismiss_timer)
                    except Exception: pass
                    self._dismiss_timer = None

                inst = tk.Label(frame,
                    text="Open item inspect in EQ, then click Capture:",
                    bg=_BG, fg=_GOLD, font=_FONT_SMALL, anchor="w")
                inst.pack(fill="x")

                btn_row = tk.Frame(frame, bg=_BG)
                btn_row.pack(fill="x", pady=(3, 0))

                def _countdown(lbl, remaining, a=a):
                    if remaining > 0:
                        lbl.config(text=f"📸 Capturing in {remaining}s…")
                        win.after(1000, lambda: _countdown(lbl, remaining - 1, a))
                    else:
                        lbl.config(text="📸 Capturing…")
                        if self._on_submit_screenshot:
                            self._on_submit_screenshot(a)

                cap_btn = tk.Button(btn_row, text="📸 Capture Screen",
                    bg=_BG, fg=_GOLD, font=_FONT_SMALL,
                    bd=1, relief="solid", cursor="hand2",
                    activebackground=_HOVER, activeforeground=_GOLD)
                cap_btn.pack(side="left", padx=(0, 6))
                cap_btn.config(command=lambda b=cap_btn: (
                    b.config(state="disabled"),
                    _countdown(b, 3)
                ))

                tk.Button(btn_row, text="🌐 Submit on Website",
                    bg=_BG, fg=_SEC, font=_FONT_SMALL,
                    bd=0, relief="flat", cursor="hand2",
                    activebackground=_HOVER, activeforeground=_PRI,
                    command=lambda u=fb: webbrowser.open(u),
                ).pack(side="left")

            tk.Button(sub_frame, text="📤  Submit Screenshot",
                bg=_BG, fg=color, font=_FONT_SMALL,
                bd=1, relief="solid", cursor="hand2",
                activebackground=_HOVER, activeforeground=color,
                command=_show_capture_ui,
            ).place(x=0, y=14, width=W - PAD * 2, height=24)

        # Thin divider + item extras
        if alert.alert_type == "item":
            tk.Frame(win, bg=_BORDER, height=1).place(x=0, y=160, width=W)
            self._build_item_extras(win, alert, PAD)

    def _build_item_extras(self, win: tk.Toplevel, alert: Alert, PAD: int):
        W       = _W
        Y_NPC   = 166
        Y_VOTE  = 200
        Y_STAT  = 234

        # Mob row — read-only from log parser; zone is user-editable
        mob_text = alert.npc_name or "unknown"
        tk.Label(win, text="Mob:", bg=_BG, fg=_MUTED,
                 font=_FONT_SMALL).place(x=PAD, y=Y_NPC)
        tk.Label(win, text=mob_text, bg=_BG, fg=_PRI,
                 font=_FONT_SMALL).place(x=PAD + 34, y=Y_NPC)
        tk.Label(win, text="@", bg=_BG, fg=_MUTED,
                 font=_FONT_SMALL).place(x=PAD + 168, y=Y_NPC)
        zone_var = tk.StringVar(value=self.last_zone or "")
        tk.Entry(win, textvariable=zone_var, bg=_BG2, fg=_PRI,
                 font=_FONT_SMALL, bd=1, relief="solid",
                 insertbackground=_PRI,
                 ).place(x=PAD + 182, y=Y_NPC - 2, width=126, height=18)

        # Vote frame
        vf = tk.Frame(win, bg=_BG)
        vf.place(x=PAD, y=Y_VOTE, width=W - PAD * 2, height=28)

        status_var = tk.StringVar()
        status_lbl = tk.Label(win, textvariable=status_var,
                               bg=_BG, fg=theme.ALERT_ITEM_VERIFIED,
                               font=_FONT_SMALL)
        status_lbl.place(x=PAD, y=Y_STAT)

        def _cast(vote_type, reason):
            for w in vf.winfo_children(): w.destroy()
            status_var.set(f"Voted {vote_type.title()} — {reason} ✓")
            zone = zone_var.get() or self.last_zone or ""
            if self._supabase:
                threading.Thread(target=lambda: (
                    self._supabase.submit_vote(alert.item_name, 0, vote_type, reason),
                    self._supabase.submit_drop_report(
                        alert.item_name, 0, zone, alert.npc_name or "",
                    ) if (alert.npc_name or zone) else None,
                ), daemon=True).start()

        def _show_reasons(vote_type, col):
            for w in vf.winfo_children(): w.destroy()
            for reason in VOTE_REASONS[vote_type]:
                tk.Button(
                    vf, text=reason[:14], bg=_BG, fg=col,
                    font=_FONT_SMALL, bd=1, relief="solid",
                    activebackground=_HOVER, activeforeground=col,
                    cursor="hand2",
                    command=lambda vt=vote_type, r=reason: _cast(vt, r),
                ).pack(side="left", padx=2)

        for vtype, label, col in [
            ("keep",    "👍 Keep",    theme.ALERT_ITEM_VERIFIED),
            ("sell",    "💰 Sell",    _GOLD),
            ("tribute", "🔥 Tribute", theme.ALERT_QUEST_UNVERIFIED),
        ]:
            tk.Button(
                vf, text=label, bg=_BG, fg=col,
                font=_FONT_SMALL, bd=1, relief="solid",
                activebackground=_HOVER, activeforeground=col,
                cursor="hand2",
                command=lambda vt=vtype, c=col: _show_reasons(vt, c),
            ).pack(side="left", padx=2)

    # ── Dismiss / drag / sound ────────────────────────────────────────────────

    def _dismiss(self, win: tk.Toplevel):
        if self._dismiss_timer:
            try:
                win.after_cancel(self._dismiss_timer)
            except Exception:
                pass
            self._dismiss_timer = None
        try:
            x, y = win.winfo_x(), win.winfo_y()
            if self._on_position_save:
                self._on_position_save(x, y)
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass
        self._current_alert = None

    def _play_sound(self, badge: str):
        import os, sys
        played = False
        # Try pygame first (full MP3 support, respects volume slider)
        try:
            import pygame.mixer
            filename = "alert_verified.mp3" if badge == "Verified" else "alert_loot.mp3"
            base = getattr(sys, "_MEIPASS",
                           os.path.dirname(os.path.dirname(os.path.dirname(
                               os.path.abspath(__file__)))))
            path = os.path.join(base, "assets", "sounds", filename)
            if os.path.isfile(path):
                vol = max(0.0, min(1.0, self._config.get("audio_volume", 50) / 100.0))
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                snd = pygame.mixer.Sound(path)
                snd.set_volume(vol)
                snd.play()
                played = True
        except Exception:
            log.debug("pygame sound failed", exc_info=True)
        # Fallback: Windows PC speaker beep
        if not played:
            try:
                import winsound
                winsound.Beep(1200 if badge == "Verified" else 900, 120)
            except Exception:
                pass

    def _make_draggable(self, win: tk.Toplevel):
        state: dict = {}

        def on_press(e):
            state["x"] = e.x
            state["y"] = e.y

        def on_drag(e):
            win.geometry(
                f"+{win.winfo_x() + e.x - state['x']}"
                f"+{win.winfo_y() + e.y - state['y']}"
            )

        win.bind("<ButtonPress-1>", on_press)
        win.bind("<B1-Motion>", on_drag)
