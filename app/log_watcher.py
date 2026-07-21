"""
Real-time log file tail — watches EVERY character log in the EQ Logs folder.

EQL writes a separate log per character (eqlog_<Char>_<server>.txt), and a player
switches characters without telling us. So instead of tailing one configured file,
we watch the whole folder and tail every file matching the log glob (default
eqlog_*.txt) — whichever character you play, its lines flow in automatically, and a
brand-new character's log is picked up the moment it appears.

Uses watchdog for file-change notifications, then manually reads new bytes to handle
partial lines safely (watchdog events fire mid-write). Dispatches parsed events to
registered callbacks. Runs on its own thread and is always live — never paused.

Note: zone tracking is shared across files. Normal single-character play is fine;
if two characters were logged at once (multiboxing) their zones could interleave —
an acceptable trade for zero-config multi-character support.
"""

import glob as _glob
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.parsers.loot_parser import LootParser, LootEvent as LootEvt
from app.parsers.npc_parser import NPCParser, DialogueEvent
from app.parsers.game_events import GameEventParser, TurnInEvent

log = logging.getLogger(__name__)


class LogWatcher:
    def __init__(self, config: dict):
        self._config = config
        # The folder to watch + the filename glob. Derived from log_file_path (existing
        # config) or an explicit log_dir. glob defaults to every character log.
        self._path: Optional[str] = config.get("log_file_path") or None
        self._dir: Optional[str] = config.get("log_dir") or (
            os.path.dirname(self._path) if self._path else None)
        self._glob: str = config.get("log_file_glob", "eqlog_*.txt")
        self._observer: Optional[Observer] = None
        # Per-file tail state — we tail EVERY matching log at once.
        self._files: dict[str, object] = {}      # path -> open handle
        self._pos: dict[str, int] = {}           # path -> byte position
        self._partial: dict[str, str] = {}       # path -> incomplete-line fragment
        self._lock = threading.Lock()
        self._running = False

        patterns = config.get("log_patterns", {})
        self._ts_pattern = re.compile(
            patterns.get("timestamp", r"\[\w+ \w+ +\d+ \d+:\d+:\d+ \d+\]")
        )
        self._loot_parser = LootParser(patterns.get("loot_triggers", []))
        self._npc_parser = NPCParser(patterns)
        self._event_parser = GameEventParser(patterns)
        # EQL appends a difficulty suffix to zone names: "<Zone> <N> (<Label>)".
        # Capture the clean zone + the difficulty (0 Normal/2 Adaptive/3 Fused/4 Refined).
        self._zone_pattern = re.compile(
            patterns.get("zone_line",
                         r"You have entered (?P<zone>.+?)(?: (?P<diff>\d+) \((?P<difflabel>[^)]+)\))?\."),
            re.IGNORECASE,
        )
        self._zone_status_pattern = re.compile(
            patterns.get("zone_status",
                         r"You are currently in: (?P<zone>.+?)(?: (?P<diff>\d+) \((?P<difflabel>[^)]+)\))?$"),
            re.IGNORECASE,
        )
        # "You have slain <mob>!" — confirmed real EQL format (see
        # reference_eql_log_formats). Feeds quest_matcher's `kill` trigger type.
        self._kill_pattern = re.compile(
            patterns.get("kill_line", r"You have slain (?P<mob>.+?)!"), re.IGNORECASE)
        self._current_zone = None
        self._current_difficulty = None

        # Callbacks — registered by other modules
        self._on_loot: list[Callable[[LootEvt], None]] = []
        self._on_dialogue: list[Callable[[DialogueEvent], None]] = []
        self._on_turn_in: list[Callable[[TurnInEvent], None]] = []
        self._on_zone: list[Callable[[str], None]] = []
        self._on_kill: list[Callable[[str], None]] = []
        self._on_any_line: list[Callable[[], None]] = []  # for the silence timer

        self.status = "stopped"  # 'watching' | 'paused' | 'error' | 'stopped'
        self._partial_line = ""  # buffer for incomplete lines between watchdog reads

    # ── Registration ─────────────────────────────────────────────────────────

    def on_loot(self, fn): self._on_loot.append(fn)
    def on_dialogue(self, fn): self._on_dialogue.append(fn)
    def on_turn_in(self, fn): self._on_turn_in.append(fn)
    def on_zone(self, fn): self._on_zone.append(fn)
    def on_kill(self, fn): self._on_kill.append(fn)
    def on_any_line(self, fn): self._on_any_line.append(fn)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, path: Optional[str] = None):
        # `path` (a single file) still works — we just watch its whole folder now.
        if path:
            self._path = path
            self._dir = os.path.dirname(path)
        if not self._dir or not os.path.isdir(self._dir):
            self.status = "error"
            log.error("Log folder not found: %s", self._dir)
            return

        # Open every existing character log, seeking to the end (only new lines).
        matches = self._matching_files()
        for p in matches:
            self._open_file(p, seek_end=True)
        self._observer = Observer()
        self._observer.schedule(_FileHandler(self), self._dir, recursive=False)
        self._observer.start()
        self._running = True
        self.status = "watching"
        log.info("Log watcher started on %s (%d logs: %s)", self._dir, len(matches),
                 ", ".join(os.path.basename(m) for m in matches) or "none yet")

    def stop(self):
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join()
        with self._lock:
            for f in self._files.values():
                try:
                    f.close()
                except Exception:
                    pass
            self._files.clear()
        self.status = "stopped"

    def _matching_files(self) -> list[str]:
        if not self._dir:
            return []
        return sorted(_glob.glob(os.path.join(self._dir, self._glob)))

    def _is_match(self, path: str) -> bool:
        return (self._dir is not None
                and os.path.dirname(os.path.abspath(path)) == os.path.abspath(self._dir)
                and _glob.fnmatch.fnmatch(os.path.basename(path), self._glob))

    @property
    def log_path(self) -> Optional[str]:
        """The most-recently-written matching log (for display + rotation targeting)."""
        matches = self._matching_files()
        if not matches:
            return self._path
        try:
            return max(matches, key=os.path.getmtime)
        except Exception:
            return matches[0]

    def rotate_to(self, archive_dir: str) -> Optional[str]:
        """Move EVERY matching character log into archive_dir and drop our handles so EQ
        creates fresh logs next launch. ONLY call when EQ is closed (no active writer) —
        moving a file EQ holds open would fail or corrupt it. Returns the archive path of
        the last file moved, or None if nothing was rotated. Fresh logs are reopened
        automatically as they appear."""
        with self._lock:
            matches = self._matching_files()
            if not matches:
                return None
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs(archive_dir, exist_ok=True)
            last_dest = None
            for path in matches:
                f = self._files.pop(path, None)
                if f:
                    try:
                        f.close()
                    except Exception:
                        pass
                self._pos.pop(path, None)
                self._partial.pop(path, None)
                try:
                    dest = os.path.join(archive_dir, f"{os.path.basename(path)}.{stamp}.bak")
                    shutil.move(path, dest)
                    last_dest = dest
                    log.info("Rotated log -> %s", dest)
                except Exception:
                    log.exception("log rotation move failed for %s", path)
                    if os.path.exists(path):
                        self._open_file(path)
            return last_dest

    def reload_patterns(self, config: dict):
        """Hot-reload all patterns from updated config."""
        patterns = config.get("log_patterns", {})
        self._loot_parser.reload(patterns.get("loot_triggers", []))
        self._npc_parser.reload(patterns)
        self._event_parser.reload(patterns)
        self._ts_pattern = re.compile(
            patterns.get("timestamp", r"\[\w+ \w+ +\d+ \d+:\d+:\d+ \d+\]")
        )
        self._zone_pattern = re.compile(
            patterns.get("zone_line",
                         r"You have entered (?P<zone>.+?)(?: (?P<diff>\d+) \((?P<difflabel>[^)]+)\))?\."),
            re.IGNORECASE,
        )
        self._zone_status_pattern = re.compile(
            patterns.get("zone_status",
                         r"You are currently in: (?P<zone>.+?)(?: (?P<diff>\d+) \((?P<difflabel>[^)]+)\))?$"),
            re.IGNORECASE,
        )
        self._kill_pattern = re.compile(
            patterns.get("kill_line", r"You have slain (?P<mob>.+?)!"), re.IGNORECASE)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _open_file(self, path: str, seek_end: bool = True):
        """Open one log file for tailing and record its start position."""
        old = self._files.get(path)
        if old:
            try:
                old.close()
            except Exception:
                pass
        f = open(path, "r", encoding="utf-8", errors="replace")
        if seek_end:
            f.seek(0, 2)   # only new lines, not the whole history, on first open
        self._files[path] = f
        self._pos[path] = f.tell()
        self._partial.setdefault(path, "")

    def _read_new_lines(self, path: str):
        """Read and dispatch any new lines appended to one specific log file."""
        if not self._is_match(path):
            return
        f = self._files.get(path)
        if not f:
            # A newly-created log (new character, or fresh log after rotation) — open
            # from the START so we don't miss its first lines.
            if os.path.exists(path):
                self._open_file(path, seek_end=False)
                f = self._files.get(path)
            if not f:
                return
        with self._lock:
            try:
                f.seek(self._pos[path])
                new_data = f.read()
                self._pos[path] = f.tell()
            except Exception:
                return

        if not new_data:
            return

        new_data = self._partial.get(path, "") + new_data
        lines = new_data.split("\n")
        self._partial[path] = lines[-1]   # "" if ended on \n, else an incomplete fragment
        for line in lines[:-1]:
            line = line.rstrip("\r")
            if not line:
                continue
            self._dispatch(line)

    def _dispatch(self, line: str):
        # Notify the silence timer first (any log activity resets it)
        for fn in self._on_any_line:
            try:
                fn()
            except Exception:
                log.exception("on_any_line callback error")

        # Only parse lines that start with the expected timestamp
        if not self._ts_pattern.match(line):
            return

        # Zone change — "You have entered <Zone> <N> (<Label>)." or the status echo
        # "You are currently in: <Zone> <N> (<Label>)". EQL appends a difficulty
        # suffix; strip it (captured separately) and fire only when the clean zone
        # changes. Skip the non-zone "You have entered an area where …" messages.
        zm = self._zone_pattern.search(line) or self._zone_status_pattern.search(line)
        if zm:
            zone = zm.group("zone").strip()
            self._current_difficulty = zm.groupdict().get("diff")
            if (zone and zone != self._current_zone
                    and not zone.lower().startswith(("an area", "the area"))):
                self._current_zone = zone
                for fn in self._on_zone:
                    try: fn(zone)
                    except Exception: log.exception("on_zone callback error")
                return

        # Try each parser in priority order
        loot = self._loot_parser.parse(line)
        if loot:
            for fn in self._on_loot:
                try: fn(loot)
                except Exception: log.exception("on_loot callback error")
            return

        km = self._kill_pattern.search(line)
        if km:
            mob = km.group("mob").strip()
            for fn in self._on_kill:
                try: fn(mob)
                except Exception: log.exception("on_kill callback error")
            return

        dialogue = self._npc_parser.parse_dialogue(line)
        if dialogue:
            for fn in self._on_dialogue:
                try: fn(dialogue)
                except Exception: log.exception("on_dialogue callback error")
            return

        # Quest turn-in — silent, feeds the journal
        turn_in = self._event_parser.parse_turn_in(line)
        if turn_in:
            for fn in self._on_turn_in:
                try: fn(turn_in)
                except Exception: log.exception("on_turn_in callback error")
            return


class _FileHandler(FileSystemEventHandler):
    def __init__(self, watcher: LogWatcher):
        self._watcher = watcher

    def on_modified(self, event: FileModifiedEvent):
        if isinstance(event, FileModifiedEvent) and not event.is_directory:
            if self._watcher._is_match(event.src_path):
                self._watcher._read_new_lines(event.src_path)

    def on_created(self, event: FileCreatedEvent):
        # A new character's log (or a fresh log after rotation) just appeared — start
        # tailing it from the beginning.
        if isinstance(event, FileCreatedEvent) and not event.is_directory:
            if self._watcher._is_match(event.src_path):
                self._watcher._read_new_lines(event.src_path)
