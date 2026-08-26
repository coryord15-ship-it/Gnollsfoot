"""Host for the ported devkit tabs, used by BOTH the Combat and Tools sections.

The ported builders in `extra_views` have the signature `tab_x(tab, app)` and reach into
`app.tail`, `app.equipped`, `app.inventory_path` and `app.redraw_*`. Rather than bolt those
onto the real application controller -- which owns the database, sync and updater and has no
business growing UI state -- this frame IS the `app` they receive. The ported bodies stay
byte-identical to code that was already smoke-tested, and the controller stays clean.

Refresh policy is the one the owner asked for on 2026-08-23: *"the only thing that should
refresh that often is the layover live view."* Only the visible tab repopulates, only when its
data changed, and never while the window is in the background.
"""
from __future__ import annotations

import logging

import customtkinter as ctk

from app.ui import datapaths, dps_test, extra_views, history_load, theme

log = logging.getLogger(__name__)

REFRESH_MS = 4000

#: Tabs that are driven by the live log, so polling them is worthwhile. Gear and Codex are
#: driven by files and filter widgets -- repopulating those on a timer is pure waste.
LIVE_TABS = ("Combat", "Healing", "Loot")


class _HistoryFeed:
    """Stands in for CombatFeed when browsing a past day.

    Same attribute surface, so every ported tab renders it without knowing the
    difference. `live_seen` is False on purpose -- a fight from last Tuesday must
    never render as "IN COMBAT".
    """

    def __init__(self, lc, note=""):
        self.lc = lc
        self.loot = []
        self.zone = note
        self.live_seen = False
        self.last_ts = 0.0
        self.heal_zero = {"you": 0, "others": 0}
        self.heal_any = {"you": 0, "others": 0}


