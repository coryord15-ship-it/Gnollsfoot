"""
Real-time log file tail.

Uses watchdog for file-change notifications, then manually reads new bytes
to handle partial lines safely (watchdog events fire mid-write).
Dispatches parsed events to registered callbacks.
The watcher runs on its own thread and is always live — never paused.
"""

import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.parsers.loot_parser import LootParser, LootEvent as LootEvt
from app.parsers.npc_parser import NPCParser, DialogueEvent, LocEvent, WhoEvent
from app.parsers.combat_parser import (
    CombatParser,
    NpcTargetEvent, NpcSlainEvent, VendorSellEvent, VendorBuyEvent, TurnInEvent,
    AutoSoldEvent,
)

log = logging.getLogger(__name__)


class LogWatcher:
    def __init__(self, config: dict):
        self._config = config
        self._path: Optional[str] = config.get("log_file_path") or None
        self._observer: Optional[Observer] = None
        self._file = None
        self._file_pos = 0
        self._lock = threading.Lock()
        self._running = False

        patterns = config.get("log_patterns", {})
        self._ts_pattern = re.compile(
            patterns.get("timestamp", r"\[\w+ \w+ +\d+ \d+:\d+:\d+ \d+\]")
        )
        self._loot_parser = LootParser(patterns.get("loot_triggers", []))
        self._npc_parser = NPCParser(patterns)
        self._combat_parser = CombatParser(patterns)
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
        self._current_zone = None
        self._current_difficulty = None

        # Callbacks — registered by other modules
        self._on_loot: list[Callable[[LootEvt], None]] = []
        self._on_dialogue: list[Callable[[DialogueEvent], None]] = []
        self._on_loc: list[Callable[[LocEvent], None]] = []
        self._on_who: list[Callable[[WhoEvent], None]] = []
        self._on_npc_target: list[Callable[[NpcTargetEvent], None]] = []
        self._on_npc_slain: list[Callable[[NpcSlainEvent], None]] = []
        self._on_vendor_sell: list[Callable[[VendorSellEvent], None]] = []
        self._on_vendor_buy: list[Callable[[VendorBuyEvent], None]] = []
        self._on_turn_in: list[Callable[[TurnInEvent], None]] = []
        self._on_zone: list[Callable[[str], None]] = []
        self._on_auto_sold: list[Callable[[AutoSoldEvent], None]] = []
        self._on_any_line: list[Callable[[], None]] = []  # for the silence timer

        self.status = "stopped"  # 'watching' | 'paused' | 'error' | 'stopped'
        self._partial_line = ""  # buffer for incomplete lines between watchdog reads

    # ── Registration ─────────────────────────────────────────────────────────

    def on_loot(self, fn): self._on_loot.append(fn)
    def on_dialogue(self, fn): self._on_dialogue.append(fn)
    def on_loc(self, fn): self._on_loc.append(fn)
    def on_who(self, fn): self._on_who.append(fn)
    def on_npc_target(self, fn): self._on_npc_target.append(fn)
    def on_npc_slain(self, fn): self._on_npc_slain.append(fn)
    def on_vendor_sell(self, fn): self._on_vendor_sell.append(fn)
    def on_vendor_buy(self, fn): self._on_vendor_buy.append(fn)
    def on_turn_in(self, fn): self._on_turn_in.append(fn)
    def on_zone(self, fn): self._on_zone.append(fn)
    def on_auto_sold(self, fn): self._on_auto_sold.append(fn)
    def on_any_line(self, fn): self._on_any_line.append(fn)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, path: Optional[str] = None):
        if path:
            self._path = path
        if not self._path or not os.path.isfile(self._path):
            self.status = "error"
            log.error("Log file not found: %s", self._path)
            return

        self._open_file()
        self._observer = Observer()
        handler = _FileHandler(self)
        self._observer.schedule(handler, os.path.dirname(self._path), recursive=False)
        self._observer.start()
        self._running = True
        self.status = "watching"
        log.info("Log watcher started: %s", self._path)

    def stop(self):
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join()
        if self._file:
            self._file.close()
        self.status = "stopped"

    @property
    def log_path(self) -> Optional[str]:
        return self._path

    def rotate_to(self, archive_dir: str) -> Optional[str]:
        """Move the current log into archive_dir and drop our handle so EQ creates a
        fresh log next launch. ONLY call when EQ is closed (no active writer) — moving
        a file EQ holds open would fail or corrupt it. Returns the archive path, or
        None if nothing was rotated. The watcher reopens the fresh log automatically."""
        with self._lock:
            path = self._path
            if not path or not os.path.exists(path):
                return None
            if self._file:
                self._file.close()
                self._file = None
            try:
                os.makedirs(archive_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = os.path.join(archive_dir, f"{os.path.basename(path)}.{stamp}.bak")
                shutil.move(path, dest)
            except Exception:
                log.exception("log rotation move failed")
                if os.path.exists(path):
                    self._open_file()
                return None
            self._file_pos = 0
            self._partial_line = ""
            log.info("Rotated log -> %s", dest)
            return dest

    def reload_patterns(self, config: dict):
        """Hot-reload all patterns from updated config."""
        patterns = config.get("log_patterns", {})
        self._loot_parser.reload(patterns.get("loot_triggers", []))
        self._npc_parser.reload(patterns)
        self._combat_parser.reload(patterns)
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

    # ── Internal ─────────────────────────────────────────────────────────────

    def _open_file(self, seek_end: bool = True):
        if self._file:
            self._file.close()
        self._file = open(self._path, "r", encoding="utf-8", errors="replace")
        if seek_end:
            # Seek to end so we only process new lines (not the full history on launch)
            self._file.seek(0, 2)
        self._file_pos = self._file.tell()

    def _read_new_lines(self):
        if not self._file:
            # File may have just been recreated by EQ after a rotation — reopen from
            # the start of the fresh log so we don't miss its first lines.
            if self._path and os.path.exists(self._path):
                self._open_file(seek_end=False)
            else:
                return
        with self._lock:
            self._file.seek(self._file_pos)
            new_data = self._file.read()
            self._file_pos = self._file.tell()

        if not new_data:
            return

        # Prepend any leftover fragment from the previous watchdog read, then
        # hold the last element as a potential incomplete line until the next read.
        new_data = self._partial_line + new_data
        lines = new_data.split("\n")
        self._partial_line = lines[-1]  # "" if data ended with \n, else a fragment
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

        dialogue = self._npc_parser.parse_dialogue(line)
        if dialogue:
            for fn in self._on_dialogue:
                try: fn(dialogue)
                except Exception: log.exception("on_dialogue callback error")
            return

        loc = self._npc_parser.parse_loc(line)
        if loc:
            for fn in self._on_loc:
                try: fn(loc)
                except Exception: log.exception("on_loc callback error")
            return

        who = self._npc_parser.parse_who(line)
        if who:
            for fn in self._on_who:
                try: fn(who)
                except Exception: log.exception("on_who callback error")
            return

        # Combat / vendor parsers — silent, no alert fired from these
        npc_target = self._combat_parser.parse_npc_target(line)
        if npc_target:
            for fn in self._on_npc_target:
                try: fn(npc_target)
                except Exception: log.exception("on_npc_target callback error")
            return

        npc_slain = self._combat_parser.parse_npc_slain(line)
        if npc_slain:
            for fn in self._on_npc_slain:
                try: fn(npc_slain)
                except Exception: log.exception("on_npc_slain callback error")
            return

        vendor_sell = self._combat_parser.parse_vendor_sell(line)
        if vendor_sell:
            for fn in self._on_vendor_sell:
                try: fn(vendor_sell)
                except Exception: log.exception("on_vendor_sell callback error")
            return

        vendor_buy = self._combat_parser.parse_vendor_buy(line)
        if vendor_buy:
            for fn in self._on_vendor_buy:
                try: fn(vendor_buy)
                except Exception: log.exception("on_vendor_buy callback error")
            return

        turn_in = self._combat_parser.parse_turn_in(line)
        if turn_in:
            for fn in self._on_turn_in:
                try: fn(turn_in)
                except Exception: log.exception("on_turn_in callback error")
            return

        auto_sold = self._combat_parser.parse_auto_sold(line)
        if auto_sold:
            for fn in self._on_auto_sold:
                try: fn(auto_sold)
                except Exception: log.exception("on_auto_sold callback error")


class _FileHandler(FileSystemEventHandler):
    def __init__(self, watcher: LogWatcher):
        self._watcher = watcher

    def on_modified(self, event: FileModifiedEvent):
        if not isinstance(event, FileModifiedEvent):
            return
        if os.path.abspath(event.src_path) == os.path.abspath(self._watcher._path):
            self._watcher._read_new_lines()
