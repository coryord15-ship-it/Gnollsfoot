"""Anonymous launch headcount.

The ONLY thing this does is let us count how many people run the app. On first
launch it generates a random install id (a UUID with no connection to the user,
their character, their account, or anything personal) and stores it locally. On
each launch it sends that id plus the app version and OS name to the website's
/api/ping endpoint.

What is NEVER sent or stored: character names, the log path, account info, email,
IP (the server does not persist it), or anything a person could be identified by.
The install id is random and means nothing outside this counter.

Everything here is best-effort and completely silent: it runs on a background
thread, times out fast, and swallows every error. If the network is down, the app
does not notice or care. It must never block startup or raise into the app.
"""
import json
import logging
import os
import platform
import threading
import urllib.request
import uuid

log = logging.getLogger(__name__)

PING_URL = "https://gnollguard.com/api/ping"


def _install_id_path() -> str:
    r"""The random install id lives next to the user's settings in
    %APPDATA%\GnollGuard\ — the same per-user, out-of-repo location as everything
    else personal. One file, one line, one UUID."""
    user_dir = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"), "GnollGuard"
    )
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, "install_id")


def get_or_create_install_id() -> str:
    """Return this install's random id, creating it on first run. Any failure just
    yields a throwaway id for this session rather than breaking anything."""
    path = _install_id_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        new_id = str(uuid.uuid4())
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_id)
        return new_id
    except Exception:
        return str(uuid.uuid4())


def _do_ping(version: str) -> None:
    try:
        payload = json.dumps({
            "install_id": get_or_create_install_id(),
            "app_version": version,
            "os": platform.system(),   # "Windows" — the OS name only, nothing more
        }).encode("utf-8")
        req = urllib.request.Request(
            PING_URL, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass   # silent by design — a headcount is never worth a log line the user sees


def ping_async(version: str) -> None:
    """Fire the launch ping on a daemon thread and return immediately."""
    try:
        threading.Thread(target=_do_ping, args=(version,), daemon=True).start()
    except Exception:
        pass
