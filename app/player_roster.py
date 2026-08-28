"""Tell other PLAYERS apart from NPCs, so their chat never leaves this machine.

WHY THIS EXISTS
---------------
The journal reads logs only. Its dialogue pattern is

    (?P<npc>[\\w ]+) says(?:, '| ')(?P<text>.+?)'?$

and there is nothing in an EQ log line that marks `Helga says, 'yeah'` as a
player and `Doug says, 'Hail and well met.'` as an NPC. They are the same shape.

On 2026-08-04, driving the real QuestSightingCollector showed this reaching the
server: if another player speaks, you `/say` something, and they speak again
inside CONVERSATION_GAP_SECONDS, `_awaiting_reply` is still set, `categorise()`
returns `quest_step`, and the row is queued with the speaker stored VERBATIM.
`_norm()` only strips the LOCAL user's own character names, so another player's
name and their whole message went up untouched. One night's log contained ten
such lines from five real players.

    Aevus says, 'howdyyall'
    Helga says, 'could i join you guys ? im after the mace as well'

Nothing had actually leaked yet (0 of 124 uploaded rows), so this is preventive.

THE SIGNAL THAT WORKS
---------------------
NPCs only ever `say`. **Players have channels NPCs cannot use** — tells, guild,
group, shout, OOC, auction. That is decisive and it is all in the log. One
night yielded 92 players and 165 NPCs.

⚠ AND THE ONE THAT DOES NOT: `/consider`.
`X regards you indifferently` looks like a perfect NPC marker and is not — you
can consider anyone. `Blend`, `Helga` and `Funia` are all confirmed guildmates
and all appear as "regards you". Six names landed in both sets and consider
caused every one. Using it would have promoted real players onto the publish
list, which is the exact failure this module prevents. Do not add it back.

TRUNCATIONS MATTER AS MUCH AS THE ROSTER
----------------------------------------
Decoded/garbled names arrive shortened: `ntis`/`antis`/`agantis`/`ragantis` are
all one guild member's name. `hiad` is that player's full name — and `hiad` is the
name that "said" *"Anyone around level 14 want to group?"*. A plain roster
lookup lets all four through, so suffix/prefix matching is part of the check,
not a refinement of it.

TWO GATES, DELIBERATELY DIFFERENT
---------------------------------
`is_player()`  — used for LOCAL processing (quest matching, in memory). Drops
                 only names we can PROVE are players. Being wrong here just
                 loses a quest hint; nothing leaves the machine.
`may_publish()` — used at the UPLOAD boundary. DENY BY DEFAULT: a name ships
                 only with positive NPC evidence. Being wrong here publishes a
                 real person, so it fails closed.

Filtering hard at the point of transmission is the same boundary `/api/submit-log`
already relies on.

THE ROSTER NEVER LEAVES THIS MACHINE
------------------------------------
It is literally a list of real player names — uploading it would be the same
leak wearing a different hat. It is held in memory, optionally cached to the
local config dir, and is never serialised into any outbound payload.

Spec: the player-vs-NPC identification spec
"""
from __future__ import annotations

import glob
import io
import json
import logging
import os
import re
import threading

log = logging.getLogger(__name__)

# Channels an NPC can never use. Seeing a name here proves it is a player.
_PLAYER_PATTERNS = [
    re.compile(r"^(?P<n>[A-Za-z_'`]+) tells (?:you|the group|the guild|your guild|the raid)", re.I),
    re.compile(r"^You told (?P<n>[A-Za-z_'`]+)", re.I),
    re.compile(r"^(?P<n>[A-Za-z_'`]+) says out of character", re.I),
    re.compile(r"^(?P<n>[A-Za-z_'`]+) auctions", re.I),
    re.compile(r"^(?P<n>[A-Za-z_'`]+) shouts", re.I),
    re.compile(r"^(?P<n>[A-Za-z_'`]+) has joined the group", re.I),
    re.compile(r"^(?P<n>[A-Za-z_'`]+) has been added to your group", re.I),
]

# Positive NPC evidence. Deliberately does NOT include /consider — see module docstring.
_HAIL = re.compile(r"^You say,?\s*'Hail,?\s+(?P<n>.+?)'\s*$", re.I)
_SLAIN = re.compile(r"^You have slain (?P<n>.+?)!", re.I)

# "a rock golem", "an elemental warrior", "the Ancient one" — sufficient, never necessary.
_ARTICLE = re.compile(r"^(a|an|the)\s+", re.I)

# Pets carry owner-derived names and are slain constantly; never let one become NPC evidence.
_PET = re.compile(r"\b(pet|familiar|warder|skeleton pet)\b", re.I)

# Zone codes the packet decoder emits as "speakers" (hole, paineel, soldungb…).
# All-lowercase, no spaces — a real NPC name is capitalised or carries an article.
_ZONE_CODE = re.compile(r"^[a-z][a-z0-9]{2,}$")

_MIN_TRUNCATION = 3     # shorter than this is too ambiguous to match on


def _clean(name: str) -> str:
    return (name or "").strip().strip("'`\"").rstrip(".")


def player_names_from_log_dir(log_dir: str) -> set:
    """Every character THIS user plays, read from `eqlog_<Character>_<server>.txt`.

    Free and exact. The watcher tails the whole folder, so all of the user's
    alts must be known or a line heard on a second character leaks that name.
    """
    out = set()
    if not log_dir or not os.path.isdir(log_dir):
        return out
    for p in glob.glob(os.path.join(log_dir, "eqlog_*.txt")):
        m = re.match(r"eqlog_([A-Za-z]+)_", os.path.basename(p))
        if m:
            out.add(m.group(1).lower())
    return out


