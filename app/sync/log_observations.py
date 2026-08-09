"""
Durable upload queue for log observations (crafts, turn-ins, kills, zones, loot).

Owner, 2026-07-30: "we need these journals to start uploading their info somewhere we can
extract it later and parse it against what is already learned."

DESIGN
  1. add()     — append one observation to a local JSONL file. The FILE is the source of
                 truth: nothing is held only in memory, so a crash or a closed laptop
                 loses nothing.
  2. flush()   — upload everything past the last-sent byte offset, then advance the
                 offset. Idempotent and resumable across restarts, and the table has a
                 unique index on (install_id, kind, payload) so a double-send is a no-op.
  3. prune()   — only AFTER a confirmed upload, delete OUR archived copies of the EQ logs
                 so the app does not eat the user's disk.

⚠ NEVER TOUCHES THE LIVE EQ LOG. prune() deletes only files under our own
Documents\GnollGuard\logs_archive. The game owns eqlog_*.txt in its Logs folder; other
tools read it, the player may want it, and log_rotate.py already refuses to move it while
EQ is running. We delete copies we made, nothing else.

⚠ PRIVACY: game nouns only — item, npc, mob, zone, tradeskill. NEVER a player or character
name. The turn-in refusal line contains the player's name; we store only item/npc/verdict.
install_id is an opaque per-install uuid for dedup, not identity.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

SUPABASE_URL = "https://ratezylqpxgruyjscpbu.supabase.co"
BATCH = 200          # rows per POST
MIN_INTERVAL = 60.0  # seconds between flushes, so a busy session is not chatty


class ObservationQueue:
    def __init__(self, data_dir: str, archive_dir: str, install_id: str,
                 get_token=None, anon_key: str = ""):
        self._file = os.path.join(data_dir, "log_observations.jsonl")
        self._offset_file = self._file + ".sent"
        self._archive_dir = archive_dir
        self._install_id = install_id
        self._get_token = get_token          # returns the signed-in user's JWT, or None
        self._anon_key = anon_key
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self.queued = 0
        self.uploaded = 0
        os.makedirs(data_dir, exist_ok=True)

    # ── collect ───────────────────────────────────────────────────────────────
    def add(self, kind: str, payload: dict, zone: Optional[str] = None) -> None:
        """Append one observation. Cheap, never blocks, never raises into the app."""
        try:
            row = {
                "install_id": self._install_id,
                "kind": kind,
                "payload": {k: v for k, v in payload.items() if v not in (None, "")},
                "zone": zone,
                "seen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            with self._lock, open(self._file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.queued += 1
        except Exception:
            log.debug("observation add failed", exc_info=True)

    # ── upload ────────────────────────────────────────────────────────────────
    def _offset(self) -> int:
        try:
            n = int(open(self._offset_file).read().strip())
            # If the file shrank (user cleared cache), start over rather than seeking
            # past the end and silently uploading nothing ever again.
            return 0 if n > os.path.getsize(self._file) else n
        except Exception:
            return 0

    def _write_offset(self, n: int) -> None:
        try:
            with open(self._offset_file, "w") as fh:
                fh.write(str(n))
        except Exception:
            pass

    def flush(self, force: bool = False) -> int:
        """Upload unsent rows. Returns how many were accepted. Never raises."""
        if not force and time.time() - self._last_flush < MIN_INTERVAL:
            return 0
        self._last_flush = time.time()
        if not os.path.exists(self._file):
            return 0
        token = None
        try:
            token = self._get_token() if self._get_token else None
        except Exception:
            pass
        if not token:
            return 0                      # signed out — keep queuing, upload later

        rows, new_off = [], self._offset()
        try:
            with open(self._file, "r", encoding="utf-8") as fh:
                fh.seek(new_off)
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
                new_off = fh.tell()
        except Exception:
            return 0
        if not rows:
            return 0

        sent = 0
        try:
            import urllib.error
            import urllib.request
            for i in range(0, len(rows), BATCH):
                chunk = rows[i:i + BATCH]
                req = urllib.request.Request(
                    f"{SUPABASE_URL}/rest/v1/log_observations",
                    data=json.dumps(chunk).encode("utf-8"), method="POST")
                req.add_header("apikey", self._anon_key)
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Content-Type", "application/json")
                req.add_header("Prefer", "return=minimal")
                try:
                    with urllib.request.urlopen(req, timeout=30):
                        sent += len(chunk)
                except urllib.error.HTTPError as he:
                    # 🔴 A 409 IS NOT "ALREADY STORED". Postgres aborts the ENTIRE INSERT on
                    # the first duplicate key, so a batch containing one dup stores NOTHING —
                    # yet the old code counted the whole chunk as sent and let the offset
                    # advance past it. Those rows were dropped AND skipped forever.
                    # Measured on one install 2026-08-09: 2,074 local observations →
                    # 460 survived the (then time-less) dedup index → only 37 ever reached
                    # the table. The queue reported itself healthy the whole time.
                    #
                    # The original comment was right about the danger of the opposite bug —
                    # treating 409 as failure jams the queue forever on one poisoned row —
                    # so do neither. RETRY THE BATCH ROW BY ROW: genuine duplicates 409
                    # individually and are skipped, every other row still lands.
                    if he.code == 409:
                        sent += self._insert_individually(chunk, token)
                    else:
                        raise
        except Exception as e:
            log.debug("observation flush failed: %s", e)
            return 0                      # offset NOT advanced — retried next time

        self._write_offset(new_off)
        self.uploaded += sent
        return sent

    def _insert_individually(self, chunk: list, token: str) -> int:
        """Fallback for a 409'd batch: post each row alone so one duplicate cannot sink 499
        good rows. Returns how many were genuinely stored (409s do not count).

        Slow by design — it only runs on the batch that actually collided, and a batch
        rarely collides twice. Correctness beats throughput here: the alternative silently
        discarded up to BATCH rows per collision and advanced the offset past them.
        """
        import urllib.error
        import urllib.request

        stored = 0
        for row in chunk:
            req = urllib.request.Request(
                f"{SUPABASE_URL}/rest/v1/log_observations",
                data=json.dumps([row]).encode("utf-8"), method="POST")
            req.add_header("apikey", self._anon_key)
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            req.add_header("Prefer", "return=minimal")
            try:
                with urllib.request.urlopen(req, timeout=30):
                    stored += 1
            except urllib.error.HTTPError as he:
                if he.code == 409:
                    continue          # genuine duplicate — already stored, nothing lost
                raise                 # anything else is a real failure; do not advance
        return stored

    # ── clean up ──────────────────────────────────────────────────────────────
    def prune_archives(self, keep_days: int = 2) -> tuple[int, int]:
        """Delete OUR archived EQ-log copies older than keep_days. Returns (files, bytes).

        Only called after a successful flush. Keeps a couple of days so a user can still
        report 'it missed something' and we can re-scan.
        """
        removed = freed = 0
        if not self._archive_dir or not os.path.isdir(self._archive_dir):
            return (0, 0)
        cutoff = time.time() - keep_days * 86400
        for p in glob.glob(os.path.join(self._archive_dir, "*")):
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    sz = os.path.getsize(p)
                    os.remove(p)
                    removed += 1
                    freed += sz
            except OSError:
                continue
        if removed:
            log.info("pruned %d archived log(s), freed %.1f MB", removed, freed / 1048576)
        return (removed, freed)

    def clear_cache(self) -> tuple[int, int]:
        """User pressed 'Clear cache' in Settings. Flush first so nothing is lost, then
        drop the local queue and every archived log regardless of age."""
        try:
            self.flush(force=True)
        except Exception:
            pass
        removed = freed = 0
        for p in ([self._file, self._offset_file]
                  + (glob.glob(os.path.join(self._archive_dir, "*"))
                     if self._archive_dir and os.path.isdir(self._archive_dir) else [])):
            try:
                if os.path.isfile(p):
                    sz = os.path.getsize(p)
                    os.remove(p)
                    removed += 1
                    freed += sz
            except OSError:
                continue
        self.queued = 0
        return (removed, freed)

    def disk_usage(self) -> int:
        """Bytes the app is using for the queue + archived logs (for the Settings label)."""
        total = 0
        for p in ([self._file, self._offset_file]
                  + (glob.glob(os.path.join(self._archive_dir, "*"))
                     if self._archive_dir and os.path.isdir(self._archive_dir) else [])):
            try:
                if os.path.isfile(p):
                    total += os.path.getsize(p)
            except OSError:
                continue
        return total
