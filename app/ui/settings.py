"""Settings panel — renders and handles every user-configurable option.

Covers log file paths, UI theme, pop-out quest overlay styling, the community
database connection, Discord authentication, app updates, and local storage /
cache management. Rendered inside the main window's tab view.

⚠ THE ATTRIBUTE TRAP THAT BROKE v1.5.15
    __init__ takes `app_state` as a PARAMETER and stores it as `self._app`.
    There is no `self.app_state`. Two helpers in the Storage section used the
    parameter name, so building the tab raised AttributeError and the whole
    panel rendered "Settings failed to load" — which ALSO locked users out of
    the theme switch, because that lives here. Use `self._app`.


LAZY AND DEFENSIVE RENDERING
    CustomTkinter has a layout bug where a CTkScrollableFrame built while its
    parent is hidden (0×0) renders permanently blank. Several defences exist and
    none of them are decorative:

    * `ensure_visible()` / `_build()` — show a lightweight "Loading settings…"
      placeholder first and defer the real build until the tab is actually
      mapped and visible. This is the fix for the classic "Settings is blank"
      report.
    * `_nudge_scroll_layout()` — force the internal canvas to recompute its size
      and scrollregion once the window has real dimensions.
    * `_ensure_tk_default_root()` — restore a default tkinter root. Destroying
      the boot splash can leave tkinter without one, and the next font lookup
      then raises at runtime.


SECTIONS

    Logs directory — `_browse_log_folder()`
        Set or browse for the EverQuest Legends `Logs` directory.
        ⚠ `_save()` validates the choice and REFUSES a live-EverQuest log
        directory. Live-EQ logs are format-identical to Legends, so attaching to
        one would push live-EQ quest dialogue into the Legends community
        database. Do not weaken this guard.

    Display & overlay — `_build_overlay_typography()`, `_apply_overlay_opacity()`
        Light/Dark theme, plus pop-out quest overlay opacity, font family and
        text scale, with live preview.

    Community & account
        Supabase connection status; Discord OAuth via `_sign_in_discord()` /
        `_sign_out()`.

    Updates — `_check_for_updates()`
        Asks the background updater thread to check now.

    Storage & cache — `_cache_label()`, `_clear_cache()`
        Reports the disk space Gnoll Guard's own archived log copies use, flushes
        any queued observations, and clears the cache.
        ⚠ Only ever touches OUR archived copies — never the player's real
        EverQuest log files.

    Persistence — `_save()`
        Collects every input, updates `self._app.config`, writes it through
        `self._app.save_config()`, and tells the log watcher if the directory
        changed.
"""

import logging
import os
import threading
import tkinter.filedialog as fd
import tkinter.messagebox as mb

import customtkinter as ctk

from app.ui import theme

log = logging.getLogger(__name__)


