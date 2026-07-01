"""
Research queue with 6-second silence throttle.

This is the core "don't ruin gameplay" mechanism.
Items are only researched during genuine log downtime:
  - Any log activity resets a 6s countdown.
  - When the countdown expires, process ONE item, wait 1s, repeat.
  - If the log wakes up mid-research, pause until it goes quiet again.

Status: 'idle' | 'queued' | 'researching'
"""

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger(__name__)


class ResearchQueue:
    def __init__(
        self,
        silence_seconds: float = 6.0,
        cooldown_seconds: float = 1.0,
        on_status_change: Optional[Callable[[str], None]] = None,
    ):
        self._silence_seconds = silence_seconds
        self._cooldown_seconds = cooldown_seconds
        self._on_status_change = on_status_change

        self._queue: deque[str] = deque()
        self._in_flight: set[str] = set()       # items currently being researched
        self._lock = threading.Lock()
        self._last_log_activity = time.monotonic()
        self._running = False
        self._worker: Optional[threading.Thread] = None

        # Called with (item_name) when an item needs to be researched
        self._research_fn: Optional[Callable[[str], None]] = None

        self.status = "idle"

    def set_research_fn(self, fn: Callable[[str], None]):
        self._research_fn = fn

    # ── Public API ───────────────────────────────────────────────────────────

    def enqueue(self, item_name: str):
        with self._lock:
            if item_name in self._in_flight:
                return
            if item_name in self._queue:
                return
            self._queue.append(item_name)
            log.debug("Enqueued for research: %s", item_name)
        self._update_status()

    def on_log_activity(self):
        """Called by the log watcher on every new line — resets the silence timer."""
        self._last_log_activity = time.monotonic()

    def start(self):
        self._running = True
        self._worker = threading.Thread(target=self._loop, daemon=True, name="ResearchQueue")
        self._worker.start()

    def stop(self):
        self._running = False

    def update_config(self, silence_seconds: float, cooldown_seconds: float):
        self._silence_seconds = silence_seconds
        self._cooldown_seconds = cooldown_seconds

    # ── Internal ─────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            time.sleep(0.5)

            with self._lock:
                pending = len(self._queue) > 0

            if not pending:
                self._set_status("idle")
                continue

            silence = time.monotonic() - self._last_log_activity
            if silence < self._silence_seconds:
                self._set_status("queued")
                continue

            with self._lock:
                if not self._queue:
                    continue
                item_name = self._queue.popleft()
                self._in_flight.add(item_name)

            self._set_status("researching")
            log.info("Researching: %s", item_name)
            try:
                if self._research_fn:
                    self._research_fn(item_name)
            except Exception:
                log.exception("Research failed for: %s", item_name)
            finally:
                with self._lock:
                    self._in_flight.discard(item_name)

            time.sleep(self._cooldown_seconds)
            self._update_status()

    def _update_status(self):
        with self._lock:
            if self._in_flight:
                self._set_status("researching")
            elif self._queue:
                self._set_status("queued")
            else:
                self._set_status("idle")

    def _set_status(self, status: str):
        if status != self.status:
            self.status = status
            if self._on_status_change:
                try:
                    self._on_status_change(status)
                except Exception:
                    log.exception("Status change callback error")
