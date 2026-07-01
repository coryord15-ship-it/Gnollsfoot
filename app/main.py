"""
Gnoll Guard entry point — a Quest Journal + Item Database utility for EverQuest Legends.

Startup:
1. Load config from settings.json.
2. Initialize SQLite DB.
3. Start the log watcher (if a log path is configured).
4. Build the UI + floating quest-item alert window.
5. Wire the pieces together and hand control to the tkinter event loop.
6. Start the system tray icon (in its own thread).

The app reads your EverQuest log to tick off Quest Journal items and silently
contribute item data to the community database. Verified items sync to Supabase.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import customtkinter as ctk

from app.alerts.engine import Alert, AlertEngine
from app.alerts.window import AlertWindow
from app import quest_progress
from app.db.models import create_db_engine, make_session_factory
from app.db.queries import (
    get_item, get_items, delete_item, log_loot_event, prune_loot_events,
    upsert_item, upsert_npc, add_dialogue, set_npc_location, verify_item,
    log_vendor_price,
)
from app.clipboard_watcher import ClipboardWatcher
from app.log_watcher import LogWatcher
from app.log_rotate import LogRotator
from app.parsers.item_ocr import ocr_image, parse_item_text
from app.parsers.npc_parser import extract_item_hints
from app.updater import UpdateChecker
from app.sync.auth import AuthManager
from app.sync.supabase import SupabaseSync
from app.parsers.inventory_parser import parse_inventory
from app.ui.main_window import MainWindow

log = logging.getLogger(__name__)


def _resolve_config_path() -> str:
    """
    When frozen (installed .exe), config lives in %APPDATA%\GnollGuard\ so it
    persists across updates. On first run, the bundled defaults are copied there.
    When running from source, use the repo's config/settings.json as normal.
    """
    if getattr(sys, "frozen", False):
        user_dir = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "GnollGuard"
        )
        os.makedirs(user_dir, exist_ok=True)
        user_path = os.path.join(user_dir, "settings.json")
        if not os.path.exists(user_path):
            bundled = os.path.join(sys._MEIPASS, "config", "settings.json")
            if os.path.exists(bundled):
                shutil.copy(bundled, user_path)
        return user_path
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "settings.json",
    )


def _migrate_legacy_dirs():
    """Rebrand carryover: data used to live under 'GnollLoot' folders. Move them
    to the new 'GnollGuard' name so existing users keep their login, config, local
    database, and quest progress. Runs once, before any folder is created."""
    for base in (
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        os.path.join(os.path.expanduser("~"), "Documents"),
    ):
        old = os.path.join(base, "GnollLoot")
        new = os.path.join(base, "GnollGuard")
        try:
            if os.path.isdir(old) and not os.path.exists(new):
                os.rename(old, new)
        except Exception:
            pass


_migrate_legacy_dirs()
_CONFIG_PATH = _resolve_config_path()
_LOG_DIR = os.path.join(os.path.expanduser("~"), "Documents", "GnollGuard")


def _setup_logging():
    os.makedirs(_LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(_LOG_DIR, "app.log"), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("settings.json not found — using defaults")
        return {}
    except json.JSONDecodeError as e:
        log.error("settings.json is malformed: %s", e)
        return {}


def _save_config(config: dict):
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        log.error("Failed to save config: %s", e)


def _migrate_config(config: dict) -> dict:
    """
    Ensure user's persisted config has the latest log_patterns.
    The APPDATA copy is only created on first run, so pattern fixes in new
    versions would never reach existing installs without this migration.
    """
    bundled_path = None
    if getattr(sys, "frozen", False):
        bundled_path = os.path.join(sys._MEIPASS, "config", "settings.json")
    else:
        bundled_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "settings.json",
        )

    try:
        with open(bundled_path, encoding="utf-8") as f:
            bundled = json.load(f)
    except Exception:
        return config

    bundled_patterns = bundled.get("log_patterns", {})
    user_patterns = config.get("log_patterns", {})

    # If loot_triggers has fewer entries than bundled (old single-pattern format),
    # replace the entire log_patterns block with the latest bundled version.
    bundled_triggers = bundled_patterns.get("loot_triggers", [])
    user_triggers = user_patterns.get("loot_triggers", [])
    if len(user_triggers) < len(bundled_triggers):
        log.info(
            "Migrating log_patterns: user has %d trigger(s), bundled has %d — updating",
            len(user_triggers), len(bundled_triggers),
        )
        config["log_patterns"] = bundled_patterns
        _save_config(config)
    else:
        # Remove the false-positive vendor-sell loot trigger if it still exists
        _bad = r"You receive (?P<item>.+?) from (?P<npc>.+?)\."
        cleaned = [t for t in user_triggers if t != _bad]
        if len(cleaned) != len(user_triggers):
            log.info("Removed false-positive vendor-sell loot trigger from user config")
            user_patterns["loot_triggers"] = cleaned
            config["log_patterns"] = user_patterns
            _save_config(config)
        # Copy over any new pattern keys the bundled version added
        for key in ("npc_target", "npc_slain", "vendor_sell", "vendor_buy",
                    "zone_line", "zone_status", "quest_turn_in", "auto_sold"):
            if key not in user_patterns and key in bundled_patterns:
                user_patterns[key] = bundled_patterns[key]
        # Force-refresh zone_line for installs that still have the pre-difficulty
        # pattern (EQL appends "<N> (<Label>)" to zone names — old pattern kept it).
        if "?P<diff>" not in user_patterns.get("zone_line", "") and bundled_patterns.get("zone_line"):
            user_patterns["zone_line"] = bundled_patterns["zone_line"]
        config["log_patterns"] = user_patterns
        _save_config(config)

    return config


class AppState:
    """Central state holder — passed to UI components so they can reach everything."""

    def __init__(self):
        _setup_logging()
        self.config = _migrate_config(_load_config())

        # Apply the selected UI theme (default = dark | light) before any widget
        # is built, and match CustomTkinter's appearance mode for native chrome.
        try:
            from app.ui import theme
            _theme = self.config.get("theme", "default")
            theme.apply(_theme)
            ctk.set_appearance_mode("light" if _theme == "light" else "dark")
        except Exception:
            log.debug("theme apply failed", exc_info=True)

        # DB
        engine = create_db_engine()
        Session = make_session_factory(engine)
        self.db_session = Session()

        # Sync
        self.supabase = SupabaseSync(
            self.config.get("supabase_url", "") or "https://ratezylqpxgruyjscpbu.supabase.co",
            self.config.get("supabase_key", "") or "sb_publishable_hI8WF4abCLXa3SvVrChszA_z-Udl584",
        )
        self.auth = AuthManager(self.supabase._client)

        # Alert engine
        self.alert_engine = AlertEngine()

        # Log watcher
        self.log_watcher = LogWatcher(self.config)

        # Smart log rotation — archives the main log when EQ is closed + oversized
        self.log_rotator = LogRotator(
            get_log_path=lambda: self.log_watcher.log_path,
            archive_dir=os.path.join(_LOG_DIR, "logs_archive"),
            rotate_fn=self.log_watcher.rotate_to,
            threshold_mb=int(self.config.get("log_rotate_threshold_mb", 50)),
            check_every_s=int(self.config.get("log_rotate_check_seconds", 300)),
            enabled=bool(self.config.get("log_rotate_enabled", True)),
        )

        # In-memory community cache: lower(name) → community row dict.
        # Populated from Supabase on startup so loot lookups never hit the network.
        self._community_cache: dict = {}

        # Item names sold to / bought from vendors this session (Items tab filter)
        self._sold_items: set = set()
        self._bought_items: set = set()

        # Auto-sold loot observations buffered for batched community submit, and
        # the current zone (tagged onto each observation + the overlay).
        self._auto_sold_buffer: list = []
        self._current_zone = None

        # Quest progress — required-item → quest lookup (rebuilt from the journal),
        # the player's full journaled quests (for completion checks), the set of
        # quest items already looted, and the set of items turned in to an NPC.
        self._quest_item_index: dict = {}
        self._journal_quests: list = []
        self._quest_progress: set = quest_progress.load_progress()
        self._quest_given: set = quest_progress.load_given()

        # UI refs — set after UI is built
        self.main_window: MainWindow = None
        self.alert_window: AlertWindow = None
        self.overlay_window = None
        self.clipboard_watcher: ClipboardWatcher = None

        self._last_npc_id: int = None

    def save_config(self):
        _save_config(self.config)


# ── Event handlers ────────────────────────────────────────────────────────────


def _sync_community_data(app: AppState):
    """
    Pull all known items from Supabase into the in-memory cache, then remove
    any local copies that are now in the community DB.  Runs on a background
    thread at startup and whenever the user manually contributes an item.
    """
    # One-time cleanup: drop looted-coin entries that predate the coin filter.
    try:
        from app.db.queries import purge_coin_items
        n = purge_coin_items(app.db_session)
        if n:
            log.info("Purged %d coin entries from local items", n)
    except Exception:
        log.debug("coin purge failed", exc_info=True)

    community = app.supabase.pull_community_names()
    if not community:
        return
    app._community_cache = community
    log.info("Community cache loaded: %d items", len(community))

    try:
        local_items = get_items(app.db_session)
        removed = 0
        for item in local_items:
            if item.name.lower() in community:
                delete_item(app.db_session, item.name, getattr(item, "item_level", 0))
                removed += 1
        if removed:
            log.info("Removed %d local items now in community DB", removed)
    except Exception as exc:
        log.warning("Community sync cleanup failed: %s", exc)


def _build_quest_index(app):
    """Fetch the player's journaled quests and rebuild the required-item lookup
    so loot can tick quests off. Safe to call on a background thread."""
    try:
        quests = app.supabase.get_journal()
        app._journal_quests = quests
        app._quest_item_index = quest_progress.build_index(quests)
    except Exception:
        log.debug("quest index build failed", exc_info=True)


def _refresh_quest_views(app: AppState):
    """Refresh the Quest Journal in the main window AND the overlay (if open)."""
    win = app.main_window
    if not win:
        return
    if hasattr(win, "_journal_scroll"):
        win.after(0, win._refresh_journal)
    ov = getattr(app, "overlay_window", None)
    if ov is not None:
        try:
            if ov.winfo_exists():
                win.after(0, ov.refresh_journal)
        except Exception:
            pass


def _on_loot(app: AppState, loot_evt):
    item_name = loot_evt.item_name
    if not item_name:
        return

    npc_name = getattr(loot_evt, "npc_name", "") or ""
    log.info("Loot event: %s (mob: %s)", item_name, npc_name or "unknown")

    # Always record the loot (quest-hint matching + Items tracking); pruned to 24 h
    threading.Thread(
        target=lambda: log_loot_event(app.db_session, item_name),
        daemon=True,
    ).start()

    # Quest progress: if this drop is a required item in one of the player's
    # journaled quests, tick it off (✓), persist it, and fire a quest alert.
    # ALERTS FIRE ONLY FOR ACTIVE QUEST ITEMS — all other loot is silent.
    quest_name = quest_progress.match(app._quest_item_index, item_name)
    if quest_name and item_name.lower() not in app._quest_progress:
        app._quest_progress.add(item_name.lower())
        quest_progress.save_progress(app._quest_progress)
        app.alert_engine.quest_item_obtained(item_name, quest_name, npc_name=npc_name)
        _refresh_quest_views(app)

    # Non-quest loot: silent contribution to the DB only — no popup, no sound.
    if item_name.lower() in app._community_cache:
        return
    try:
        if get_item(app.db_session, item_name):
            return
    except Exception:
        log.warning("DB lookup failed for '%s'", item_name)
    threading.Thread(
        target=lambda: upsert_item(app.db_session, {"name": item_name, "verified": False}),
        daemon=True,
    ).start()


def _on_turn_in(app: AppState, evt):
    """Player handed a quest item to an NPC. Record it ('You have given NPC ITEM'),
    and if every required item of a journaled quest is now turned in, auto-remove
    that quest from the journal."""
    item_name = (getattr(evt, "item_name", "") or "").strip()
    npc_name = (getattr(evt, "npc_name", "") or "").strip()
    if not item_name:
        return

    # Only care about items that belong to a journaled quest.
    quest_name = quest_progress.match(app._quest_item_index, item_name)
    if not quest_name:
        return

    if item_name.lower() not in app._quest_given:
        app._quest_given.add(item_name.lower())
        quest_progress.save_given(app._quest_given)

    # Did this complete any journaled quest? If so, auto-remove it.
    completed = [q for q in app._journal_quests
                 if quest_progress.is_complete(q, app._quest_given)]

    app.alert_engine.quest_item_turned_in(item_name, npc_name, complete=bool(completed))

    for q in completed:
        qid = q.get("id")
        threading.Thread(target=lambda i=qid: app.supabase.remove_quest(i), daemon=True).start()
    if completed:
        done_ids = {q.get("id") for q in completed}
        app._journal_quests = [q for q in app._journal_quests if q.get("id") not in done_ids]
        app._quest_item_index = quest_progress.build_index(app._journal_quests)

    _refresh_quest_views(app)


def _on_auto_sold(app: AppState, evt):
    """EQL auto-sold a looted item to the bag-vendor. Silent: buffer the
    observation for batched community submit (powers the 'Frequently Auto-Sold'
    signal + passive vendor prices + drop data) and flag it for this session."""
    base = (getattr(evt, "base_name", "") or "").strip()
    if not base:
        return
    app._sold_items.add(base.lower())
    if getattr(evt, "item_name", ""):
        app._sold_items.add(evt.item_name.lower())
    buf = app._auto_sold_buffer
    if len(buf) < 500:  # safety cap between flushes
        buf.append({
            "item_name": base,
            "tier": int(getattr(evt, "tier", 0) or 0),
            "quantity": int(getattr(evt, "quantity", 1) or 1),
            "price_copper": int(getattr(evt, "price_copper", 0) or 0),
            "sold_for_free": bool(getattr(evt, "sold_for_free", False)),
            "drop_mob": getattr(evt, "npc_name", "") or "",
            "drop_zone": app._current_zone or "",
        })


def _on_zone(app: AppState, zone: str):
    """Player entered a new zone — update the overlay's 'Quests in Zone' tab and
    remember the zone for OCR submissions + auto-sold drop tagging."""
    app._current_zone = zone
    if app.alert_window is not None:
        app.alert_window.last_zone = zone
    ov = getattr(app, "overlay_window", None)
    win = app.main_window
    if ov is not None and win is not None:
        try:
            if ov.winfo_exists():
                win.after(0, lambda: ov.update_zone(zone))
        except Exception:
            log.debug("overlay zone update failed", exc_info=True)


def _on_dialogue(app: AppState, evt):
    def process():
        npc, is_new = upsert_npc(app.db_session, evt.npc_name)
        app._last_npc_id = npc.id

        hints = extract_item_hints(evt.text)
        add_dialogue(app.db_session, npc.id, evt.text, hints or None)

        if is_new:
            app.main_window.after(0, lambda: app.alert_engine.loc_prompt(evt.npc_name))

        if hints:
            recent = [
                r.item_name for r in
                app.db_session.execute(
                    __import__("sqlalchemy").text(
                        "SELECT item_name FROM loot_events ORDER BY real_timestamp DESC LIMIT 20"
                    )
                ).fetchall()
            ]
            for hint in hints:
                for looted in recent:
                    if hint.lower() in looted.lower() or looted.lower() in hint.lower():
                        verified = bool(get_item(app.db_session, looted) and
                                        get_item(app.db_session, looted).verified)
                        app.main_window.after(
                            0,
                            lambda h=hint, l=looted, v=verified:
                                app.alert_engine.quest_hint(l, evt.npc_name, h, v),
                        )
                        break

    threading.Thread(target=process, daemon=True).start()


def _on_loc(app: AppState, evt):
    # Track zone from /who output to auto-fill drop location on alerts
    if hasattr(evt, "zone") and evt.zone:
        if app.alert_window:
            app.alert_window.last_zone = evt.zone
    if app._last_npc_id is None:
        return
    npc_id = app._last_npc_id
    threading.Thread(
        target=lambda: set_npc_location(app.db_session, npc_id, evt.x, evt.y, evt.z),
        daemon=True,
    ).start()


def _on_npc_target(app: AppState, evt):
    """Silently upsert the targeted NPC so it exists in the DB before a kill/loot."""
    def _do():
        npc, is_new = upsert_npc(app.db_session, evt.npc_name)
        app._last_npc_id = npc.id
        if is_new:
            log.debug("New NPC discovered (target): %s", evt.npc_name)
    threading.Thread(target=_do, daemon=True).start()


def _on_npc_slain(app: AppState, evt):
    """Silently confirm the NPC exists when we land the killing blow."""
    def _do():
        npc, is_new = upsert_npc(app.db_session, evt.npc_name)
        if is_new:
            log.debug("New NPC discovered (slain): %s", evt.npc_name)
    threading.Thread(target=_do, daemon=True).start()


def _on_vendor_sell(app: AppState, evt):
    """Silently log a sell-to-vendor transaction — no popup, no sound."""
    if evt.item_name:
        app._sold_items.add(evt.item_name.lower())
    def _do():
        try:
            log_vendor_price(
                app.db_session,
                item_name=evt.item_name,
                merchant_name=evt.merchant_name,
                transaction_type="sell",
                price_copper=evt.price_copper,
                price_raw=evt.price_raw,
            )
            log.debug("Vendor sell logged: %s → %s @ %s", evt.item_name, evt.merchant_name, evt.price_raw)
            app.supabase.submit_vendor_price(
                evt.item_name, evt.merchant_name, "sell",
                evt.price_copper, evt.price_raw,
            )
        except Exception as exc:
            log.warning("Vendor sell log failed: %s", exc)
    threading.Thread(target=_do, daemon=True).start()


def _on_vendor_buy(app: AppState, evt):
    """Silently log a buy-from-vendor transaction — no popup, no sound."""
    if evt.item_name:
        app._bought_items.add(evt.item_name.lower())
    def _do():
        try:
            log_vendor_price(
                app.db_session,
                item_name=evt.item_name,
                merchant_name=evt.merchant_name,
                transaction_type="buy",
                price_copper=evt.price_copper,
                price_raw=evt.price_raw,
                quantity=evt.quantity,
            )
            log.debug("Vendor buy logged: %s × %d from %s @ %s",
                      evt.item_name, evt.quantity, evt.merchant_name, evt.price_raw)
            app.supabase.submit_vendor_price(
                evt.item_name, evt.merchant_name, "buy",
                evt.price_copper, evt.price_raw, evt.quantity,
            )
        except Exception as exc:
            log.warning("Vendor buy log failed: %s", exc)
    threading.Thread(target=_do, daemon=True).start()


# ── System tray ───────────────────────────────────────────────────────────────

def _build_tray(app: AppState):
    try:
        import pystray
        from PIL import Image, ImageDraw

        icon_path = os.path.join(
            getattr(sys, "_MEIPASS",
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "assets", "tray_icon.png",
        )
        if os.path.isfile(icon_path):
            image = Image.open(icon_path)
        else:
            image = Image.new("RGBA", (64, 64), "#0D0A0B")
            draw = ImageDraw.Draw(image)
            draw.ellipse([8, 8, 56, 56], fill="#C8960C")
            draw.text((20, 20), "GL", fill="#0D0A0B")

        def show_window(icon, item):
            app.main_window.after(0, app.main_window.deiconify)

        def quit_app(icon, item):
            icon.stop()
            app.main_window.after(0, _shutdown(app))

        menu = pystray.Menu(
            pystray.MenuItem("Show Gnoll Guard", show_window, default=True),
            pystray.MenuItem("Quit", quit_app),
        )
        tray = pystray.Icon("GnollGuard", image, "Gnoll Guard", menu)
        tray.run()
    except Exception as e:
        log.error("System tray failed: %s", e)


def _shutdown(app: AppState):
    def do_shutdown():
        app.log_watcher.stop()
        app.log_rotator.stop()
        try:
            app.db_session.close()
        except Exception:
            pass
        app.main_window.destroy()
    return do_shutdown


# ── Main ─────────────────────────────────────────────────────────────────────

def _run_setup_wizard(app: "AppState", win) -> str:
    """First-run flow: walk the user to their EQ directory + character log file
    and persist the chosen path. Returns the path, or '' if they cancelled."""
    import tkinter.messagebox as _mb
    import tkinter.filedialog as _fd
    try:
        _mb.showinfo(
            "Welcome to Gnoll Guard",
            "Let's find your EverQuest log file so Gnoll Guard can track your loot.\n\n"
            "1. Browse to your EverQuest game folder\n"
            "2. Open the 'Logs' folder\n"
            "3. Pick your character's log:  eqlog_<Character>_<Server>.txt\n\n"
            "You can change this any time in Settings.",
            parent=win,
        )
        path = _fd.askopenfilename(
            title="Select your EverQuest character log file",
            filetypes=[("EQ log files", "eqlog_*.txt"),
                       ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path and os.path.isfile(path):
            app.config["log_file_path"] = path
            _save_config(app.config)
            log.info("Setup wizard set log path: %s", path)
            return path
    except Exception:
        log.debug("setup wizard failed", exc_info=True)
    return ""


def _ensure_single_instance() -> bool:
    """
    On Windows, use a named mutex to allow only one running instance.
    If another instance is already running, bring its window to the front
    and return False so this process can exit cleanly.
    """
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "GnollGuard_v1_Mutex")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            hwnd = ctypes.windll.user32.FindWindowW(None, "Gnoll Guard")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)       # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            else:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "Gnoll Guard is already running.\n\n"
                    "Check the system tray (bottom-right of your taskbar) "
                    "and click the Gnoll Guard icon to reopen it.",
                    "Gnoll Guard",
                    0x40,  # MB_ICONINFORMATION
                )
            return False
    except Exception:
        pass  # Non-Windows or ctypes missing — proceed
    return True


def main():
    if not _ensure_single_instance():
        return

    app = AppState()

    # Wire log watcher callbacks
    app.log_watcher.on_loot(lambda evt: _on_loot(app, evt))
    app.log_watcher.on_dialogue(lambda evt: _on_dialogue(app, evt))
    app.log_watcher.on_loc(lambda evt: _on_loc(app, evt))
    app.log_watcher.on_npc_target(lambda evt: _on_npc_target(app, evt))
    app.log_watcher.on_npc_slain(lambda evt: _on_npc_slain(app, evt))
    app.log_watcher.on_vendor_sell(lambda evt: _on_vendor_sell(app, evt))
    app.log_watcher.on_vendor_buy(lambda evt: _on_vendor_buy(app, evt))
    app.log_watcher.on_turn_in(lambda evt: _on_turn_in(app, evt))
    app.log_watcher.on_zone(lambda z: _on_zone(app, z))
    app.log_watcher.on_auto_sold(lambda evt: _on_auto_sold(app, evt))

    # Build UI
    win = MainWindow(app)
    app.main_window = win

    def _arm_submit_ocr(alert):
        """
        Called after the countdown in the popup completes.
        Grabs the full screen, runs OCR to pull item stats, then opens the
        submit website with everything pre-filled — no clipboard needed.
        """
        def _do():
            import webbrowser
            from urllib.parse import quote as _q
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab()
            except Exception as e:
                log.warning("Screen capture failed: %s", e)
                img = None

            text     = ocr_image(img) if img else None
            ocr_item = parse_item_text(text or "", fallback_name=alert.item_name) if text else None

            npc  = getattr(alert, "npc_name", "") or ""
            zone = (app.alert_window.last_zone or "") if app.alert_window else ""

            params = [f"item={_q(alert.item_name)}"]
            if npc:
                params.append(f"mob={_q(npc)}")
            if zone:
                params.append(f"zone={_q(zone)}")
            if ocr_item:
                desc = ocr_item.to_description()
                if desc:
                    params.append(f"notes={_q(desc[:400])}")

            url = "https://gnollguard.com/submit?" + "&".join(params)
            log.info("OCR submit: opening browser for '%s' — %s", alert.item_name, url)
            webbrowser.open(url)

        threading.Thread(target=_do, daemon=True).start()

    alert_win = AlertWindow(
        app.config,
        supabase=app.supabase,
        on_position_save=lambda x, y: (
            app.config.setdefault("window", {}).update({"alert_x": x, "alert_y": y}),
            _save_config(app.config),
        ),
        on_submit_screenshot=_arm_submit_ocr,
        is_admin=lambda: app.auth.is_admin,
    )
    app.alert_window = alert_win

    # Verify callback: marks item correct locally and pushes to community DB.
    def on_verify_item(item_name: str):
        def _do():
            verify_item(app.db_session, item_name)
            item = get_item(app.db_session, item_name)
            if item and app.supabase.is_configured and app.auth.is_admin:
                ok = app.supabase.contribute_item(item)
                if ok:
                    # Add to in-memory cache so future loots skip local storage
                    app._community_cache[item_name.lower()] = {
                        "item_name": item.name,
                        "description": item.description,
                        "source_url": item.source_url,
                        "verified": True,
                    }
                    log.info("Contributed '%s' to community DB", item_name)
            else:
                # Local-only: either Supabase isn't configured, or the user isn't an
                # admin (community item authoring is admin-only).
                log.info("Marked '%s' as correct (local only)", item_name)
        threading.Thread(target=_do, daemon=True).start()

    # Wire alert engine → floating window + history tab
    def on_alert(alert: Alert):
        alert_win.push(alert)
        win.after(0, lambda a=alert: win.add_alert_row(a, on_verify=on_verify_item))

    app.alert_engine.add_listener(on_alert)

    # Wire log watcher status → status bar
    def poll_watcher_status():
        if win.winfo_exists():
            win.after(0, lambda: win.update_watcher_status(app.log_watcher.status))
            win.after(2000, poll_watcher_status)

    win.after(2000, poll_watcher_status)

    # ── Item-ID harvest from /outputfile inventory dumps ──────────────────────
    def _inventory_dir() -> str:
        # EQ writes <Char>-Inventory.txt to the install root (parent of Logs\).
        lp = app.config.get("log_file_path", "")
        return os.path.dirname(os.path.dirname(lp)) if lp else ""

    app._inv_mtimes = {}

    def _submit_inventory_file(path: str):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                items = parse_inventory(f.read())
            if items:
                app.supabase.submit_inventory(items)
        except Exception:
            log.debug("inventory submit error", exc_info=True)

    def poll_inventory():
        try:
            inv_dir = _inventory_dir()
            if inv_dir and app.auth.is_logged_in and os.path.isdir(inv_dir):
                for fn in os.listdir(inv_dir):
                    if not fn.endswith("-Inventory.txt"):
                        continue
                    path = os.path.join(inv_dir, fn)
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    if app._inv_mtimes.get(path) != mtime:
                        app._inv_mtimes[path] = mtime
                        threading.Thread(target=_submit_inventory_file,
                                         args=(path,), daemon=True).start()
        except Exception:
            log.debug("inventory poll error", exc_info=True)
        finally:
            if win.winfo_exists():
                win.after(30000, poll_inventory)

    win.after(15000, poll_inventory)

    # Flush buffered auto-sold observations to the community in batches.
    def poll_auto_sold():
        try:
            buf = app._auto_sold_buffer
            if buf and app.auth.is_logged_in:
                batch, app._auto_sold_buffer = buf[:], []
                threading.Thread(
                    target=lambda b=batch: app.supabase.submit_auto_sold(b),
                    daemon=True,
                ).start()
        except Exception:
            log.debug("auto-sold flush error", exc_info=True)
        finally:
            if win.winfo_exists():
                win.after(45000, poll_auto_sold)

    win.after(20000, poll_auto_sold)

    # The periodic "Help map item IDs" reminder popup was removed (too distracting).
    # The silent harvest above (poll_inventory) still submits IDs whenever an
    # /outputfile inventory dump appears — no nag needed.

    # Start alert window polling loop
    alert_win.start(win)

    # Clipboard OCR — fires when user presses Alt+Print Screen over an EQ item window
    def _on_clipboard_image(img):
        if not app.config.get("clipboard_ocr_enabled", True):
            return

        text = ocr_image(img)
        if not text:
            return
        # Try to match against a recently looted item name as a hint
        recent_name = ""
        try:
            from sqlalchemy import text as sql_text
            row = app.db_session.execute(
                sql_text("SELECT item_name FROM loot_events ORDER BY real_timestamp DESC LIMIT 1")
            ).fetchone()
            if row:
                recent_name = row[0]
        except Exception:
            pass
        ocr_item = parse_item_text(text, fallback_name=recent_name)
        if not ocr_item or not ocr_item.item_name:
            # Only prompt if the player recently looted something — they're
            # probably trying to capture stats but got a bad crop.
            if recent_name:
                win.after(0, lambda n=recent_name: app.alert_engine.ocr_hint(n))
            else:
                log.debug("Clipboard image not recognized as EQ item window")
            return
        desc = ocr_item.to_description()
        log.info("Clipboard OCR captured: %s", ocr_item.item_name)
        def _save_and_alert():
            upsert_item(app.db_session, {
                "name": ocr_item.item_name,
                "item_level": 0,
                "description": desc,
                "slot": ocr_item.slot,
                "verified": True,
            })
            # Community item authoring is admin-only; non-admins still get the
            # local verified alert, just no community push.
            if app.auth.is_admin:
                threading.Thread(
                    target=lambda: app.supabase.contribute_item(
                        app.db_session.query(__import__("app.db.models", fromlist=["Item"]).Item)
                        .filter_by(name=ocr_item.item_name).first()
                    ),
                    daemon=True,
                ).start()
            win.after(0, lambda: app.alert_engine.item_verified(
                ocr_item.item_name, desc or "Stats captured from item inspect window."
            ))
        threading.Thread(target=_save_and_alert, daemon=True).start()

    if app.config.get("clipboard_ocr_enabled", True):
        app.clipboard_watcher = ClipboardWatcher(on_image=_on_clipboard_image)
        app.clipboard_watcher.start()

    # Start log watcher
    def apply_log_path(path: str):
        try:
            app.log_watcher.stop()
        except Exception:
            pass
        if path and os.path.isfile(path):
            app.log_watcher.start(path)
            app.log_rotator.start()
            win.update_watcher_status(f"watching — {os.path.basename(path)}")
            log.info("Log watcher now watching: %s", path)
        elif path:
            win.update_watcher_status(f"file not found: {path}")
            log.warning("Log watcher: file not found at %s", path)
        else:
            win.update_watcher_status("not configured — set log file in Settings")
            log.info("Log watcher idle (no log_file_path in config)")

    app.apply_log_path = apply_log_path

    # Auto-detect EQL/EQ Live log file — check several known Daybreak directories
    _log_path = app.config.get("log_file_path", "")
    if not _log_path or not os.path.isfile(_log_path):
        try:
            import glob as _glob
            _search_dirs = [
                app.config.get("eql_log_dir", ""),
                r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest Legends\Logs",
                r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest\Logs",
                r"C:\Program Files (x86)\EverQuest\Logs",
                r"C:\Program Files\EverQuest\Logs",
            ]
            _candidates: list[str] = []
            for _log_dir in _search_dirs:
                if not _log_dir:
                    continue
                _candidates = _glob.glob(os.path.join(_log_dir, "eqlog_*.txt"))
                if not _candidates:
                    _bare = os.path.join(_log_dir, "eqlog.txt")
                    if os.path.isfile(_bare):
                        _candidates = [_bare]
                if _candidates:
                    break
            if _candidates:
                _log_path = max(_candidates, key=os.path.getmtime)
                app.config["log_file_path"] = _log_path
                _save_config(app.config)
                log.info("Auto-detected EQ log: %s", _log_path)
        except Exception as e:
            log.debug("Log auto-detect failed: %s", e)

    # First-run setup wizard — nothing auto-detected and nothing saved.
    if not _log_path or not os.path.isfile(_log_path):
        _log_path = _run_setup_wizard(app, win) or _log_path

    apply_log_path(_log_path)

    # Direct loot-injection hook for the Settings debug button
    app._fire_loot = lambda evt: _on_loot(app, evt)

    # Refresh Settings tab and sync auth token to supabase when auth state changes
    def _on_auth_change():
        app.supabase.set_auth_token(app.auth.access_token)
        threading.Thread(target=lambda: _build_quest_index(app), daemon=True, name="QuestIndex").start()
        win.after(0, win._refresh_auth_header)
        win.after(0, lambda: win._settings_tab._build())
    app.auth.set_auth_change_callback(_on_auth_change)
    app.auth.restore_session()
    app.supabase.set_auth_token(app.auth.access_token)  # apply restored session token
    # Build the quest-item index so looting ticks off journaled quests even if
    # the Quest Log tab is never opened this session.
    threading.Thread(target=lambda: _build_quest_index(app), daemon=True, name="QuestIndexInit").start()

    # Pull community data on startup — populates cache + cleans local queue
    threading.Thread(
        target=lambda: _sync_community_data(app),
        daemon=True,
        name="SupabaseInit",
    ).start()

    # Prune old loot events — keep only last 24 h for quest-hint matching
    threading.Thread(
        target=lambda: prune_loot_events(app.db_session),
        daemon=True,
    ).start()

    # Auto-update checker — quiet background check, shows banner if newer version found
    def _on_update(version: str, url: str, changelog: str):
        win.after(0, lambda: win.show_update_banner(version, url, changelog))

    app.update_checker = UpdateChecker(_on_update)
    app.update_checker.start()

    # Start tray in background thread
    threading.Thread(target=lambda: _build_tray(app), daemon=True, name="SysTray").start()

    log.info("GnollGuard started")
    win.mainloop()


if __name__ == "__main__":
    main()