class PortedSection(ctk.CTkFrame):
    """Builds one or more ported tabs and gives them the `app` surface they expect.

    `builders`     ordered {tab name: builder fn}
    `show_classes` draw the 3-slot class picker (drives the Codex filter)
    `show_overlay` draw the Overlay button
    """

    def __init__(self, master, app=None, feed=None, builders=None,
                 show_classes=False, show_overlay=True, show_history=False):
        super().__init__(master, fg_color=theme.BG, corner_radius=0)
        self._app = app

        # `tail` is the shared CombatFeed. The name is kept because the ported bodies say
        # `app.tail.lc`; one alias is cheaper and safer than editing 750 lines of working code.
        self.tail = feed
        self._live_feed = feed          # restored when leaving history mode
        self.equipped = []
        self.inventory_path = ""
        self._last_sig = None
        self._built = set()
        self._overlay = None
        self._builders = builders or {}

        self._rescan_inventory()

        gone = datapaths.missing()
        if gone and show_classes:
            # Only worth saying on the section that actually uses the snapshots.
            ctk.CTkLabel(
                self, anchor="w", justify="left", font=theme.FONT_BODY_SMALL,
                text_color=theme.DANGER,
                text=("Reference data not found: %s\nExpected in %s  —  Gear and Codex will be "
                      "empty until it is there." % (", ".join(gone), datapaths.DATA_DIR))
            ).pack(fill="x", padx=12, pady=(8, 0))

        if show_classes or show_overlay:
            top = ctk.CTkFrame(self, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(8, 0))
            if show_overlay:
                # 🔴 Owner, 2026-08-25: *"why doesnt combat have an overlay popout ... healing
                # does but not combat."* It was a misplacement: the button lived on the Tools
                # header only, and the overlay is a COMBAT readout. Both sections get one now.
                ctk.CTkButton(top, text="Overlay", width=90, height=28, corner_radius=8,
                              fg_color=theme.PANEL_HOVER, hover_color=theme.PANEL,
                              text_color=theme.GOLD, font=theme.FONT_BODY_SMALL,
                              command=self.open_overlay).pack(side="right")
            if show_classes:
                self._build_class_picker(top)
            if show_history:
                self._build_history_picker(top)

        self.tabs = ctk.CTkTabview(
            self, fg_color=theme.PANEL, segmented_button_fg_color=theme.PANEL_HOVER,
            segmented_button_selected_color=theme.GOLD,
            segmented_button_selected_hover_color=theme.GOLD,
            text_color=theme.TEXT_PRIMARY, corner_radius=10)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=8)

        for name in self._builders:
            self.tabs.add(name)
        self.tabs.configure(command=self._on_tab_change)
        first = next(iter(self._builders), None)
        if first:
            self.tabs.set(first)
            self._ensure_tab(force=True)
        self.after(REFRESH_MS, self._tick)

    # ── class picker ────────────────────────────────────────────────────────
    def _build_class_picker(self, parent):
        """Three ordered class slots weighted 3:2:1 — the same ranking the site's BiS uses.

        Was lost in the first merge pass (the Codex silently fell back to a hardcoded
        PAL/MNK/ENC). The owner plays a combo, so this is not cosmetic: it decides which
        exaltation donors are usable at all.
        """
        ctk.CTkLabel(parent, text="YOUR CLASSES", font=theme.FONT_BODY_SMALL,
                     text_color=theme.TEXT_MUTED).pack(side="left", padx=(4, 8))
        self.menus = []
        for i in range(3):
            m = ctk.CTkOptionMenu(
                parent, width=104, height=28, values=["-"] + extra_views.CLASSES,
                fg_color=theme.PANEL, button_color=theme.PANEL_HOVER,
                button_hover_color=theme.GOLD, text_color=theme.TEXT_PRIMARY,
                dropdown_fg_color=theme.PANEL, dropdown_text_color=theme.TEXT_PRIMARY,
                dropdown_hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
                dropdown_font=theme.FONT_BODY_SMALL, corner_radius=7,
                command=self._classes_changed)
            cur = extra_views.MY_CLASSES[i] if i < len(extra_views.MY_CLASSES) else ""
            m.set(cur or "-")
            m.pack(side="left", padx=3)
            self.menus.append(m)
            ctk.CTkLabel(parent, text="x%d" % extra_views.POSITION_WEIGHTS[i],
                         font=theme.FONT_BODY_SMALL,
                         text_color=theme.TEXT_MUTED).pack(side="left")

    # ── browsing a past day ─────────────────────────────────────────────────
    def _build_history_picker(self, parent):
        """Load a past day out of the log archives.

        The live parser trims to 300 fights so it can run for hours without growing without
        bound; everything older is still on disk. This re-reads a chosen day into a separate
        parser and swaps it in behind the same tabs.
        """
        ctk.CTkLabel(parent, text="HISTORY", font=theme.FONT_BODY_SMALL,
                     text_color=theme.TEXT_MUTED).pack(side="left", padx=(4, 8))
        try:
            days = history_load.available_days()
        except Exception:
            log.debug("could not list log days", exc_info=True)
            days = []
        vals = ["live"] + [d.strftime("%Y-%m-%d") for d in days]
        self._day_menu = ctk.CTkOptionMenu(
            parent, width=132, height=28, values=vals or ["live"],
            fg_color=theme.PANEL, button_color=theme.PANEL_HOVER,
            button_hover_color=theme.GOLD, text_color=theme.TEXT_PRIMARY,
            dropdown_fg_color=theme.PANEL, dropdown_text_color=theme.TEXT_PRIMARY,
            dropdown_hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
            dropdown_font=theme.FONT_BODY_SMALL, corner_radius=7,
            command=self._day_chosen)
        self._day_menu.set("live")
        self._day_menu.pack(side="left", padx=3)
        self._day_note = ctk.CTkLabel(parent, text="", font=theme.FONT_BODY_SMALL,
                                      text_color=theme.TEXT_MUTED)
        self._day_note.pack(side="left", padx=8)

    def _day_chosen(self, value):
        if value == "live":
            self.tail = self._live_feed
            self._day_note.configure(text="", text_color=theme.TEXT_MUTED)
            self._last_sig = None
            self._rebuild(self.tabs.get())
            return
        try:
            import datetime
            day = datetime.datetime.strptime(value, "%Y-%m-%d").date()
        except Exception:
            return
        self._day_note.configure(text="reading archives…", text_color=theme.GOLD)

        def done(lc, note):
            # Called on the WORKER thread -- bounce to the UI thread before touching widgets.
            def apply():
                if lc is None:
                    self._day_note.configure(text=note, text_color=theme.DANGER)
                    return
                self.tail = _HistoryFeed(lc, note)
                self._day_note.configure(text=note, text_color=theme.TEXT_MUTED)
                self._last_sig = None
                self._rebuild(self.tabs.get())
            try:
                self.after(0, apply)
            except Exception:
                pass

        history_load.load_day(day, done)

    def _classes_changed(self, _=None):
        picked = [m.get() for m in self.menus]
        # Mutate IN PLACE -- the ported tabs hold a reference to this list, so rebinding the
        # module attribute would leave them reading the old one.
        extra_views.MY_CLASSES[:] = [c for c in picked if c and c != "-"]
        for name in ("Codex", "Gear"):
            if name in self._builders:
                self._rebuild(name)

    # ── build vs refresh ────────────────────────────────────────────────────
    # 🔴 THE CONTRACT, inverted on the first attempt: a builder draws only its static header,
    # then ASSIGNS its populate closure onto the host (`app.redraw_loot = redraw`). It never
    # calls that closure. Building without invoking it renders a title and nothing else --
    # which is exactly what the first smoke test showed: 9 widgets for 80 loot events.
    _ATTR = {"Combat": "redraw_combat", "Healing": "redraw_healing", "Loot": "redraw_loot",
             "Gear": "redraw_gear", "Codex": "redraw_codex", "DPS Test": "redraw_dps_test"}

    def _rebuild(self, name):
        tab = self.tabs.tab(name)
        for w in tab.winfo_children():
            w.destroy()
        # Drop the stale closure first: it captures widgets we just destroyed, so if the
        # builder raises, `_refresh` must not keep calling the old one.
        self.__dict__.pop(self._ATTR.get(name, ""), None)
        try:
            if name == "Gear":
                self._rescan_inventory()
            self._builders[name](tab, self)
            self._built.add(name)
            self._refresh(name)
        except Exception:
            log.exception("tab %s failed to build", name)
            ctk.CTkLabel(tab, text="This tab hit an error — see the log.",
                         text_color=theme.DANGER, font=theme.FONT_BODY).pack(padx=16, pady=16)

    def _refresh(self, name):
        fn = self.__dict__.get(self._ATTR.get(name, ""))
        if callable(fn):
            try:
                fn()
            except Exception:
                log.exception("tab %s failed to refresh", name)

    def _rescan_inventory(self):
        # Re-read the dump so a fresh /outputfile is picked up without restarting.
        # A STALE dump is worse than none -- it looks authoritative and is wrong.
        try:
            self.inventory_path = extra_views.find_inventory() or self.inventory_path
        except Exception:
            log.debug("inventory dump not found", exc_info=True)

    # ── refresh loop ────────────────────────────────────────────────────────
    def _signature(self):
        t = self.tail
        if not t or not getattr(t, "lc", None):
            return None
        cur = getattr(t.lc, "attacking", None)
        return (len(t.lc.fights), len(t.loot), t.zone,
                round(cur.end, 1) if cur is not None else 0)

    def _visible(self):
        try:
            return bool(self.winfo_ismapped()) and self.winfo_toplevel().focus_displayof() is not None
        except Exception:
            return True

    def _ensure_tab(self, force=False):
        name = self.tabs.get()
        if force or name not in self._built:
            self._rebuild(name)

    def _on_tab_change(self):
        self._ensure_tab()

    def _tick(self):
        try:
            name = self.tabs.get()
            if name in LIVE_TABS and self._visible():
                sig = self._signature()
                if sig != self._last_sig:
                    self._last_sig = sig
                    # Repopulate, do NOT rebuild: the header and controls are unchanged, and
                    # tearing the subtree down on a timer is what made the devkit window
                    # impossible to drag.
                    self._refresh(name)
        except Exception:
            log.exception("section refresh")
        self.after(REFRESH_MS, self._tick)

    # ── overlay ─────────────────────────────────────────────────────────────
    def open_overlay(self):
        """Always-on-top HUD. Never steals focus, opens no popups.

        One overlay per app, not per section: opening it from Combat and again from Tools
        must raise the existing window rather than stack a second copy on the first.
        """
        try:
            root = self.winfo_toplevel()
            existing = getattr(root, "_gg_overlay", None)
            if existing is not None and existing.winfo_exists():
                existing.lift()
                self._overlay = existing
                return
            self._overlay = extra_views.Overlay(root, self.tail)
            root._gg_overlay = self._overlay
        except Exception:
            log.exception("overlay failed to open")
            self._overlay = None


