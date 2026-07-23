"""
Settings tab content. Rendered inside the main window's tab view.
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

        self._overlay_var = ctk.BooleanVar(value=self._app.config.get("overlay_enabled", False))
        ctk.CTkSwitch(
            scroll, text="Enable Overlay Window", variable=self._overlay_var,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
            progress_color=theme.GOLD, button_color=theme.GOLD,
            command=self._toggle_overlay,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        self._overlay_borderless_var = ctk.BooleanVar(
            value=self._app.config.get("overlay_borderless", False)
        )
        ctk.CTkSwitch(
            scroll, text="Borderless overlay (slim title bar)",
            variable=self._overlay_borderless_var,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
            progress_color=theme.GOLD, button_color=theme.GOLD,
            command=self._toggle_overlay,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        self._overlay_clickthrough_var = ctk.BooleanVar(
            value=self._app.config.get("overlay_click_through", True)
        )
        ctk.CTkSwitch(
            scroll, text="Overlay click-through (clicks pass to the game unless on text/buttons)",
            variable=self._overlay_clickthrough_var,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
            progress_color=theme.GOLD, button_color=theme.GOLD,
            command=lambda: self._app.config.update(
                {"overlay_click_through": bool(self._overlay_clickthrough_var.get())}
            ),
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
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
            op_row, text="Overlay opacity", font=theme.FONT_BODY,
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

        self._section(scroll, "Alerts")
        self._duration_var = ctk.IntVar(value=self._app.config.get("alert_duration_seconds", 10))
        self._slider_row(
            scroll, "Auto-dismiss (seconds)", self._duration_var, 3, 30,
        )
        self._audio_var = ctk.BooleanVar(value=self._app.config.get("audio_enabled", True))
        self._audio_switch = ctk.CTkSwitch(
            scroll,
            text="Sound alerts: ON" if self._audio_var.get() else "Sound alerts: OFF",
            variable=self._audio_var,
            text_color=theme.TEXT_PRIMARY, font=theme.FONT_BODY,
            progress_color=theme.GOLD, button_color=theme.GOLD,
        )
        self._audio_switch.pack(anchor="w", pady=(0, theme.PAD_SM))
        self._audio_var.trace_add("write", lambda *_: self._audio_switch.configure(
            text="Sound alerts: ON" if self._audio_var.get() else "Sound alerts: OFF"
        ))

        self._volume_var = ctk.IntVar(value=self._app.config.get("audio_volume", 50))
        vol_row = ctk.CTkFrame(scroll, fg_color="transparent")
        vol_row.pack(fill="x", pady=(0, theme.PAD))
        vol_val = ctk.CTkLabel(
            vol_row, text=f"{self._volume_var.get()}%",
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY, width=36,
        )
        ctk.CTkLabel(
            vol_row, text="Alert volume",
            font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")
        vol_val.pack(side="right")
        ctk.CTkSlider(
            vol_row, variable=self._volume_var, from_=0, to=100,
            button_color=theme.GOLD, progress_color=theme.GOLD,
            fg_color=theme.PANEL,
            command=lambda v: vol_val.configure(text=f"{int(v)}%"),
        ).pack(side="right", padx=theme.PAD, fill="x", expand=True)

        self._export_dir_var = ctk.StringVar(
            value=self._app.config.get("export_directory", "")
        )

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

        ctk.CTkButton(
            scroll, text="Save Settings",
            fg_color=theme.GOLD, text_color=theme.BG,
            hover_color=theme.GREEN, font=theme.FONT_SUBHEADER,
            command=self._save,
        ).pack(anchor="w", pady=(theme.PAD, 0))
        self._built_while_mapped = True

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_overlay_typography(self, scroll):
        """Font family + size for Quest Dock / bubbles. Isolated so failures don't blank Settings."""
        families = list(getattr(theme, "FONT_FAMILIES", None) or (
            "Segoe UI", "Helvetica", "Georgia", "Consolas"))
        ctk.CTkLabel(
            scroll,
            text="Overlay font (hub + quest bubbles). Helvetica uses Arial on Windows if needed.",
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

    def _slider_row(self, parent, label: str, var, from_: int, to: int):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, theme.PAD))
        val_label = ctk.CTkLabel(
            row, text=f"{var.get()}s",
            font=theme.FONT_BODY, text_color=theme.TEXT_SECONDARY, width=32,
        )
        ctk.CTkLabel(
            row, text=label,
            font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY, anchor="w",
        ).pack(side="left")
        val_label.pack(side="right")
        slider = ctk.CTkSlider(
            row, variable=var, from_=from_, to=to,
            button_color=theme.GOLD, progress_color=theme.GOLD,
            fg_color=theme.PANEL,
            command=lambda v: val_label.configure(text=f"{int(v)}s"),
        )
        slider.pack(side="right", padx=theme.PAD, fill="x", expand=True)

    def _toggle_overlay(self):
        cfg = self._app.config
        cfg["overlay_enabled"] = bool(self._overlay_var.get())
        cfg["overlay_borderless"] = bool(self._overlay_borderless_var.get())
        cfg["overlay_opacity"] = round(self._overlay_opacity_var.get() / 100.0, 2)
        self._app.save_config()
        mw = getattr(self._app, "main_window", None)
        if mw is None or not hasattr(mw, "toggle_overlay"):
            return
        if cfg["overlay_enabled"]:
            # Recreate so borderless / opacity changes take effect right away.
            mw.toggle_overlay(False)
            mw.toggle_overlay(True)
        else:
            mw.toggle_overlay(False)

    def _apply_overlay_typography(self):
        """Live-update overlay hub + bubbles when font family/scale changes."""
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
        if ov is not None:
            try:
                if ov.winfo_exists() and hasattr(ov, "apply_typography"):
                    ov.apply_typography()
            except Exception:
                log.debug("overlay apply_typography failed", exc_info=True)

    def _apply_overlay_opacity(self, v):
        alpha = max(0.4, min(1.0, v / 100.0))
        self._app.config["overlay_opacity"] = round(alpha, 2)
        ov = getattr(self._app, "overlay_window", None)
        if ov is not None:
            try:
                if ov.winfo_exists():
                    ov.attributes("-alpha", alpha)
                # Apply to open quest bubbles too
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
        self._app.config["overlay_enabled"] = bool(self._overlay_var.get())
        self._app.config["overlay_borderless"] = bool(self._overlay_borderless_var.get())
        self._app.config["overlay_click_through"] = bool(self._overlay_clickthrough_var.get())
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
        self._app.config["alert_duration_seconds"] = self._duration_var.get()
        self._app.config["audio_enabled"] = self._audio_var.get()
        self._app.config["audio_volume"] = self._volume_var.get()
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

    def _fire_test_alert(self):
        from app.parsers.loot_parser import LootEvent
        fake = LootEvent(item_name="[DEBUG] Gnoll Test Widget", raw_line="[TEST] --You have looted a [DEBUG] Gnoll Test Widget.--")
        if hasattr(self._app, "_fire_loot"):
            threading.Thread(target=self._app._fire_loot, args=(fake,), daemon=True).start()
        else:
            mb.showinfo("Not wired", "Test callback not available.")