class SettingsTab(ctk.CTkFrame):
    def __init__(self, parent, app_state, **kwargs):
        super().__init__(parent, fg_color=theme.BG, **kwargs)
        self._app = app_state
        # Defer the full build until the Settings section is actually shown. Building a
        # CTkScrollableFrame while its parent is pack_forget()'d (0×0) often yields a
        # permanently empty Settings panel — the classic "Settings is blank" bug.
        self._built_while_mapped = False
        self._build_placeholder()

    def _build_placeholder(self):
        for w in self.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self, text="Loading settings…",
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED,
        ).pack(expand=True, pady=theme.PAD * 2)

    def ensure_visible(self):
        """Call when the Settings section is packed/mapped so controls actually render.

        CTkScrollableFrame created while the parent is pack_forget()'d (0×0) often
        stays permanently empty — so we only build after the section is shown.
        """
        kids = list(self.winfo_children())
        only_placeholder = False
        if len(kids) == 1:
            try:
                only_placeholder = (kids[0].cget("text") or "").startswith("Loading settings")
            except Exception:
                only_placeholder = False
        if self._built_while_mapped and kids and not only_placeholder:
            # Already built while visible; still force a layout pass for CTkScrollableFrame.
            try:
                self.update_idletasks()
                # Nudge internal canvas (CustomTkinter sometimes leaves 0 height until this)
                self.after(30, self._nudge_scroll_layout)
            except Exception:
                pass
            return
        self._build()
        self._built_while_mapped = True
        try:
            self.update_idletasks()
            self.after(30, self._nudge_scroll_layout)
        except Exception:
            pass

    def _nudge_scroll_layout(self):
        """Force CTkScrollableFrame to recompute after the parent has a real size."""
        try:
            for w in self.winfo_children():
                try:
                    w.update_idletasks()
                except Exception:
                    pass
                # CTkScrollableFrame keeps content on _parent_frame / _parent_canvas
                canvas = getattr(w, "_parent_canvas", None)
                if canvas is not None:
                    try:
                        canvas.configure(scrollregion=canvas.bbox("all"))
                        canvas.yview_moveto(0)
                    except Exception:
                        pass
        except Exception:
            pass

    def _build(self):
        # Clear any previous render so repeated builds (e.g. after sign-out) don't
        # stack multiple scroll frames on top of each other.
        try:
            for w in self.winfo_children():
                w.destroy()
        except Exception:
            pass
        try:
            self._build_body()
        except Exception:
            log.exception("Settings tab failed to build")
            try:
                for w in self.winfo_children():
                    w.destroy()
            except Exception:
                pass
            ctk.CTkLabel(
                self,
                text="Settings failed to load. Check Documents\\GnollGuard\\app.log",
                font=theme.FONT_BODY, text_color=theme.DANGER, wraplength=520, justify="left",
            ).pack(anchor="w", padx=theme.PAD, pady=theme.PAD)
            ctk.CTkButton(
                self, text="Retry", fg_color=theme.GOLD, text_color=theme.BG,
                hover_color=theme.GREEN, font=theme.FONT_BODY,
                command=self._build,
            ).pack(anchor="w", padx=theme.PAD, pady=theme.PAD_SM)

    def _ensure_tk_default_root(self):
        """CTkScrollableFrame creates CTkFont() which requires tkinter's default root.

        After the boot splash (a temporary Tk) is destroyed, _default_root can be None
        even though MainWindow is alive — that yields:
            RuntimeError: Too early to use font: no default root window
        """
        try:
            import tkinter as tk
            if tk._default_root is None:
                top = self.winfo_toplevel()
                if top is not None:
                    tk._default_root = top
        except Exception:
            pass

    def _build_body(self):
        self._ensure_tk_default_root()
        scroll = ctk.CTkScrollableFrame(self, fg_color=theme.BG)
        scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=theme.PAD)

        self._section(scroll, "EQ Legends Logs Folder")
        ctk.CTkLabel(
            scroll,
            text="Point this at your EverQuest Legends → Logs folder. The app reads EVERY "
                 "character log in it automatically (eqlog_<Character>_<Server>.txt), so you "
                 "don't pick a single file. Default: your Legends install's Logs folder — only "
                 "change this if you installed Legends somewhere else.",
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED, anchor="w",
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        # The watcher works off the FOLDER (it tails every log in it). We keep the folder in
        # log_dir; log_file_path stays supported for back-compat but the folder is what matters.
        _cur_dir = self._app.config.get("log_dir") or os.path.dirname(
            self._app.config.get("log_file_path", "") or "")
        path_row = ctk.CTkFrame(scroll, fg_color="transparent")
        path_row.pack(fill="x", pady=(0, theme.PAD))
        self._log_dir_var = ctk.StringVar(value=_cur_dir)
        # kept for _save() back-compat with existing config
        self._log_path_var = ctk.StringVar(value=self._app.config.get("log_file_path", ""))
        ctk.CTkEntry(
            path_row, textvariable=self._log_dir_var,
            fg_color=theme.PANEL, text_color=theme.TEXT_PRIMARY,
            border_color=theme.BORDER, font=theme.FONT_BODY,
        ).pack(side="left", fill="x", expand=True, padx=(0, theme.PAD_SM))
        ctk.CTkButton(
            path_row, text="Browse…", width=90,
            fg_color=theme.GOLD, text_color=theme.BG,
            hover_color=theme.GREEN, font=theme.FONT_BODY,
            command=self._browse_log_folder,
        ).pack(side="right")

        self._section(scroll, "Display")
        theme_row = ctk.CTkFrame(scroll, fg_color="transparent")
        theme_row.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            theme_row, text="Theme", font=theme.FONT_BODY,
            text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")
        self._theme_var = ctk.StringVar(
            value="Light" if self._app.config.get("theme") == "light" else "Dark"
        )
        ctk.CTkOptionMenu(
            theme_row, values=["Dark", "Light"], variable=self._theme_var,
            fg_color=theme.PANEL, button_color=theme.PANEL,
            button_hover_color=theme.PANEL_HOVER, text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY, width=140,
        ).pack(side="right")
        ctk.CTkLabel(
            scroll, text="Theme changes apply after you restart the app.",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED, anchor="w",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))

        ctk.CTkLabel(
            scroll,
            text="Quest overlays: open the Journal tab and click Pop out on up to 5 quests. "
                 "Drag windows near each other to snap into a movable cluster; "
                 "Shift+drag a window to break it off the cluster. "
                 "Dock returns a quest to the Journal.",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED, anchor="w",
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        # Kept for config back-compat (older installs may still have these keys).
        self._overlay_var = ctk.BooleanVar(value=True)
        self._overlay_borderless_var = ctk.BooleanVar(
            value=self._app.config.get("overlay_borderless", True)
        )
        self._overlay_clickthrough_var = ctk.BooleanVar(
            value=self._app.config.get("overlay_click_through", True)
        )
        self._overlay_opacity_var = ctk.IntVar(
            value=int(float(self._app.config.get("overlay_opacity", 0.92)) * 100)
        )
        op_row = ctk.CTkFrame(scroll, fg_color="transparent")
        op_row.pack(fill="x", pady=(0, theme.PAD))
        op_val = ctk.CTkLabel(
            op_row, text=f"{self._overlay_opacity_var.get()}%", width=36,
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY,
        )
        ctk.CTkLabel(
            op_row, text="Pop-out window opacity", font=theme.FONT_BODY,
            text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")
        op_val.pack(side="right")
        ctk.CTkSlider(
            op_row, variable=self._overlay_opacity_var, from_=40, to=100,
            button_color=theme.GOLD, progress_color=theme.GOLD, fg_color=theme.PANEL,
            command=lambda v: (op_val.configure(text=f"{int(v)}%"),
                               self._apply_overlay_opacity(int(v))),
        ).pack(side="right", padx=theme.PAD, fill="x", expand=True)

        # ── Overlay typography (defensive — never block the rest of Settings) ─
        try:
            self._build_overlay_typography(scroll)
        except Exception:
            log.exception("overlay typography controls failed to build")
            self._overlay_font_var = ctk.StringVar(value="Segoe UI")
            self._overlay_font_scale_pct_var = ctk.IntVar(value=100)

        self._export_dir_var = ctk.StringVar(
            value=self._app.config.get("export_directory", "")
        )

        # ── Alerts ───────────────────────────────────────────────────────────
        #
        # Owner, 2026-08-16: *"if we are going to alart the person its there we need
        # some kinda alart sound plus a blinking taskbar"*, and then *"let them turn the
        # sound up or down via a slider and give them the option to turn the sound off"*.
        #
        # Sound and flash both default ON — unlike the tray, these were asked for. The
        # player is in-game and full-screen; a row appearing in a list behind the game
        # is not an alert. But both are switchable, and the slider goes to 0.
        self._section(scroll, "Alerts")

        self._alert_sound_var = ctk.BooleanVar(
            value=bool(self._app.config.get("alert_sound", True))
        )
        ctk.CTkSwitch(
            scroll,
            text="Play a sound when a quest item drops",
            variable=self._alert_sound_var,
            command=self._save_alert_prefs,
            font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
            progress_color=theme.GOLD,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))

        vol_row = ctk.CTkFrame(scroll, fg_color="transparent")
        vol_row.pack(fill="x", pady=(0, theme.PAD_SM))
        ctk.CTkLabel(
            vol_row, text="Volume", font=theme.FONT_BODY,
            text_color=theme.TEXT_PRIMARY, anchor="w", width=70,
        ).pack(side="left")

        self._alert_volume_var = ctk.IntVar(
            value=int(self._app.config.get("alert_volume", 70))
        )
        self._alert_volume_label = ctk.CTkLabel(
            vol_row, text=f"{self._alert_volume_var.get()}%",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_SECONDARY, width=44,
        )
        ctk.CTkSlider(
            vol_row, from_=0, to=100, number_of_steps=20,
            variable=self._alert_volume_var,
            command=self._on_alert_volume,
            progress_color=theme.GOLD, button_color=theme.GOLD, width=220,
        ).pack(side="left", padx=(0, theme.PAD_SM))
        self._alert_volume_label.pack(side="left")

        # A volume slider you cannot hear while dragging is a guess. This plays the
        # real chime at the real level, so the setting is chosen rather than estimated.
        ctk.CTkButton(
            vol_row, text="Test", width=64, height=26,
            command=self._test_alert_sound,
            fg_color=theme.PANEL_HOVER, hover_color=theme.GOLD,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY_SMALL,
        ).pack(side="left", padx=(theme.PAD_SM, 0))

        self._alert_flash_var = ctk.BooleanVar(
            value=bool(self._app.config.get("alert_flash", True))
        )
        ctk.CTkSwitch(
            scroll,
            text="Flash the taskbar button until you look at the alert",
            variable=self._alert_flash_var,
            command=self._save_alert_prefs,
            font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
            progress_color=theme.GOLD,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))

        # 🔴 DEFAULT OFF — owner asked for it as opt-in. An overlay that draws itself
        # over the game uninvited is the single most intrusive thing this app could do,
        # and the tray episode already showed what happens when something appears
        # without being asked for.
        self._alert_toast_var = ctk.BooleanVar(
            value=bool(self._app.config.get("alert_toast", False))
        )
        toast_row = ctk.CTkFrame(scroll, fg_color="transparent")
        toast_row.pack(fill="x", pady=(0, 2))
        ctk.CTkSwitch(
            toast_row,
            text="Show a 5-second pop-up over the game when a quest item drops",
            variable=self._alert_toast_var,
            command=self._save_alert_prefs,
            font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
            progress_color=theme.GOLD,
        ).pack(side="left")
        ctk.CTkButton(
            toast_row, text="Test", width=64, height=26,
            command=self._test_alert_toast,
            fg_color=theme.PANEL_HOVER, hover_color=theme.GOLD,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY_SMALL,
        ).pack(side="left", padx=(theme.PAD_SM, 0))
        ctk.CTkLabel(
            scroll,
            text=("Appears on the monitor EverQuest is on, and never takes focus.\n"
                  "⚠ If you run the game in exclusive full-screen, Windows will not draw "
                  "anything over it — use borderless/windowed, or rely on the sound."),
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_SECONDARY,
            anchor="w", justify="left", wraplength=560,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))

        # ── Window behaviour ─────────────────────────────────────────────────
        #
        # Owner, 2026-08-12: *"the tray request is okay as long as its an option and not
        # default"*. DEFAULT FALSE, and the label says plainly where the app goes — the
        # original complaint was that users could not find it after it vanished.
        self._section(scroll, "Window")
        self._tray_var = ctk.BooleanVar(
            value=bool(self._app.config.get("minimize_to_tray", False))
        )
        ctk.CTkSwitch(
            scroll,
            text="Minimise to the system tray instead of quitting",
            variable=self._tray_var,
            command=self._toggle_tray,
            font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
            progress_color=theme.GOLD,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        ctk.CTkLabel(
            scroll,
            text="Off by default. When on, the X button hides Gnoll Guard to the "
                 "notification area (bottom-right, next to the clock) and it keeps "
                 "logging — click the icon there to bring it back, or use Quit on its "
                 "menu to close it properly.",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED, anchor="w",
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, theme.PAD))

        self._section(scroll, "Community")
        ctk.CTkLabel(
            scroll,
            text="Verifications are saved locally on your machine only. "
                 "Looted items are shared with the community database "
                 "automatically in the background.",
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED, anchor="w",
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        self._supa_status_label = ctk.CTkLabel(
            scroll,
            text="Connected ✓" if self._app.supabase.is_configured else "Not connected to community server",
            font=theme.FONT_BODY_SMALL, anchor="w",
            text_color=theme.ALERT_ITEM_VERIFIED if self._app.supabase.is_configured
                        else theme.TEXT_MUTED,
        )
        self._supa_status_label.pack(anchor="w", pady=(0, theme.PAD))
        self._supa_url_var = ctk.StringVar(value=self._app.config.get("supabase_url", ""))
        self._supa_key_var = ctk.StringVar(value=self._app.config.get("supabase_key", ""))

        self._section(scroll, "Account")
        ctk.CTkLabel(
            scroll,
            text="Log in with Discord to contribute loot data to the community database. "
                 "Your Discord username is stored anonymously — we never see your password.",
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED, anchor="w",
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))

        if self._app.auth.is_logged_in:
            name = self._app.auth.username or "Adventurer"
            ctk.CTkLabel(
                scroll,
                text=f"Signed in as  {name}",
                font=theme.FONT_BODY, text_color=theme.ALERT_ITEM_VERIFIED, anchor="w",
            ).pack(anchor="w", pady=(0, theme.PAD_SM))
            ctk.CTkButton(
                scroll, text="Log Out",
                fg_color=theme.PANEL, text_color=theme.TEXT_PRIMARY,
                hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY,
                command=self._sign_out,
            ).pack(anchor="w", pady=(0, theme.PAD))
        else:
            ctk.CTkButton(
                scroll, text="Login with Discord",
                fg_color="#5865F2", text_color="#FFFFFF",
                hover_color="#4752C4", font=theme.FONT_BODY,
                command=self._sign_in_discord,
            ).pack(anchor="w", pady=(0, theme.PAD))

        self._section(scroll, "Updates")
        from app.version import __version__
        self._update_status_var = ctk.StringVar(value=f"Current version: {__version__}")
        ctk.CTkLabel(
            scroll, textvariable=self._update_status_var,
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED, anchor="w",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        ctk.CTkButton(
            scroll, text="Check for Updates",
            fg_color=theme.PANEL, text_color=theme.TEXT_PRIMARY,
            hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY,
            command=self._check_for_updates,
        ).pack(anchor="w", pady=(0, theme.PAD))

        # ── Storage ──────────────────────────────────────────────────────────
        # The app archives copies of the EQ log so it can re-scan them; those add up.
        # Uploading prunes them automatically, but give the user a manual lever too.
        self._section(scroll, "Storage")
        self._cache_var = ctk.StringVar(value=self._cache_label())
        ctk.CTkLabel(
            scroll, textvariable=self._cache_var,
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED, anchor="w",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        ctk.CTkLabel(
            scroll,
            text="Clearing uploads anything still queued first, then deletes Gnoll Guard's "
                 "own archived copies of your log. Your EverQuest log files are never touched.",
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED, anchor="w",
            justify="left", wraplength=520,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        ctk.CTkButton(
            scroll, text="Clear Cache",
            fg_color=theme.PANEL, text_color=theme.TEXT_PRIMARY,
            hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY,
            command=self._clear_cache,
        ).pack(anchor="w", pady=(0, theme.PAD))

        ctk.CTkButton(
            scroll, text="Save Settings",
            fg_color=theme.GOLD, text_color=theme.BG,
            hover_color=theme.GREEN, font=theme.FONT_SUBHEADER,
            command=self._save,
        ).pack(anchor="w", pady=(theme.PAD, 0))
        self._built_while_mapped = True

    # ── Storage helpers ──────────────────────────────────────────────────────

    def _cache_label(self) -> str:
        # self._app, not self.app_state — the constructor takes `app_state` as a
        # PARAMETER and stores it as self._app (line 21). These two helpers used
        # the parameter name, so every attempt to build the Settings tab raised
        # AttributeError and the whole tab showed "Settings failed to load".
        # The getattr default never helped: the error was on self.app_state
        # itself, before the attribute lookup. Fixed 2026-08-03.
        obs = getattr(self._app, "observations", None)
        if obs is None:
            return "Cache: unavailable"
        try:
            mb = obs.disk_usage() / 1048576
            return (f"Using {mb:.1f} MB  ·  {obs.queued} observation(s) queued, "
                    f"{obs.uploaded} uploaded this session")
        except Exception:
            return "Cache: unavailable"

    def _clear_cache(self):
        # self._app, not self.app_state — the constructor takes `app_state` as a
        # PARAMETER and stores it as self._app (line 21). These two helpers used
        # the parameter name, so every attempt to build the Settings tab raised
        # AttributeError and the whole tab showed "Settings failed to load".
        # The getattr default never helped: the error was on self.app_state
        # itself, before the attribute lookup. Fixed 2026-08-03.
        obs = getattr(self._app, "observations", None)
        if obs is None:
            return
        try:
            files, freed = obs.clear_cache()
            self._cache_var.set(
                f"Cleared {files} file(s), freed {freed / 1048576:.1f} MB")
        except Exception:
            self._cache_var.set("Clear failed — see app.log")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_overlay_typography(self, scroll):
        """Font family + size for pop-out quest windows. Isolated so failures don't blank Settings."""
        families = list(getattr(theme, "FONT_FAMILIES", None) or (
            "Segoe UI", "Helvetica", "Georgia", "Consolas"))
        ctk.CTkLabel(
            scroll,
            text="Pop-out font (quest overlay windows). Helvetica uses Arial on Windows if needed.",
            font=theme.FONT_BODY_SMALL, text_color=theme.TEXT_MUTED, anchor="w",
        ).pack(anchor="w", pady=(theme.PAD_SM, 2))

        font_row = ctk.CTkFrame(scroll, fg_color="transparent")
        font_row.pack(fill="x", pady=(0, theme.PAD_SM))
        ctk.CTkLabel(
            font_row, text="Font family", font=theme.FONT_BODY,
            text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")
        _fam = str(self._app.config.get("overlay_font_family") or "Segoe UI").strip()
        if _fam.lower() in ("arial", "helvetica neue"):
            _fam = "Helvetica"
        if _fam not in families:
            _fam = "Segoe UI"
        self._overlay_font_var = ctk.StringVar(value=_fam)
        ctk.CTkOptionMenu(
            font_row, values=families, variable=self._overlay_font_var,
            fg_color=theme.PANEL, button_color=theme.PANEL,
            button_hover_color=theme.PANEL_HOVER, text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY, width=160,
            command=self._on_font_family_change,
        ).pack(side="right")

        # Use integer percent (80–160) — more reliable than DoubleVar on some CTk builds
        try:
            _sc = float(self._app.config.get("overlay_font_scale", 1.0))
        except Exception:
            _sc = 1.0
        pct = int(round(max(0.8, min(1.6, _sc)) * 100))
        self._overlay_font_scale_pct_var = ctk.IntVar(value=pct)
        scale_row = ctk.CTkFrame(scroll, fg_color="transparent")
        scale_row.pack(fill="x", pady=(0, theme.PAD))
        scale_val = ctk.CTkLabel(
            scale_row, text=f"{pct}%", width=44,
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY,
        )
        ctk.CTkLabel(
            scale_row, text="Font size", font=theme.FONT_BODY,
            text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")
        scale_val.pack(side="right")
        ctk.CTkSlider(
            scale_row, variable=self._overlay_font_scale_pct_var, from_=80, to=160,
            button_color=theme.GOLD, progress_color=theme.GOLD, fg_color=theme.PANEL,
            command=lambda v, lbl=scale_val: self._on_font_scale_change(v, lbl),
        ).pack(side="right", padx=theme.PAD, fill="x", expand=True)

        # ── App text size ───────────────────────────────────────────────────
        # DISTINCT from the "Font size" slider above, which only scales pop-out
        # quest windows. This one scales the APP. They are separate because
        # main() calls deactivate_automatic_dpi_awareness() for multi-monitor
        # stability, which pins CustomTkinter at 1.0 — so on a high-DPI display
        # the main window never grows. That is why the pop-out was readable
        # while the app itself was not (owner report, 2026-08-06).
        self._section(scroll, "App text size")
        try:
            _us = float(self._app.config.get("ui_scale", 1.0))
        except Exception:
            _us = 1.0
        us_pct = int(round(max(0.8, min(2.0, _us)) * 100))
        self._ui_scale_pct_var = ctk.IntVar(value=us_pct)
        ui_row = ctk.CTkFrame(scroll, fg_color="transparent")
        ui_row.pack(fill="x", pady=(0, theme.PAD_SM))
        ui_val = ctk.CTkLabel(
            ui_row, text=f"{us_pct}%", width=44,
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY,
        )
        ctk.CTkLabel(
            ui_row, text="App scale", font=theme.FONT_BODY,
            text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")
        ui_val.pack(side="right")
        ui_slider = ctk.CTkSlider(
            ui_row, variable=self._ui_scale_pct_var, from_=80, to=200,
            button_color=theme.GOLD, progress_color=theme.GOLD, fg_color=theme.PANEL,
            command=lambda v, lbl=ui_val: self._on_ui_scale_preview(v, lbl),
        )
        ui_slider.pack(side="right", padx=theme.PAD, fill="x", expand=True)
        # Apply on RELEASE, not on every tick: set_widget_scaling() re-lays out
        # every widget including this slider, so live-applying would drag the
        # handle out from under the cursor.
        ui_slider.bind("<ButtonRelease-1>", self._on_ui_scale_commit)
        ctk.CTkLabel(
            scroll, text="Scales the whole app. Applies when you release the slider.",
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY, anchor="w",
        ).pack(anchor="w", pady=(0, theme.PAD))

    def _on_ui_scale_preview(self, value, label=None):
        """Update the % readout while dragging. Does NOT rescale — see commit."""
        try:
            if label is not None:
                label.configure(text=f"{int(float(value))}%")
        except Exception:
            log.debug("ui scale preview failed", exc_info=True)

    def _on_ui_scale_commit(self, _event=None):
        """Apply and persist the app scale once the slider is released."""
        try:
            pct = int(self._ui_scale_pct_var.get())
            scale = max(0.8, min(2.0, pct / 100.0))
            ctk.set_widget_scaling(scale)
            self._app.config["ui_scale"] = round(scale, 2)
            try:
                self._app.save_config()
            except Exception:
                log.debug("save_config failed after ui_scale", exc_info=True)
            log.info("ui_scale set to %.2f", scale)
        except Exception:
            log.exception("ui scale change failed")

    def _on_font_family_change(self, _value=None):
        try:
            self._apply_overlay_typography()
        except Exception:
            log.exception("font family change failed")

    def _on_font_scale_change(self, value, label=None):
        try:
            pct = int(float(value))
            if label is not None:
                label.configure(text=f"{pct}%")
            self._apply_overlay_typography()
        except Exception:
            log.exception("font scale change failed")

    def _section(self, parent, title: str):
        ctk.CTkLabel(
            parent, text=title,
            font=theme.FONT_SUBHEADER, text_color=theme.GOLD, anchor="w",
        ).pack(anchor="w", pady=(theme.PAD, theme.PAD_SM))

    # ── Alert preferences ────────────────────────────────────────────────────

    def _save_alert_prefs(self):
        """Persist immediately — these are toggles, not a form with a Save button."""
        try:
            self._app.config["alert_sound"] = bool(self._alert_sound_var.get())
            self._app.config["alert_flash"] = bool(self._alert_flash_var.get())
            self._app.config["alert_volume"] = int(self._alert_volume_var.get())
            self._app.config["alert_toast"] = bool(self._alert_toast_var.get())
            self._app.save_config()
        except Exception:
            log.exception("could not save alert preferences")

    def _on_alert_volume(self, value):
        """Live label + persist. No preview here — dragging a slider that chirps on
        every step is maddening; that is what the Test button is for."""
        try:
            v = int(float(value))
            self._alert_volume_label.configure(text=f"{v}%")
            self._app.config["alert_volume"] = v
            self._app.save_config()
        except Exception:
            log.debug("alert volume update failed", exc_info=True)

    def _test_alert_sound(self):
        """Play the real chime at the real level, so the slider is a choice not a guess."""
        try:
            from app.ui import notify
            notify.play_alert_sound(int(self._alert_volume_var.get()))
        except Exception:
            log.debug("alert sound test failed", exc_info=True)

    def _test_alert_toast(self):
        """Show the actual overlay, on the actual monitor it would use."""
        try:
            from app.ui import notify
            notify.show_toast(
                self.winfo_toplevel(),
                "Quest item: Divine Honeycomb",
                "This is where a quest-item alert will appear.",
                seconds=int(self._app.config.get("alert_toast_secs", 10)),
            )
        except Exception:
            log.exception("toast test failed")

    def _toggle_tray(self):
        """Persist immediately and apply live — no Save button round-trip.

        🔴 Turning it OFF must also un-hide the window. If the user is toggling this
        from a hidden state they have just removed their only way back to the app;
        apply_tray_setting() restores it. Do not "optimise" that call away.
        """
        try:
            self._app.config["minimize_to_tray"] = bool(self._tray_var.get())
            self._app.save_config()
        except Exception:
            log.exception("could not save minimize_to_tray")
            return
        try:
            from app.main import apply_tray_setting
            apply_tray_setting(self._app)
        except Exception:
            log.exception("could not apply tray setting")

    def _toggle_overlay(self):
        # No dock hub anymore — opacity/typography apply to open pop-outs only.
        cfg = self._app.config
        cfg["overlay_opacity"] = round(self._overlay_opacity_var.get() / 100.0, 2)
        try:
            self._app.save_config()
        except Exception:
            pass
        self._apply_overlay_opacity(self._overlay_opacity_var.get())

    def _apply_overlay_typography(self):
        """Live-update all pop-out quest windows when font family/scale changes."""
        fam = "Segoe UI"
        if hasattr(self, "_overlay_font_var"):
            try:
                fam = self._overlay_font_var.get() or "Segoe UI"
            except Exception:
                pass
        scale = 1.0
        if hasattr(self, "_overlay_font_scale_pct_var"):
            try:
                scale = max(0.8, min(1.6, int(self._overlay_font_scale_pct_var.get()) / 100.0))
            except Exception:
                scale = 1.0
        elif hasattr(self, "_overlay_font_scale_var"):
            try:
                scale = max(0.8, min(1.6, float(self._overlay_font_scale_var.get())))
            except Exception:
                scale = 1.0
        self._app.config["overlay_font_family"] = fam
        self._app.config["overlay_font_scale"] = round(scale, 2)
        try:
            self._app.save_config()
        except Exception:
            log.debug("save_config during typography failed", exc_info=True)
        ov = getattr(self._app, "overlay_window", None)
        if ov is not None and hasattr(ov, "apply_typography"):
            try:
                ov.apply_typography()
            except Exception:
                log.debug("overlay apply_typography failed", exc_info=True)

    def _apply_overlay_opacity(self, v):
        """Apply opacity globally to every open pop-out quest window."""
        alpha = max(0.4, min(1.0, v / 100.0))
        self._app.config["overlay_opacity"] = round(alpha, 2)
        try:
            self._app.save_config()
        except Exception:
            pass
        ov = getattr(self._app, "overlay_window", None)
        if ov is not None:
            try:
                if hasattr(ov, "apply_opacity"):
                    ov.apply_opacity(alpha)
                else:
                    for bub in getattr(ov, "_bubbles", {}).values():
                        try:
                            if bub.winfo_exists():
                                bub.attributes("-alpha", alpha)
                        except Exception:
                            pass
            except Exception:
                pass

    def _browse_log_folder(self):
        initial = self._log_dir_var.get() or \
                  r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest Legends\Logs"
        if not os.path.isdir(initial):
            initial = os.path.expanduser("~")
        path = fd.askdirectory(title="Select your EQ Legends Logs folder", initialdir=initial)
        if path:
            self._log_dir_var.set(path)

    def _browse_export_dir(self):
        path = fd.askdirectory(title="Select Export Directory")
        if path:
            self._export_dir_var.set(path)

    def _export_jsonl(self):
        def run():
            try:
                from app.db.export import export_jsonl, DEFAULT_EXPORT_DIR
                export_dir = self._export_dir_var.get() or DEFAULT_EXPORT_DIR
                path, count = export_jsonl(self._app.db_session, export_dir)
                mb.showinfo("Export Complete", f"Exported {count} records to:\n{path}")
            except Exception as e:
                mb.showerror("Export Failed", str(e))
        threading.Thread(target=run, daemon=True).start()

    def _sign_in_discord(self):
        self._app.auth.sign_in_discord()

    def _sign_out(self):
        self._app.auth.sign_out()
        self._built_while_mapped = False
        self.ensure_visible()

    def _save(self):
        # Cross-game guard — this app reads EverQuest LEGENDS logs only. Reject a folder that is
        # unambiguously LIVE EverQuest (a path segment exactly "EverQuest" with no "Legends"
        # anywhere) so live-EQ dialogue can never reach the Legends community database.
        _pick = self._log_dir_var.get().strip()
        _low = _pick.lower()
        if _pick and "legends" not in _low and any(
                s.strip() == "everquest" for s in _low.replace("/", "\\").split("\\")):
            mb.showerror(
                "That's live EverQuest, not Legends",
                "Gnoll Guard reads EverQuest LEGENDS logs only.\n\n"
                "That folder looks like live EverQuest. Point it at your "
                "EverQuest Legends → Logs folder instead.")
            return
        # Folder is the source of truth now — the watcher tails every log in it. Keep
        # log_file_path pointed inside the chosen folder so back-compat paths stay valid.
        _dir = self._log_dir_var.get().strip()
        if _dir:
            self._app.config["log_dir"] = _dir
            if not os.path.dirname(self._log_path_var.get()) == _dir:
                import glob as _g
                _found = sorted(_g.glob(os.path.join(_dir, "eqlog_*.txt")))
                self._log_path_var.set(_found[0] if _found else os.path.join(_dir, "eqlog.txt"))
        self._app.config["log_file_path"] = self._log_path_var.get()
        self._app.config["theme"] = "light" if self._theme_var.get() == "Light" else "default"
        # Pop-out overlays are always available from the Journal tab (no dock hub toggle).
        self._app.config["overlay_enabled"] = True
        self._app.config["overlay_opacity"] = round(self._overlay_opacity_var.get() / 100.0, 2)
        if hasattr(self, "_overlay_font_var"):
            try:
                self._app.config["overlay_font_family"] = self._overlay_font_var.get()
            except Exception:
                pass
        if hasattr(self, "_overlay_font_scale_pct_var"):
            try:
                self._app.config["overlay_font_scale"] = round(
                    max(0.8, min(1.6, int(self._overlay_font_scale_pct_var.get()) / 100.0)), 2)
            except Exception:
                self._app.config["overlay_font_scale"] = 1.0
        elif hasattr(self, "_overlay_font_scale_var"):
            try:
                self._app.config["overlay_font_scale"] = round(
                    float(self._overlay_font_scale_var.get()), 2)
            except Exception:
                self._app.config["overlay_font_scale"] = 1.0
        # Sound / auto-dismiss alert settings removed — feed is silent + in-window only.
        self._app.config["audio_enabled"] = False
        self._app.config["export_directory"] = self._export_dir_var.get()
        if self._supa_url_var.get().strip():
            self._app.config["supabase_url"] = self._supa_url_var.get().strip()
        if self._supa_key_var.get().strip():
            self._app.config["supabase_key"] = self._supa_key_var.get().strip()
        self._app.save_config()

        applied = False
        if hasattr(self._app, "apply_log_path"):
            try:
                self._app.apply_log_path(self._log_path_var.get())
                applied = True
            except Exception:
                applied = False

        if applied:
            mb.showinfo("Settings Saved",
                        "Settings saved. Now watching your log file.\n"
                        "(Log pattern changes still need an app restart.)")
        else:
            mb.showinfo("Settings Saved",
                        "Settings saved. Restart the app to apply log changes.")

    def _check_for_updates(self):
        self._update_status_var.set("Checking…")
        checker = getattr(self._app, "update_checker", None)
        if checker:
            checker.check_now()
            # Show "up to date" after a few seconds if no banner appeared
            def _maybe_up_to_date():
                import time; time.sleep(6)
                from app.version import __version__
                # If banner was shown, main_window will have handled it;
                # set the label back to current version in either case
                self.after(0, lambda: self._update_status_var.set(
                    f"Current version: {__version__}"
                ))
            threading.Thread(target=_maybe_up_to_date, daemon=True).start()
        else:
            mb.showinfo("Update Check", "Updater not available.")

