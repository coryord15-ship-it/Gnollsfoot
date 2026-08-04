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

# Both domains serve the SAME Vercel deployment, so either answers correctly.
#
# ⚠ WHY THERE IS A SECOND ONE. On 2026-08-04 a user on Comcast could not reach
# gnollguard.com at all — Xfinity's web protection blocks it, almost certainly on
# domain reputation (the app is unsigned and the domain is weeks old). They could
# reach legendsgnollloot.com, the older domain, which resolves to a different IP.
#
# Without a fallback that user installs fine from the old domain and then their
# update check fails FOREVER, silently: _fetch_latest() swallows the error and
# logs at debug, so the app never says an update exists. That is the same
# stuck-and-never-told failure as the 1.5.13 loop, through a different door.
#
# Order matters: primary first, fallback only when the primary is unreachable.
_VERSION_URLS = (
    "https://gnollguard.com/api/version",
    "https://legendsgnollloot.com/api/version",
)
_CHECK_INTERVAL = 24 * 60 * 60  # 24 hours in seconds


def _parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def _fetch_latest() -> Optional[dict]:
    """The version feed, trying each host until one answers.

    Logs at WARNING (not debug) when every host fails. A silent version check is
    indistinguishable from "you are up to date", and that is exactly how users
    sat on a stale build without knowing.
    """
    last = None
    for url in _VERSION_URLS:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if url is not _VERSION_URLS[0]:
                log.info("Version check used fallback host: %s", url)
            return data
        except Exception as e:
            last = e
            log.debug("Version check failed on %s: %s", url, e)
    log.warning("Version check failed on ALL hosts (%s) — last error: %s",
                len(_VERSION_URLS), last)
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
