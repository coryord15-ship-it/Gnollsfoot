"""
Safe log rotation.

Keeps the main EQ log from growing forever WITHOUT ever touching it while EQ is running
(moving/truncating a file EQ holds open risks sharing errors, lost lines, or a corrupt
log). Periodically checks: if EQ (eqgame.exe) is NOT running AND the log is over the size
threshold, it asks the watcher to archive the log so EQ starts a fresh one next launch.

Runs on a daemon timer; the actual move is done by LogWatcher.rotate_to (which holds the
file lock and reopens the fresh log).
"""

import logging
import os
import subprocess
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — never flash a console (app invariant)


def eq_running(exe: str = "eqgame.exe") -> bool:
    """True if the EQ client is running. Fails safe to True (don't rotate if unsure)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe}"],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        ).stdout or ""
        return exe.lower() in out.lower()
    except Exception:
        log.exception("eq_running check failed; assuming running")
        return True


class LogRotator:
    def __init__(self, get_log_path: Callable[[], Optional[str]], archive_dir: str,
                 rotate_fn: Callable[[str], Optional[str]],
                 threshold_mb: int = 50, check_every_s: int = 300,
                 enabled: bool = True, eq_exe: str = "eqgame.exe"):
        self._get_log_path = get_log_path
        self._archive_dir = archive_dir
        self._rotate_fn = rotate_fn
        self._threshold = max(1, threshold_mb) * 1024 * 1024
        self._interval = max(30, check_every_s)
        self._enabled = enabled
        self._eq_exe = eq_exe
        self._timer: Optional[threading.Timer] = None
        self._stopped = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if not self._enabled:
            log.info("log rotation disabled")
            return
        self._schedule()

    def stop(self):
        self._stopped = True
        if self._timer:
            self._timer.cancel()

    def _schedule(self):
        if self._stopped:
            return
        self._timer = threading.Timer(self._interval, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        try:
            self._check()
        except Exception:
            log.exception("log rotation check failed")
        self._schedule()

    # ── core ─────────────────────────────────────────────────────────────────

    def _check(self):
        path = self._get_log_path()
        if not path or not os.path.exists(path):
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < self._threshold:
            return
        if eq_running(self._eq_exe):
            log.info("log is %.0f MB but EQ is running; deferring rotation",
                     size / 1024 / 1024)
            return
        self._rotate_fn(self._archive_dir)

    def rotate_now(self) -> Optional[str]:
        """Manual rotate (e.g. a UI button). Refuses while EQ is running."""
        if eq_running(self._eq_exe):
            return None
        return self._rotate_fn(self._archive_dir)