class PlayerRoster:
    """Learns who is a player from the log, and gates dialogue on the way out.

    Thread-safe: the log watcher thread calls observe() on every line while the
    UI thread may query it.
    """

    def __init__(self, cache_path: str | None = None):
        self._players: set[str] = set()
        self._npcs: set[str] = set()
        self._lock = threading.Lock()
        self._cache_path = cache_path
        self.dropped_players = 0      # dialogue suppressed because the speaker is a player
        self.dropped_unknown = 0      # dialogue not published for lack of NPC evidence
        self._load()

    # ── learning ─────────────────────────────────────────────────────────────
    def add_local_characters(self, log_dir: str) -> int:
        names = player_names_from_log_dir(log_dir)
        with self._lock:
            before = len(self._players)
            self._players |= names
            return len(self._players) - before

    def observe(self, line: str) -> None:
        """Feed every timestamped log line. Cheap: a few anchored regexes."""
        if not line:
            return
        # strip the "[Mon Aug 03 20:46:40 2026] " prefix if present
        body = line
        if body.startswith("["):
            close = body.find("] ")
            if close != -1:
                body = body[close + 2:]

        for rx in _PLAYER_PATTERNS:
            m = rx.match(body)
            if m:
                n = _clean(m.group("n")).lower()
                if n:
                    with self._lock:
                        self._players.add(n)
                        self._npcs.discard(n)   # player evidence always wins
                return

        m = _HAIL.match(body)
        if m:
            n = _clean(m.group("n")).lower()
            if n and n not in self._players:
                with self._lock:
                    self._npcs.add(n)
            return

        m = _SLAIN.match(body)
        if m:
            n = _clean(m.group("n")).lower()
            # Pets are slain constantly and are named after their owner.
            if n and not _PET.search(n) and n not in self._players:
                with self._lock:
                    self._npcs.add(n)

    # ── verdicts ─────────────────────────────────────────────────────────────
    def is_player(self, name: str) -> bool:
        """PROVEN a player: exact roster hit, or a truncation of one.

        Used for local, in-memory processing. False here costs a quest hint.
        """
        n = _clean(name).lower()
        if not n:
            return False
        with self._lock:
            if n in self._players:
                return True
            if len(n) < _MIN_TRUNCATION:
                return False
            # `ntis` -> `<fullname>`, `rbid` -> `<yourname>`, `hiad` -> `<fullname2>`
            for p in self._players:
                if p != n and len(p) > len(n) and (p.endswith(n) or p.startswith(n)):
                    return True
        return False

    def is_zone_code(self, name: str) -> bool:
        """`hole`, `paineel`, `soldungb` — decoder noise, not a name at all."""
        n = _clean(name)
        return bool(n) and " " not in n and bool(_ZONE_CODE.match(n))

    def has_npc_evidence(self, name: str) -> bool:
        n = _clean(name).lower()
        if not n:
            return False
        if _ARTICLE.match(n):
            return True
        with self._lock:
            return n in self._npcs

    def may_publish(self, name: str) -> bool:
        """DENY BY DEFAULT — the gate for anything leaving this machine.

        Ships only with positive NPC evidence. Unknown means no.
        """
        n = _clean(name)
        if not n:
            return False
        if self.is_zone_code(n):
            self.dropped_unknown += 1
            return False
        if self.is_player(n):
            self.dropped_players += 1
            return False
        if self.has_npc_evidence(n):
            return True
        self.dropped_unknown += 1
        return False

    def looks_truncated(self, name: str) -> str | None:
        """A name that is a proper suffix of a known one means the decoder read
        from a wrong offset. Useful as a health metric, not just a drop."""
        n = _clean(name).lower()
        if len(n) < _MIN_TRUNCATION:
            return None
        with self._lock:
            for p in self._players | self._npcs:
                if p != n and len(p) > len(n) and (p.endswith(n) or p.startswith(n)):
                    return p
        return None

    # ── stats / persistence ──────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            return {
                "players": len(self._players),
                "npcs": len(self._npcs),
                "dropped_players": self.dropped_players,
                "dropped_unknown": self.dropped_unknown,
            }

    def _load(self) -> None:
        if not self._cache_path or not os.path.exists(self._cache_path):
            return
        try:
            with io.open(self._cache_path, encoding="utf-8") as fh:
                d = json.load(fh)
            self._players = {str(x).lower() for x in d.get("players", [])}
            self._npcs = {str(x).lower() for x in d.get("npcs", [])}
        except Exception:
            log.debug("roster cache unreadable; starting empty", exc_info=True)

    def save(self) -> None:
        """Cache locally so a fresh session starts knowing who is who.

        ⚠ LOCAL ONLY. This file is a list of real player names. It must never be
        uploaded, attached to a report, or included in a diagnostic bundle.
        """
        if not self._cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self._cache_path) or ".", exist_ok=True)
            tmp = self._cache_path + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as fh:
                with self._lock:
                    json.dump({"players": sorted(self._players),
                               "npcs": sorted(self._npcs)}, fh, indent=0)
            os.replace(tmp, self._cache_path)
        except Exception:
            log.debug("could not save roster cache", exc_info=True)
