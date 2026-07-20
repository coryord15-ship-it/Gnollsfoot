r"""quest_sightings.py — turn logged NPC conversations into quest sightings, and ship them
to the community DB without hammering it.

WHY THIS EXISTS
The Quest Journal's third promise is "grow the quest DB from real play". Until now the app
parsed NPC dialogue but only used it to guess item hints — nothing was ever grouped into a
conversation or submitted, so the quest database grew from nobody's play. This module is
the missing half.

Everything here is LOG-BASED. It reads the log file the game writes for the player and
nothing else — no packets, no hooks, no memory. That keeps the shipped app squarely on the
safe side of the line (see CLAUDE.md).

THE DESIGN RULE (owner constraint: "I don't want 10,000 people hitting the DB all the time")

    The client decides "is this new?" BEFORE it ever contacts the DB, and never uploads
    one row at a time.

Five things make that work:

1. CONTENT HASH AS THE ID. Every line becomes
   ``line_id = sha1(npc + "|" + normalised_text)[:12]``. Normalising strips the player's own
   name (NPCs say "Greetings, Morbid"), lowercases and collapses whitespace. Two players who
   hear the same line compute the SAME id independently — no server round-trip, no sequence
   allocation, no coordination.

2. A CACHED "ALREADY KNOWN" MANIFEST. The server publishes the ids it already has; we cache
   it and silently drop anything already known. Most of what a player hears is already known,
   so this removes the bulk of traffic before it exists.

3. A WANT-LIST. The manifest also carries the chains we're missing a reply for, so a client
   that captures a wanted piece marks it priority — the community fills our gaps instead of
   the owner hand-farming them.

4. CATEGORISE AND DROP LOCALLY. Combat barks ("Death to all who oppose the Crushbone orcs!")
   and bare greetings never get queued at all.

5. DURABLE QUEUE + BATCHED UPLOAD. Every kept line is appended to disk THE INSTANT it's seen,
   then uploaded in batches on app open, on close, and on a slow timer. Writing first means a
   crash or power cut loses nothing — the same pattern that carried the devkit through a real
   outage with zero loss. A byte offset tracks what's been sent, so replays are free.

PRIVACY: only the NPC's name, the NPC's words, and the zone are ever uploaded. The player's
own name is normalised out of the text before hashing or sending, and player chat/tells are
never touched — this reads NPC speech only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

# A conversation is "lines from one NPC around one Hail". If the player wanders off and comes
# back later, that's a new conversation rather than one giant blob.
CONVERSATION_GAP_SECONDS = 90
# Don't ship single-line scraps unless they actually offer something.
MAX_BATCH = 300

# Bracket keywords are the branch words the player repeats back: "will you [help] me?"
_BRACKET = re.compile(r"\[([^\]\[]{2,60})\]")
# Combat/aggro barks share the same log channel as quest dialogue — they are pure noise here.
_BARK = re.compile(
    r"\b(attack|attacks|die|dies|death|slay|slain|kill|destroy|blood|vengeance|"
    r"no match for|you will (pay|suffer)|prepare to|feel my|taste my)\b", re.I)
# A turn-in / reward line is worth keeping even without brackets.
# NOTE: deliberately does NOT include a bare "take this" — quest givers say "take this flask
# to Idia" as an INSTRUCTION, which is a chain step, not a reward. Requiring "here, take"
# keeps the reward sense without swallowing directions.
_REWARD = re.compile(
    r"\b(you receive|hands you|gives you|here,? take|your faction (?:standing )?(?:has )?got|"
    r"thank you|my thanks|well done|as promised)\b", re.I)


# NPCs interpolate the player's RACE as well as their name — proven in real EQL logs:
#   "hail yourself, ogre! i hope you're here to [help]..." vs the same line to a barbarian.
# Same logical line, two hashes, two rows unless we normalise race out too. MUST match
# devtool/log_quest_hails.py exactly, or app and devkit findings stop merging.
_RACES = (r"human|barbarian|erudite|wood elf|high elf|dark elf|half elf|halfling|dwarf|"
          r"troll|ogre|gnome|iksar|vah shir|froglok|drakkin")
_RACE_RE = re.compile(rf"\b({_RACES})\b", re.I)


def _norm(text: str, player: str = "") -> str:
    """Normalise a line so the same sentence hashes identically for every player.

    NPCs address you by name ("Greetings, Morbid.") AND by race ("hail yourself, ogre!") —
    left in, one quest becomes N near-identical rows, one per player name/race that heard it."""
    t = (text or "").strip()
    if player:
        t = re.sub(rf"\b{re.escape(player)}\b", "<player>", t, flags=re.I)
    t = _RACE_RE.sub("<race>", t)
    t = re.sub(r"\s+", " ", t)
    return t.lower()


def line_id(npc: str, text: str, player: str = "") -> str:
    """Stable content id — the same line from any player yields the same id."""
    basis = f"{(npc or '').strip().lower()}|{_norm(text, player)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def categorise(text: str, has_brackets: bool, is_reply: bool) -> str:
    """What kind of line is this? Drives what we keep and what we discard.

    Order matters. `is_reply` is STRUCTURAL — we know the player just repeated a bracket
    phrase, so this line is the answer — whereas the reward/bark patterns are only keyword
    guesses. Structure beats heuristics, so a reply is classified as a chain step unless it
    is unambiguously a reward. Getting this backwards left NPCs looking permanently
    incomplete, because a "missing middle" is defined as an offer with no step."""
    reward = bool(_REWARD.search(text or ""))
    if is_reply:
        return "turn_in" if reward else "quest_step"
    if reward:
        return "turn_in"
    if has_brackets:
        return "quest_offer"
    if _BARK.search(text or ""):
        return "bark"
    return "greeting"


KEEP = {"quest_offer", "quest_step", "turn_in"}


def player_from_log_path(path: str) -> str:
    """EQL names logs eqlog_<Character>_<server>.txt — that's the cheapest way to learn who
    the player is, and we need it to normalise their name out of NPC speech."""
    base = os.path.basename(path or "")
    m = re.match(r"eqlog_([A-Za-z]+)_", base)
    return m.group(1) if m else ""


class QuestSightingCollector:
    """Groups logged NPC speech into conversations and queues the quest-bearing parts.

    Fed from log_watcher callbacks. Deliberately cheap: the log watcher thread runs hot, so
    this does string work and an append — never a network call or a DB write."""

    def __init__(self, queue_path: str, player: str = "", known: set | None = None):
        self.queue_path = queue_path
        self.player = player
        self.known = known or set()      # ids the server already has → never queue them
        self.wanted = set()              # ids/NPCs we're specifically missing a reply for
        self.zone = ""
        self._npc = ""                   # NPC of the conversation in progress
        self._last_ts = 0.0
        self._awaiting_reply = False     # player just said a bracket phrase
        self._lock = threading.Lock()
        self.queued = 0
        self.skipped_known = 0
        self.dropped = 0
        os.makedirs(os.path.dirname(queue_path) or ".", exist_ok=True)

    # ── fed from the log watcher ──────────────────────────────────────────────
    def set_zone(self, zone: str) -> None:
        self.zone = (zone or "").strip()

    def on_player_say(self, text: str, ts: float | None = None) -> None:
        """`You say, '...'` — either a Hail (opens a conversation) or a bracket phrase said
        back to the NPC (meaning the NPC's next line is the answer we're usually missing)."""
        ts = ts or time.time()
        t = (text or "").strip()
        m = re.match(r"hail,?\s+(.+)", t, re.I)
        if m:
            self._npc = m.group(1).strip().rstrip(".")
            self._last_ts = ts
            self._awaiting_reply = False
            return
        # Not a hail — if we're mid-conversation, the player repeating a branch word means
        # whatever the NPC says next is a chain response.
        if self._npc and (ts - self._last_ts) <= CONVERSATION_GAP_SECONDS:
            self._awaiting_reply = True
            self._last_ts = ts

    def on_dialogue(self, npc: str, text: str, ts: float | None = None) -> None:
        """An `<NPC> says, '...'` line. Categorise, drop the noise, queue the rest."""
        ts = ts or time.time()
        npc = (npc or "").strip()
        text = (text or "").strip()
        if not npc or not text:
            return

        # New speaker (or a long gap) starts a fresh conversation.
        if npc.lower() != (self._npc or "").lower() or (ts - self._last_ts) > CONVERSATION_GAP_SECONDS:
            if npc.lower() != (self._npc or "").lower():
                self._awaiting_reply = False
            self._npc = npc
        self._last_ts = ts

        links = [b.strip() for b in _BRACKET.findall(text)]
        kind = categorise(text, bool(links), self._awaiting_reply)
        self._awaiting_reply = False      # consumed

        if kind not in KEEP:
            self.dropped += 1
            return

        lid = line_id(npc, text, self.player)
        if lid in self.known:
            self.skipped_known += 1       # server already has it — never send
            return

        self._append({
            "line_id": lid,
            "npc": npc,
            "text": _norm(text, self.player),   # player's name already stripped
            "links": links,
            "kind": kind,
            "zone": self.zone,
            "wanted": lid in self.wanted,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
        })

    # ── durable queue ─────────────────────────────────────────────────────────
    def _append(self, row: dict) -> None:
        """Write to disk immediately. Never hold a finding only in memory — a crash or power
        cut must not cost us data (the devkit learned this the hard way)."""
        try:
            with self._lock:
                with open(self.queue_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.known.add(row["line_id"])   # don't re-queue it this session either
                self.queued += 1
        except Exception:
            log.debug("sighting queue write failed", exc_info=True)

    def stats(self) -> dict:
        return {"queued": self.queued, "skipped_known": self.skipped_known,
                "dropped": self.dropped}
