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

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time  # used by _obs_pump; missing until 2026-08-08 — see below

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import customtkinter as ctk

from app.alerts.engine import Alert, AlertEngine
from app import quest_progress
from app import quest_matcher
from app.db.models import create_db_engine, make_session_factory
from app.db.queries import (
    get_item, get_items, delete_item, log_loot_event, prune_loot_events,
    upsert_item, verify_item,
)
from app.log_watcher import LogWatcher
from app.log_rotate import LogRotator
from app.parsers.npc_parser import extract_item_hints
from app.updater import UpdateChecker
from app.sync.auth import AuthManager
from app.sync.supabase import SupabaseSync
from app.parsers.inventory_parser import parse_inventory
from app.ui.main_window import MainWindow

log = logging.getLogger(__name__)


def _bundled_config_path() -> str:
    """Path to the read-only DEFAULT settings that ship with the app (never written
    to). Frozen: inside the PyInstaller bundle. Source: the repo's config/ template."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "config", "settings.json")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "settings.json",
    )


def _resolve_config_path() -> str:
    r"""User settings ALWAYS live in %APPDATA%\GnollGuard\settings.json — per-user,
    outside the repo and the install dir — for BOTH installed builds and dev/source
    runs. This keeps personal values (the log path embeds a character name, plus
    window positions, etc.) out of the shipped build and the git repo entirely.
    The bundled config/settings.json is only a read-only template, copied here on
    first run.
    """
    user_dir = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "GnollGuard"
    )
    os.makedirs(user_dir, exist_ok=True)
    user_path = os.path.join(user_dir, "settings.json")
    if not os.path.exists(user_path):
        bundled = _bundled_config_path()
        if os.path.exists(bundled):
            shutil.copy(bundled, user_path)
    return user_path


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
    """Load %APPDATA%\\GnollGuard\\settings.json.

    utf-8-sig strips a leading BOM if present (Notepad / some editors write one).
    Without that, json.load fails and the whole config is dropped — Settings and
    overlay options appear broken until the file is rewritten cleanly.
    """
    try:
        with open(_CONFIG_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        log.warning("settings.json not found — using defaults")
        return {}
    except json.JSONDecodeError as e:
        log.error("settings.json is malformed: %s", e)
        return {}
    except OSError as e:
        log.error("settings.json unreadable: %s", e)
        return {}


def _save_config(config: dict):
    try:
        # Always write plain UTF-8 (no BOM) so reloads stay clean.
        with open(_CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        log.error("Failed to save config: %s", e)


def _looks_like_live_eq(path: str) -> bool:
    r"""True if a log path points at LIVE EverQuest instead of EverQuest LEGENDS.

    This app is Legends-only, but Legends logs are byte-for-byte the same shape as live EQ's.
    So if a user's saved config points at a live-EQ Logs folder, the quest pipeline happily
    submits live-EQ NPC dialogue into the Legends community database — the exact cross-game
    pollution we spent 2026-07-19 scrubbing out of community_items. We can't demand the exact
    canonical Daybreak path (a player may install Legends elsewhere), so we reject only the
    UNAMBIGUOUS live-EQ signature: a path segment that is exactly "EverQuest" while the path
    mentions "Legends" nowhere.

        C:\...\Installed Games\EverQuest Legends\Logs   -> allowed (segment carries "Legends")
        C:\...\Installed Games\EverQuest\Logs           -> REJECTED (live EQ)
        C:\Program Files\Sony\EverQuest\Logs            -> REJECTED (live EQ)
        D:\Games\EQLegends\Logs                         -> allowed (path mentions "legends")
        D:\MyStuff\Logs                                 -> allowed (can't tell — stay permissive)
    """
    low = (path or "").strip().lower()
    if not low or "legends" in low:
        return False
    parts = low.replace("/", "\\").split("\\")
    return any(seg.strip() == "everquest" for seg in parts)


ARCHIVE_SUBDIR = "old"          # lives INSIDE the EQ Logs folder
_ARCHIVE_README = """This folder was created by Gnoll Guard.

EverQuest appends to its character logs forever — one of them had reached 54 MB.
When EverQuest is CLOSED and a log has grown past the size limit, Gnoll Guard
moves the logs in here and EverQuest starts fresh, empty ones next launch.

  * Nothing is deleted. Files are renamed <original>.<timestamp>.bak
  * Gnoll Guard never touches a log while EverQuest is running
  * Safe to delete these yourself once you no longer want the history
  * Turn it off entirely: Settings, or "log_rotate_enabled": false in config

Before 2026-08-08 these were moved to Documents\\GnollGuard\\logs_archive instead.
If you are missing older logs, look there.
"""


def _write_archive_readme(archive_dir: str) -> None:
    """Explain the folder to whoever finds it. Best-effort, never fatal.

    The old behaviour's real sin was silence — files left the game folder with no
    note anywhere saying what took them or where they went.
    """
    try:
        os.makedirs(archive_dir, exist_ok=True)
        readme = os.path.join(archive_dir, "README.txt")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as fh:
                fh.write(_ARCHIVE_README)
    except OSError:
        pass


def _eq_legends_logs_from_registry() -> list[str]:
    r"""Every EverQuest LEGENDS Logs folder we can find from the Windows registry.

    WHY: auto-detect used to be a single hardcoded path —
        C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest Legends\Logs
    so anyone who installed to D:\Games, another drive, or a non-default Daybreak
    folder fell straight through to the first-run wizard and had to browse for a
    file the machine already knew about. Owner, 2026-08-08: "we should be able to
    find their install directory pretty easy right? so why do we have to ask them
    anything".

    HOW: the installer registers an uninstall entry. On a real machine it looks like
        DisplayName    = "EverQuest Legends"
        InstallLocation= ""                      <- often EMPTY, do not rely on it
        DisplayIcon    = "<install dir>\Everquest.ico"
    so the install directory is the icon's folder. Verified 2026-08-08.

    ⚠ LEGENDS ONLY. The same scan also turns up plain "EverQuest" (live). Their logs
    are format-identical, and attaching to a live-EQ log would feed live-EQ dialogue
    into the Legends database — the exact cross-game hole closed on 2026-07-20. So we
    match the name EXACTLY and let _looks_like_live_eq() backstop us.
    """
    if os.name != "nt":
        return []
    found: list[str] = []
    try:
        import winreg
    except Exception:
        return []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in roots:
        try:
            key = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                except OSError:
                    continue
                try:
                    def _val(name):
                        try:
                            return str(winreg.QueryValueEx(sub, name)[0] or "")
                        except OSError:
                            return ""
                    # Exact match only — "EverQuest" alone is LIVE EQ and must not match.
                    if _val("DisplayName").strip().casefold() != "everquest legends":
                        continue
                    base = _val("InstallLocation").strip().strip('"')
                    if not base:
                        icon = _val("DisplayIcon").strip().strip('"').split(",")[0]
                        if icon:
                            base = os.path.dirname(icon)
                    if base:
                        logs = os.path.join(base, "Logs")
                        if os.path.isdir(logs) and logs not in found:
                            found.append(logs)
                finally:
                    sub.Close()
        finally:
            key.Close()
    return found


def _eq_legends_logs_from_running_game() -> list[str]:
    """Logs folder inferred from a running eqgame.exe, if we can see its path.

    Costs nothing and is the most authoritative source there is: the player is
    literally playing the install we want. Best-effort — psutil may be absent and
    reading another process's path can be denied, so every failure is silent.
    """
    if os.name != "nt":
        return []
    try:
        import psutil  # optional dependency; absent in some builds
    except Exception:
        return []
    out: list[str] = []
    try:
        for p in psutil.process_iter(["name", "exe"]):
            if (p.info.get("name") or "").lower() != "eqgame.exe":
                continue
            exe = p.info.get("exe") or ""
            if not exe:
                continue
            logs = os.path.join(os.path.dirname(exe), "Logs")
            if os.path.isdir(logs) and logs not in out:
                out.append(logs)
    except Exception:
        return out
    return out


def _migrate_config(config: dict) -> dict:
    """
    Ensure user's persisted config has the latest log_patterns.
    The APPDATA copy is only created on first run, so pattern fixes in new
    versions would never reach existing installs without this migration.
    """
    # ── Cross-game guard (runs FIRST, before any early return below) ──────────────────────
    # Discard any saved log path that points at LIVE EverQuest rather than EverQuest Legends.
    # Existing installs (incl. 1.5.6) may have auto-attached to a live-EQ log before the
    # Legends-only lock landed. A saved path that still exists is trusted verbatim at startup,
    # so such an install would keep tailing live EQ — and feeding live-EQ quest dialogue into
    # the Legends community DB — forever. Dropping the keys forces the Legends-locked auto-detect
    # to re-run and re-point at the real Legends Logs folder. This MUST run even when the bundled
    # template can't be read (the log_patterns block below returns early in that case). Owner,
    # 2026-07-21: the journal must read ONLY from the EverQuest Legends Logs folder.
    _scrubbed = False
    for _k in ("log_file_path", "log_dir"):
        if _looks_like_live_eq(config.get(_k, "")):
            log.warning("Cross-game guard: discarding non-Legends %s = %s", _k, config.get(_k))
            config.pop(_k, None)
            _scrubbed = True
    if _scrubbed:
        _save_config(config)

    bundled_path = _bundled_config_path()

    try:
        with open(bundled_path, encoding="utf-8-sig") as f:
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


def _primary_character(config: dict) -> str:
    """Best-guess "the" character for this install, for naming the local quest-step
    progress file. Prefers the configured log_dir's first character (alphabetical,
    stable across launches); falls back to the single configured log_file_path."""
    from app import quest_sightings as _qs
    log_dir = config.get("log_dir") or os.path.dirname(config.get("log_file_path") or "")
    names = _qs.players_from_log_folder(log_dir)
    if names:
        return names[0]
    return _qs.player_from_log_path(config.get("log_file_path") or "") or "unknown"


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
            self.config.get("supabase_key", "") or "sb_publishable_P8BT37b8iYnHHisNegOU6w_dqqP3dGB",
        )
        self.auth = AuthManager(self.supabase._client)
        # Supabase refresh tokens are SINGLE USE — every background refresh
        # rotates them. Without this hook the pair saved in .session.json goes
        # stale within the hour, and the next launch is met with "Invalid
        # Refresh Token: Already Used" and a forced re-login. That was the
        # every-single-time logout bug. Fixed 2026-08-03.
        self.supabase.on_session_refreshed = self.auth.persist_refreshed_session

        # Alert engine
        self.alert_engine = AlertEngine()

        # Log watcher
        self.log_watcher = LogWatcher(self.config)

        # Smart log rotation — archives the main log when EQ is closed + oversized.
        #
        # ARCHIVES LIVE INSIDE THE EQ LOGS FOLDER (changed 2026-08-08).
        # They used to go to ~/Documents/GnollGuard/logs_archive, which meant the app
        # silently moved 12 files out of the user's game folder to a location nothing
        # in the game folder pointed at. Owner asked for them to stay put: "lets be
        # careful about moving them all the way out of the eq log folder we should make
        # another folder in the log folder called old or backup and move them there."
        #
        # SAFE because LogWatcher._matching_files() globs NON-recursively
        # (glob(os.path.join(dir, "eqlog_*.txt"))), so a subfolder is invisible to it —
        # and archives are renamed to ".bak" anyway, which the glob would not match.
        # If that glob is ever made recursive, this subfolder MUST be excluded or the
        # app will re-ingest its own archives and double-count every event.
        self.log_rotator = LogRotator(
            get_log_path=lambda: self.log_watcher.log_path,
            archive_dir=lambda: self._archive_dir_for_logs(),
            rotate_fn=self.log_watcher.rotate_to,
            threshold_mb=int(self.config.get("log_rotate_threshold_mb", 50)),
            check_every_s=int(self.config.get("log_rotate_check_seconds", 300)),
            enabled=bool(self.config.get("log_rotate_enabled", True)),
        )

        # In-memory community cache: lower(name) → community row dict.
        # Populated from Supabase on startup so loot lookups never hit the network.
        self._community_cache: dict = {}

        # Current zone — used to update the overlay's "Quests in Zone".
        self._current_zone = None

        # Quest progress — required-item → quest lookup (rebuilt from the journal),
        # the player's full journaled quests (for completion checks), the set of
        # quest items already looted, and the set of items turned in to an NPC.
        self._quest_item_index: dict = {}
        self._journal_quests: list = []
        self._quest_progress: set = quest_progress.load_progress()
        self._quest_given: set = quest_progress.load_given()
        # How many times each item has been handed in. The set above can only say
        # "ever" — see quest_progress.load_given_counts() for why that broke repeat runs.
        self._quest_given_counts: dict = quest_progress.load_given_counts()

        # Structured quest-step auto-completion (QUEST_STEPS_PLAN.md v1). State is
        # local, keyed to the primary character on this install — same single-file
        # simplicity as quest_progress above, not yet split per character (a real
        # gap on a shared PC with multiple mains; the manual Mark done/undone
        # override in the Journal covers it either way).
        _charname = _primary_character(self.config)
        _step_state = quest_matcher.StepState.load(quest_matcher.state_path_for(_charname))
        self.quest_matcher = quest_matcher.QuestMatcher([], _step_state)

        # UI refs — set after UI is built
        self.main_window: MainWindow = None
        self.overlay_window = None

    # ⚠ KEEP NEW METHODS BELOW __init__, NOT INSIDE IT.
    # On 2026-08-08 this method was inserted into the MIDDLE of __init__, which
    # silently truncated the constructor: everything after it — quest_matcher,
    # quest progress state, the UI refs — became unreachable code inside this
    # method instead. The file still parsed, `import app.main` still worked, and
    # all 44 tests still passed. It only surfaced as
    # "AttributeError: 'AppState' object has no attribute 'quest_matcher'"
    # from a log-watcher zone callback at runtime.
    def _archive_dir_for_logs(self) -> str:
        """Returns "" — archives are RENAMED IN PLACE, in the EQ Logs folder.

        Owner's call, 2026-08-08, after asking the right question: "im worried
        people are gonna look for their logs and not know where they are". Every
        design that MOVES the file has the same flaw — the user has to already know
        where it went. A renamed file in the folder they are already looking at does
        not. So rotate_to() gets an empty archive_dir and renames beside the original:

            eqlog_Morbid_freeport.txt
            eqlog_Morbid_freeport_2026-07-11_to_2026-08-07.bak

        Kept as a method (rather than deleted) because LogRotator resolves the
        archive dir on every rotation, and a future "archive to a chosen folder"
        setting plugs straight back in here.
        """
        return ""

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


# Common false-positive loot names for untracked-quest prompts (always silent).
_NOISY_LOOT_DEFAULTS = frozenset({
    "bone chips", "cloth cap", "rusty dagger", "rusty short sword", "rusty axe",
    "snake scales", "bat wing", "spiderling silk", "rat ear", "fire beetle eye",
    "copper band", "gold", "silver", "platinum", "copper",
})


def _ignored_loot_path() -> str:
    import os
    base = os.path.join(os.path.expanduser("~"), "AppData", "Local", "GnollGuard")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.path.expanduser("~")
    return os.path.join(base, "ignored_loot.json")


def _load_ignored_loot() -> set:
    import json
    try:
        with open(_ignored_loot_path(), encoding="utf-8") as f:
            data = json.load(f)
        return {str(x).lower() for x in (data if isinstance(data, list) else [])}
    except Exception:
        return set()


def _save_ignored_loot(names: set) -> None:
    import json
    try:
        with open(_ignored_loot_path(), "w", encoding="utf-8") as f:
            json.dump(sorted(names), f)
    except Exception:
        log.debug("save ignored loot failed", exc_info=True)


def _build_quest_index(app):
    """Fetch the player's journaled quests and rebuild the required-item lookup
    so loot can tick quests off. Safe to call on a background thread.
    Hidden/completed quests stay in the journal list but do not receive loot ticks."""
    try:
        quests = app.supabase.get_journal()
        app._journal_quests = quests
        active = [
            q for q in (quests or [])
            if (q.get("journal_status") or "active") in ("active", "pinned")
        ]
        app._quest_item_index = quest_progress.build_index(active)
        app.quest_matcher.set_quests(active)
    except Exception:
        log.debug("quest index build failed", exc_info=True)


def _refresh_quest_views(app: AppState):
    """Refresh the Quest Journal in the main window AND any open pop-out bubbles."""
    win = app.main_window
    if not win:
        return
    if hasattr(win, "_journal_scroll"):
        win.safe_after(0, win._refresh_journal)
    ov = getattr(app, "overlay_window", None)
    if ov is not None and hasattr(ov, "refresh_journal"):
        try:
            win.safe_after(0, ov.refresh_journal)
        except Exception:
            pass


def _handle_step_completions(app: AppState, newly_done: list):
    """Common tail for every quest_matcher event hook: log + refresh the
    Journal + a quiet alert per completed step. Never raises — a matcher bug
    must not take down loot/dialogue handling."""
    if not newly_done:
        return
    try:
        for d in newly_done:
            log.info("Quest step complete: %s — step %s (%s)",
                      d.get("quest_name"), d.get("step_order"), d.get("instruction"))
            app.alert_engine.quest_step_complete(
                d.get("instruction"), d.get("quest_name"), d.get("step_order"))
        _refresh_quest_views(app)
    except Exception:
        log.debug("quest step completion handling failed", exc_info=True)


def _on_loot(app: AppState, loot_evt):
    item_name = loot_evt.item_name
    if not item_name:
        return

    npc_name = getattr(loot_evt, "npc_name", "") or ""
    log.info("Loot event: %s (mob: %s)", item_name, npc_name or "unknown")

    # Always record the loot (quest-hint matching + Items tracking); pruned to 24 h.
    # quantity/tier come off the parsed event — item_name is the BASE name now, so
    # "2 Bone Chips" stores as ("Bone Chips", qty 2) and actually matches the item table.
    _qty = int(getattr(loot_evt, "quantity", 1) or 1)
    _tier = int(getattr(loot_evt, "tier", 0) or 0)
    threading.Thread(
        target=lambda: log_loot_event(app.db_session, item_name,
                                      quantity=_qty, tier=_tier),
        daemon=True,
    ).start()

    _handle_step_completions(app, app.quest_matcher.on_loot(item_name))

    # Quest progress: if this drop is a required item in one of the player's
    # journaled quests, tick it off (✓), persist it, and fire a quest alert.
    # ALERTS FIRE ONLY FOR ACTIVE QUEST ITEMS — all other loot is silent.
    quest_name = quest_progress.match(app._quest_item_index, item_name)
    if quest_name and item_name.lower() not in app._quest_progress:
        app._quest_progress.add(item_name.lower())
        quest_progress.save_progress(app._quest_progress)
        app.alert_engine.quest_item_obtained(item_name, quest_name, npc_name=npc_name)
        _refresh_quest_views(app)
    elif not quest_name:
        # T1.5 — untracked loot: suggest adding a matching quest if we can find one
        # Skip noisy commons the player has ignored.
        try:
            ignored = getattr(app, "_ignored_loot_names", None)
            if ignored is None:
                ignored = _load_ignored_loot()
                app._ignored_loot_names = ignored
            if item_name.lower() in ignored or item_name.lower() in _NOISY_LOOT_DEFAULTS:
                pass
            else:
                suggestions = app.supabase.find_quests_for_item(item_name, limit=1) or []
                if suggestions:
                    s0 = suggestions[0]
                    app.alert_engine.quest_item_untracked(
                        item_name,
                        s0.get("quest_name") or "",
                        quest_id=str(s0.get("id") or ""),
                    )  # Add/Ignore wired in MainWindow.add_alert_row
        except Exception:
            log.debug("untracked quest lookup failed", exc_info=True)

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

    # ⚠ COUNT IT, don't just flag it. The old code only recorded the item the FIRST time
    # it was ever handed over ("if not in set"), so a repeatable quest run a second time
    # produced no record at all and the journal reported it permanently complete. Always
    # increment; the set is kept alongside for the ✔ markers and older clients.
    app._quest_given_counts, app._quest_given = quest_progress.record_given(
        item_name, getattr(app, "_quest_given_counts", None) or {}, app._quest_given)
    quest_progress.save_given(app._quest_given)
    quest_progress.save_given_counts(app._quest_given_counts)

    # Did this complete any journaled quest? If so, auto-remove it.
    completed = [q for q in app._journal_quests
                 if quest_progress.is_complete(q, app._quest_given)]

    app.alert_engine.quest_item_turned_in(item_name, npc_name, complete=bool(completed))

    for q in completed:
        qid = q.get("id")
        # MARK completed, do NOT delete (changed 2026-08-03).
        #
        # This used to call remove_quest(), which deleted the user_quests row
        # outright. That threw away the single most valuable signal we collect:
        # proof that a specific player actually finished a specific quest. It is
        # why only 1 of 76 journal rows read 'completed' — the rest were deleted
        # as they completed.
        #
        # That record is the weight-4 evidence tier the community quest
        # verification spec is built on: "this person provably did this quest"
        # is worth 4 votes, versus 1 for someone who merely says they know it.
        # See updates/2026-08-01-SPEC-community-quest-verification.md
        #
        # The quest still disappears from the active journal view because it is
        # removed from app._journal_quests below — the UI behaviour is unchanged,
        # only the durable record survives.
        threading.Thread(
            target=lambda i=qid: app.supabase.set_quest_status(i, "completed"),
            daemon=True,
        ).start()
    if completed:
        done_ids = {q.get("id") for q in completed}
        app._journal_quests = [q for q in app._journal_quests if q.get("id") not in done_ids]
        app._quest_item_index = quest_progress.build_index(app._journal_quests)
        app.quest_matcher.set_quests(app._journal_quests)

    _refresh_quest_views(app)


def _on_zone(app: AppState, zone: str):
    """Player entered a new zone — re-render open pop-out bubbles if needed."""
    app._current_zone = zone
    _handle_step_completions(app, app.quest_matcher.on_zone(zone))
    ov = getattr(app, "overlay_window", None)
    win = app.main_window
    if ov is not None and win is not None and hasattr(ov, "update_zone"):
        try:
            win.safe_after(0, lambda: ov.update_zone(zone))
        except Exception:
            log.debug("overlay zone update failed", exc_info=True)


def _on_kill(app: AppState, mob: str):
    """Player slew a mob ('You have slain <mob>!') — feeds the matcher's `kill`
    trigger type and slayer achievement counters for journaled achievements."""
    _handle_step_completions(app, app.quest_matcher.on_kill(mob))
    # T1.8 — slayer kill targets (log-only local progress)
    try:
        from app import slayer_progress
        achs = getattr(app, "_achievement_journal", None) or []
        if achs:
            prog = getattr(app, "_slayer_progress", None)
            if prog is None:
                prog = slayer_progress.load_progress()
                app._slayer_progress = prog
            advanced = slayer_progress.on_kill(mob, achs, prog)
            if advanced:
                win = app.main_window
                if win is not None:
                    win.safe_after(0, win._refresh_achievements)
    except Exception:
        log.debug("slayer progress on_kill failed", exc_info=True)


def _start_quest_sightings(app: AppState):
    r"""Wire the quest-sighting collector to the log watcher and flush its queue.

    Everything lives in %APPDATA%\GnollGuard\ alongside settings — the queue file embeds
    NPC text from the player's own log, so it stays per-user and out of the install dir.

    Order matters: fetch the manifest FIRST so the collector can drop already-known lines
    before they are ever queued (that is what keeps the database from being hammered), then
    flush anything left over from the previous session.
    """
    from app import quest_sightings as qs
    from app import quest_sighting_sync as qsync

    user_dir = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "GnollGuard")
    os.makedirs(user_dir, exist_ok=True)
    queue_path = os.path.join(user_dir, "quest_sightings.jsonl")
    manifest_cache = os.path.join(user_dir, "sightings_manifest.json")

    # Strip EVERY character's name, not just one — the watcher tails all logs in the folder,
    # so a line heard on a second character would otherwise leak that name into stored text
    # and hash differently from the same line on the first character.
    _log_dir = app.config.get("log_dir") or os.path.dirname(app.log_watcher.log_path or "")
    players = qs.players_from_log_folder(_log_dir) or \
        [qs.player_from_log_path(app.log_watcher.log_path or "")]
    player = players                       # collector accepts a list of names
    known, wanted = qsync.load_manifest(manifest_cache)
    # Share the watcher's roster so the collector inherits everything it has learned
    # about who is a player. The watcher drops PROVEN players; the collector is
    # stricter and requires positive NPC evidence before anything is uploaded.
    _roster = getattr(app.log_watcher, "roster", None)
    if _roster is not None:
        try:
            _roster.add_local_characters(_log_dir)
        except Exception:
            log.debug("could not seed roster from log dir", exc_info=True)
    collector = qs.QuestSightingCollector(queue_path, player=player, known=known,
                                          roster=_roster)
    collector.wanted = wanted
    app.quest_sightings = collector

    app.log_watcher.on_dialogue(
        lambda evt: collector.on_dialogue(evt.npc_name, evt.text))
    app.log_watcher.on_zone(lambda z: collector.set_zone(z))

    def _current_access_token(app_state):
        """The signed-in user's JWT, or None. Tolerates access_token being a property
        or a method, and never raises — a signed-out user just keeps queuing locally."""
        try:
            auth = getattr(app_state, "auth", None)
            tok = getattr(auth, "access_token", None) if auth is not None else None
            return tok() if callable(tok) else tok
        except Exception:
            return None

    # ── log observations: crafts, turn-ins, kills, zones, loot ───────────────
    # Until 2026-07-30 only DIALOGUE was ever uploaded; loot/kills/zones/turn-ins fed the
    # local journal and went nowhere, so 30 installs generated data nobody could see.
    # Queue is durable (JSONL + byte offset) so a crash or a closed laptop loses nothing.
    # GAME NOUNS ONLY — no player or character names ever go into a payload.
    try:
        from app.sync.log_observations import ObservationQueue
        from app.telemetry import get_or_create_install_id

        # Same %APPDATA%\GnollGuard dir the sightings queue already uses (`user_dir`,
        # set just above) — one place for everything per-user.
        obs = ObservationQueue(
            data_dir=user_dir,
            archive_dir=os.path.join(_LOG_DIR, "logs_archive"),
            install_id=get_or_create_install_id(),
            # Resolve the bearer LAZILY at flush time. Referencing _sighting_token here
            # raised UnboundLocalError — it is defined further down this function — and
            # a late resolve is correct anyway: the user may sign in after startup.
            get_token=lambda: _current_access_token(app),
            anon_key=app.config.get("supabase_key", "")
            or "sb_publishable_P8BT37b8iYnHHisNegOU6w_dqqP3dGB",
        )
        app.observations = obs

        _zone_now = {"z": None}
        app.log_watcher.on_zone(lambda z: _zone_now.__setitem__("z", z))
        app.log_watcher.on_craft(lambda c: obs.add(
            "craft", {"item": c.item_name, "success": c.success}, _zone_now["z"]))
        def _turn_in_obs(t):
            """Turn-in observation, with the player's last /loc attached.

            The position at a hand-in is approximately where the NPC stands —
            the player had to be next to them. That is the only source of NPC
            coordinates we have: the `entities` table (loc_x/y/z) is empty
            because nothing ever captured /loc. See log_watcher.last_loc.

            ⚠ APPROXIMATE and explicitly labelled so. `loc_age_s` lets the
            server discard a stale fix, and nothing here may overwrite a
            location a human has confirmed.
            """
            payload = {"item": t.item_name, "npc": t.npc_name,
                       "verdict": getattr(t, "verdict", "unknown")}
            loc = app.log_watcher.last_loc()
            if loc:
                payload["loc"] = {"x": loc["x"], "y": loc["y"], "z": loc["z"],
                                  "zone": loc.get("zone")}
                payload["loc_approx"] = True
                payload["loc_age_s"] = loc.get("age_s")
            obs.add("turn_in", payload, _zone_now["z"])

        app.log_watcher.on_turn_in(_turn_in_obs)
        app.log_watcher.on_kill(lambda mob: obs.add("kill", {"mob": mob}, _zone_now["z"]))
        # Faction. GAME NOUNS ONLY — a faction name, a signed integer, and the mob or NPC
        # that caused it. Nothing here identifies a player.
        #
        # ⚠ `delta` may legitimately be 0-free/None when `capped` is set ("could not
        # possibly get any better/worse"). Do NOT coerce that to 0: capped means MAXED,
        # and a 0 would be averaged in as "this kill gives nothing".
        app.log_watcher.on_faction(lambda e: obs.add(
            "faction",
            {"faction": e.get("faction"), "delta": e.get("delta"), "capped": e.get("capped"),
             "cause_kind": e.get("cause_kind"), "cause_name": e.get("cause_name")},
            _zone_now["z"]))
        app.log_watcher.on_zone(lambda z: obs.add("zone", {"zone": z}, z))
        # ⚠ THE FIELD IS `npc_name`. This read `mob_name` — a name LootEvent has never had —
        # so getattr returned its default None on every single loot event, and
        # ObservationQueue.add() strips None, so the key vanished rather than showing as
        # null. Every loot row we have collected reads {"item": ...} with no mob attached.
        #
        # That one word is why we could not answer "which mob drops this, and how often" —
        # the single most-asked question about any EQ item. The parser had the mob the whole
        # time (loot_triggers captures `npc`), the kill side already worked, and the
        # drop_rate_stats view joins the two. Nothing else was missing. (2026-08-08)
        app.log_watcher.on_loot(lambda ev: obs.add(
            "loot", {"item": getattr(ev, "item_name", None),
                     "mob": getattr(ev, "npc_name", None)}, _zone_now["z"]))

        # Upload on a background timer, then prune OUR archived log copies — never the
        # live EQ log. Pruning only runs after a CONFIRMED upload, so a user who is
        # signed out keeps their archives until the data is safely off the machine.
        # ⚠ THIS THREAD WAS DEAD ON ARRIVAL UNTIL 2026-08-08.
        # `time` was never imported in this module, so the very first statement
        # raised NameError and the thread died on every launch, silently, because
        # it is a daemon thread and nothing joins it. That is the whole reason
        # `log_observations` had ZERO rows and "the learning loop has never run
        # once" — it was never a data problem or an adoption problem. Board item
        # #31 ("watch log_observations for a week before building") would have
        # waited forever.
        # Found by actually launching the app; a clean py_compile had hidden it
        # for weeks. The try/except below deliberately does NOT wrap the sleep,
        # so a repeat of this fails loudly in the log rather than silently.
        def _obs_pump():
            while True:
                time.sleep(120)
                try:
                    if obs.flush() > 0:
                        obs.prune_archives(keep_days=2)
                except Exception:
                    log.debug("observation pump error", exc_info=True)

        threading.Thread(target=_obs_pump, name="obs-pump", daemon=True).start()
    except Exception:
        log.exception("log observation queue not started (app continues without it)")
        app.observations = None

    # The player's own line is the conversation anchor ("You say, 'Hail, Guard Bml'") and
    # marks when a bracket phrase was repeated back — the NPC's next line is then the chain
    # response we are usually missing. It isn't covered by the npc_dialogue pattern, which
    # matches "<NPC> says," not "You say," — so read it off the raw line.
    _you_say = re.compile(r"You say,?\s*'(?P<text>.+?)'\s*$", re.I)

    def _raw(line: str):
        m = _you_say.search(line or "")
        if m:
            collector.on_player_say(m.group("text"))
    app.log_watcher.on_any_line(_raw)

    # Flush last session's leftovers now (app open). Uploading is idempotent and resumable,
    # so a crash mid-send costs nothing.
    def _sighting_token():
        """Bearer for Path Marks on sighting upload (optional; unauthed still banks dialogue)."""
        try:
            auth = getattr(app, "auth", None)
            if auth is not None:
                tok = getattr(auth, "access_token", None)
                if callable(tok):
                    tok = tok()
                if tok:
                    return tok
            sb = getattr(app, "supabase", None)
            if sb is not None:
                # refresh so long sessions still credit Path
                if hasattr(sb, "_refresh_token"):
                    try:
                        sb._refresh_token()
                    except Exception:
                        pass
                return getattr(sb, "_auth_token", None)
        except Exception:
            return None
        return None

    qsync.upload_async(
        queue_path,
        get_token=_sighting_token,
        on_done=lambda n: log.info("uploaded %s quest sighting(s)", n),
    )
    log.info("quest sightings active (player=%s, %s known ids cached)", player or "?", len(known))


def _on_dialogue(app: AppState, evt):
    """NPC said something — scan it for quest-item hints and, if one matches a
    recently looted item, fire a quest hint. Purely in-memory; no NPC data stored."""
    _handle_step_completions(app, app.quest_matcher.on_npc_line(evt.npc_name, evt.text))

    def process():
        hints = extract_item_hints(evt.text)
        if not hints:
            return
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
                    app.main_window.safe_after(
                        0,
                        lambda h=hint, l=looted, v=verified:
                            app.alert_engine.quest_hint(l, evt.npc_name, h, v),
                    )
                    break

    threading.Thread(target=process, daemon=True).start()


# ── System tray: OPT-IN ONLY ────────────────────────────────────────
#
# HISTORY, READ IT BEFORE CHANGING ANY OF THIS.
#
# The tray was REMOVED 2026-08-09 after the owner objected twice: *"i dont want the journal
# minizing to the mini taskbar again its confusing users"* and, on seeing the icon still
# present in 1.5.21, *"can you tell me why its going to my mini task bar again?"*.
#
# It came back 2026-08-12 on a user request, and the owner approved it with one condition:
# *"the tray request is okay as long as its an option and not default."*
#
# 🔴 SO THE RULES ARE NOT NEGOTIABLE:
#   1. `minimize_to_tray` defaults to FALSE. A fresh install behaves exactly as it does
#      today: X quits, and NO icon ever appears in the notification area.
#   2. When the setting is off there is no icon AT ALL. 1.5.21 removed the hide-on-close
#      behaviour but left the icon, and the owner correctly called that out — an icon in the
#      tray still tells the user the app went somewhere.
#   3. The icon is created lazily, only once someone turns the setting on.
#
# WHY IT IS SAFE NOW, WHEN IT WAS NOT BEFORE. The original bug was not the icon; it was that
# X hid the window and the user could not get it back. Two separate defects caused that and
# BOTH are fixed:
#   * `_find_main_window()` matched the title exactly ("Gnoll Guard") while the real title
#     carries a version ("Gnoll Guard v1.5.13"), so relaunching never re-showed the hidden
#     window — it showed a message box. It now enumerates and prefix-matches.
#   * `_force_window_visible()` defeats the CustomTkinter withdraw bug that could leave the
#     app running with no window on a COLD start.
# ⚠ Do not weaken either of those while this feature exists, or hiding becomes a trap again.


class TrayIcon:
    """Notification-area icon, started and stopped on demand.

    pystray's `run()` blocks, so it owns a daemon thread. Stopping is best-effort: the
    icon may already be gone if the user quit from its own menu.
    """

    def __init__(self, app):
        self._app = app
        self._icon = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()

    def stop(self):
        icon, self._icon = self._icon, None
        self._thread = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                log.debug("tray stop failed", exc_info=True)

    def _run(self):
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
                # Drawn fallback so a missing asset degrades to a plain icon rather than
                # to NO icon — which, with the setting on, would strand a hidden window.
                image = Image.new("RGBA", (64, 64), "#0D0A0B")
                draw = ImageDraw.Draw(image)
                draw.ellipse([8, 8, 56, 56], fill="#C8960C")
                draw.text((20, 20), "GG", fill="#0D0A0B")

            def show_window(icon, item):
                restore_main_window(self._app)

            def quit_app(icon, item):
                icon.stop()
                win = getattr(self._app, "main_window", None)
                if win is not None:
                    win.safe_after(0, _shutdown(self._app))

            menu = pystray.Menu(
                pystray.MenuItem("Show Gnoll Guard", show_window, default=True),
                pystray.MenuItem("Quit", quit_app),
            )
            self._icon = pystray.Icon("GnollGuard", image, "Gnoll Guard", menu)
            self._icon.run()
        except Exception as e:
            log.error("System tray failed: %s", e)
            # ⚠ If the icon cannot start, the window must NOT stay hidden — that is the
            # exact "running process the user cannot reach" failure this guards against.
            try:
                restore_main_window(self._app)
            except Exception:
                log.debug("failed to restore window after tray failure", exc_info=True)


def restore_main_window(app):
    """Bring the main window back, from any thread.

    Always goes through `safe_after` so the Tk call happens on the Tk thread; pystray
    menu callbacks run on the tray's own thread and touching Tk from there is undefined.
    """
    win = getattr(app, "main_window", None)
    if win is None:
        return

    def _do():
        try:
            win.deiconify()
            win.lift()
            win.focus_force()
        except Exception:
            log.debug("deiconify failed", exc_info=True)

    try:
        win.safe_after(0, _do)
    except Exception:
        log.debug("safe_after failed during restore", exc_info=True)


def tray_enabled(app) -> bool:
    """Opt-in, and the default is OFF. Anything unparseable reads as OFF on purpose —
    a corrupt config must not silently start hiding the window."""
    try:
        return bool(app.config.get("minimize_to_tray", False))
    except Exception:
        return False


def apply_tray_setting(app):
    """Start or stop the icon to match the setting. Safe to call repeatedly.

    🔴 Turning the setting OFF while the window is hidden MUST un-hide it, or the user
    has just removed the only way back to their own app.
    """
    tray = getattr(app, "tray", None)
    if tray is None:
        tray = app.tray = TrayIcon(app)
    if tray_enabled(app):
        tray.start()
    else:
        tray.stop()
        restore_main_window(app)


def _force_window_visible(win):
    r"""Make absolutely sure the main window is on screen. This is the "it didn't even
    open" bug, root-caused 2026-08-09.

    🔴 CUSTOMTKINTER REFUSES TO SHOW A WINDOW THAT WAS WITHDRAWN BEFORE IT EXISTED.
        We call `win.withdraw()` right after building MainWindow so it does not flash up
        behind the boot splash. CustomTkinter's CTk overrides withdraw (ctk_tk.py:133):

            def withdraw(self):
                self._withdraw_called_before_window_exists = True
                super().withdraw()

        and its mainloop (ctk_tk.py:159) then does:

            if not self._withdraw_called_before_window_exists ...:
                self.deiconify()

        Reading that flag as "the app wants this window hidden", it skips the deiconify.
        A plain `win.deiconify()` beforehand does NOT clear the flag — CTk does not
        override deiconify — so the app boots fully, logs "GnollGuard started", runs the
        log watcher, and presents NO WINDOW. Users reported this as the app not opening.
        They were right, and it had nothing to do with the tray.

        Confirmed on 1.5.20 and 1.5.21 alike: the main TkTopLevel sits at 916x689 with
        showCmd=SW_SHOWNORMAL and not iconic, but WS_VISIBLE unset — the signature of a
        withdraw that was never undone. It is timing-dependent, which is why it looked
        random and only some users hit it.

    Two layers, deliberately:
      1. clear the flag so CTk's own mainloop deiconify is allowed to run
      2. re-assert shortly after the mainloop starts, because CTk ALSO withdraws and
         restores the window when it sets the titlebar colour (ctk_tk.py:278), and that
         runs on a timer after we are gone from this function

    ⚠ Layer 2 is the one that must not be removed. Layer 1 touches a private attribute
    and will silently stop working if CustomTkinter renames it; layer 2 asks the window
    what state it is actually in and cannot go stale."""
    try:
        win._withdraw_called_before_window_exists = False
        win._iconify_called_before_window_exists = False
    except Exception:
        pass

    def _show():
        try:
            # state("normal") covers BOTH failure modes in one call: "withdrawn" (the CTk
            # flag bug) and "iconic" (minimised). ⚠ deiconify() alone is not enough — with
            # the flag cleared, the window came back as iconic at (-32000,-32000), which is
            # still no window as far as the user is concerned.
            if win.state() != "normal":
                win.state("normal")
            win.lift()
        except Exception:
            log.debug("could not force the window visible", exc_info=True)
            # Last resort: ask Windows directly. Tk can be wedged while the HWND is fine.
            try:
                import ctypes
                hwnd = ctypes.windll.user32.FindWindowW(None, win.title())
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            except Exception:
                pass

    _show()
    # CustomTkinter withdraws and restores the window again when it sets the titlebar
    # colour (ctk_tk.py:278), on its own timers — so one call here is not enough. These
    # re-assert over the first few seconds and then stop, so a user who deliberately
    # minimises later is left alone.
    for delay in (300, 1200, 2500, 4000):
        try:
            win.after(delay, _show)
        except Exception:
            pass


def _shutdown(app: AppState):
    def do_shutdown():
        # Flush queued quest sightings on the way out. Best-effort and non-blocking — the
        # queue is already durable on disk, so anything missed here just goes next launch.
        try:
            if getattr(app, "quest_sightings", None):
                from app import quest_sighting_sync as qsync
                def _tok():
                    try:
                        auth = getattr(app, "auth", None)
                        if auth is not None:
                            t = getattr(auth, "access_token", None)
                            if callable(t):
                                t = t()
                            if t:
                                return t
                        sb = getattr(app, "supabase", None)
                        if sb is not None and hasattr(sb, "_refresh_token"):
                            try:
                                sb._refresh_token()
                            except Exception:
                                pass
                        return getattr(sb, "_auth_token", None) if sb else None
                    except Exception:
                        return None
                qsync.upload_async(app.quest_sightings.queue_path, get_token=_tok)
        except Exception:
            log.debug("sighting flush on shutdown failed", exc_info=True)
        app.log_watcher.stop()
        app.log_rotator.stop()
        try:
            app.db_session.close()
        except Exception:
            pass
        app.main_window._shutting_down = True
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
            "Let's find your EverQuest Logs folder so Gnoll Guard can track your loot.\n\n"
            "1. Browse to your EverQuest game folder\n"
            "2. Open the 'Logs' folder\n"
            "3. Pick ANY character's log:  eqlog_<Character>_<Server>.txt\n\n"
            "You only need to pick one. Gnoll Guard watches the whole folder, so\n"
            "every character you play is tracked automatically — you never have to\n"
            "come back and switch this when you swap characters.\n\n"
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
            hwnd = _find_main_window()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)       # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            else:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "Gnoll Guard is already running.\n\n"
                    "Look for its window — it may be behind another window.",
                    "Gnoll Guard",
                    0x40,  # MB_ICONINFORMATION
                )
            return False
    except Exception:
        pass  # Non-Windows or ctypes missing — proceed
    return True


def _find_main_window():
    """HWND of a running Gnoll Guard main window, or 0.

    🔴 THIS IS THE "it didn't even open" BUG. The old code called
    `FindWindowW(None, "Gnoll Guard")`, which matches the window title **exactly** — but
    the title is `Gnoll Guard v{__version__}` (main_window.py sets it), so the lookup has
    returned NULL for every version that has ever shipped with a version in the title.
    Second launch therefore never raised the window; it showed a message box and nothing
    else, which is indistinguishable from "the app didn't open".

    Enumerating and prefix-matching fixes it and cannot break again when the version
    changes. Matching on the prefix is safe because the title is ours and the mutex has
    already proved one of our processes is alive.
    """
    import ctypes
    from ctypes import wintypes

    found = ctypes.c_void_p(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.startswith("Gnoll Guard"):
            # Skip our own message boxes and any tool window with the same prefix.
            if ctypes.windll.user32.GetWindow(hwnd, 4) == 0:   # GW_OWNER: top-level only
                found.value = hwnd
                return False                                   # stop enumerating
        return True

    try:
        ctypes.windll.user32.EnumWindows(_cb, 0)
    except Exception:
        return 0
    return found.value or 0


def _show_boot_splash():
    """A plain (non-CTk) Tk window shown for the couple of seconds it takes a frozen
    .exe to finish importing + build the real window — otherwise the user sees nothing
    but a spinning cursor while PyInstaller's bundle unpacks. Destroyed once MainWindow
    is built and about to be shown; independent of CTk so it can exist before the real
    CTk root is created."""
    import tkinter as tk
    root = tk.Tk()
    root.overrideredirect(True)
    w, h = 360, 150
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    root.configure(bg="#0D0A0B")
    try:
        ico = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "assets", "icon.ico")
        if os.path.isfile(ico):
            root.iconbitmap(ico)
    except Exception:
        pass
    tk.Label(root, text="GNOLL GUARD", fg="#C8960C", bg="#0D0A0B",
             font=("Segoe UI Semibold", 18)).pack(pady=(32, 6))
    tk.Label(root, text="loading…", fg="#8899A8", bg="#0D0A0B",
             font=("Segoe UI", 10)).pack()
    root.update()
    return root


def main():
    if not _ensure_single_instance():
        return

    splash = _show_boot_splash()

    # Multi-monitor / mixed-DPI stability. CustomTkinter normally grabs per-monitor DPI
    # awareness (SetProcessDpiAwareness(2)) and re-scales windows when they cross to a monitor
    # with different scaling — which double-scales and BALLOONS the window (the owner hit this
    # dragging the main window between screens). Turning off CTk's automatic DPI awareness keeps
    # scaling constant across monitors, so windows stay put. Must run before any CTk/CTkToplevel
    # window is created. Trade-off: slightly blurry at >100% display scaling — CustomTkinter has
    # no per-monitor fix (see its Scaling docs), and on a mixed-DPI rig stability wins.
    try:
        ctk.deactivate_automatic_dpi_awareness()
    except Exception:
        log.debug("deactivate_automatic_dpi_awareness unavailable", exc_info=True)

    app = AppState()

    # App-wide UI scale. deactivate_automatic_dpi_awareness() above pins CustomTkinter
    # at 1.0 so windows stay put across mixed-DPI monitors — correct for that bug, but it
    # also means the app does NOT grow on a high-DPI display, and the owner reported it
    # unreadable on 2026-08-06. The journal overlay already had its own font-scale slider,
    # which is exactly why the pop-out was legible while the main window was not. This is
    # the same control for the app itself. Default 1.0 keeps existing installs unchanged.
    try:
        _scale = float((app.config or {}).get("ui_scale", 1.0))
        _scale = max(0.8, min(2.0, _scale))
        if abs(_scale - 1.0) > 0.001:
            ctk.set_widget_scaling(_scale)
            log.info("ui_scale applied: %.2f", _scale)
    except Exception:
        log.debug("ui_scale could not be applied", exc_info=True)

    # Anonymous headcount + "users online" heartbeat — daemon thread, silent, best-effort.
    # No personal data (random install id only); never blocks startup. See telemetry.py.
    try:
        from app.version import __version__
        from app import telemetry
        telemetry.start(__version__)
    except Exception:
        log.debug("telemetry heartbeat skipped", exc_info=True)

    # Wire log watcher callbacks
    app.log_watcher.on_loot(lambda evt: _on_loot(app, evt))
    app.log_watcher.on_dialogue(lambda evt: _on_dialogue(app, evt))
    app.log_watcher.on_turn_in(lambda evt: _on_turn_in(app, evt))
    app.log_watcher.on_zone(lambda z: _on_zone(app, z))
    app.log_watcher.on_kill(lambda mob: _on_kill(app, mob))

    # The player's own "You say, '...'" line isn't covered by any structured
    # parser/callback (same reason quest_sightings reads it off the raw line
    # below) — classify it as a hail or a plain player_line for the matcher.
    _you_say_for_matcher = re.compile(r"You say,?\s*'(?P<text>.+?)'\s*$", re.I)

    def _matcher_raw_line(line: str):
        m = _you_say_for_matcher.search(line or "")
        if not m:
            return
        kind, payload = quest_matcher.classify_player_say(m.group("text"))
        if kind == "hail":
            _handle_step_completions(app, app.quest_matcher.on_hail(payload))
        else:
            _handle_step_completions(app, app.quest_matcher.on_player_line(payload))
    app.log_watcher.on_any_line(_matcher_raw_line)

    # ── Quest sightings: grow the community quest DB from real play ──────────────
    # Log-based only. Groups NPC speech into conversations, drops combat barks and bare
    # greetings, dedupes against the server's manifest, queues to disk, and uploads in
    # batches on open/close. Wired through the EXISTING callbacks so the hot log path is
    # untouched. Fail-safe: any error here must never affect loot/journal handling.
    try:
        _start_quest_sightings(app)
    except Exception:
        log.debug("quest sightings unavailable", exc_info=True)

    # ONE combat parser for the whole app, built BEFORE the window so the Combat tab and
    # the Tools tabs (Healing / Loot) both receive the same instance. Two parsers reading
    # the same log would drift apart and show different numbers for the same fight.
    try:
        from app.ui.combat_feed import CombatFeed
        app.combat_feed = CombatFeed()
        # 🔴 DO NOT rely on log_watcher here: it is not started until much later in this
        # function (see log_watcher.start below), so log_path() on an unstarted watcher
        # returns nothing or raises. That silently left _lp empty, which skipped BOTH the
        # damage priming and the charm seed on EVERY launch. It looked fine only because
        # live lines arrive seconds later and refill the parser.
        _lp = ""
        try:
            _lp = app.log_watcher.log_path() or ""
        except Exception:
            _lp = ""
        if not _lp or not os.path.exists(_lp):
            # Resolve it ourselves: newest eqlog_*.txt in the configured directory, or the
            # configured file. Deliberately ignores .bak archives -- priming wants the
            # CURRENT session, not a rotated one.
            try:
                import glob as _glob
                cfg = getattr(app, "config", None) or {}
                cands = []
                for _k in ("log_file_path", "log_dir", "eq_log_dir"):
                    _v = cfg.get(_k) if hasattr(cfg, "get") else None
                    if not _v:
                        continue
                    if os.path.isdir(_v):
                        cands += _glob.glob(os.path.join(_v, "eqlog_*.txt"))
                    elif os.path.exists(_v):
                        cands.append(_v)
                if not cands:
                    _default = ("C:/Users/Public/Daybreak Game Company/Installed Games/"
                                "EverQuest Legends/Logs")
                    cands = _glob.glob(os.path.join(_default, "eqlog_*.txt"))
                if cands:
                    _lp = max(cands, key=os.path.getmtime)
            except Exception:
                log.debug("could not resolve a log path for priming", exc_info=True)
        log.info("combat priming source: %s", _lp or "(none found)")
        app.combat_feed.start(_lp)
        # Establish WHO IS OUR PET from a much larger window than damage priming reads.
        # A pet charmed an hour ago renders as an independent combatant otherwise.
        try:
            app.combat_feed.seed_charm_state(_lp)
        except Exception:
            log.debug("charm seed skipped", exc_info=True)
        # Prime from recent history so the first view has context. These lines are fed with
        # live=False, which leaves `live_seen` False — otherwise a fight that ended hours ago
        # renders as "IN COMBAT" the instant the app opens.
        if _lp and os.path.exists(_lp):
            try:
                _sz = os.path.getsize(_lp)
                with open(_lp, "rb") as _fh:
                    _fh.seek(max(0, _sz - 4 * 1024 * 1024))
                    _fh.readline()
                    for _raw in _fh:
                        app.combat_feed.feed_line(
                            _raw.decode("utf-8", "replace").rstrip(), live=False)
            except Exception:
                log.debug("combat priming skipped", exc_info=True)
    except Exception:
        app.combat_feed = None
        log.debug("combat feed unavailable", exc_info=True)

    # Build UI — withdraw immediately so it doesn't flash up behind/beside the splash;
    # shown for real once everything below is wired.
    win = MainWindow(app)
    win.withdraw()
    app.main_window = win

    # Feed the Combat tab from the ONE log reader the app already runs. LogWatcher
    # dispatches every raw line to on_any_line callbacks, so this adds a consumer, not
    # a second tail. Guarded: the combat view is optional and must never break startup.
    def _combat_raw(line: str):
        feed = getattr(app, "combat_feed", None)
        if feed is not None:
            feed.feed_line(line, live=True)
        # 🔴 The Combat section is now `CombatSection`, which reads the SHARED feed above and
        # has no feed_line of its own. The legacy `CombatView` did. Calling it unconditionally
        # raised AttributeError on EVERY log line -- caught only by launching the app, since
        # both classes import and construct perfectly well.
        # Kept as a guarded call rather than deleted: CombatView is still the fallback if
        # CombatSection fails to build, and it DOES need feeding.
        cv = getattr(win, "_combat_view", None)
        fn = getattr(cv, "feed_line", None)
        if callable(fn):
            fn(line)
    try:
        app.log_watcher.on_any_line(_combat_raw)
    except Exception:
        log.debug("combat feed not wired", exc_info=True)
    # Boot splash is a plain tk.Tk() created FIRST, so it became tkinter's default root.
    # CTkFont / CTkScrollableFrame need a default root; if we leave splash as default and
    # then destroy it, _default_root becomes None and Settings (built lazily after splash
    # is gone) crashes with: RuntimeError: Too early to use font: no default root window.
    # Point the default root at the real app window immediately.
    try:
        import tkinter as _tk
        _tk._default_root = win
    except Exception:
        pass

    # Verify callback: marks item correct locally and pushes to community DB.
    def on_verify_item(item_name: str):
        def _do():
            verify_item(app.db_session, item_name)
            # Marks the looted item confirmed in the local DB. Community item data
            # now comes from the harvest pipeline, so there's no in-app authoring.
            log.info("Marked '%s' as correct (local only)", item_name)
        threading.Thread(target=_do, daemon=True).start()

    # Wire alert engine → in-window activity feed (Recent Alerts tab). No popups.
    def on_alert(alert: Alert):
        win.safe_after(0, lambda a=alert: win.add_alert_row(a, on_verify=on_verify_item))

    app.alert_engine.add_listener(on_alert)

    # Wire log watcher status → status bar
    def poll_watcher_status():
        win.safe_after(0, lambda: win.update_watcher_status(app.log_watcher.status))
        win.safe_after(2000, poll_watcher_status)

    win.after(2000, poll_watcher_status)

    # ── Item-ID harvest from /outputfile inventory dumps ──────────────────────
    def _inventory_dir() -> str:
        # EQ writes <Char>-Inventory.txt to the install root (parent of Logs\).
        lp = app.config.get("log_file_path", "")
        return os.path.dirname(os.path.dirname(lp)) if lp else ""

    app._inv_mtimes = {}

    # 🔴 Owner, 2026-08-25: *"that should have a checksum has it changed since last recorded
    # if yes then upload differance."* Right on both counts, and it was worse than it looked:
    #   * `_inv_mtimes` lives only in memory, so it reset on EVERY launch and the whole dump
    #     was re-POSTed each start -- the repeated "Submitted 245 / 127" pairs in his log.
    #   * even when it did fire it sent the entire file, never the delta.
    # Now: content hash decides IF anything is sent (mtime moves when a dump is rewritten
    # identically, a hash does not), and a persisted ledger of already-sent name|id pairs
    # decides WHAT is sent. Nothing is marked sent unless the POST actually succeeded, so a
    # failure retries rather than silently dropping the data.
    _INV_STATE = "inventory_sync.json"
    # 🔴 One thread per inventory file, all writing ONE state file. Without this lock the
    # read-modify-write races and the loser's entry is silently dropped -- observed live:
    # freeport and rivervale both submitted on one launch, only freeport's ledger survived,
    # so rivervale re-uploaded its entire 127 items on the next start. The lock has to cover
    # the whole read-modify-write, not just the write.
    _inv_lock = threading.Lock()

    def _inv_state() -> dict:
        try:
            from app.ui import datapaths
            with open(datapaths.path(_INV_STATE), "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_inv_state(state: dict) -> None:
        try:
            from app.ui import datapaths
            os.makedirs(os.path.dirname(datapaths.path(_INV_STATE)), exist_ok=True)
            with open(datapaths.path(_INV_STATE), "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=1)
        except Exception:
            log.debug("could not persist inventory sync state", exc_info=True)

    def _submit_inventory_file(path: str):
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            digest = hashlib.sha256(raw).hexdigest()

            with _inv_lock:
                state = _inv_state()
            entry = state.get(path) or {}
            if entry.get("hash") == digest:
                log.info("inventory unchanged (%s) - nothing to send", os.path.basename(path))
                return

            items = parse_inventory(raw.decode("utf-8", "replace"))
            if not items:
                return
            # The ledger keys on BOTH name and id: the same name against a different id is
            # genuinely new identity data, which is the whole point of this feed.
            sent = set(entry.get("sent") or [])
            delta = [it for it in items if "%s|%s" % (it["name"], it["id"]) not in sent]

            if not delta:
                # Content changed (moved bags, quantities) but no new identities. Record the
                # hash so the next poll short-circuits, and send nothing.
                with _inv_lock:
                    state = _inv_state()          # re-read: another file may have saved
                    entry["hash"] = digest
                    state[path] = entry
                    _save_inv_state(state)
                log.info("inventory changed but no new item identities (%s)",
                         os.path.basename(path))
                return

            if app.supabase.submit_inventory(delta):
                with _inv_lock:
                    state = _inv_state()          # re-read: another file may have saved
                    sent.update("%s|%s" % (it["name"], it["id"]) for it in delta)
                    entry["hash"] = digest
                    entry["sent"] = sorted(sent)
                    state[path] = entry
                    _save_inv_state(state)
                log.info("inventory delta sent: %d new of %d total (%s)",
                         len(delta), len(items), os.path.basename(path))
            else:
                # Leave hash and ledger untouched so the next poll retries this delta.
                log.info("inventory delta of %d NOT sent (submit failed) - will retry",
                         len(delta))
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

    # The periodic "Help map item IDs" reminder popup was removed (too distracting).
    # The silent harvest above (poll_inventory) still submits IDs whenever an
    # /outputfile inventory dump appears — no nag needed.

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
            return
        # No FILE, but we may still know the FOLDER — the normal state right after
        # log rotation, and on a fresh install before EQ has ever been launched.
        # Watch it anyway; LogWatcher opens new logs as they appear, so the app is
        # ready the moment EQ writes its first line. Before 2026-08-08 this fell
        # through to "file not found" and the app sat there doing nothing.
        _dir = app.config.get("log_dir") or getattr(app.log_watcher, "_dir", "")
        if _dir and os.path.isdir(_dir) and not _looks_like_live_eq(_dir):
            app.log_watcher._dir = _dir
            app.log_watcher.start()
            app.log_rotator.start()
            n = len(glob.glob(os.path.join(_dir, "eqlog_*.txt")))
            win.update_watcher_status(
                f"watching {os.path.basename(_dir.rstrip(os.sep))} — "
                + (f"{n} logs" if n else "waiting for EverQuest to start"))
            log.info("Log watcher watching folder: %s (%d logs)", _dir, n)
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
            # EverQuest LEGENDS only. Do NOT add live-EverQuest fallback paths here: this app
            # is for Legends, its logs are format-identical to live EQ, and the 1.5.6 quest
            # pipeline would happily submit live-EQ quest dialogue into the EQL database if we
            # auto-attached to a live-EQ log. (Removed 2026-07-20 — those fallbacks were the
            # real cross-game hole.)
            # Widest net first, cheapest-and-most-authoritative order. The old list
            # was just the config value plus ONE hardcoded C: path, so any non-default
            # install went to the wizard for a folder the machine already knew.
            _search_dirs = [app.config.get("eql_log_dir", "")]
            _search_dirs += _eq_legends_logs_from_running_game()   # they're playing it
            _search_dirs += _eq_legends_logs_from_registry()       # installer told us
            _search_dirs += [
                r"C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest Legends\Logs",
                r"C:\Users\Public\Sony Online Entertainment\Installed Games\EverQuest Legends\Logs",
            ]
            # Same well-known layouts on any other fixed drive — a second install disk
            # is the common case the single C: path missed.
            try:
                import string as _string
                for _d in _string.ascii_uppercase[3:]:  # D: onward
                    _root = f"{_d}:\\"
                    if not os.path.isdir(_root):
                        continue
                    for _mid in ("Daybreak Game Company", "Sony Online Entertainment"):
                        _search_dirs.append(
                            os.path.join(_root, "Users", "Public", _mid,
                                         "Installed Games", "EverQuest Legends", "Logs"))
                        _search_dirs.append(
                            os.path.join(_root, _mid, "Installed Games",
                                         "EverQuest Legends", "Logs"))
            except Exception:
                pass
            # ⚠ WE ARE LOOKING FOR A FOLDER, NOT A FILE (fixed 2026-08-08).
            # This used to require a live eqlog_*.txt and gave up if it found none —
            # which is exactly the state our OWN log rotation leaves behind. Rotation
            # moves every eqlog_*.txt into the archive when EQ closes, so the next
            # launch saw an empty folder, "detected" nothing, and threw the first-run
            # wizard, asking the user to browse for a file the app itself had moved.
            # Owner, 2026-08-08: "this popup needs to stop it should already know and
            # it keeps asking me where."
            # An empty Logs folder is NORMAL and LogWatcher.start() already handles it
            # ("0 logs: none yet") — EQ recreates them on next launch and the watcher
            # picks them up automatically. So a readable folder is all we need.
            _found_dir = ""
            for _cand_dir in _search_dirs:
                if _cand_dir and os.path.isdir(_cand_dir) and not _looks_like_live_eq(_cand_dir):
                    _found_dir = _cand_dir
                    break
            if _found_dir:
                app.config["log_dir"] = _found_dir
                app.log_watcher._dir = _found_dir
                # Newest existing log, purely so the status line can name something.
                # None is fine and must NOT trigger the wizard.
                _existing = _glob.glob(os.path.join(_found_dir, "eqlog_*.txt"))
                if _existing:
                    _log_path = max(_existing, key=os.path.getmtime)
                    app.config["log_file_path"] = _log_path
                _save_config(app.config)
                log.info("Auto-detected EQ Logs folder: %s (%d logs present)",
                         _found_dir, len(_existing))
        except Exception as e:
            log.debug("Log auto-detect failed: %s", e)

    # First-run setup wizard — ONLY when we could not resolve a FOLDER. Having no
    # log file is not a reason to ask: rotation and a fresh install both look like
    # that, and in both cases the folder is the answer.
    if not (app.config.get("log_dir") and os.path.isdir(app.config["log_dir"])):
        _log_path = _run_setup_wizard(app, win) or _log_path
        if _log_path and os.path.isfile(_log_path):
            app.config["log_dir"] = os.path.dirname(_log_path)
            app.log_watcher._dir = app.config["log_dir"]
            _save_config(app.config)

    # Final cross-game guard — never hand a LIVE-EverQuest log to the watcher, even if one
    # slipped in via a manual wizard pick. The app reads ONLY EverQuest Legends logs.
    if _looks_like_live_eq(_log_path):
        log.warning("Cross-game guard: refusing to watch non-Legends log: %s", _log_path)
        _log_path = ""
        app.config.pop("log_file_path", None)
        app.config.pop("log_dir", None)
        _save_config(app.config)

    apply_log_path(_log_path)

    # Direct loot-injection hook for the Settings debug button
    app._fire_loot = lambda evt: _on_loot(app, evt)

    # Refresh Settings tab and sync auth token to supabase when auth state changes
    def _on_auth_change():
        app.supabase.set_auth_token(app.auth.access_token)
        threading.Thread(target=lambda: _build_quest_index(app), daemon=True, name="QuestIndex").start()
        win.safe_after(0, win._refresh_auth_header)
        # Force a full rebuild of Settings (login/logout changes Account section).
        # ensure_visible() only rebuilds when mapped; mark dirty so next open rebuilds too.
        def _refresh_settings():
            try:
                win._settings_tab._built_while_mapped = False
                if getattr(win, "_active_section", None) == "Settings":
                    win._settings_tab.ensure_visible()
            except Exception:
                log.exception("settings refresh after auth change failed")
        win.safe_after(0, _refresh_settings)
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
        win.safe_after(0, lambda: win.show_update_banner(version, url, changelog))

    app.update_checker = UpdateChecker(_on_update)
    app.update_checker.start()

    # The window's X button needs a real quit, and the shutdown routine lives here rather
    # than in the UI layer (it flushes sightings and stops the watcher/rotator). Hand it
    # to the window instead of having main_window import main — that would be a cycle.
    app.shutdown = lambda: win.safe_after(0, _shutdown(app))

    # ── Optional system tray ───────────────────────────────────────────
    #
    # OFF unless the user asked for it — owner, 2026-08-12: "the tray request is okay as
    # long as its an option and not default". apply_tray_setting() is a no-op when the
    # setting is false, so a default install never creates an icon. See TrayIcon above for
    # the full history and why hiding is safe now when it was not in 1.5.21.
    try:
        apply_tray_setting(app)
    except Exception:
        log.exception("tray setup failed; continuing without it")

    # Everything is wired — swap the boot splash for the real window.
    try:
        splash.destroy()
    except Exception:
        pass
    # Re-assert default root after splash teardown (destroy clears it if splash was root).
    try:
        import tkinter as _tk
        _tk._default_root = win
    except Exception:
        pass
    _force_window_visible(win)

    # Pooled farming rates. Opt-in and OFF by default; when on it sends only the increment
    # since the last successful send, every 6 hours -- never per drop. Arming the timer is
    # harmless when sharing is off: the tick reads a flag and returns.
    try:
        from app.ui import farm_share
        farm_share.start_timer(win)
    except Exception:
        log.debug("farm share timer not armed", exc_info=True)

    log.info("GnollGuard started")
    win.mainloop()


if __name__ == "__main__":
    main()
