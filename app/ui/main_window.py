"""
Main application window (opened from the system tray).
Tabs: Recent Alerts | Items | Settings
Status bar at the bottom shows the log watcher state.
"""

import logging
import os
import sys
import threading
from typing import Optional

import customtkinter as ctk
from PIL import Image

from app.ui import theme
from app.ui import journal_view
from app.ui.settings import SettingsTab


def _asset(*parts) -> str:
    """Resolve a path under /assets/ whether running from source or frozen .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", *parts)

log = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class MainWindow(ctk.CTk):
    def __init__(self, app_state):
        super().__init__(fg_color=theme.BG)
        self._app = app_state
        from app.version import __version__
        self.title(f"Gnoll Guard v{__version__}")

        cfg_win = self._app.config.get("window", {})
        w = cfg_win.get("main_width", 900)
        h = cfg_win.get("main_height", 650)
        self.geometry(f"{w}x{h}")

        x = cfg_win.get("main_x")
        y = cfg_win.get("main_y")
        if x is not None and y is not None:
            self.geometry(f"+{int(x)}+{int(y)}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set window icon (title bar + taskbar)
        ico = _asset("icon.ico")
        png = _asset("icon.png")
        try:
            if os.path.isfile(ico):
                self.iconbitmap(ico)
            elif os.path.isfile(png):
                img = ctk.CTkImage(Image.open(png))
                self.iconphoto(True, img._light_image)
        except Exception:
            pass
        # CustomTkinter resets the window icon ~200ms after creation, which is why
        # the title bar showed the default blue square. Re-apply it after that.
        if os.path.isfile(ico):
            self.after(300, lambda: self._safe_iconbitmap(ico))

        self._overlay = None  # OverlayManager — owns pop-out bubbles (no dock hub)
        self._shutting_down = False
        self._build()
        # Always create the bubble manager (Journal tab pop-outs). No standalone dock window.
        self._ensure_overlay_manager()

    def _safe_iconbitmap(self, ico):
        try:
            self.iconbitmap(ico)
        except Exception:
            pass

    def safe_after(self, delay, fn):
        """Guarded self.after() for worker-thread -> UI callbacks. A background thread
        (log parsing, a Supabase fetch, the updater) can finish AFTER the window has been
        torn down on Quit; without this guard that after() crashes on a dead Tk root."""
        if self._shutting_down:
            return
        try:
            if not self.winfo_exists():
                return
            self.after(delay, fn)
        except RuntimeError:
            pass  # main loop already gone

    def _ensure_overlay_manager(self):
        """Lazy-create the OverlayManager that owns pop-out quest bubbles."""
        if getattr(self, "_overlay", None) is not None:
            return self._overlay
        try:
            from app.ui.journal_overlay import OverlayManager
            mgr = OverlayManager(self, self._app)
            mgr.set_on_change(lambda: self.safe_after(0, self._on_overlay_change))
            self._overlay = mgr
            self._app.overlay_window = mgr
            return mgr
        except Exception:
            log.exception("overlay manager init failed")
            return None

    def _on_overlay_change(self):
        """Re-render Journal when a quest is popped out or docked."""
        if getattr(self, "_journal_subtab", "Quests") == "Quests":
            # Soft refresh from in-memory journal if available (no network).
            quests = getattr(self._app, "_journal_quests", None)
            if quests is not None:
                self._render_journal(quests)
            else:
                self._refresh_journal()

    def toggle_overlay(self, enabled: bool):
        """Legacy Settings switch: show/lift open bubbles, or close all.

        There is no Quest Dock hub window anymore — quests live in the Journal tab
        and pop out individually. `enabled=True` ensures the manager exists and
        re-surfaces any open bubbles; `enabled=False` docks (closes) them all.
        """
        mgr = self._ensure_overlay_manager()
        if mgr is None:
            return
        if enabled:
            mgr.deiconify()
        else:
            mgr.close_all()

    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color=theme.PANEL, height=56, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        # Header logo — gnoll icon + title text
        try:
            _icon_img = ctk.CTkImage(Image.open(_asset("icon.png")), size=(32, 32))
            ctk.CTkLabel(
                hdr, image=_icon_img, text="",
            ).pack(side="left", padx=(theme.PAD * 2, theme.PAD_SM))
        except Exception:
            pass
        ctk.CTkLabel(
            hdr, text="GNOLL GUARD",
            font=theme.FONT_HEADER, text_color=theme.GOLD,
        ).pack(side="left")

        # Auth widget — right side of header
        self._auth_frame = ctk.CTkFrame(hdr, fg_color="transparent")
        self._auth_frame.pack(side="right", padx=theme.PAD * 2)
        self._refresh_auth_header()

        # Body: left sidebar nav + content area
        self._body = ctk.CTkFrame(self, fg_color=theme.BG, corner_radius=0)
        self._body.pack(fill="both", expand=True)

        self._sidebar = ctk.CTkFrame(self._body, fg_color=theme.PANEL, width=180, corner_radius=0)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        self._content = ctk.CTkFrame(self._body, fg_color=theme.BG, corner_radius=0)
        self._content.pack(side="left", fill="both", expand=True)

        # One content frame per section; build the existing tab content into each
        self._sections = {
            key: ctk.CTkFrame(self._content, fg_color=theme.BG, corner_radius=0)
            for key in ("Recent Alerts", "Quest Journal", "Settings")
        }
        self._build_alerts_tab(self._sections["Recent Alerts"])
        self._build_questlog_tab(self._sections["Quest Journal"])
        # SettingsTab is built lazily on first show — CTkScrollableFrame created while
        # the section is pack_forget()'d often stays permanently empty.
        self._settings_tab = SettingsTab(self._sections["Settings"], self._app)
        self._settings_tab.pack(fill="both", expand=True)

        # Sidebar nav buttons
        self._nav_buttons = {}
        for key, label, icon in (
            ("Recent Alerts", "Alerts", "🔔"),
            ("Quest Journal", "Journal", "📖"),
            ("Settings", "Settings", "⚙"),
        ):
            btn = ctk.CTkButton(
                self._sidebar, text=f"   {icon}   {label}", anchor="w",
                fg_color="transparent", text_color=theme.TEXT_SECONDARY,
                hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY,
                corner_radius=0, height=44,
                command=lambda k=key: self._show_section(k),
            )
            btn.pack(fill="x", pady=1)
            self._nav_buttons[key] = btn

        self._active_section = None
        self._show_section("Recent Alerts")

        # Status bar
        self._status_bar = ctk.CTkFrame(
            self, fg_color=theme.PANEL, height=28, corner_radius=0
        )
        self._status_bar.pack(fill="x", side="bottom")
        self._status_bar.pack_propagate(False)

        self._log_light = ctk.CTkLabel(
            self._status_bar, text="●", font=("Segoe UI", 14),
            text_color=theme.STATUS_LOG_DISCONNECTED,
        )
        self._log_light.pack(side="left", padx=(theme.PAD, 2))
        self._watcher_label = ctk.CTkLabel(
            self._status_bar, text="Log: not connected",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_SECONDARY,
        )
        self._watcher_label.pack(side="left")

        self._sync_label = ctk.CTkLabel(
            self._status_bar, text="",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED,
        )
        self._sync_label.pack(side="right", padx=theme.PAD)

    def _refresh_auth_header(self):
        for w in self._auth_frame.winfo_children():
            w.destroy()
        if self._app.auth.is_logged_in:
            name = self._app.auth.username or "Adventurer"
            ctk.CTkLabel(
                self._auth_frame, text=f"⚔  {name}",
                font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
            ).pack(side="left", padx=(0, theme.PAD_SM))
            ctk.CTkButton(
                self._auth_frame, text="Logout", width=64,
                fg_color="transparent", text_color=theme.TEXT_MUTED,
                hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
                border_width=1, border_color=theme.BORDER,
                command=lambda: self._app.auth.sign_out(),
            ).pack(side="left")
        else:
            ctk.CTkButton(
                self._auth_frame, text="Login with Discord", width=148,
                fg_color="#5865F2", text_color="#FFFFFF",
                hover_color="#4752C4", font=theme.FONT_BODY_SMALL,
                command=lambda: self._app.auth.sign_in_discord(),
            ).pack(side="left")

    # ── Alerts tab ────────────────────────────────────────────────────────────

    def _build_alerts_tab(self, parent):
        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.pack(fill="x", padx=theme.PAD, pady=theme.PAD_SM)
        ctk.CTkButton(
            toolbar, text="Clear Session",
            fg_color=theme.PANEL, text_color=theme.TEXT_SECONDARY,
            hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY, width=110,
            command=self._clear_alerts,
        ).pack(side="right")

        self._alerts_scroll = ctk.CTkScrollableFrame(
            parent, fg_color=theme.BG, scrollbar_button_color=theme.PANEL,
        )
        self._alerts_scroll.pack(fill="both", expand=True, padx=theme.PAD)
        self._alert_rows: list = []

    def add_alert_row(self, alert, on_verify=None):
        """Called from the main thread when a new alert fires."""
        row = ctk.CTkFrame(
            self._alerts_scroll, fg_color=theme.PANEL,
            corner_radius=theme.RADIUS, border_width=1,
            border_color=alert.color,
        )
        row.pack(fill="x", pady=2)

        stripe = ctk.CTkFrame(row, fg_color=alert.color, width=4, corner_radius=0)
        stripe.pack(side="left", fill="y")

        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(side="left", fill="x", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)

        title_row = ctk.CTkFrame(inner, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(
            title_row, text=alert.title,
            font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")

        badge_label = ctk.CTkLabel(
            title_row, text=alert.badge,
            font=theme.FONT_BODY_SMALL, text_color=alert.color, anchor="e",
        )
        badge_label.pack(side="right")

        ctk.CTkLabel(
            inner, text=alert.body,
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_SECONDARY,
            anchor="w", wraplength=700, justify="left",
        ).pack(fill="x")

        # Untracked quest loot: one-click Add + Ignore
        qid = getattr(alert, "quest_id", "") or ""
        item = getattr(alert, "item_name", "") or ""
        if getattr(alert, "badge", "") == "Untracked" and (qid or item):
            actions = ctk.CTkFrame(inner, fg_color="transparent")
            actions.pack(fill="x", pady=(4, 0))
            if qid:
                ctk.CTkButton(
                    actions, text="Add to journal", width=110, height=24,
                    font=theme.FONT_BODY_SMALL, fg_color=theme.GOLD,
                    text_color=theme.BG, hover_color="#e0b010",
                    command=lambda i=qid, r=row: self._alert_add_quest(i, r),
                ).pack(side="left", padx=(0, 6))
            if item:
                ctk.CTkButton(
                    actions, text="Ignore item", width=90, height=24,
                    font=theme.FONT_BODY_SMALL, fg_color=theme.PANEL_HOVER,
                    text_color=theme.TEXT_SECONDARY,
                    command=lambda n=item, r=row: self._alert_ignore_item(n, r),
                ).pack(side="left")

        self._alert_rows.append(row)
        self._alerts_scroll._parent_canvas.yview_moveto(1.0)

    def _alert_add_quest(self, quest_id, row=None):
        def work():
            ok = self._app.supabase.add_quest(quest_id)
            if ok:
                from app.main import _build_quest_index
                _build_quest_index(self._app)
                self.safe_after(0, self._refresh_journal)
        threading.Thread(target=work, daemon=True).start()
        try:
            if row is not None:
                row.destroy()
        except Exception:
            pass

    def _alert_ignore_item(self, item_name: str, row=None):
        from app.main import _load_ignored_loot, _save_ignored_loot
        names = getattr(self._app, "_ignored_loot_names", None)
        if names is None:
            names = _load_ignored_loot()
            self._app._ignored_loot_names = names
        names.add(item_name.lower())
        _save_ignored_loot(names)
        try:
            if row is not None:
                row.destroy()
        except Exception:
            pass

    def _show_section(self, key):
        if key == self._active_section:
            return
        for frame in self._sections.values():
            frame.pack_forget()
        self._sections[key].pack(fill="both", expand=True)
        self._active_section = key
        for k, btn in self._nav_buttons.items():
            if k == key:
                btn.configure(fg_color=theme.PANEL_HOVER, text_color=theme.GOLD)
            else:
                btn.configure(fg_color="transparent", text_color=theme.TEXT_SECONDARY)
        if key == "Quest Journal":
            self._refresh_active_journal()
        elif key == "Settings":
            # Must rebuild/layout only after the section is mapped, or Settings appears blank.
            # Double-call: immediate (fast) + after idle (real geometry available).
            def _show_settings():
                try:
                    self.update_idletasks()
                    self._settings_tab.ensure_visible()
                except Exception:
                    log.exception("Settings section failed to show")
            try:
                _show_settings()
                self.after(50, _show_settings)
            except Exception:
                log.exception("Settings section failed to schedule")

    def _clear_alerts(self):
        for row in self._alert_rows:
            row.destroy()
        self._alert_rows.clear()

    # ── Quest Journal tab ─────────────────────────────────────────────────────

    def _build_questlog_tab(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", padx=theme.PAD, pady=theme.PAD)
        # Sub-tab toggle — Quests and Achievements both live in "the journal".
        self._journal_subtab = "Quests"
        self._journal_seg = ctk.CTkSegmentedButton(
            header, values=["Quests", "Achievements"],
            command=self._show_journal_subtab,
            fg_color=theme.PANEL, selected_color=theme.PANEL_HOVER,
            selected_hover_color=theme.PANEL_HOVER, unselected_color=theme.PANEL,
            unselected_hover_color=theme.PANEL_HOVER,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
        )
        self._journal_seg.set("Quests")
        self._journal_seg.pack(side="left")
        ctk.CTkButton(
            header, text="Refresh", width=80,
            fg_color=theme.PANEL, hover_color=theme.PANEL_HOVER,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
            command=self._refresh_active_journal,
        ).pack(side="right")

        # Two scrollable lists; only the active sub-tab is packed at a time.
        self._journal_scroll = ctk.CTkScrollableFrame(parent, fg_color=theme.BG)
        self._journal_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._journal_widgets: list = []

        self._ach_scroll = ctk.CTkScrollableFrame(parent, fg_color=theme.BG)
        self._ach_widgets: list = []  # packed on demand by _show_journal_subtab

    def _show_journal_subtab(self, name):
        """Toggle the journal body between the Quests and Achievements lists."""
        self._journal_subtab = name
        if name == "Achievements":
            self._journal_scroll.pack_forget()
            self._ach_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
            self._refresh_achievements()
        else:
            self._ach_scroll.pack_forget()
            self._journal_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
            self._refresh_journal()

    def _refresh_active_journal(self):
        """Refresh whichever journal sub-tab is currently showing."""
        if getattr(self, "_journal_subtab", "Quests") == "Achievements":
            self._refresh_achievements()
        else:
            self._refresh_journal()

    def _refresh_journal(self):
        for w in getattr(self, "_journal_widgets", []):
            w.destroy()
        self._journal_widgets = []

        if not self._app.auth.is_logged_in:
            self._journal_msg("Log in with Discord (top right) to use your Quest Journal.")
            return

        self._journal_msg("Loading your journal…")
        def load():
            quests = self._app.supabase.get_journal()
            self.safe_after(0, lambda: self._render_journal(quests))
        threading.Thread(target=load, daemon=True).start()

    def _journal_msg(self, text):
        lbl = ctk.CTkLabel(
            self._journal_scroll, text=text, justify="left",
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY,
        )
        lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
        self._journal_widgets.append(lbl)

    def _render_journal(self, quests):
        for w in self._journal_widgets:
            w.destroy()
        self._journal_widgets = []

        # Keep the loot→journal index fresh so required items added/edited on the
        # site start ticking off as soon as the journal is refreshed.
        # Hidden/completed stay listed but do not receive loot ticks.
        try:
            from app import quest_progress
            self._app._journal_quests = quests or []
            tracking = [
                q for q in (quests or [])
                if (q.get("journal_status") or "active") in ("active", "pinned")
            ]
            self._app._quest_item_index = quest_progress.build_index(tracking)
            self._app.quest_matcher.set_quests(tracking)
        except Exception:
            pass

        if not quests:
            self._journal_msg(
                "No quests in your journal yet.\n"
                "Browse quests at gnollguard.com/quests and click “Add to Journal.”\n"
                "Plane of Sky unlocks: gnollguard.com/quests/plane-of-sky"
            )
            self._render_pos_board_button()
            self._render_rescan_button(self._journal_scroll, self._journal_widgets)
            return

        show_hidden = getattr(self, "_journal_show_hidden", False)
        active = [
            q for q in quests
            if (q.get("journal_status") or "active") not in ("hidden",)
        ]
        hidden = [q for q in quests if (q.get("journal_status") or "") == "hidden"]
        visible = quests if show_hidden else active

        # Filter bar: show/hide hidden + Plane of Sky board + re-scan log
        bar = ctk.CTkFrame(self._journal_scroll, fg_color="transparent")
        bar.pack(fill="x", padx=theme.PAD, pady=(theme.PAD, 0))
        self._journal_widgets.append(bar)
        ctk.CTkButton(
            bar, text=("Hide hidden" if show_hidden else f"Show hidden ({len(hidden)})"),
            width=130, height=26, font=theme.FONT_BODY_SMALL,
            fg_color=theme.PANEL_HOVER, text_color=theme.TEXT_SECONDARY,
            command=self._toggle_show_hidden,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            bar, text="🌥 Plane of Sky",
            width=120, height=26, font=theme.FONT_BODY_SMALL,
            fg_color=theme.PANEL_HOVER, text_color=theme.GOLD,
            command=self._open_pos_board,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            bar, text="↺ Re-scan log",
            width=110, height=26, font=theme.FONT_BODY_SMALL,
            fg_color=theme.PANEL_HOVER, text_color=theme.TEXT_SECONDARY,
            command=self._rescan_log_catchup,
        ).pack(side="left")

        if not visible:
            self._journal_msg("All journal quests are hidden. Click “Show hidden” above.")
            return

        for q in visible:
            self._render_journal_quest(q)

    def _toggle_show_hidden(self):
        self._journal_show_hidden = not getattr(self, "_journal_show_hidden", False)
        self._refresh_journal()

    def _render_pos_board_button(self):
        btn = ctk.CTkButton(
            self._journal_scroll, text="Open Plane of Sky unlock board…",
            command=self._open_pos_board, font=theme.FONT_BODY,
            fg_color=theme.PANEL_HOVER, text_color=theme.GOLD,
        )
        btn.pack(anchor="w", padx=theme.PAD, pady=6)
        self._journal_widgets.append(btn)

    def _render_rescan_button(self, parent, widget_list):
        btn = ctk.CTkButton(
            parent, text="↺ Re-scan recent log (catch up loot + slayer kills)",
            command=self._rescan_log_catchup, font=theme.FONT_BODY_SMALL,
            fg_color=theme.PANEL_HOVER, text_color=theme.TEXT_SECONDARY,
        )
        btn.pack(anchor="w", padx=theme.PAD, pady=4)
        widget_list.append(btn)

    def _rescan_log_catchup(self):
        """T1.5 — re-read recent log lines for missed journal loot + slayer kills."""
        import tkinter.messagebox as _mb
        def work():
            try:
                watcher = getattr(self._app, "log_watcher", None)
                if watcher is None:
                    self.safe_after(0, lambda: _mb.showinfo(
                        "Re-scan", "Log watcher not ready.", parent=self))
                    return
                lines = watcher.rescan_recent()
                loot_evts = watcher.parse_loot_from_lines(lines)
                from app.main import _on_loot
                n_loot = 0
                for evt in loot_evts:
                    try:
                        _on_loot(self._app, evt)
                        n_loot += 1
                    except Exception:
                        pass
                # Slayer kills from same window
                from app import slayer_progress
                import re
                kill_re = re.compile(r"You have slain (?P<mob>.+?)!", re.I)
                achs = getattr(self._app, "_achievement_journal", None) or []
                if not achs:
                    try:
                        achs = self._app.supabase.get_achievement_journal() or []
                        self._app._achievement_journal = achs
                    except Exception:
                        achs = []
                n_kill = slayer_progress.rescan_kills_from_lines(
                    lines, achs, kill_re)
                msg = (
                    f"Scanned {len(lines):,} log lines.\n"
                    f"Loot events re-checked: {n_loot:,}\n"
                    f"Kill lines matched: {n_kill:,}\n\n"
                    "Journal items already ticked stay ticked; new matches apply now."
                )
                self.safe_after(0, lambda: (
                    _mb.showinfo("Re-scan complete", msg, parent=self),
                    self._refresh_journal(),
                    self._refresh_achievements(),
                ))
            except Exception as e:
                self.safe_after(0, lambda: _mb.showerror(
                    "Re-scan failed", str(e), parent=self))
        threading.Thread(target=work, daemon=True).start()

    def _open_pos_board(self):
        """Lightweight PoS class-unlock board window (T1.6)."""
        import webbrowser
        webbrowser.open("https://www.gnollguard.com/quests/plane-of-sky")
        # Also show a local checklist if we can load quests
        def load():
            rows = self._app.supabase.get_plane_of_sky_quests() or []
            journal_ids = {
                q.get("id") for q in (getattr(self._app, "_journal_quests", None) or [])
            }
            self.safe_after(0, lambda: self._show_pos_board(rows, journal_ids))
        threading.Thread(target=load, daemon=True).start()

    def _show_pos_board(self, rows, journal_ids):
        win = ctk.CTkToplevel(self)
        win.title("Plane of Sky Class Unlocks")
        win.geometry("480x560")
        win.attributes("-topmost", True)
        scroll = ctk.CTkScrollableFrame(win, fg_color=theme.BG)
        scroll.pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkLabel(
            scroll, text="Plane of Sky — class primary unlocks",
            font=theme.FONT_SUBHEADER, text_color=theme.GOLD,
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(
            scroll,
            text="Add tests to your journal on the site or here. "
                 "✓ = already in journal. Full board: gnollguard.com/quests/plane-of-sky",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_SECONDARY,
            wraplength=440, justify="left",
        ).pack(anchor="w", pady=(0, 8))
        by_class = {}
        for r in rows:
            c = r.get("char_class") or "Other"
            by_class.setdefault(c, []).append(r)
        for cls in sorted(by_class.keys()):
            ctk.CTkLabel(
                scroll, text=cls, font=theme.FONT_BODY, text_color=theme.GOLD,
            ).pack(anchor="w", pady=(8, 2))
            for r in by_class[cls]:
                qid = r.get("id")
                name = r.get("quest_name") or "Quest"
                mark = "✓ " if qid in journal_ids else "○ "
                row = ctk.CTkFrame(scroll, fg_color=theme.PANEL, corner_radius=6)
                row.pack(fill="x", pady=2)
                ctk.CTkLabel(
                    row, text=mark + name, font=theme.FONT_BODY_SMALL,
                    text_color=theme.TEXT_PRIMARY, anchor="w",
                ).pack(side="left", padx=8, pady=4, fill="x", expand=True)
                if qid not in journal_ids:
                    ctk.CTkButton(
                        row, text="Add", width=50, height=24,
                        font=theme.FONT_BODY_SMALL,
                        command=lambda i=qid, w=win: self._add_quest_from_board(i, w),
                    ).pack(side="right", padx=6, pady=4)

    def _add_quest_from_board(self, quest_id, win=None):
        def work():
            self._app.supabase.add_quest(quest_id)
            self.safe_after(0, self._refresh_journal)
        threading.Thread(target=work, daemon=True).start()

    # journal_view's default theme is already this app's dark-gold palette, so no
    # override dict is needed here — the Officer Console passes its own steel-cyan one.
    def _journal_quest_header(self, card, title_row, q):
        """Trash + Pop out/Dock + pin/hide/done triage on each journal quest card."""
        status = (q.get("journal_status") or "active").lower()
        ctk.CTkButton(
            title_row, text="🗑", width=28, height=24,
            fg_color="transparent", text_color=theme.TEXT_MUTED,
            hover_color=theme.DANGER, font=theme.FONT_BODY_SMALL,
            command=lambda qq=q: self._delete_quest(qq),
        ).pack(side="right", padx=1)
        qid = q.get("id")
        mgr = self._ensure_overlay_manager()
        popped = bool(mgr and mgr.is_popped(qid))
        if popped:
            ctk.CTkButton(
                title_row, text="Dock", width=56, height=26,
                fg_color=theme.PANEL_HOVER, hover_color=theme.GOLD,
                text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY_SMALL,
                corner_radius=8,
                command=lambda i=qid: self._dock_quest(i),
            ).pack(side="right", padx=(0, 4))
        else:
            ctk.CTkButton(
                title_row, text="Pop out", width=72, height=26,
                fg_color=theme.PANEL_HOVER, hover_color=theme.GOLD,
                text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY_SMALL,
                corner_radius=8,
                command=lambda qq=q: self._pop_out_quest(qq),
            ).pack(side="right", padx=(0, 4))
        ctk.CTkButton(
            title_row, text="✓", width=28, height=24,
            fg_color="transparent",
            text_color=theme.GREEN if status == "completed" else theme.TEXT_MUTED,
            hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
            command=lambda qq=q: self._set_quest_status(qq, "completed"),
        ).pack(side="right", padx=1)
        ctk.CTkButton(
            title_row, text="Hide", width=40, height=24,
            fg_color="transparent",
            text_color=theme.TEXT_MUTED,
            hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
            command=lambda qq=q: self._set_quest_status(qq, "hidden"),
        ).pack(side="right", padx=1)
        ctk.CTkButton(
            title_row, text="📌", width=28, height=24,
            fg_color="transparent",
            text_color=theme.GOLD if status == "pinned" else theme.TEXT_MUTED,
            hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
            command=lambda qq=q: self._set_quest_status(
                qq, "active" if status == "pinned" else "pinned"
            ),
        ).pack(side="right", padx=1)
        if status in ("pinned", "completed", "hidden"):
            ctk.CTkLabel(
                title_row, text=status.upper(), font=theme.FONT_BODY_SMALL,
                text_color=theme.TEXT_SECONDARY,
            ).pack(side="right", padx=4)

    def _set_quest_status(self, q, status: str):
        qid = q.get("id")
        if not qid:
            return
        q["journal_status"] = status
        # Keep local list in sync for matchers (hidden still in list but filtered in UI)
        for jq in getattr(self._app, "_journal_quests", []) or []:
            if jq.get("id") == qid:
                jq["journal_status"] = status
        # Active tracking index: exclude hidden/completed from loot ticks
        try:
            from app import quest_progress
            active = [
                jq for jq in (self._app._journal_quests or [])
                if (jq.get("journal_status") or "active") in ("active", "pinned")
            ]
            self._app._quest_item_index = quest_progress.build_index(active)
            self._app.quest_matcher.set_quests(active)
        except Exception:
            pass
        threading.Thread(
            target=lambda: self._app.supabase.set_quest_status(qid, status),
            daemon=True,
        ).start()
        self._refresh_journal()

    def _pop_out_quest(self, q):
        """Open this journal quest as a standalone always-on-top overlay (max 5)."""
        from app.ui.journal_overlay import MAX_BUBBLES
        mgr = self._ensure_overlay_manager()
        if mgr is None:
            return
        if not mgr.pop_out(q):
            import tkinter.messagebox as _mb
            n = len(mgr.popped_ids())
            if n >= MAX_BUBBLES:
                _mb.showinfo(
                    "Pop-out limit",
                    f"You can have up to {MAX_BUBBLES} quest windows open.\n"
                    "Dock one first, then pop out another.",
                    parent=self,
                )

    def _dock_quest(self, qid):
        """Close the pop-out window and restore the quest to the Journal tab only."""
        mgr = self._ensure_overlay_manager()
        if mgr is not None:
            mgr.dock(qid)

    def _render_journal_quest(self, q):
        matcher = getattr(self._app, "quest_matcher", None)
        prog = getattr(self._app, "_quest_progress", set())
        given = getattr(self._app, "_quest_given", set())
        # Pass the app's LIVE theme (it varies dark/light at runtime via theme.apply())
        # rather than relying on journal_view's static fallback palette.
        jv_theme = {
            "panel": theme.PANEL, "panel_hover": theme.PANEL_HOVER, "border": theme.BORDER,
            "gold": theme.GOLD, "green": theme.GREEN, "text": theme.TEXT_PRIMARY,
            "text_secondary": theme.TEXT_SECONDARY, "font_body": theme.FONT_BODY,
            "font_body_small": theme.FONT_BODY_SMALL, "font_subheader": theme.FONT_SUBHEADER,
        }
        card = journal_view.render_quest_card(
            self._journal_scroll, q, matcher, prog, given, theme=jv_theme,
            on_toggle_step=self._toggle_step,
            on_copy=self._copy_to_clipboard,
            extra_header=lambda card, title_row: self._journal_quest_header(card, title_row, q),
        )
        # Status line when already popped out
        mgr = getattr(self, "_overlay", None)
        if mgr is not None and mgr.is_popped(q.get("id")):
            note = ctk.CTkLabel(
                card, text="  ● open as overlay — drag near others to snap · Shift+drag to unsnap",
                font=theme.FONT_BODY_SMALL, text_color=theme.GOLD, anchor="w",
            )
            note.pack(anchor="w", padx=10, pady=(0, 6))
        self._journal_widgets.append(card)

    def _copy_to_clipboard(self, text: str):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except Exception:
            log.debug("clipboard copy failed", exc_info=True)

    def _toggle_step(self, q, s, was_done: bool):
        """Manual override — the safety net when the log misses a line (zone
        crash, lag, an unmatched phrasing). Never touches the network."""
        matcher = getattr(self._app, "quest_matcher", None)
        if not matcher:
            return
        qid, order = q.get("id"), s.get("step_order")
        if was_done:
            matcher.mark_undone(qid, order)
        else:
            matcher.mark_done(qid, order)
        self._refresh_journal()

    def _delete_quest(self, q):
        """Trashcan: remove this quest from the journal (local + Supabase)."""
        import tkinter.messagebox as _mb
        name = q.get("quest_name", "this quest")
        if not _mb.askyesno("Remove quest",
                            f"Remove “{name}” from your journal?", parent=self):
            return
        qid = q.get("id")
        # Dock any open pop-out for this quest first.
        self._dock_quest(qid)
        # Drop it locally right away so the UI feels instant.
        try:
            self._app._journal_quests = [
                jq for jq in getattr(self._app, "_journal_quests", []) if jq.get("id") != qid
            ]
            from app import quest_progress
            self._app._quest_item_index = quest_progress.build_index(self._app._journal_quests)
            self._app.quest_matcher.set_quests(self._app._journal_quests)
        except Exception:
            pass
        threading.Thread(
            target=lambda: self._app.supabase.remove_quest(qid), daemon=True
        ).start()
        self._refresh_journal()

    # ── Achievement journal sub-tab ───────────────────────────────────────────
    # Mirrors the quest journal, but achievements aren't item-loot-based, so there's
    # no loot->tick logic here — it's a saved, browsable checklist of their steps.

    def _refresh_achievements(self):
        for w in getattr(self, "_ach_widgets", []):
            w.destroy()
        self._ach_widgets = []

        if not self._app.auth.is_logged_in:
            self._ach_journal_msg("Log in with Discord (top right) to use your journal.")
            return

        self._ach_journal_msg("Loading your achievements…")
        def load():
            achs = self._app.supabase.get_achievement_journal()
            self._app._achievement_journal = achs or []
            try:
                from app import slayer_progress
                prog = slayer_progress.load_progress()
                self._app._slayer_progress = prog
                achs = [slayer_progress.enrich_achievement(a, prog) for a in (achs or [])]
            except Exception:
                pass
            self.safe_after(0, lambda: self._render_achievement_journal(achs))
        threading.Thread(target=load, daemon=True).start()

    def _ach_journal_msg(self, text):
        lbl = ctk.CTkLabel(
            self._ach_scroll, text=text, justify="left",
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY,
        )
        lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
        self._ach_widgets.append(lbl)

    def _render_achievement_journal(self, achs):
        for w in self._ach_widgets:
            w.destroy()
        self._ach_widgets = []

        if not achs:
            self._ach_journal_msg(
                "No achievements in your journal yet.\n"
                "Browse gnollguard.com/achievements and click “Add to Journal.”"
            )
            return

        for a in achs:
            self._render_journal_achievement(a)

    def _render_journal_achievement(self, a):
        card = ctk.CTkFrame(self._ach_scroll, fg_color=theme.PANEL, corner_radius=8)
        card.pack(fill="x", padx=theme.PAD, pady=4)

        title_row = ctk.CTkFrame(card, fg_color="transparent")
        title_row.pack(fill="x", padx=theme.PAD, pady=(theme.PAD_SM, 0))
        ctk.CTkLabel(
            title_row, text=a.get("name", "Achievement"), font=theme.FONT_SUBHEADER,
            text_color=theme.GOLD, anchor="w",
        ).pack(side="left")
        ctk.CTkButton(
            title_row, text="🗑", width=30, height=26,
            fg_color="transparent", text_color=theme.TEXT_MUTED,
            hover_color=theme.DANGER, font=theme.FONT_BODY,
            command=lambda aa=a: self._delete_achievement(aa),
        ).pack(side="right")

        meta = []
        if a.get("category"):
            meta.append(a["category"])
        if a.get("points"):
            meta.append(f"{a['points']} pts")
        if meta:
            ctk.CTkLabel(
                card, text="  •  ".join(meta), font=theme.FONT_BODY_SMALL,
                text_color=theme.TEXT_SECONDARY, anchor="w",
            ).pack(anchor="w", padx=theme.PAD)
        if a.get("description"):
            ctk.CTkLabel(
                card, text=a["description"], font=theme.FONT_BODY_SMALL,
                text_color=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=460,
            ).pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, 0))

        # Steps as a numbered checklist. Slayer steps show current/target kills.
        steps = sorted(a.get("steps", []) or [], key=lambda s: s.get("step_order", 0))
        MAX_STEPS = 40
        for i, s in enumerate(steps[:MAX_STEPS], 1):
            desc = s.get("description") or ""
            tcount = s.get("target_count")
            if tcount and (s.get("target_kind") == "kill" or s.get("progress_count") is not None):
                cur = int(s.get("progress_count") or 0)
                mobs = s.get("target_mobs") or desc
                done = cur >= int(tcount)
                line = f"  {i}. {cur}/{int(tcount)} kills — {mobs}"
                color = theme.GREEN if done else theme.TEXT_SECONDARY
            else:
                line = f"  {i}. {desc}"
                color = theme.TEXT_SECONDARY
            ctk.CTkLabel(
                card, text=line, font=theme.FONT_BODY_SMALL,
                text_color=color, anchor="w", justify="left", wraplength=460,
            ).pack(anchor="w", padx=theme.PAD, pady=(1, 0))
        if len(steps) > MAX_STEPS:
            ctk.CTkLabel(
                card, text=f"  +{len(steps) - MAX_STEPS} more — see gnollguard.com/achievements",
                font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED, anchor="w",
            ).pack(anchor="w", padx=theme.PAD, pady=(1, 0))

        ctk.CTkLabel(card, text="", font=theme.FONT_BODY_SMALL).pack(pady=(0, theme.PAD_SM))
        self._ach_widgets.append(card)

    def _delete_achievement(self, a):
        """Trashcan: remove this achievement from the journal (local + Supabase)."""
        import tkinter.messagebox as _mb
        name = a.get("name", "this achievement")
        if not _mb.askyesno("Remove achievement",
                            f"Remove “{name}” from your journal?", parent=self):
            return
        aid = a.get("achievement_id")
        threading.Thread(
            target=lambda: self._app.supabase.remove_achievement(aid), daemon=True
        ).start()
        self._refresh_achievements()

    # ── Status bar updates ────────────────────────────────────────────────────

    def update_watcher_status(self, status: str):
        if status.startswith("watching"):
            light_color = theme.STATUS_LOG_WATCHING
        elif status.startswith("reading"):
            light_color = theme.STATUS_LOG_READING
        else:
            light_color = theme.STATUS_LOG_DISCONNECTED
        self._log_light.configure(text_color=light_color)
        self._watcher_label.configure(text=f"Log: {status}")

    def update_sync_status(self, text: str):
        self._sync_label.configure(text=text)

    def show_update_banner(self, version: str, download_url: str, changelog: str):
        """Non-intrusive gold banner at top when a new version is available."""
        import webbrowser
        banner = ctk.CTkFrame(self, fg_color="#1A1000", corner_radius=0)
        banner.pack(fill="x", before=self._body)
        ctk.CTkLabel(
            banner,
            text=f"⬆  Gnoll Guard {version} is available!",
            font=theme.FONT_BODY_SMALL, text_color=theme.GOLD,
        ).pack(side="left", padx=theme.PAD, pady=4)
        if changelog:
            ctk.CTkLabel(
                banner, text=changelog[:80],
                font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED,
            ).pack(side="left")
        ctk.CTkButton(
            banner, text="Update Now", width=110,
            fg_color=theme.GOLD, text_color=theme.BG,
            hover_color=theme.GREEN, font=theme.FONT_BODY_SMALL,
            command=lambda u=download_url: self._install_update(u),
        ).pack(side="right", padx=theme.PAD, pady=3)

    # ── In-app updater ────────────────────────────────────────────────────────

    def _install_update(self, page_url: str):
        import tkinter.messagebox as _mb
        import urllib.request, tempfile, os, subprocess, threading

        if not _mb.askyesno("Update Gnoll Guard",
                             "Download and install the latest version now?\n\n"
                             "The app will close automatically to run the installer."):
            return

        # Resolve direct installer URL from the download page URL
        installer_url = "https://gnollguard.com/api/download"

        def _do():
            try:
                tmp = os.path.join(tempfile.gettempdir(), "GnollGuard-Setup.exe")
                self.safe_after(0, lambda: _mb.showinfo("Downloading…",
                    "Downloading update. The app will close and the installer will open."))
                urllib.request.urlretrieve(installer_url, tmp)
                subprocess.Popen([tmp])
                os._exit(0)
            except Exception as e:
                self.safe_after(0, lambda: _mb.showerror("Update Failed",
                    f"Could not download update:\n{e}\n\nTry downloading manually at gnollguard.com/download"))

        threading.Thread(target=_do, daemon=True).start()

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        """Hide to tray rather than quit. Show a one-time hint the first time."""
        import tkinter.messagebox as _mb
        cfg = getattr(self._app, "config", {})
        if not cfg.get("_tray_hint_shown"):
            cfg["_tray_hint_shown"] = True
            try:
                self._app.save_config()
            except Exception:
                pass
            _mb.showinfo(
                "Gnoll Guard is still running",
                "Gnoll Guard is watching your loot in the background.\n\n"
                "Find it in the system tray (bottom-right of your taskbar).\n"
                "Right-click the icon to quit.",
                parent=self,
            )
        self.withdraw()
