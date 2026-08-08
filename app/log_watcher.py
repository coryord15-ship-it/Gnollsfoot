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

from app.player_roster import PlayerRoster

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
        # /loc — the pattern has lived in settings.json ("loc_output") since the
        # start but nothing ever compiled it. See _dispatch for why it matters.
        self._loc_pattern = re.compile(
            patterns.get("loc_output",
                         r"Your Location is (?P<x>-?[\d.]+), (?P<y>-?[\d.]+), (?P<z>-?[\d.]+)"),
            re.IGNORECASE,
        )
        self._last_loc: Optional[dict] = None
        self._current_zone = None
        self._current_difficulty = None

        # Who is a player vs an NPC. Nothing in "X says, '...'" distinguishes
        # `Helga says, 'yeah'` from `Doug says, 'Hail and well met.'`, so the
        # roster learns players from channels NPCs cannot use (guild/group/tell/
        # shout/OOC) and gates the dialogue callbacks below. See player_roster.py.
        self.roster = PlayerRoster()

        # Callbacks — registered by other modules
        self._on_loot: list[Callable[[LootEvt], None]] = []
        self._on_dialogue: list[Callable[[DialogueEvent], None]] = []
        self._on_turn_in: list[Callable[[TurnInEvent], None]] = []
        self._on_zone: list[Callable[[str], None]] = []
        self._on_kill: list[Callable[[str], None]] = []
        self._on_any_line: list[Callable[[str], None]] = []  # raw-line callbacks (matcher hail/say, …)
        self._on_craft: list[Callable] = []                  # tradeskill combines
        # An offer's verdict lives in the NEXT few lines, so hold the pending offer and
        # emit it once we have seen enough to classify it. Without this we would publish
        # every offer as a turn-in, including the ones the NPC refused.
        self._pending_offer = None
        self._pending_after: list[str] = []

        self.status = "stopped"  # 'watching' | 'paused' | 'error' | 'stopped'
        self._partial_line = ""  # buffer for incomplete lines between watchdog reads

    # ── Registration ─────────────────────────────────────────────────────────

    def on_loot(self, fn): self._on_loot.append(fn)
    def on_dialogue(self, fn): self._on_dialogue.append(fn)
    def on_turn_in(self, fn): self._on_turn_in.append(fn)

    def last_loc(self, max_age_s: float = 900.0) -> Optional[dict]:
        """The most recent /loc, or None if there isn't a recent enough one.

        Returns {x, y, z, zone, age_s}. `max_age_s` guards against attaching a
        position from an hour and three zones ago to a hand-in — an old fix is
        worse than no fix, because it looks like data.

        `age_s` is computed HERE rather than by the caller: this class owns the
        timestamp, and it saves every consumer from needing its own clock import.
        """
        loc = self._last_loc
        if not loc:
            return None
        age = time.time() - loc.get("at", 0)
        if max_age_s and age > max_age_s:
            return None
        out = dict(loc)
        out["age_s"] = round(age, 1)
        return out
    def on_zone(self, fn): self._on_zone.append(fn)
    def on_kill(self, fn): self._on_kill.append(fn)
    def on_any_line(self, fn): self._on_any_line.append(fn)
    def on_craft(self, fn): self._on_craft.append(fn)

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
        # A hand-in waiting on its verdict would otherwise be lost when the app closes —
        # the queue holds it for 5 lines and a session can end inside that window. Emit it
        # with whatever verdict we have (usually 'unknown'), which is still worth keeping.
        try:
            self._flush_offer()
        except Exception:
            log.debug("pending offer flush on stop failed", exc_info=True)
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

    _TS_LINE = re.compile(r"^\[(?:\w{3}) (\w{3}) +(\d{1,2}) \d{2}:\d{2}:\d{2} (\d{4})\]")
    _MONTHS = {m: i for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

    @classmethod
    def _log_date_range(cls, path: str) -> tuple[str, str]:
        """(first_date, last_date) as YYYY-MM-DD, read from the log's own lines.

        EQ stamps every line "[Sat Jul 11 13:11:40 2026] ...", so the range is the
        first and last such line. Reads a small window from each END of the file —
        these reach 50 MB+ and must never be read whole just to build a filename.
        Returns ("", "") if the file has no parseable timestamps.
        """
        first = last = ""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                for raw in fh.read(65536).splitlines():
                    m = cls._TS_LINE.match(raw.decode("utf-8", "replace"))
                    if m:
                        first = f"{m.group(3)}-{cls._MONTHS.get(m.group(1), 1):02d}-{int(m.group(2)):02d}"
                        break
                fh.seek(max(0, size - 65536))
                for raw in reversed(fh.read().splitlines()):
                    m = cls._TS_LINE.match(raw.decode("utf-8", "replace"))
                    if m:
                        last = f"{m.group(3)}-{cls._MONTHS.get(m.group(1), 1):02d}-{int(m.group(2)):02d}"
                        break
        except OSError:
            pass
        return first, last

    @classmethod
    def archive_name_for(cls, path: str) -> str:
        r"""The archived filename: original stem + the date range it covers.

        eqlog_Morbid_freeport.txt  ->  eqlog_Morbid_freeport_2026-07-11_to_2026-08-07.bak

        Owner asked for the span readable at a glance and tied to the original name
        (2026-08-08). ISO dates rather than US: they sort chronologically in the
        folder and cannot be misread day/month. Keeping the original stem as the
        PREFIX makes every archive for a character sort right next to its live log,
        which is the whole point — you find it without looking for it.

        ⚠ The ".bak" extension is load-bearing. Archives sit in the SAME folder as
        live logs, and the watcher globs "eqlog_*.txt" — ending in .bak is what stops
        the app re-ingesting its own archives and double-counting every event.
        """
        base = os.path.basename(path)
        stem = base[:-4] if base.lower().endswith(".txt") else base
        first, last = cls._log_date_range(path)
        # NEVER mix a date parsed from the log with one from the filesystem — that
        # invented a "2026-07-11_to_2026-08-08" span for a log whose lines were all
        # from Jul 11, purely because the file had been touched today. If only one
        # end parses, that date IS the span; mtime is used only when neither does.
        if not first and not last:
            try:
                first = last = datetime.fromtimestamp(
                    os.path.getmtime(path)).strftime("%Y-%m-%d")
            except OSError:
                first = last = datetime.now().strftime("%Y-%m-%d")
        else:
            first = first or last
            last = last or first
        span = f"{first}_to_{last}" if first != last else first
        return f"{stem}_{span}.bak"

    def rotate_to(self, archive_dir: Optional[str] = None,
                  min_bytes: int = 0) -> Optional[str]:
        """Archive character logs so EQ starts fresh ones next launch.

        RENAMES IN PLACE by default (archive_dir=None) — the archive stays in the
        EQ Logs folder next to the live log. Changed 2026-08-08: this used to MOVE
        every log to a folder outside the game directory, and the owner's concern
        was the right one — "im worried people are gonna look for their logs and not
        know where they are". A renamed file in the same folder cannot be hunted for.

        min_bytes: only archive logs at least this big. Previously ONE oversized log
        crossing the threshold caused ALL of them to be archived — on the owner's
        machine that moved 12 files when only 1 was large and nine were empty July
        stubs. The size check lives here now because rotate_to is what iterates.

        ONLY call when EQ is closed — renaming a file EQ holds open fails on Windows.
        Returns the last archive path, or None if nothing qualified.
        """
        with self._lock:
            matches = self._matching_files()
            if not matches:
                return None
            if archive_dir:
                os.makedirs(archive_dir, exist_ok=True)
            last_dest = None
            for path in matches:
                try:
                    if min_bytes and os.path.getsize(path) < min_bytes:
                        continue          # leave small logs exactly where EQ put them
                except OSError:
                    continue
                f = self._files.pop(path, None)
                if f:
                    try:
                        f.close()
                    except Exception:
                        pass
                self._pos.pop(path, None)
                self._partial.pop(path, None)
                try:
                    name = self.archive_name_for(path)
                    dest = os.path.join(archive_dir or os.path.dirname(path), name)
                    if os.path.exists(dest):      # same span archived twice
                        root, ext = os.path.splitext(dest)
                        n = 2
                        while os.path.exists(f"{root}_{n}{ext}"):
                            n += 1
                        dest = f"{root}_{n}{ext}"
                    shutil.move(path, dest)
                    last_dest = dest
                    log.info("Archived log -> %s", dest)
                except Exception:
                    log.exception("log rotation failed for %s", path)
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
        # Every on_any_line callback gets the raw line. (This was `fn()` with zero args — a leftover
        # from when on_any_line was only a silence-timer ping. The matcher's hail/"You say" callback
        # needs the line, and _dispatch was never updated to pass it, so EVERY line threw a
        # caught-and-logged TypeError and hail/say quest-step matching never actually fired. Fixed
        # 2026-07-21.)
        for fn in self._on_any_line:
            try:
                fn(line)
            except Exception:
                log.exception("on_any_line callback error")

        # Only parse lines that start with the expected timestamp
        if not self._ts_pattern.match(line):
            return

        # Learn who is a player before anything can act on a speaker name. Channel
        # lines (guild/group/tell/shout/OOC) are proof of a player; Hail targets and
        # slain mobs are proof of an NPC. Cheap — a handful of anchored regexes, and
        # it returns early on the first match.
        try:
            self.roster.observe(line)
        except Exception:
            log.debug("roster.observe failed", exc_info=True)

        # A pending offer is waiting to learn whether the NPC took it, and the answer is
        # usually a DIALOGUE line ("<NPC> says, 'I have no need for this…'"). This must run
        # BEFORE the per-parser returns below, or the dialogue branch consumes the verdict
        # and every refused hand-in is recorded as 'unknown'. Collect first, decide later.
        if self._pending_offer is not None:
            self._pending_after.append(line)
            if len(self._pending_after) >= 5:
                self._flush_offer()

        # /loc — "Your Location is 123.45, -67.89, 12.00"
        #
        # Added 2026-08-03. The regex has been sitting unused in settings.json
        # ("loc_output") since the beginning: the app read entity coordinates FROM
        # the database but never captured the player's own position, so the
        # `entities` table (loc_x/loc_y/loc_z) has zero rows and we cannot tell
        # anyone where a quest NPC actually stands.
        #
        # We only remember the most recent fix. On a quest hand-in, that position
        # is approximately where the NPC is — the player was standing next to them
        # to hand the item over.
        #
        # ⚠ APPROXIMATE. The player may have moved between typing /loc and the
        # hand-in, which is why the timestamp is kept: a consumer can discard a
        # fix that is too old to be meaningful. Never let a correlated position
        # overwrite one a human confirmed.
        lm = self._loc_pattern.search(line) if self._loc_pattern else None
        if lm:
            try:
                self._last_loc = {
                    "x": float(lm.group("x")),
                    "y": float(lm.group("y")),
                    "z": float(lm.group("z")),
                    "zone": self._current_zone,
                    "at": time.time(),
                }
            except (ValueError, IndexError):
                pass
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

        # ⚠ parse_dialogue returns a DialogueEvent with EMPTY npc_name/text for lines that
        # are not dialogue at all, and an empty dataclass is still TRUTHY. So a bare
        # `if dialogue:` matched EVERY line and returned — making everything below this
        # point unreachable dead code. That is why quest turn-ins never fired even before
        # the regex was wrong: two bugs stacked. Require a real speaker. (2026-07-30)
        dialogue = self._npc_parser.parse_dialogue(line)
        if dialogue and getattr(dialogue, "npc_name", "").strip():
            # ⚠ PRIVACY GATE. `<Name> says, '...'` is the SAME shape for a player
            # and an NPC, and the speaker is stored verbatim downstream — the
            # quest-sighting collector strips only the LOCAL user's names from the
            # text. So another player's chat could reach the server intact.
            # Verified 2026-08-04 by driving the real collector: player speaks →
            # you /say → they speak again inside the conversation gap → queued.
            # Drop anything we can PROVE is a player (roster hit, or a truncation
            # of one — `ntis` is `Dragantis`). Publishing has a stricter,
            # deny-by-default check of its own; this one only removes what is
            # certain, so quest matching keeps working. See player_roster.py.
            if self.roster.is_player(dialogue.npc_name):
                self.roster.dropped_players += 1
                return
            for fn in self._on_dialogue:
                try: fn(dialogue)
                except Exception: log.exception("on_dialogue callback error")
            return

        # Tradeskill combine — feeds the recipe/item DB
        craft = self._event_parser.parse_craft(line)
        if craft:
            for fn in self._on_craft:
                try: fn(craft)
                except Exception: log.exception("on_craft callback error")
            return

        # Quest turn-in — silent, feeds the journal. Held until classified.
        turn_in = self._event_parser.parse_turn_in(line)
        if turn_in:
            self._flush_offer()          # emit any previous one first
            self._pending_offer = turn_in
            self._pending_after = []
            return

    def _flush_offer(self):
        """Classify the held offer against the lines that followed, then emit it."""
        offer, after = self._pending_offer, self._pending_after
        self._pending_offer, self._pending_after = None, []
        if offer is None:
            return
        try:
            offer = self._event_parser.classify_turn_in(offer, after)
        except Exception:
            log.exception("turn-in classification error")
        for fn in self._on_turn_in:
            try: fn(offer)
            except Exception: log.exception("on_turn_in callback error")

    def rescan_recent(
        self,
        max_bytes: int = 2_000_000,
        max_lines: int = 80_000,
    ) -> list[str]:
        """Read the last ~max_bytes of every open character log; return raw lines.

        Used for journal catch-up (loot + kills) without moving the live tail.
        """
        lines: list[str] = []
        paths = list(self._matching_files()) if hasattr(self, "_matching_files") else []
        if not paths and self._dir:
            try:
                paths = _glob.glob(os.path.join(self._dir, self._glob))
            except Exception:
                paths = []
        for p in paths:
            try:
                size = os.path.getsize(p)
                start = max(0, size - max_bytes)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if start:
                        f.seek(start)
                        f.readline()  # drop partial first line
                    for i, line in enumerate(f):
                        if i >= max_lines:
                            break
                        lines.append(line.rstrip("\n"))
            except Exception:
                log.debug("rescan read failed for %s", p, exc_info=True)
        return lines

    def parse_loot_from_lines(self, lines: list[str]) -> list:
        """Parse loot events from historical lines (catch-up)."""
        out = []
        for line in lines:
            try:
                evt = self._loot_parser.parse(line)
                if evt:
                    out.append(evt)
            except Exception:
                pass
        return out


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
