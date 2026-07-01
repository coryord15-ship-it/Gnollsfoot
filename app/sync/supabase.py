"""
Supabase community sync.

CRITICAL: Every method must be completely safe when unconfigured.
If supabase_url or supabase_key are absent/empty, every method is a silent no-op.
Zero exceptions must leak to callers from this module when unconfigured.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)


class SupabaseSync:
    def __init__(self, url: str = "", key: str = ""):
        self._client = None
        self._last_sync: Optional[str] = None
        self._auth_token: Optional[str] = None
        self._configure(url, key)

    def set_auth_token(self, token: Optional[str]):
        """Call this after Discord login so API submissions carry the user's session,
        AND so direct table() queries run as the user (RLS) instead of as anon."""
        self._auth_token = token
        self._apply_postgrest_auth(token)

    def _apply_postgrest_auth(self, token):
        """Point the table()/PostgREST layer at the user's token so RLS sees
        auth.uid() = the user. Without this, supabase-py queries run as anon and
        user-scoped reads (the Quest Journal) come back empty even when logged in."""
        if not self._client or not token:
            return
        try:
            self._client.postgrest.auth(token)
        except Exception as e:
            log.debug("postgrest auth set failed: %s", e)

    def _configure(self, url: str, key: str):
        if not url or not key:
            log.debug("Supabase not configured — sync disabled")
            return
        try:
            from supabase import create_client, ClientOptions
            # flow_type="pkce" so the desktop OAuth code exchange reuses the
            # verifier supabase-py stores internally. Without it, the token
            # exchange 400s with "code challenge does not match ... verifier".
            self._client = create_client(url, key, options=ClientOptions(flow_type="pkce"))
            log.info("Supabase client initialized")
        except Exception as e:
            log.warning("Supabase init failed (continuing without sync): %s", e)
            self._client = None

    def reconfigure(self, url: str, key: str):
        self._configure(url, key)

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    @property
    def last_sync(self) -> Optional[str]:
        return self._last_sync

    # ── Community contribution ────────────────────────────────────────────────

    def contribute_item(self, item) -> bool:
        """
        Push a verified item to the community via /api/submit.
        Routes through the Next.js API (service_role key) instead of writing
        directly with the anon key — anon writes are blocked by RLS.
        Returns True on success, False on failure or unconfigured.
        """
        if not self._client:
            return False
        try:
            import requests as _req
            payload = {
                "item_name":        item.name,
                "item_level":       getattr(item, "item_level", 0),
                "description":      item.description,
                "drop_mob":         item.drop_mob,
                "drop_zone":        item.drop_zone,
                "drop_time_of_day": item.drop_time_of_day or "unknown",
                "quest_linked":     bool(item.quest_linked),
                "quest_npc":        item.quest_npc.name if item.quest_npc else None,
                "quest_reward":     item.quest_reward,
                "source_url":       item.source_url,
            }
            resp = _req.post(
                "https://gnollguard.com/api/submit",
                json=payload,
                headers=self._auth_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                log.info("Contributed item to community: %s", item.name)
                return True
            log.warning("contribute_item HTTP %s for '%s': %s",
                        resp.status_code, item.name, resp.text[:200])
            return False
        except Exception as e:
            log.warning("Supabase contribute_item failed for '%s': %s", item.name, e)
            return False

    def _auth_headers(self) -> dict:
        self._refresh_token()
        if self._auth_token:
            return {"Authorization": f"Bearer {self._auth_token}"}
        return {}

    def _refresh_token(self):
        """Keep the Bearer token fresh for the raw requests calls. Our cached token
        can go stale on a long session even though the client refreshes its own
        session, which 401s authed GET/POSTs (e.g. the Quest Journal)."""
        if not self._client:
            return
        try:
            import time as _t
            if _t.time() - getattr(self, "_last_token_refresh", 0.0) < 2400:
                return
            res = self._client.auth.refresh_session()
            sess = getattr(res, "session", None) or res
            tok = getattr(sess, "access_token", None)
            if tok:
                self._auth_token = tok
                self._apply_postgrest_auth(tok)
            self._last_token_refresh = _t.time()
        except Exception as e:
            log.debug("token refresh failed: %s", e)

    def submit_drop_report(self, item_name: str, item_level: int,
                           drop_zone: str, drop_npc: str) -> bool:
        if not self._client:
            return False
        try:
            import requests as _req
            _req.post(
                "https://gnollguard.com/api/drop-report",
                json={"item_name": item_name, "item_level": item_level,
                      "drop_zone": drop_zone, "drop_npc": drop_npc},
                headers=self._auth_headers(),
                timeout=8,
            )
            log.info("Drop report submitted: %s from %s / %s", item_name, drop_npc, drop_zone)
            return True
        except Exception as e:
            log.debug("Drop report failed: %s", e)
            return False

    def submit_vote(self, item_name: str, item_level: int,
                    vote_type: str, reason: str) -> bool:
        if not self._client:
            return False
        try:
            import requests as _req
            _req.post(
                "https://gnollguard.com/api/vote",
                json={"item_name": item_name, "item_level": item_level,
                      "vote_type": vote_type, "reason": reason},
                headers=self._auth_headers(),
                timeout=8,
            )
            log.info("Vote submitted: %s → %s (%s)", item_name, vote_type, reason)
            return True
        except Exception as e:
            log.debug("Vote submission failed: %s", e)
            return False

    def submit_inventory(self, items: list) -> bool:
        """
        Push a batch of {name, id} identity pairs (from /outputfile inventory)
        to the community via /api/inventory. Open to any logged-in user — this
        is identity data, not item authoring.
        """
        if not self._client or not items:
            return False
        try:
            import requests as _req
            _req.post(
                "https://gnollguard.com/api/inventory",
                json={"items": items},
                headers=self._auth_headers(),
                timeout=15,
            )
            log.info("Submitted %d inventory item IDs to community", len(items))
            return True
        except Exception as e:
            log.debug("Inventory submit failed: %s", e)
            return False

    def get_journal(self) -> list:
        """The logged-in user's quest journal, read directly via the authed client.
        RLS limits user_quests to this user; quests/quest_steps are public-read. The
        client manages its own token refresh, so this stays reliable on long sessions
        (the old HTTP-with-cached-token path went stale and returned empty)."""
        if not self._client:
            return []
        try:
            uq = self._client.table("user_quests").select("quest_id, status").execute()
            ids = [r["quest_id"] for r in (uq.data or [])]
            if not ids:
                return []
            quests = self._client.table("quests").select(
                "id, quest_name, zone, reward_items, faction_rewards"
            ).in_("id", ids).execute()
            steps = self._client.table("quest_steps").select(
                "quest_id, step_order, npc_name, instruction, required_items, notes"
            ).in_("quest_id", ids).order("step_order").execute()
            by_q: dict = {}
            for s in (steps.data or []):
                by_q.setdefault(s["quest_id"], []).append(s)
            out = []
            for q in (quests.data or []):
                d = dict(q)
                d["steps"] = by_q.get(q["id"], [])
                out.append(d)
            return out
        except Exception as e:
            log.warning("get_journal failed: %s", e)
            return []

    def remove_quest(self, quest_id) -> bool:
        """Remove a quest from the logged-in user's journal (delete its user_quests
        row). RLS scopes the delete to this user. Used by the journal trashcan and
        the auto-remove-on-turn-in flow."""
        if not self._client or quest_id is None:
            return False
        try:
            self._client.table("user_quests").delete().eq("quest_id", quest_id).execute()
            log.info("Removed quest %s from journal", quest_id)
            return True
        except Exception as e:
            log.warning("remove_quest failed for %s: %s", quest_id, e)
            return False

    def ping(self) -> bool:
        """Quick connectivity check. Returns True if reachable."""
        if not self._client:
            return False
        try:
            self._client.table("community_items").select("id").limit(1).execute()
            return True
        except Exception:
            return False

    # ── Pull community data on launch ────────────────────────────────────────

    def pull_community_names(self) -> dict:
        """
        Pull all items from community DB into a dict keyed by lowercase name.
        Used to populate the in-memory cache so loot lookups never hit the network.
        Returns: {item_name_lower: {item_name, description, source_url}}
        """
        if not self._client:
            return {}
        try:
            result = self._client.table("community_items").select(
                "item_name,item_level,description,source_url"
            ).execute()
            from datetime import datetime
            self._last_sync = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            return {
                row["item_name"].lower(): row
                for row in (result.data or [])
            }
        except Exception as e:
            log.warning("Supabase pull failed: %s", e)
            return {}

    def submit_auto_sold(self, rows: list) -> bool:
        """Push a batch of auto-sold loot observations to the community via
        /api/auto-sold (server attributes them to the user for distinct counts).
        Each row: {item_name, tier, quantity, price_copper, sold_for_free,
        drop_mob, drop_zone}. Requires login (Bearer token)."""
        if not self._client or not self._auth_token or not rows:
            return False
        try:
            import requests as _req
            resp = _req.post(
                "https://gnollguard.com/api/auto-sold",
                json={"observations": rows},
                headers=self._auth_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                log.info("Submitted %d auto-sold observations", len(rows))
                return True
            log.debug("submit_auto_sold HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            log.debug("submit_auto_sold failed: %s", e)
            return False

    def get_popular_items(self, limit: int = 50) -> list:
        """Most-submitted community items (drives the Items → Popular Items tab).
        Returns a list of {item_name, description, submission_count, drop_mob, drop_zone}."""
        if not self._client:
            return []
        try:
            res = self._client.table("community_items").select(
                "item_name, description, submission_count, drop_mob, drop_zone"
            ).order("submission_count", desc=True).limit(limit).execute()
            return res.data or []
        except Exception as e:
            log.debug("get_popular_items failed: %s", e)
            return []

    def pull_verified_data(self) -> dict:
        """Download community-verified items + NPCs on launch. Legacy method."""
        if not self._client:
            return {"items": [], "npcs": []}
        try:
            items = self._client.table("community_items").select("*").execute()
            from datetime import datetime
            self._last_sync = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            return {"items": items.data or [], "npcs": []}
        except Exception as e:
            log.warning("Supabase pull failed: %s", e)
            return {"items": [], "npcs": []}
