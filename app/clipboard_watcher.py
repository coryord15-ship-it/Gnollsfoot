"""
Watches the Windows clipboard for new images (Print Screen / Alt+Print Screen).
When a new image appears it fires on_image(PIL.Image) on a background thread.
"""

import hashlib
import io
import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5  # seconds


def _img_hash(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return hashlib.md5(buf.getvalue()).hexdigest()


class ClipboardWatcher:
    def __init__(self, on_image: Callable):
        self._on_image = on_image
        self._last_hash: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ClipboardWatcher"
        )
        self._thread.start()
        log.info("Clipboard watcher started")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._check()
            except Exception:
                pass
            time.sleep(_POLL_INTERVAL)

    def _check(self):
        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
        except Exception:
            return

        if img is None:
            return

        # grabclipboard() can return a list of file paths — skip those
        if not hasattr(img, "save"):
            return

        try:
            h = _img_hash(img)
        except Exception:
            return

        if h == self._last_hash:
            return

        self._last_hash = h
        log.debug("New clipboard image: %dx%d", img.width, img.height)

        try:
            self._on_image(img.copy())
        except Exception:
            log.exception("Clipboard image callback error")
