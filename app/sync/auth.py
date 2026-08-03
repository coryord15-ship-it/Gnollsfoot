"""
Discord OAuth 2.0 via Supabase Auth.

Desktop OAuth flow (PKCE):
1. Generate a PKCE code verifier + challenge.
2. Ask Supabase for a Discord OAuth URL.
3. Open the URL in the system browser.
4. Start a tiny local HTTP server on port 8788 to catch the redirect.
5. Extract the `code` parameter from the callback URL.
6. Exchange code + verifier for a Supabase session.
7. Store user info and notify the app.

Supabase setup required:
  Authentication → Providers → Discord → enable → Client ID + Secret
  Authentication → URL Configuration → add http://localhost:8788/callback
  to "Redirect URLs".
"""

import base64
import http.server
import json
import logging
import os
import threading
import time
import urllib.parse
import webbrowser
from typing import Callable, Optional

log = logging.getLogger(__name__)

_CALLBACK_PORT = 8788
_CALLBACK_PATH = "/callback"

_DISCORD_SCOPES = "identify email"

def _session_file() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "GnollGuard")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, ".session.json")


class AuthManager:
    def __init__(self, supabase_client=None):
        self._client = supabase_client
        self._user: Optional[dict] = None
        self._access_token: Optional[str] = None
        self._on_auth_change: Optional[Callable] = None

    def _save_session(self, access_token: str, refresh_token: str, user: dict):
        try:
            with open(_session_file(), "w") as f:
                # default=str so datetime fields on the user object serialize
                # instead of crashing json.dump mid-write — that crash left a
                # truncated/corrupt .session.json, so restore_session failed and
                # users got logged out on every launch.
                json.dump(
                    {"access_token": access_token, "refresh_token": refresh_token, "user": user},
                    f, default=str,
                )
        except Exception as e:
            log.debug("Could not save session: %s", e)

    def _clear_session(self):
        try:
            path = _session_file()
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _load_session(self) -> Optional[dict]:
        try:
            with open(_session_file()) as f:
                return json.load(f)
        except Exception:
            return None

    def set_auth_change_callback(self, fn: Callable):
        self._on_auth_change = fn

    def persist_refreshed_session(self, access_token: str, refresh_token: str):
        """Re-save after the client rotated the tokens underneath us.

        Supabase refresh tokens are SINGLE USE. Every background refresh mints a
        new one and kills the old, so the pair on disk must be rewritten each
        time or the next launch presents a spent token and the user is forced to
        log in again. SupabaseSync._refresh_token calls this via
        `on_session_refreshed`. Wired in main.py.
        """
        if not (access_token and refresh_token):
            return
        self._access_token = access_token
        if self._user:
            self._save_session(access_token, refresh_token, self._user)
            log.info("Saved rotated session tokens.")

    @property
    def is_logged_in(self) -> bool:
        return self._user is not None

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    @property
    def username(self) -> Optional[str]:
        if not self._user:
            return None
        meta = self._user.get("user_metadata", {})
        return (
            meta.get("full_name")
            or meta.get("global_name")
            or meta.get("name")
            or meta.get("user_name")
            or self._user.get("email")
        )

    # ── Sign in ──────────────────────────────────────────────────────────────

    def sign_in_discord(self):
        """Open Discord OAuth flow in the system browser."""
        if not self._client:
            log.warning("Cannot sign in: Supabase not configured")
            return
        threading.Thread(target=self._oauth_flow, daemon=True, name="DiscordOAuth").start()

    def _oauth_flow(self):
        try:
            redirect_uri = f"http://localhost:{_CALLBACK_PORT}{_CALLBACK_PATH}"

            # Let supabase-py own the entire PKCE lifecycle: it generates the
            # code verifier, stores it, and reuses it in exchange_code_for_session.
            # Hand-rolling our own verifier/challenge here caused the token
            # endpoint to 400 with "code challenge does not match previously
            # saved code verifier". The client is created with flow_type="pkce".
            resp = self._client.auth.sign_in_with_oauth({
                "provider": "discord",
                "options": {
                    "redirect_to": redirect_uri,
                    "scopes": _DISCORD_SCOPES,
                },
            })

            auth_url = getattr(resp, "url", None)
            if not auth_url:
                log.error("Supabase did not return an OAuth URL — is Discord provider enabled?")
                return

            code_holder: list = []
            server = _CallbackServer(_CALLBACK_PORT, code_holder)
            server_thread = threading.Thread(target=server.handle_request, daemon=True)
            server_thread.start()

            log.info("Opening browser for Discord sign-in")
            webbrowser.open(auth_url)

            server_thread.join(timeout=300)

            if not code_holder:
                log.warning("OAuth callback not received within timeout")
                return

            session = self._client.auth.exchange_code_for_session({
                "auth_code": code_holder[0],
            })
            if session and session.user:
                self._user = session.user.model_dump()
                self._access_token = session.session.access_token if session.session else None
                if session.session and session.session.refresh_token:
                    self._save_session(self._access_token, session.session.refresh_token, self._user)
                log.info("Signed in as: %s", self.username)
                if self._on_auth_change:
                    self._on_auth_change()
            else:
                log.error("OAuth exchange returned no user")

        except Exception as e:
            log.error("Discord sign-in failed: %s", e)

    # ── Sign out ─────────────────────────────────────────────────────────────

    def sign_out(self):
        if not self._client:
            return
        try:
            self._client.auth.sign_out()
        except Exception as e:
            log.error("Sign-out failed: %s", e)
        finally:
            self._user = None
            self._access_token = None
            self._clear_session()
            if self._on_auth_change:
                self._on_auth_change()

    # ── Session restore ──────────────────────────────────────────────────────

    @staticmethod
    def _token_expired(access_token: str) -> bool:
        """True if the JWT's exp is in the past (or unreadable)."""
        try:
            payload = access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
            return bool(exp) and exp <= time.time()
        except Exception:
            return True     # unreadable -> treat as expired and refresh

    def restore_session(self):
        """Attempt to restore a saved auth session on launch.

        FIXED 2026-08-03 — users were being logged out on every launch.

        Three separate faults, all silent:

        1. `set_session(access, refresh)` was called even when the access token
           had ALREADY EXPIRED. supabase-py does not reliably refresh from an
           expired access token, and returns a session object with no `user`
           instead of raising — so `if session and session.user` was simply
           false and the whole function did nothing at all. No exception, no
           log, no restore. We now check `exp` and call refresh_session()
           directly when it is stale.

        2. Failures logged at DEBUG, which is off in shipped builds. Anything
           that loses a login is at least WARNING.

        3. `_clear_session()` on ANY exception. A network blip on launch
           therefore DELETED a perfectly good refresh token and forced a real
           re-login. We now only clear when the server explicitly rejects the
           grant; transient errors leave the file alone so the next launch can
           retry.
        """
        if not self._client:
            return
        saved = self._load_session()
        if not saved:
            return
        access = saved.get("access_token") or ""
        refresh = saved.get("refresh_token") or ""
        if not refresh:
            log.warning("Saved session has no refresh token; cannot restore.")
            return

        session = None
        try:
            if access and not self._token_expired(access):
                session = self._client.auth.set_session(access, refresh)
            else:
                # Stale (the normal case — access tokens last ~1h, launches don't).
                log.info("Access token expired; refreshing from the stored refresh token.")
                session = self._client.auth.refresh_session(refresh)
        except Exception as e:
            msg = str(e).lower()
            fatal = any(k in msg for k in
                        ("invalid_grant", "invalid refresh", "already used",
                         "not found", "revoked", "expired"))
            if fatal:
                log.warning("Refresh token rejected (%s); clearing saved session.", e)
                self._clear_session()
            else:
                # Offline / DNS / 5xx — keep the token and try again next launch.
                log.warning("Could not restore session (transient, keeping it): %s", e)
            return

        if not (session and getattr(session, "user", None)):
            # The silent path that caused the bug. Say so loudly instead.
            log.warning("Session restore returned no user; staying logged out. "
                        "Saved token kept for the next attempt.")
            return

        self._user = session.user.model_dump()
        self._access_token = session.session.access_token if session.session else None
        if session.session and session.session.refresh_token:
            self._save_session(self._access_token, session.session.refresh_token, self._user)
        log.info("Session restored for: %s", self.username)


