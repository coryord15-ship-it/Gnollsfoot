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
        self._build()

    def _build(self):
        # Clear any previous render so repeated builds (e.g. after sign-out) don't
        # stack multiple scroll frames on top of each other.
        for w in self.winfo_children():
            w.destroy()
        scroll = ctk.CTkScrollableFrame(self, fg_color=theme.BG)
        scroll.pack(fill="both", expand=True, padx=theme.PAD, pady=theme.PAD)

        self._section(scroll, "Log File")
        ctk.CTkLabel(
            scroll,
            text="Point this at your EverQuest log file. "
                 "It's usually inside your EQ game folder — look for a file named "
                 "eqlog_<CharacterName>_<Server>.txt",
            font=theme.FONT_BODY, text_color=theme.TEXT_MUTED, anchor="w",
            wraplength=700, justify="left",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        path_row = ctk.CTkFrame(scroll, fg_color="transparent")
        path_row.pack(fill="x", pady=(0, theme.PAD))
        self._log_path_var = ctk.StringVar(value=self._app.config.get("log_file_path", ""))
        ctk.CTkEntry(
            path_row, textvariable=self._log_path_var,
            fg_color=theme.PANEL, text_color=theme.TEXT_PRIMARY,
            border_color=theme.BORDER, font=theme.FONT_BODY,
        ).pack(side="left", fill="x", expand=True, padx=(0, theme.PAD_SM))
        ctk.CTkButton(
            path_row, text="Browse", width=80,
            fg_color=theme.GOLD, text_color=theme.BG,
            hover_color=theme.GREEN, font=theme.FONT_BODY,
            command=self._browse_log,
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

    # ── Helpers ──────────────────────────────────────────────────────────────

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

    def _apply_overlay_opacity(self, v):
        self._app.config["overlay_opacity"] = round(v / 100.0, 2)
        ov = getattr(self._app, "overlay_window", None)
        if ov is not None:
            try:
                ov.attributes("-alpha", max(0.4, min(1.0, v / 100.0)))
            except Exception:
                pass

    def _browse_log(self):
        initial = self._app.config.get("eql_log_dir") or \
                  self._app.config.get("eql_game_dir") or \
                  os.path.expanduser("~")
        path = fd.askopenfilename(
            title="Select EQL Log File",
            initialdir=initial,
            filetypes=[("Log files", "*.txt *.log"), ("All files", "*.*")],
        )
        if path:
            self._log_path_var.set(path)

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
        self._build()

    def _save(self):
        self._app.config["log_file_path"] = self._log_path_var.get()
        self._app.config["theme"] = "light" if self._theme_var.get() == "Light" else "default"
        self._app.config["overlay_enabled"] = bool(self._overlay_var.get())
        self._app.config["overlay_borderless"] = bool(self._overlay_borderless_var.get())
        self._app.config["overlay_click_through"] = bool(self._overlay_clickthrough_var.get())
        self._app.config["overlay_opacity"] = round(self._overlay_opacity_var.get() / 100.0, 2)
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
