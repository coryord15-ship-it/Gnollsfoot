"""
Main application window (opened from the system tray).
Tabs: Recent Alerts | Items | Settings
Status bar at the bottom shows LLM research state and log watcher state.
"""

import logging
import os
import sys
import threading
import webbrowser
from typing import Optional
from urllib.parse import quote as _url_quote

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
            for key in ("Recent Alerts", "Items", "Quest Journal", "Settings")
        }
        self._build_alerts_tab(self._sections["Recent Alerts"])
        self._build_items_tab(self._sections["Items"])
        self._build_questlog_tab(self._sections["Quest Journal"])
        self._settings_tab = SettingsTab(self._sections["Settings"], self._app)
        self._settings_tab.pack(fill="both", expand=True)

        # Sidebar nav buttons
        self._nav_buttons = {}
        for key, label, icon in (
            ("Recent Alerts", "Alerts", "🔔"),
            ("Items", "Items", "🎒"),
            ("Quest Journal", "Quest Journal", "📖"),
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
            if self._app.auth.is_real_admin:
                _vlabel = "View: Player" if self._app.auth.preview_non_admin else "View: Admin"
                ctk.CTkButton(
                    self._auth_frame, text=_vlabel, width=108,
                    fg_color="transparent", text_color=theme.TEXT_SECONDARY,
                    hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
                    border_width=1, border_color=theme.BORDER,
                    command=lambda: self._app.auth.set_preview_non_admin(
                        not self._app.auth.preview_non_admin
                    ),
                ).pack(side="left", padx=(0, theme.PAD_SM))
                ctk.CTkButton(
                    self._auth_frame, text="Sync", width=60,
                    fg_color="transparent", text_color=theme.TEXT_SECONDARY,
                    hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
                    border_width=1, border_color=theme.BORDER,
                    command=self._admin_sync,
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

    def _admin_sync(self):
        """Admin: push captured NPC dialogue to the community (verification store)."""
        self._sync_label.configure(text="Syncing…")

        def run():
            try:
                from app.db.queries import get_all_dialogue
                rows = get_all_dialogue(self._app.db_session)
                n = self._app.supabase.push_npc_dialogue(rows)
                self.after(0, lambda: self._sync_label.configure(text=f"Synced {n} dialogue lines"))
            except Exception:
                log.exception("admin sync failed")
                self.after(0, lambda: self._sync_label.configure(text="Sync failed"))

        threading.Thread(target=run, daemon=True).start()

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

        # Submit Screenshot removed — OCR/screenshot capture deprecated
        if False:
            _p = [f"item={_url_quote(alert.item_name)}"]
            if getattr(alert, "npc_name", ""):
                _p.append(f"mob={_url_quote(alert.npc_name)}")
            _generic = ("No info yet", "Researching", "Community-verified", "Item looted")
            if alert.body and not any(g in alert.body for g in _generic):
                _p.append(f"notes={_url_quote(alert.body[:300])}")
            url = "https://gnollguard.com/submit?" + "&".join(_p)
            ctk.CTkButton(
                title_row, text="📤  Submit Screenshot", width=140,
                fg_color="transparent", text_color=alert.color,
                hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
                border_width=1, border_color=alert.color,
                command=lambda u=url: webbrowser.open(u),
            ).pack(side="right", padx=(0, theme.PAD_SM))

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
        if key == "Items":
            self._refresh_items()
        elif key == "Quest Journal":
            self._refresh_journal()

    def _clear_alerts(self):
        for row in self._alert_rows:
            row.destroy()
        self._alert_rows.clear()

    # ── Quest Journal tab ─────────────────────────────────────────────────────

    def _build_questlog_tab(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", padx=theme.PAD, pady=theme.PAD)
        ctk.CTkLabel(
            header, text="Your quest journal — added from the website",
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY,
        ).pack(side="left")
        ctk.CTkButton(
            header, text="Refresh", width=80,
            fg_color=theme.PANEL, hover_color=theme.PANEL_HOVER,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
            command=self._refresh_journal,
        ).pack(side="right")

        self._journal_scroll = ctk.CTkScrollableFrame(parent, fg_color=theme.BG)
        self._journal_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._journal_widgets: list = []

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

    # ── Items tab ─────────────────────────────────────────────────────────────

    def _build_items_tab(self, parent):
        search_row = ctk.CTkFrame(parent, fg_color="transparent")
        search_row.pack(fill="x", padx=theme.PAD, pady=theme.PAD)
        self._item_search_var = ctk.StringVar()
        self._item_search_var.trace_add("write", lambda *_: self._refresh_items())
        ctk.CTkEntry(
            search_row, textvariable=self._item_search_var,
            placeholder_text="Search items…",
            fg_color=theme.PANEL, text_color=theme.TEXT_PRIMARY,
            border_color=theme.BORDER, font=theme.FONT_BODY,
        ).pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            search_row, text="Clear", width=64,
            fg_color=theme.PANEL, text_color=theme.TEXT_SECONDARY,
            hover_color=theme.DANGER, font=theme.FONT_BODY,
            command=self._clear_items,
        ).pack(side="right", padx=(theme.PAD_SM, 0))

        self._items_filter_var = ctk.StringVar(value="Quest Items")
        ctk.CTkSegmentedButton(
            search_row,
            values=["Quest Items", "Popular Items"],
            variable=self._items_filter_var,
            fg_color=theme.PANEL,
            selected_color=theme.GOLD, selected_hover_color=theme.GREEN,
            unselected_color=theme.PANEL, unselected_hover_color=theme.PANEL_HOVER,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
            command=lambda _: self._refresh_items(),
        ).pack(side="right", padx=(theme.PAD, 0))

        self._items_scroll = ctk.CTkScrollableFrame(
            parent, fg_color=theme.BG,
        )
        self._items_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._item_row_widgets: list = []

    _AUTOSOLD_FALLBACK = "Early gear, not useful past level 10."

    def _refresh_items(self):
        for w in self._item_row_widgets:
            w.destroy()
        self._item_row_widgets.clear()

        query = self._item_search_var.get().lower()
        filt = getattr(self, "_items_filter_var", None)
        filt = filt.get() if filt else "Quest Items"

        if filt == "Popular Items":
            self._items_msg("Loading popular items…")
            def load():
                try:
                    items = self._app.supabase.get_popular_items()
                except Exception as e:
                    log.debug("popular items load failed: %s", e)
                    items = []
                self.after(0, lambda: self._render_popular_items(items, query))
            threading.Thread(target=load, daemon=True).start()
        else:
            self._render_quest_items(query)

    def _items_msg(self, text):
        for w in self._item_row_widgets:
            w.destroy()
        self._item_row_widgets.clear()
        lbl = ctk.CTkLabel(
            self._items_scroll, text=text,
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED,
            anchor="w", justify="left", wraplength=700, fg_color="transparent",
        )
        lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD * 2)
        self._item_row_widgets.append(lbl)

    # ── Quest Items ───────────────────────────────────────────────────────────

    def _render_quest_items(self, query: str):
        for w in self._item_row_widgets:
            w.destroy()
        self._item_row_widgets.clear()
        from app import quest_progress
        quests = getattr(self._app, "_journal_quests", []) or []
        prog = getattr(self._app, "_quest_progress", set())
        given = getattr(self._app, "_quest_given", set())
        sold = getattr(self._app, "_sold_items", set())
        seen, shown = set(), 0
        for q in quests:
            qn = q.get("quest_name", "Quest")
            for it in sorted(quest_progress.required_items(q)):
                if it in seen:
                    continue
                seen.add(it)
                if query and query not in it:
                    continue
                self._render_quest_item_row(it, qn, prog, given, sold)
                shown += 1
        if shown == 0:
            self._items_msg(
                "No quest items yet.\n"
                "Add quests to your Journal at gnollguard.com/quests — their required "
                "items appear here and tick off as you loot and turn them in."
            )

    def _render_quest_item_row(self, item_name, quest_name, prog, given, sold):
        low = item_name.lower()
        if low in given:
            status, col = "✔ Turned in", theme.GREEN
        elif low in prog:
            status, col = "✓ Looted", theme.GOLD
        else:
            status, col = "○ Needed", theme.TEXT_SECONDARY
        row = ctk.CTkFrame(self._items_scroll, fg_color=theme.PANEL,
                           corner_radius=theme.RADIUS, border_width=1, border_color=col)
        row.pack(fill="x", pady=2)
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=theme.PAD, pady=theme.PAD_SM)
        title = item_name.title() if item_name.islower() else item_name
        ctk.CTkLabel(left, text=title, font=theme.FONT_BODY,
                     text_color=theme.TEXT_PRIMARY, anchor="w").pack(anchor="w")
        sub = f"for “{quest_name}”"
        if low in sold:
            sub += "   ·   ⚠ Frequently Auto-Sold"
        ctk.CTkLabel(left, text=sub, font=theme.FONT_BODY_SMALL,
                     text_color=theme.TEXT_MUTED, anchor="w").pack(anchor="w")
        ctk.CTkLabel(row, text=status, font=theme.FONT_BODY_SMALL, text_color=col,
                     anchor="e").pack(side="right", padx=(theme.PAD_SM, theme.PAD))
        self._item_row_widgets.append(row)

    # ── Popular Items ─────────────────────────────────────────────────────────

    def _render_popular_items(self, items, query: str):
        for w in self._item_row_widgets:
            w.destroy()
        self._item_row_widgets.clear()
        sold = getattr(self._app, "_sold_items", set())
        shown = 0
        for it in (items or []):
            name = (it.get("item_name") or "").strip()
            if not name:
                continue
            if query and query not in name.lower():
                continue
            self._render_popular_item_row(it, sold)
            shown += 1
        if shown == 0:
            self._items_msg("No popular items to show yet.")

    def _render_popular_item_row(self, it, sold):
        name = it.get("item_name", "")
        desc = (it.get("description") or "").strip() or self._AUTOSOLD_FALLBACK
        count = it.get("submission_count") or 0
        row = ctk.CTkFrame(self._items_scroll, fg_color=theme.PANEL,
                           corner_radius=theme.RADIUS, border_width=1, border_color=theme.BORDER)
        row.pack(fill="x", pady=2)
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=theme.PAD, pady=theme.PAD_SM)
        head = name + ("   ·   ⚠ Frequently Auto-Sold" if name.lower() in sold else "")
        ctk.CTkLabel(left, text=head, font=theme.FONT_BODY,
                     text_color=theme.TEXT_PRIMARY, anchor="w").pack(anchor="w")
        ctk.CTkLabel(left, text=desc[:140], font=theme.FONT_BODY_SMALL,
                     text_color=theme.TEXT_SECONDARY, anchor="w",
                     justify="left", wraplength=620).pack(anchor="w")
        if count:
            ctk.CTkLabel(row, text=f"{count}×", font=theme.FONT_BODY_SMALL,
                         text_color=theme.GOLD).pack(side="right", padx=(theme.PAD_SM, theme.PAD))
        self._item_row_widgets.append(row)

    def _clear_items(self):
        """Wipe the local items the app has accumulated this session + clear the view."""
        import tkinter.messagebox as _mb
        if not _mb.askyesno("Clear items",
                            "Clear the local items the app has collected this session?",
                            parent=self):
            return
        def _do():
            try:
                from app.db.queries import list_items, delete_item
                for item in list_items(self._app.db_session):
                    try:
                        delete_item(self._app.db_session, item.name, getattr(item, "item_level", 0))
                    except Exception:
                        pass
            except Exception:
                log.debug("clear items failed", exc_info=True)
            self.after(0, self._refresh_items)
        try:
            getattr(self._app, "_sold_items", set()).clear()
            getattr(self._app, "_bought_items", set()).clear()
        except Exception:
            pass
        threading.Thread(target=_do, daemon=True).start()
        self._items_msg("Cleared.")

    # ── Status bar updates ────────────────────────────────────────────────────

    def update_llm_status(self, status: str):
        # Scraping is disabled; no-op to avoid AttributeError on old callers.
        pass

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