# ── Minimal callback HTTP server ──────────────────────────────────────────────

_CALLBACK_HTML = """\
<!DOCTYPE html><html><head><title>Gnoll Guard</title>
<style>body{background:#0D0A0B;color:#E8E0D0;font-family:sans-serif;
text-align:center;padding-top:80px;}h1{color:#C8960C;}</style></head>
<body><h1>Signed in!</h1>
<p>You can close this tab and return to Gnoll Guard.</p></body></html>
"""

_ERROR_HTML = """\
<!DOCTYPE html><html><head><title>Gnoll Guard</title>
<style>body{background:#0D0A0B;color:#E8E0D0;font-family:sans-serif;
text-align:center;padding-top:80px;}h1{color:#FFA726;}</style></head>
<body><h1>Sign-in failed</h1>
<p>Return to Gnoll Guard and try again.</p></body></html>
"""


class _CallbackServer(http.server.HTTPServer):
    def __init__(self, port: int, code_holder: list):
        self._code_holder = code_holder

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                if code:
                    self.server._code_holder.append(code)
                    body = _CALLBACK_HTML.encode()
                else:
                    body = _ERROR_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("localhost", port), Handler)
        self._code_holder = code_holder
        # super().__init__ already calls server_bind + server_activate by default;
        # do NOT call them again or the second bind raises OSError.

    def handle_request(self):
        self.handle_request = self._handle_one
        self._handle_one()

    def _handle_one(self):
        try:
            self._BaseServer__is_shut_down.clear()
            self._handle_request_noblock()
        except Exception:
            pass
        finally:
            self.server_close()
