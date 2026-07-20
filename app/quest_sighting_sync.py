r"""quest_sighting_sync.py — fetch the known/wanted manifest, upload queued sightings in batches.

Companion to quest_sightings.py. That module decides WHAT to keep; this one decides WHEN it
leaves the machine, and makes sure it leaves rarely and in bulk.

Two jobs:

1. MANIFEST (GET /api/quest-sightings/manifest)
   Returns the line ids the server already has, plus a "wanted" list of chains we're missing
   a reply for. Cached to disk so a cold start doesn't stall, refreshed on app open. The
   collector consults it to drop known lines before they're ever queued — the DB never sees
   a request for text it already has.

2. UPLOAD (POST /api/quest-sightings)
   Sends the queue in batches, tracking a BYTE OFFSET of what's been pushed. Uploading is
   therefore idempotent and resumable: if the app dies mid-send, the next run picks up from
   the same offset, and the server upserts on line_id so a duplicate batch costs nothing.
   Same shape as devtool/capture_publisher.py, which carried the devkit through a real power
   cut without losing a row.

Runs on background threads and fails silent — a community-data upload must never interrupt
someone's play session or block app shutdown.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

log = logging.getLogger(__name__)

# The apex 308-redirects to www and urllib RAISES on a 308 POST instead of following it —
# that silently killed telemetry once. Always target www directly. (See CLAUDE.md.)
API_BASE = "https://www.gnollguard.com"
MANIFEST_URL = API_BASE + "/api/quest-sightings/manifest"
SUBMIT_URL = API_BASE + "/api/quest-sightings"

BATCH = 250
TIMEOUT = 15


def _offset_path(queue_path: str) -> str:
    return queue_path + ".pushed"


def load_manifest(cache_path: str) -> tuple[set, set]:
    """(known_ids, wanted_ids). Tries the network, falls back to the cached copy, and
    finally to empty — an unreachable server must not stop capture, it just means we
    queue a little more than strictly necessary."""
    try:
        req = urllib.request.Request(MANIFEST_URL, headers={"Accept": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read().decode())
        known = set(data.get("known") or [])
        wanted = set(data.get("wanted") or [])
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            json.dump({"known": sorted(known), "wanted": sorted(wanted)},
                      open(cache_path, "w", encoding="utf-8"))
        except Exception:
            pass
        return known, wanted
    except Exception:
        log.debug("manifest fetch failed; using cache", exc_info=True)
    try:
        d = json.load(open(cache_path, encoding="utf-8"))
        return set(d.get("known") or []), set(d.get("wanted") or [])
    except Exception:
        return set(), set()


def _read_offset(queue_path: str) -> int:
    try:
        off = int(open(_offset_path(queue_path), encoding="utf-8").read().strip())
    except Exception:
        off = 0
    try:                                   # queue cleared/rotated → don't skip its content
        if off > os.path.getsize(queue_path):
            off = 0
    except OSError:
        off = 0
    return off


def _write_offset(queue_path: str, off: int) -> None:
    try:
        open(_offset_path(queue_path), "w", encoding="utf-8").write(str(off))
    except Exception:
        pass


def upload_pending(queue_path: str, get_token=None) -> int:
    """Push everything queued since the last successful upload. Returns rows accepted.

    On ANY failure the offset is left untouched, so the same rows are retried next time
    rather than silently lost."""
    if not os.path.exists(queue_path):
        return 0
    off = _read_offset(queue_path)
    rows, new_off = [], off
    try:
        with open(queue_path, "r", encoding="utf-8") as fh:
            fh.seek(off)
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
            new_off = fh.tell()
    except Exception:
        return 0
    if not rows:
        _write_offset(queue_path, new_off)
        return 0

    sent = 0
    headers = {"Content-Type": "application/json"}
    if get_token:
        try:
            tok = get_token()
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
        except Exception:
            pass
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        try:
            req = urllib.request.Request(
                SUBMIT_URL, data=json.dumps({"sightings": chunk}).encode("utf-8"),
                headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=TIMEOUT).read()
            sent += len(chunk)
        except Exception:
            log.debug("sighting upload failed; will retry next flush", exc_info=True)
            return sent                    # offset NOT advanced — retry these next time
    _write_offset(queue_path, new_off)
    return sent


def upload_async(queue_path: str, get_token=None, on_done=None) -> None:
    """Fire-and-forget upload on a daemon thread — never blocks play or shutdown."""
    def run():
        try:
            n = upload_pending(queue_path, get_token)
            if n and on_done:
                on_done(n)
        except Exception:
            log.debug("async sighting upload failed", exc_info=True)
    threading.Thread(target=run, name="sighting-upload", daemon=True).start()
