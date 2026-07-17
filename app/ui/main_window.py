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

        self._overlay = None
        self._build()

        # Re-open the detached overlay if the user had it enabled.
        if self._app.config.get("overlay_enabled"):
            self.after(600, lambda: self.toggle_overlay(True))

    def _safe_iconbitmap(self, ico):
        try:
            self.iconbitmap(ico)
        except Exception:
            pass

    def toggle_overlay(self, enabled: bool):
        """Spawn or destroy the detached always-on-top Quest overlay (a child window)."""
        if enabled:
            if getattr(self, "_overlay", None) is not None and self._overlay.winfo_exists():
                self._overlay.deiconify(); self._overlay.lift()
                return
            try:
                from app.ui.journal_overlay import JournalOverlay
                self._overlay = JournalOverlay(self, self._app)
                self._app.overlay_window = self._overlay
            except Exception:
                log.exception("overlay open failed")
        else:
            ov = getattr(self, "_overlay", None)
            if ov is not None and ov.winfo_exists():
                ov.destroy()
            self._overlay = None
            self._app.overlay_window = None

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

        self._alert_rows.append(row)
        self._alerts_scroll._parent_canvas.yview_moveto(1.0)

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
            self.after(0, lambda: self._render_journal(quests))
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
        try:
            from app import quest_progress
            self._app._journal_quests = quests or []
            self._app._quest_item_index = quest_progress.build_index(quests)
        except Exception:
            pass

        if not quests:
            self._journal_msg(
                "No quests in your journal yet.\n"
                "Browse quests at gnollguard.com/quests and click “Add to Journal.”"
            )
            return

        for q in quests:
            self._render_journal_quest(q)

    def _render_journal_quest(self, q):
        card = ctk.CTkFrame(self._journal_scroll, fg_color=theme.PANEL, corner_radius=8)
        card.pack(fill="x", padx=theme.PAD, pady=4)

        title_row = ctk.CTkFrame(card, fg_color="transparent")
        title_row.pack(fill="x", padx=theme.PAD, pady=(theme.PAD_SM, 0))
        ctk.CTkLabel(
            title_row, text=q.get("quest_name", "Quest"), font=theme.FONT_SUBHEADER,
            text_color=theme.GOLD, anchor="w",
        ).pack(side="left")
        ctk.CTkButton(
            title_row, text="🗑", width=30, height=26,
            fg_color="transparent", text_color=theme.TEXT_MUTED,
            hover_color=theme.DANGER, font=theme.FONT_BODY,
            command=lambda qq=q: self._delete_quest(qq),
        ).pack(side="right")
        if q.get("zone"):
            ctk.CTkLabel(
                card, text=q["zone"], font=theme.FONT_BODY_SMALL,
                text_color=theme.TEXT_SECONDARY, anchor="w",
            ).pack(anchor="w", padx=theme.PAD)

        prog = getattr(self._app, "_quest_progress", set())
        given = getattr(self._app, "_quest_given", set())
        steps = sorted(q.get("steps", []) or [], key=lambda s: s.get("step_order", 0))
        for s in steps:
            num = s.get("step_order", "")
            npc = s.get("npc_name") or ""
            head = f"{num}. {npc}" if npc else f"{num}."
            ctk.CTkLabel(
                card, text=head, font=theme.FONT_BODY_SMALL,
                text_color=theme.TEXT_PRIMARY, anchor="w",
            ).pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, 0))
            if s.get("instruction"):
                ctk.CTkLabel(
                    card, text="   " + s["instruction"], font=theme.FONT_BODY_SMALL,
                    text_color=theme.TEXT_SECONDARY, anchor="w", justify="left", wraplength=460,
                ).pack(anchor="w", padx=theme.PAD)
            # Required items, each with a looted-state checkmark (✓ green / ○ muted)
            req = s.get("required_items") or []
            if req:
                irow = ctk.CTkFrame(card, fg_color="transparent")
                irow.pack(anchor="w", padx=theme.PAD, pady=(2, 0))
                ctk.CTkLabel(
                    irow, text="   Items:", font=theme.FONT_BODY_SMALL,
                    text_color=theme.TEXT_MUTED,
                ).pack(side="left")
                for it in req:
                    low = it.lower()
                    if low in given:
                        mark, col = "  ✔ ", theme.GREEN       # turned in to NPC
                    elif low in prog:
                        mark, col = "  ✓ ", theme.GOLD        # looted, not yet turned in
                    else:
                        mark, col = "  ○ ", theme.TEXT_SECONDARY
                    ctk.CTkLabel(
                        irow, text=mark + it,
                        font=theme.FONT_BODY_SMALL, text_color=col,
                    ).pack(side="left")

        if q.get("reward_items"):
            ctk.CTkLabel(
                card, text="Rewards: " + ", ".join(q["reward_items"]),
                font=theme.FONT_BODY_SMALL, text_color=theme.GOLD, anchor="w",
            ).pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, theme.PAD_SM))
        else:
            ctk.CTkLabel(card, text="", font=theme.FONT_BODY_SMALL).pack(pady=(0, theme.PAD_SM))

        self._journal_widgets.append(card)

    def _delete_quest(self, q):
        """Trashcan: remove this quest from the journal (local + Supabase)."""
        import tkinter.messagebox as _mb
        name = q.get("quest_name", "this quest")
        if not _mb.askyesno("Remove quest",
                            f"Remove “{name}” from your journal?", parent=self):
            return
        qid = q.get("id")
        # Drop it locally right away so the UI feels instant.
        try:
            self._app._journal_quests = [
                jq for jq in getattr(self._app, "_journal_quests", []) if jq.get("id") != qid
            ]
            from app import quest_progress
            self._app._quest_item_index = quest_progress.build_index(self._app._journal_quests)
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
            self.after(0, lambda: self._render_achievement_journal(achs))
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

        # Steps as a numbered checklist. Some achievements have 100+ components, so
        # cap the rendered rows to keep the HUD snappy and show a "+N more" pointer.
        steps = sorted(a.get("steps", []) or [], key=lambda s: s.get("step_order", 0))
        MAX_STEPS = 40
        for i, s in enumerate(steps[:MAX_STEPS], 1):
            desc = s.get("description") or ""
            ctk.CTkLabel(
                card, text=f"  {i}. {desc}", font=theme.FONT_BODY_SMALL,
                text_color=theme.TEXT_SECONDARY, anchor="w", justify="left", wraplength=460,
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
                self.after(0, lambda: _mb.showinfo("Downloading…",
                    "Downloading update. The app will close and the installer will open."))
                urllib.request.urlretrieve(installer_url, tmp)
                subprocess.Popen([tmp])
                os._exit(0)
            except Exception as e:
                self.after(0, lambda: _mb.showerror("Update Failed",
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
