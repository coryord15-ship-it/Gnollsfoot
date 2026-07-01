"""
Gnoll Guard overlay — a single always-on-top window that IS the app (no popups).

Tabs:
  • Journal        — the player's journaled quests; required items tick off (✓) as looted
  • Popular Quests — most-added quests across the community (web /api/quests/popular)
  • Quests in Zone — quests for the zone you just entered (web /api/quests/by-zone)
  • Settings       — log path, audio, etc.

Status bar shows the log-watcher state. All network reads run on background threads
and render via .after(); the loot/zone updates arrive from main.py callbacks.
"""

import logging
import os
import sys
import threading

import customtkinter as ctk
from PIL import Image

from app.ui import theme
from app.ui.settings import SettingsTab

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
    """Detached, always-on-top quest overlay tied to the main app (a child window,
    toggled from Settings → 'Enable Overlay Window')."""
    TABS = [
        ("journal", "📖  Journal"),
        ("popular", "🔥  Popular"),
        ("zone",    "🗺  In Zone"),
        ("settings", "⚙  Settings"),
    ]

    def __init__(self, master, app_state):
        super().__init__(master, fg_color=theme.BG)
        self._app = app_state
        from app.version import __version__
        self.title("Gnoll Guard")

        cfg_win = self._app.config.get("window", {})
        w = cfg_win.get("overlay_width", 360)
        h = cfg_win.get("overlay_height", 540)
        self.minsize(300, 380)
        # Place at the saved spot only if it's actually on the primary screen;
        # otherwise default to the top-right (a classic HUD position) so the
        # overlay is never lost off-screen / on a disconnected monitor.
        # Multi-monitor: the Windows "primary" monitor may not be the screen the user
        # is looking at, so ALWAYS spawn the overlay under the mouse cursor (the
        # active monitor), clamped to the virtual desktop. This guarantees it appears
        # where the user is — "remembering" an off-screen spot is what hid it before.
        vx, vy = self.winfo_vrootx(), self.winfo_vrooty()
        vw, vh = self.winfo_vrootwidth(), self.winfo_vrootheight()
        px, py = self.winfo_pointerx(), self.winfo_pointery()
        gx = min(max(px - w // 2, vx), vx + vw - w)
        gy = min(max(py - 30, vy), vy + vh - h)
        self.geometry(f"{w}x{h}+{gx}+{gy}")

        # Always-on-top overlay + transparency so it blends over the game.
        # Borderless drops the OS title bar for a true HUD look (drag via header).
        self._borderless = bool(self._app.config.get("overlay_borderless", False))
        try:
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.attributes("-alpha", float(self._app.config.get("overlay_opacity", 0.92)))
            if self._borderless:
                self.overrideredirect(True)
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        ico, png = _asset("icon.ico"), _asset("icon.png")
        try:
            if os.path.isfile(ico):
                self.iconbitmap(ico)
            elif os.path.isfile(png):
                self.iconphoto(True, ctk.CTkImage(Image.open(png))._light_image)
        except Exception:
            pass
        # Re-apply after CTk's delayed icon reset (avoids the default blue square).
        if os.path.isfile(ico):
            self.after(300, lambda: self._safe_iconbitmap(ico))

        self._version = __version__
        self._current_zone = None
        self._popular_loaded = False
        # Click-through state: when the cursor is over empty/background areas the
        # window is made transparent to the mouse so clicks fall through to the
        # game; over our text/buttons it stays clickable. (Windows-only.)
        self._hwnd = None
        self._click_through = False
        self._build()
        # Pull the overlay to the front shortly after launch so it's never hidden.
        self.after(200, self._surface)
        # Start the click-through cursor watcher once the window is realized.
        self.after(600, self._poll_click_through)

    def _surface(self):
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", bool(self._app.config.get("overlay_topmost", True)))
            self.focus_force()
            log.info("overlay geometry=%s screen=%dx%d vroot=%dx%d",
                     self.geometry(), self.winfo_screenwidth(), self.winfo_screenheight(),
                     self.winfo_vrootwidth(), self.winfo_vrootheight())
            # blink topmost so Windows reliably raises it above a fullscreen game
            self.after(400, lambda: self.attributes("-topmost", True))
        except Exception:
            pass

    def _save_geometry(self):
        try:
            win = self._app.config.setdefault("window", {})
            win["overlay_x"] = self.winfo_x()
            win["overlay_y"] = self.winfo_y()
            win["overlay_width"] = self.winfo_width()
            win["overlay_height"] = self.winfo_height()
            self._app.save_config()
        except Exception:
            pass

    def _safe_iconbitmap(self, ico):
        try:
            self.iconbitmap(ico)
        except Exception:
            pass

    def _drag_start(self, event):
        self._drag_off = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event):
        off = getattr(self, "_drag_off", None)
        if off:
            self.geometry(f"+{event.x_root - off[0]}+{event.y_root - off[1]}")

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        # Header: logo + auth
        hdr = ctk.CTkFrame(self, fg_color=theme.PANEL, height=36, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        try:
            _icon = ctk.CTkImage(Image.open(_asset("icon.png")), size=(20, 20))
            ctk.CTkLabel(hdr, image=_icon, text="").pack(side="left", padx=(theme.PAD, theme.PAD_SM))
        except Exception:
            pass
        _title = ctk.CTkLabel(hdr, text="GNOLL GUARD", font=theme.FONT_HEADER, text_color=theme.GOLD)
        _title.pack(side="left")
        # Slim title bar: a close button (works when borderless) + drag-to-move.
        ctk.CTkButton(
            hdr, text="✕", width=24, height=22, fg_color="transparent",
            text_color=theme.TEXT_MUTED, hover_color=theme.DANGER, font=theme.FONT_BODY,
            command=self._on_close,
        ).pack(side="right", padx=(0, theme.PAD_SM))
        for _w in (hdr, _title):
            _w.bind("<ButtonPress-1>", self._drag_start)
            _w.bind("<B1-Motion>", self._drag_move)
        self._auth_frame = ctk.CTkFrame(hdr, fg_color="transparent")
        self._auth_frame.pack(side="right", padx=theme.PAD)
        self._refresh_auth_header()

        # Tab bar
        tabbar = ctk.CTkFrame(self, fg_color=theme.PANEL, height=30, corner_radius=0)
        tabbar.pack(fill="x")
        tabbar.pack_propagate(False)
        self._tab_buttons = {}
        for key, label in self.TABS:
            b = ctk.CTkButton(
                tabbar, text=label, anchor="center",
                fg_color="transparent", text_color=theme.TEXT_SECONDARY,
                hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY,
                corner_radius=0, command=lambda k=key: self._show(k),
            )
            b.pack(side="left", fill="both", expand=True)
            self._tab_buttons[key] = b

        # Content
        self._content = ctk.CTkFrame(self, fg_color=theme.BG, corner_radius=0)
        self._content.pack(fill="both", expand=True)
        self._sections = {k: ctk.CTkFrame(self._content, fg_color=theme.BG, corner_radius=0) for k, _ in self.TABS}

        self._build_journal_tab(self._sections["journal"])
        self._build_popular_tab(self._sections["popular"])
        self._build_zone_tab(self._sections["zone"])
        self._settings_tab = SettingsTab(self._sections["settings"], self._app)
        self._settings_tab.pack(fill="both", expand=True)

        # Status bar
        sb = ctk.CTkFrame(self, fg_color=theme.PANEL, height=22, corner_radius=0)
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)
        self._log_light = ctk.CTkLabel(sb, text="●", font=("Segoe UI", 13),
                                       text_color=theme.STATUS_LOG_DISCONNECTED)
        self._log_light.pack(side="left", padx=(theme.PAD, 2))
        self._watcher_label = ctk.CTkLabel(sb, text="Log: not connected",
                                           font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_SECONDARY)
        self._watcher_label.pack(side="left")
        self._sync_label = ctk.CTkLabel(sb, text="", font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED)
        self._sync_label.pack(side="right", padx=theme.PAD)

        self._active = None
        self._show("journal")

    def _show(self, key):
        if key == self._active:
            return
        for f in self._sections.values():
            f.pack_forget()
        self._sections[key].pack(fill="both", expand=True)
        self._active = key
        for k, b in self._tab_buttons.items():
            b.configure(fg_color=theme.PANEL_HOVER if k == key else "transparent",
                        text_color=theme.GOLD if k == key else theme.TEXT_SECONDARY)
        if key == "journal":
            self.refresh_journal()
        elif key == "popular" and not self._popular_loaded:
            self.refresh_popular()

    # ── Auth header ─────────────────────────────────────────────────────────--

    def _refresh_auth_header(self):
        for w in self._auth_frame.winfo_children():
            w.destroy()
        auth = self._app.auth
        if auth.is_logged_in:
            ctk.CTkLabel(self._auth_frame, text=f"⚔ {auth.username or 'Adventurer'}",
                         font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_PRIMARY).pack(side="left", padx=(0, theme.PAD_SM))
            ctk.CTkButton(self._auth_frame, text="Logout", width=58,
                          fg_color="transparent", text_color=theme.TEXT_MUTED,
                          hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
                          border_width=1, border_color=theme.BORDER,
                          command=lambda: auth.sign_out()).pack(side="left")
        else:
            ctk.CTkButton(self._auth_frame, text="Login with Discord", width=140,
                          fg_color="#5865F2", text_color="#FFFFFF", hover_color="#4752C4",
                          font=theme.FONT_BODY_SMALL,
                          command=lambda: auth.sign_in_discord()).pack(side="left")

    # ── Journal tab ─────────────────────────────────────────────────────────--

    def _build_journal_tab(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=theme.PAD, pady=theme.PAD_SM)
        ctk.CTkLabel(head, text="Your quest journal", font=theme.FONT_BODY,
                     text_color=theme.TEXT_SECONDARY).pack(side="left")
        ctk.CTkButton(head, text="Refresh", width=72, fg_color=theme.PANEL,
                      hover_color=theme.PANEL_HOVER, text_color=theme.TEXT_PRIMARY,
                      font=theme.FONT_BODY_SMALL, command=self.refresh_journal).pack(side="right")
        self._journal_scroll = ctk.CTkScrollableFrame(parent, fg_color=theme.BG)
        self._journal_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._journal_widgets: list = []

    def refresh_journal(self):
        for w in getattr(self, "_journal_widgets", []):
            w.destroy()
        self._journal_widgets = []
        if not self._app.auth.is_logged_in:
            self._journal_msg("Log in with Discord (top right) to use your Quest Journal.\n"
                              "Add quests at gnollguard.com/quests → “Add to Journal.”")
            return
        self._journal_msg("Loading your journal…")

        def load():
            quests = self._app.supabase.get_journal()
            self.after(0, lambda: self._render_journal(quests))
        threading.Thread(target=load, daemon=True).start()

    def _journal_msg(self, text):
        lbl = ctk.CTkLabel(self._journal_scroll, text=text, justify="left",
                           font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY, wraplength=410)
        lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
        self._journal_widgets.append(lbl)

    def _render_journal(self, quests):
        for w in self._journal_widgets:
            w.destroy()
        self._journal_widgets = []
        try:
            from app import quest_progress
            self._app._journal_quests = quests or []
            self._app._quest_item_index = quest_progress.build_index(quests)
        except Exception:
            pass
        if not quests:
            self._journal_msg("No quests in your journal yet.\n"
                              "Browse gnollguard.com/quests and click “Add to Journal.”")
            return
        for q in quests:
            self._render_journal_quest(q)

    def _render_journal_quest(self, q):
        card = ctk.CTkFrame(self._journal_scroll, fg_color=theme.PANEL, corner_radius=8)
        card.pack(fill="x", padx=theme.PAD, pady=4)
        title_row = ctk.CTkFrame(card, fg_color="transparent")
        title_row.pack(fill="x", padx=theme.PAD, pady=(theme.PAD_SM, 0))
        ctk.CTkLabel(title_row, text=q.get("quest_name", "Quest"), font=theme.FONT_SUBHEADER,
                     text_color=theme.GOLD, anchor="w").pack(side="left")
        ctk.CTkButton(title_row, text="🗑", width=28, height=24,
                      fg_color="transparent", text_color=theme.TEXT_MUTED,
                      hover_color=theme.DANGER, font=theme.FONT_BODY_SMALL,
                      command=lambda qq=q: self._delete_quest(qq)).pack(side="right")
        if q.get("zone"):
            ctk.CTkLabel(card, text=q["zone"], font=theme.FONT_BODY_SMALL,
                         text_color=theme.TEXT_SECONDARY, anchor="w").pack(anchor="w", padx=theme.PAD)
        prog = getattr(self._app, "_quest_progress", set())
        given = getattr(self._app, "_quest_given", set())
        for s in sorted(q.get("steps", []) or [], key=lambda s: s.get("step_order", 0)):
            num, npc = s.get("step_order", ""), (s.get("npc_name") or "")
            ctk.CTkLabel(card, text=(f"{num}. {npc}" if npc else f"{num}."),
                         font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_PRIMARY,
                         anchor="w").pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, 0))
            if s.get("instruction"):
                ctk.CTkLabel(card, text="   " + s["instruction"], font=theme.FONT_BODY_SMALL,
                             text_color=theme.TEXT_SECONDARY, anchor="w", justify="left",
                             wraplength=380).pack(anchor="w", padx=theme.PAD)
            req = s.get("required_items") or []
            if req:
                irow = ctk.CTkFrame(card, fg_color="transparent")
                irow.pack(anchor="w", padx=theme.PAD, pady=(2, 0))
                ctk.CTkLabel(irow, text="   Items:", font=theme.FONT_BODY_SMALL,
                             text_color=theme.TEXT_MUTED).pack(side="left")
                for it in req:
                    low = it.lower()
                    if low in given:
                        mark, col = "  ✔ ", theme.GREEN
                    elif low in prog:
                        mark, col = "  ✓ ", theme.GOLD
                    else:
                        mark, col = "  ○ ", theme.TEXT_SECONDARY
                    ctk.CTkLabel(irow, text=mark + it, font=theme.FONT_BODY_SMALL,
                                 text_color=col).pack(side="left")
        if q.get("reward_items"):
            ctk.CTkLabel(card, text="Rewards: " + ", ".join(q["reward_items"]),
                         font=theme.FONT_BODY_SMALL, text_color=theme.GOLD, anchor="w").pack(
                anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, theme.PAD_SM))
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
        try:
            self._app._journal_quests = [
                jq for jq in getattr(self._app, "_journal_quests", []) if jq.get("id") != qid
            ]
            from app import quest_progress
            self._app._quest_item_index = quest_progress.build_index(self._app._journal_quests)
        except Exception:
            pass
        threading.Thread(target=lambda: self._app.supabase.remove_quest(qid), daemon=True).start()
        self.refresh_journal()

    # ── Popular tab ─────────────────────────────────────────────────────────--

    def _build_popular_tab(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=theme.PAD, pady=theme.PAD_SM)
        ctk.CTkLabel(head, text="Most-added quests in the community", font=theme.FONT_BODY,
                     text_color=theme.TEXT_SECONDARY).pack(side="left")
        ctk.CTkButton(head, text="Refresh", width=72, fg_color=theme.PANEL,
                      hover_color=theme.PANEL_HOVER, text_color=theme.TEXT_PRIMARY,
                      font=theme.FONT_BODY_SMALL, command=self.refresh_popular).pack(side="right")
        self._popular_scroll = ctk.CTkScrollableFrame(parent, fg_color=theme.BG)
        self._popular_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._popular_widgets: list = []

    def refresh_popular(self):
        self._popular_loaded = True
        for w in getattr(self, "_popular_widgets", []):
            w.destroy()
        self._popular_widgets = []
        self._popular_msg("Loading popular quests…")
        threading.Thread(target=lambda: self._fetch_quests(
            "/api/quests/popular?limit=30", self._render_popular), daemon=True).start()

    def _popular_msg(self, text):
        lbl = ctk.CTkLabel(self._popular_scroll, text=text, font=theme.FONT_BODY,
                           text_color=theme.TEXT_SECONDARY, wraplength=410, justify="left")
        lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
        self._popular_widgets.append(lbl)

    def _render_popular(self, quests):
        for w in self._popular_widgets:
            w.destroy()
        self._popular_widgets = []
        if not quests:
            self._popular_msg("Couldn't load popular quests right now.")
            return
        for q in quests:
            adds = q.get("adds") or 0
            sub = q.get("zone") or ""
            if adds:
                sub = f"{sub}   ·   {adds} added" if sub else f"{adds} added"
            self._popular_widgets.append(_quest_row(self._popular_scroll, q.get("quest_name", "Quest"), sub))

    # ── Quests in Zone tab ──────────────────────────────────────────────────--

    def _build_zone_tab(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", padx=theme.PAD, pady=theme.PAD_SM)
        self._zone_label = ctk.CTkLabel(head, text="Zone in to see its quests",
                                        font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY)
        self._zone_label.pack(side="left")
        self._zone_scroll = ctk.CTkScrollableFrame(parent, fg_color=theme.BG)
        self._zone_scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=(0, theme.PAD))
        self._zone_widgets: list = []

    def update_zone(self, zone: str):
        """Called from main.py when the player enters a zone."""
        self._current_zone = zone
        self._zone_label.configure(text=f"Quests in {zone}")
        for w in getattr(self, "_zone_widgets", []):
            w.destroy()
        self._zone_widgets = []
        lbl = ctk.CTkLabel(self._zone_scroll, text="Loading…", font=theme.FONT_BODY,
                           text_color=theme.TEXT_SECONDARY)
        lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
        self._zone_widgets.append(lbl)
        import urllib.parse
        path = "/api/quests/by-zone?zone=" + urllib.parse.quote(zone)
        threading.Thread(target=lambda: self._fetch_quests(path, self._render_zone), daemon=True).start()

    def _render_zone(self, quests):
        for w in self._zone_widgets:
            w.destroy()
        self._zone_widgets = []
        if not quests:
            lbl = ctk.CTkLabel(self._zone_scroll,
                               text=f"No known quests in {self._current_zone or 'this zone'} yet.",
                               font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY, wraplength=410)
            lbl.pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
            self._zone_widgets.append(lbl)
            return
        for q in quests:
            sub = q.get("quest_giver_npc") or ""
            self._zone_widgets.append(_quest_row(self._zone_scroll, q.get("quest_name", "Quest"), sub))

    # ── Shared fetch ────────────────────────────────────────────────────────--

    def _fetch_quests(self, path: str, render):
        quests = []
        if requests is not None:
            try:
                r = requests.get(API_BASE + path, timeout=12)
                if r.ok:
                    quests = r.json().get("quests", [])
            except Exception:
                log.debug("quest fetch failed: %s", path, exc_info=True)
        self.after(0, lambda: render(quests))

    # ── Status bar / updater ────────────────────────────────────────────────--

    def update_watcher_status(self, status: str):
        if status.startswith("watching"):
            color = theme.STATUS_LOG_WATCHING
        elif status.startswith("reading"):
            color = theme.STATUS_LOG_READING
        else:
            color = theme.STATUS_LOG_DISCONNECTED
        self._log_light.configure(text_color=color)
        self._watcher_label.configure(text=f"Log: {status}")

    def update_sync_status(self, text: str):
        self._sync_label.configure(text=text)

    def show_update_banner(self, version: str, download_url: str, changelog: str):
        banner = ctk.CTkFrame(self, fg_color="#1A1000", corner_radius=0)
        banner.pack(fill="x", before=self._content)  # between the tab bar and content
        ctk.CTkLabel(banner, text=f"⬆  Gnoll Guard {version} is available!",
                     font=theme.FONT_BODY_SMALL, text_color=theme.GOLD).pack(side="left", padx=theme.PAD, pady=4)
        ctk.CTkButton(banner, text="Update Now", width=100, fg_color=theme.GOLD, text_color=theme.BG,
                      hover_color=theme.GREEN, font=theme.FONT_BODY_SMALL,
                      command=lambda u=download_url: self._install_update(u)).pack(side="right", padx=theme.PAD, pady=3)

    def _install_update(self, page_url: str):
        import tkinter.messagebox as _mb
        import urllib.request, tempfile, subprocess
        if not _mb.askyesno("Update Gnoll Guard",
                            "Download and install the latest version now?\n\nThe app will close to run the installer."):
            return

        def _do():
            try:
                tmp = os.path.join(tempfile.gettempdir(), "GnollGuard-Setup.exe")
                urllib.request.urlretrieve("https://gnollguard.com/api/download", tmp)
                subprocess.Popen([tmp])
                os._exit(0)
            except Exception as e:
                self.after(0, lambda: _mb.showerror("Update Failed",
                    f"Could not download update:\n{e}\n\nDownload manually at gnollguard.com/download"))
        threading.Thread(target=_do, daemon=True).start()

    # ── Click-through (pass clicks to the game when not over our content) ──────

    # Widgets we consider "interactive" — hovering these keeps the window
    # clickable; anything else (bare frames / padding / empty scroll area) lets
    # the click fall through to whatever is behind the overlay.
    _INTERACTIVE = (
        "CTkButton", "CTkLabel", "CTkEntry", "CTkTextbox", "CTkSwitch",
        "CTkOptionMenu", "CTkComboBox", "CTkCheckBox", "CTkSlider",
        "CTkScrollbar", "Label", "Button", "Entry",
    )

    def _get_hwnd(self):
        if self._hwnd:
            return self._hwnd
        try:
            from ctypes import windll
            wid = self.winfo_id()
            hwnd = windll.user32.GetParent(wid) or wid
            self._hwnd = hwnd
            return hwnd
        except Exception:
            return None

    def _is_interactive(self, w):
        node, depth = w, 0
        while node is not None and depth < 8:
            if node.__class__.__name__ in self._INTERACTIVE:
                return True
            node = getattr(node, "master", None)
            depth += 1
        return False

    def _set_click_through(self, on: bool):
        if on == self._click_through:
            return
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        try:
            from ctypes import windll
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000      # keep so -alpha opacity still applies
            WS_EX_TRANSPARENT = 0x00000020  # mouse events pass through
            ex = windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ex = (ex | WS_EX_TRANSPARENT | WS_EX_LAYERED) if on \
                else ((ex & ~WS_EX_TRANSPARENT) | WS_EX_LAYERED)
            windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            self._click_through = on
        except Exception:
            pass

    def _poll_click_through(self):
        """Every ~60ms, decide whether the cursor is over our content or over
        empty space, and toggle mouse click-through accordingly. Uses the global
        cursor position + Tk geometry so it works even while click-through is on
        (the window gets no mouse events in that state)."""
        if not self.winfo_exists():
            return
        try:
            if self._app.config.get("overlay_click_through", True):
                w = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
                # w is None when the cursor is outside our windows (over the game).
                self._set_click_through(not self._is_interactive(w))
            else:
                self._set_click_through(False)
        except Exception:
            pass
        self.after(60, self._poll_click_through)

    # ── Close → hide to tray ────────────────────────────────────────────────--

    def _on_close(self):
        """Close just the overlay (the main window keeps running). Persists the
        position and flips the 'Enable Overlay Window' setting off."""
        try:
            self._save_geometry()
        except Exception:
            pass
        cfg = getattr(self._app, "config", {})
        cfg["overlay_enabled"] = False
        try:
            self._app.save_config()
        except Exception:
            pass
        mw = getattr(self._app, "main_window", None)
        if mw is not None and hasattr(mw, "_overlay"):
            mw._overlay = None
        try:
            self._app.overlay_window = None
        except Exception:
            pass
        self.destroy()


def _quest_row(parent, title: str, subtitle: str):
    row = ctk.CTkFrame(parent, fg_color=theme.PANEL, corner_radius=8)
    row.pack(fill="x", padx=theme.PAD, pady=3)
    ctk.CTkLabel(row, text=title, font=theme.FONT_BODY, text_color=theme.GOLD,
                 anchor="w").pack(anchor="w", padx=theme.PAD, pady=(theme.PAD_SM, 0))
    if subtitle:
        ctk.CTkLabel(row, text=subtitle, font=theme.FONT_BODY_SMALL,
                     text_color=theme.TEXT_SECONDARY, anchor="w").pack(anchor="w", padx=theme.PAD, pady=(0, theme.PAD_SM))
    else:
        ctk.CTkLabel(row, text="", font=theme.FONT_BODY_SMALL).pack(pady=(0, 2))
    return row
