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
from app.ui import notify          # alert sound + taskbar flash
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
            for key in ("Recent Alerts", "Quest Journal", "Combat", "Tools", "Settings")
        }
        self._build_alerts_tab(self._sections["Recent Alerts"])
        self._build_questlog_tab(self._sections["Quest Journal"])
        # Combat tab — the DPS parser finally has a consumer. Guarded: a fault here
        # must not stop the rest of the window from building.
        # Use the FULL combat readout -- browsable history, pinning, per-actor drill-down
        # (melee/spell/dot split, hit chance, crit rate, best and average hit). The plainer
        # `CombatView` is the one the owner had already rejected as "kinda plain no real
        # details to it"; the first merge pass regressed to it by accident. It stays in the
        # tree as the fallback if the ported section cannot build.
        self._combat_view = None
        try:
            from app.ui.tools_section import CombatSection
            self._combat_view = CombatSection(
                self._sections["Combat"], self._app,
                feed=getattr(self._app, "combat_feed", None))
            self._combat_view.pack(fill="both", expand=True)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("combat tab failed to build")
        # Tools — the devkit tabs (Healing / Loot / Gear / Codex) plus the overlay, merged
        # in on 2026-08-25 so the journal and the DPS work are one app. It shares the SAME
        # CombatFeed the Combat tab uses, so the two can never disagree. Guarded like the
        # Combat tab: a fault here degrades one section, it does not stop the window.
        self._tools_section = None
        try:
            from app.ui.tools_section import ToolsSection
            self._tools_section = ToolsSection(
                self._sections["Tools"], self._app,
                feed=getattr(self._app, "combat_feed", None))
            self._tools_section.pack(fill="both", expand=True)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("tools tab failed to build")

        # SettingsTab is built lazily on first show — CTkScrollableFrame created while
        # the section is pack_forget()'d often stays permanently empty.
        self._settings_tab = SettingsTab(self._sections["Settings"], self._app)
        self._settings_tab.pack(fill="both", expand=True)

        # Sidebar nav buttons
        self._nav_buttons = {}
        for key, label, icon in (
            ("Recent Alerts", "Alerts", "🔔"),
            ("Quest Journal", "Journal", "📖"),
            ("Combat", "Combat", "⚔"),
            ("Tools", "Tools", "🧰"),
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

    # ── Getting the player's attention ───────────────────────────────────────
    #
    # Owner, 2026-08-16: *"if we are going to alart the person its there we need some
    # kinda alart sound plus a blinking taskbar/alart tab indicating theres an alart in
    # this window (the taskbar) and on this section of the app (alart window tab)."*
    #
    # The player is in-game and full-screen. A row quietly appearing in a list behind
    # the game is not an alert, so this adds the two channels that actually reach them:
    # a sound, and a flashing taskbar button. The nav badge is for when they alt-tab
    # back and need to see WHERE the new thing is.
    def _notify_new_alert(self):
        """Sound + taskbar flash + unread badge. Never raises."""
        try:
            self._unread_alerts = getattr(self, "_unread_alerts", 0) + 1
            self._update_alert_badge()

            # 🔴 Do NOT shout if they are already looking at it. Window focused AND the
            # Alerts section open means the row appearing IS the notification; beeping
            # at someone who is watching is how a feature gets turned off for good.
            looking = (notify.window_has_focus(self)
                       and self._active_section == "Recent Alerts")
            if looking:
                self._unread_alerts = 0
                self._update_alert_badge()
                return

            if bool(self._app.config.get("alert_sound", True)):
                notify.play_alert_sound(self._app.config.get("alert_volume", 70))
            if bool(self._app.config.get("alert_flash", True)):
                notify.flash_taskbar(self)
            # Opt-in, default OFF: an overlay drawn over the game uninvited is the most
            # intrusive thing this app can do. Only reached when they switched it on.
            if bool(self._app.config.get("alert_toast", False)):
                notify.show_toast(
                    self, alert.title, alert.body,
                    seconds=int(self._app.config.get("alert_toast_secs", 5)),
                    color=getattr(alert, "color", theme.GOLD),
                )
        except Exception:
            log.debug("alert notification failed", exc_info=True)

    def _update_alert_badge(self):
        """Show the unread count on the Alerts nav button, e.g. '🔔 Alerts (3)'."""
        try:
            btn = self._nav_buttons.get("Recent Alerts")
            if btn is None:
                return
            n = getattr(self, "_unread_alerts", 0)
            if n:
                btn.configure(text=f"   🔔   Alerts  ({n})", text_color=theme.GOLD)
            else:
                active = (self._active_section == "Recent Alerts")
                btn.configure(text="   🔔   Alerts",
                              text_color=theme.GOLD if active else theme.TEXT_SECONDARY)
        except Exception:
            log.debug("alert badge update failed", exc_info=True)

    def add_alert_row(self, alert, on_verify=None):
        """Called from the main thread when a new alert fires."""
        self._notify_new_alert()
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
        # Opening Alerts IS acknowledging them: drop the unread count and stop the
        # taskbar flashing. Leaving either running after the user has looked is the
        # fastest way to teach them the indicator means nothing.
        if key == "Recent Alerts":
            self._unread_alerts = 0
            self._update_alert_badge()
            notify.stop_flashing(self)
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
                "New character? gnollguard.com/quests/starter\n"
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
        def load():
            rows = self._app.supabase.get_plane_of_sky_quests() or []
            status_by = {}
            for q in (getattr(self._app, "_journal_quests", None) or []):
                if q.get("id") is not None:
                    status_by[q.get("id")] = (q.get("journal_status") or "active")
            self.safe_after(0, lambda: self._show_pos_board(rows, status_by))
        threading.Thread(target=load, daemon=True).start()

    def _show_pos_board(self, rows, status_by):
        """PoS board: ✓ done · ▶ in progress · ○ add."""
        status_by = status_by or {}
        win = ctk.CTkToplevel(self)
        win.title("Plane of Sky Class Unlocks")
        win.geometry("500x580")
        win.attributes("-topmost", True)
        scroll = ctk.CTkScrollableFrame(win, fg_color=theme.BG)
        scroll.pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkLabel(
            scroll, text="Plane of Sky — class primary unlocks",
            font=theme.FONT_SUBHEADER, text_color=theme.GOLD,
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(
            scroll,
            text="✓ Done · ▶ In progress · ○ Not tracking. "
                 "Don't destroy completed PoS items until unlock sticks. "
                 "Full board: gnollguard.com/quests/plane-of-sky",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_SECONDARY,
            wraplength=460, justify="left",
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
                st = (status_by.get(qid) or "").lower()
                if st == "completed":
                    mark, color = "✓ ", theme.GREEN
                elif st in ("active", "pinned"):
                    mark, color = "▶ ", theme.GOLD
                else:
                    mark, color = "○ ", theme.TEXT_PRIMARY
                row = ctk.CTkFrame(scroll, fg_color=theme.PANEL, corner_radius=6)
                row.pack(fill="x", pady=2)
                ctk.CTkLabel(
                    row, text=mark + name, font=theme.FONT_BODY_SMALL,
                    text_color=color, anchor="w",
                ).pack(side="left", padx=8, pady=4, fill="x", expand=True)
                if not st:
                    ctk.CTkButton(
                        row, text="Add", width=50, height=24,
                        font=theme.FONT_BODY_SMALL,
                        command=lambda i=qid, w=win: self._add_quest_from_board(i, w),
                    ).pack(side="right", padx=6, pady=4)
                elif st == "completed":
                    ctk.CTkLabel(
                        row, text="Done", font=theme.FONT_BODY_SMALL, text_color=theme.GREEN,
                    ).pack(side="right", padx=6, pady=4)
                else:
                    ctk.CTkLabel(
                        row, text="Tracking", font=theme.FONT_BODY_SMALL, text_color=theme.GOLD,
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
        """Download the installer and hand off to it.

        HARDENED 2026-08-03. The previous version ran whatever came back from the
        URL without checking it. `/api/download` is a redirect to GitHub, and when
        that path breaks it returns an HTML ERROR PAGE, not an installer — exactly
        what happened on 2026-07-27 when the private repo gated release assets and
        every download 404'd. The app would have cheerfully executed that HTML.

        Now it: downloads to a .part file, verifies size and the MZ (PE) header,
        only then renames to .exe and launches. Any failure falls back to opening
        the download page rather than leaving the user stuck.
        """
        import tkinter.messagebox as _mb
        import os
        import subprocess
        import tempfile
        import threading
        import urllib.request
        import webbrowser

        if not _mb.askyesno("Update Gnoll Guard",
                             "Download and install the latest version now?\n\n"
                             "The app will close automatically to run the installer."):
            return

        # Both hosts serve the SAME deployment. The second exists because some
        # ISP filters block gnollguard.com outright — Comcast/Xfinity did on
        # 2026-08-04, on domain reputation (unsigned exe, weeks-old domain). The
        # older domain resolves to a different IP and got through. Without this
        # fallback, an affected user can install but can never UPDATE.
        installer_urls = (
            "https://gnollguard.com/api/download",
            "https://legendsgnollloot.com/api/download",
        )
        fallback_page = page_url or "https://gnollguard.com/download"
        MIN_BYTES = 5 * 1024 * 1024        # a real build is ~49 MB; an error page is bytes

        def _fail(msg: str):
            def _show():
                _mb.showerror(
                    "Update Failed",
                    f"{msg}\n\nOpening the download page so you can grab it manually.")
                try:
                    webbrowser.open(fallback_page)
                except Exception:
                    pass
            self.safe_after(0, _show)

        def _do():
            part = os.path.join(tempfile.gettempdir(), "GnollGuard-Setup.exe.part")
            final = os.path.join(tempfile.gettempdir(), "GnollGuard-Setup.exe")
            try:
                self.safe_after(0, lambda: _mb.showinfo(
                    "Downloading…",
                    "Downloading the update (about 50 MB).\n\n"
                    "This can take a minute on a slow connection — please leave the "
                    "app open. It will close by itself when the installer starts."))

                # .part first: a partial or interrupted download must never be
                # left sitting at the path we execute.
                #
                # Try each host in turn. Only a genuinely unreachable host moves
                # on — a host that answers with something wrong is caught by the
                # size and MZ checks below, and must NOT be retried elsewhere,
                # because that would mask a broken release as a network problem.
                last_err = None
                for _url in installer_urls:
                    try:
                        urllib.request.urlretrieve(_url, part)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                if last_err is not None:
                    _fail(f"Could not reach the download server.\n\n{last_err}")
                    return

                size = os.path.getsize(part)
                if size < MIN_BYTES:
                    os.remove(part)
                    _fail(f"The download was only {size:,} bytes — that is an error "
                          f"page, not the installer.")
                    return

                # Windows executables start with 'MZ'. An HTML error page does not.
                with open(part, "rb") as fh:
                    if fh.read(2) != b"MZ":
                        os.remove(part)
                        _fail("The downloaded file is not a Windows program. "
                              "The download link may be broken.")
                        return

                if os.path.exists(final):
                    os.remove(final)
                os.replace(part, final)

                subprocess.Popen([final])
                # Give the installer a moment to start before we release the
                # executable lock by exiting.
                import time as _t
                _t.sleep(1.5)
                os._exit(0)
            except Exception as e:
                try:
                    if os.path.exists(part):
                        os.remove(part)
                except OSError:
                    pass
                _fail(f"Could not download the update:\n{e}")

        threading.Thread(target=_do, daemon=True).start()

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        """X QUITS THE APP. It does not hide to the tray.

        Owner instruction, 2026-08-09: *"i dont want the journal minizing to the mini
        taskbar again its confusing users"* … *"no more minimizing it that was always
        weird that you did that"*.

        WHAT IT USED TO DO, and why it was worse than "weird": X called `withdraw()`,
        which unmaps the window and leaves the process running invisibly. A user who
        clicked X and then clicked the desktop shortcut again hit the single-instance
        guard, which tried to re-show the hidden window by exact title — and that lookup
        has never matched (see `_ensure_single_instance` in main.py). So they got a
        message box pointing at the system tray and no window. **That is almost certainly
        the "it didn't even open" report**: it had opened, weeks ago, and was hidden.

        Closing for real removes the whole failure class — there is no hidden window left
        to fail to restore.

        ⚠ Go through the app's real shutdown, not `destroy()`. It flushes queued quest
        sightings and stops the log watcher and rotator; skipping it drops whatever the
        session had not uploaded yet."""
        # ── Opt-in: hide to the tray instead of quitting ─────────────────────────
        #
        # Added 2026-08-12 on a user request, approved by the owner with one condition:
        # *"the tray request is okay as long as its an option and not default"*. So this
        # branch is dead code on a default install and everything above still applies.
        #
        # 🔴 HIDE ONLY IF THE ICON IS ACTUALLY RUNNING. Withdrawing the window when the
        # tray failed to start would recreate the exact bug described above — a live
        # process with no window and nothing to click. If the icon is not up, fall through
        # and quit for real, which is the behaviour the user already understands.
        try:
            from app.main import tray_enabled
            tray = getattr(self._app, "tray", None)
            if tray_enabled(self._app) and tray is not None and tray.running:
                self.withdraw()
                return
        except Exception:
            log.debug("tray-on-close check failed; quitting normally", exc_info=True)

        quit_app = getattr(self._app, "shutdown", None)
        if callable(quit_app):
            quit_app()
            return
        # Fallback only — an older AppState with no shutdown hook. Better to close
        # without the flush than to leave the user with a window that ignores X.
        self._shutting_down = True
        self.destroy()