class ToolsSection(PortedSection):
    """Loot / Gear / Codex, with the class picker.

    NO overlay button. Owner, 2026-08-25: *"in the tools section you can remove the overlay
    button."* It belongs with Combat, which is what it reports on -- and Healing moved there
    too, so nothing in Tools is a live-combat readout any more.
    """

    def __init__(self, master, app=None, feed=None):
        super().__init__(
            master, app, feed,
            builders={"Loot": extra_views.tab_loot,
                      "Gear": extra_views.tab_gear,
                      "Codex": extra_views.tab_codex},
            show_classes=True, show_overlay=False)


class CombatSection(PortedSection):
    """Combat and Healing: two readings of the same fights, so they live together.

    Healing moved here on 2026-08-25 at the owner's request -- it was in Tools, which is
    otherwise reference data (items, drops, donors) rather than anything live.

    The full combat readout: browsable history, pinning, per-actor drill-down.

    Replaces the plainer `CombatView`. Owner, 2026-08-22, about that earlier one: *"doesnt
    have a history but it shows like the last mob. kinda plain no real details to it."* The
    devkit build answered that and the first merge pass regressed it by keeping the app's
    original tab; this restores the richer version.
    """

    def __init__(self, master, app=None, feed=None):
        super().__init__(
            master, app, feed,
            builders={"Combat": extra_views.tab_combat,
                      "Healing": extra_views.tab_healing,
                      "DPS Test": dps_test.tab_dps_test},
            show_classes=False, show_overlay=True, show_history=True)
