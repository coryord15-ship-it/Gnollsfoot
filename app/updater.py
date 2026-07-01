"""
Auto-update checker. Runs on startup and every 24 hours.
Fetches gnollguard.com/api/version and compares to the local version.
Never blocks — always runs on a background thread.
Never pops up a window — notifies via callback so the UI can show a quiet banner.
"""

import logging
import threading
import time
from typing import Callable, Optional

import requests

from app.version import __version__

log = logging.getLogger(__name__)

_VERSION_URL   = "https://gnollguard.com/api/version"
_CHECK_INTERVAL = 24 * 60 * 60  # 24 hours in seconds


def _parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def _fetch_latest() -> Optional[dict]:
    try:
        resp = requests.get(_VERSION_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.debug("Version check failed: %s", e)
        return None


class UpdateChecker:
    def __init__(self, on_update_available: Callable[[str, str, str], None]):
        """
        on_update_available(version, download_url, changelog) called when a
        newer version is found. Always called on a background thread — caller
        must dispatch to UI thread if needed.
        """
        self._callback = on_update_available
        self._running  = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="UpdateChecker"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def check_now(self):
        threading.Thread(target=self._check, daemon=True).start()

    def _loop(self):
        # First check shortly after startup so we don't delay launch
        time.sleep(5)
        self._check()
        while self._running:
            time.sleep(_CHECK_INTERVAL)
            self._check()

    def _check(self):
        data = _fetch_latest()
        if not data:
            return
        latest = data.get("version", "")
        if not latest:
            return
        if _parse_version(latest) > _parse_version(__version__):
            log.info("Update available: %s (current: %s)", latest, __version__)
            self._callback(
                latest,
                data.get("download_url", "https://gnollguard.com/download"),
                data.get("changelog", ""),
            )
