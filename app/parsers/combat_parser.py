r"""Combat parser for the GnollGuard Companion overlay — EverQuest Legends.

MOVED HERE 2026-08-21 from devtool/dps_parser.py, at the owner's request to combine the
DPS parser with the Companion rather than keep a separate devkit tool. The parsing logic
below is carried across UNCHANGED and deliberately so: `resolve_friendly()` and the
second-person melee verbs are both MEASURED corrections to real bugs (a heal-graph that
put the raid boss on our own DPS chart; a verb list that matched 0 of the owner's own
4,038 melee lines). Rewriting them from scratch would re-introduce both.

WHAT CHANGED IN THE MOVE
  * the argparse CLI and the terminal `report()` are gone — the overlay is the readout now
  * `LiveCombat` (bottom) wraps LogParser with the snapshot API an overlay needs:
    current fight, last kill, session rollup — all cheap enough to call every 500 ms
  * nothing else. `feed(body, ts)` was already incremental, so no rewrite was needed.

⚠ LOG-FILE ONLY, and that is not negotiable. This reads the game's own log and nothing
else — no packets, no hooks, no memory. It observes; it never acts on the game.
"""
from __future__ import annotations

import collections
import os
import re
from dataclasses import dataclass, field

# ── Line shapes, all confirmed against real logs ─────────────────────────────
TS = re.compile(r"^\[(?P<ts>.{24})\]\s*(?P<body>.*)$")

# Observed verbs, in descending frequency. `hits` must come LAST in the alternation of
# any combined pattern or it shadows nothing — but as a set it is order-independent.
#
# 🔴 THESE ARE THIRD-PERSON FORMS AND THE PLAYER'S OWN LINES ARE NOT.
# EQ writes someone else's swing as "Xenthax bashes X for 48" but YOURS as "You bash X
# for 48" — base form, no -s. Measured 2026-08-21 on one session: the player's own melee
# damage lines numbered 4,038 and RX_MELEE matched **ZERO** of them. A DPS parser that
# reads every combatant except the person running it is worse than no parser, because the
# number it prints looks plausible.
#
# `smite` and `shoot` were missing outright, in either form — 387 more lines.
#
# So the alternation now accepts BOTH forms: `slash(?:es)?`. Keep it that way; adding a
# new verb means adding the STEM, not the conjugation.
MELEE_STEMS = ("slash", "crush", "bite", "smash", "bash", "hit", "kick",
               "punch", "pierce", "strike", "claw", "maul", "rend", "slice",
               "gore", "burn", "freeze", "smite", "shoot", "cleave", "backstab")
# third person adds -s or -es; "hit"/"hits" and "bash"/"bashes" both have to work.
MELEE_VERBS = tuple(v for stem in MELEE_STEMS
                    for v in (stem, stem + ("es" if stem.endswith(("s", "sh", "ch", "x", "z")) else "s")))
_VERB_ALT = "|".join(sorted(set(MELEE_VERBS), key=len, reverse=True))

RX_MELEE = re.compile(
    rf"^(?P<src>.+?) (?P<verb>{_VERB_ALT}) (?P<dst>.+?) for (?P<dmg>\d+) points? of damage\.?"
    r"(?P<mods>.*)$")

# "Aeaadyene hit a tormented spirit for 80680 points of cold damage by Gelid Claw XVIII."
RX_SPELL_DD = re.compile(
    r"^(?P<src>.+?) hit (?P<dst>.+?) for (?P<dmg>\d+) points? of (?P<type>[a-z]+) damage"
    r"(?: by (?P<spell>.+?))?\.?(?P<mods>.*)$")

# "A tormented spirit has taken 492752 damage from Scorpikis Blood Rk. II by Balsham."
# ⚠ NOTE THE ORDER: target first, SPELL second, CASTER LAST. Reading the first name as
# the attacker — the natural assumption — credits every DoT tick to the mob taking it.
#
# 🔴 THE TRAILING GROUP MUST NOT BE `(?P<src>.+?)\.?(?P<mods>.*)$`. That was the first
# version and it silently truncated every caster to ONE LETTER: `Savara` parsed as `S`,
# `Bandoran` as `B`. The `\.?` is optional and `.*` accepts anything, so the non-greedy
# `.+?` legally collapses to a single character and the rest lands in `mods`. It looked
# fine in the melee patterns only because those groups are pinned by required literals.
# Anchor `mods` to parenthesised tokens — the only thing that actually follows.
_MODS = r"(?P<mods>(?:\s*\([^)]*\))*)\s*$"
RX_DOT = re.compile(
    r"^(?P<dst>.+?) has taken (?P<dmg>\d+) damage from (?P<spell>.+?) by (?P<src>[^.]+?)\.?"
    + _MODS)

RX_NONMELEE = re.compile(
    r"^(?P<dst>.+?) was hit by non-melee for (?P<dmg>\d+) points? of damage")

RX_MISS = re.compile(
    rf"^(?P<src>.+?) tries to (?P<verb>\w+) (?P<dst>.+?), but (?P<how>.+?)!")

# "Risith healed itself for 0 (196) hit points by Curate's Channeled Mark."
# The bare number is EFFECTIVE healing; the parenthesised one is the ATTEMPTED amount.
# Overheal is the difference — reporting the attempt as healing is how a parser flatters
# a healer who is spamming into a full-health tank.
RX_HEAL = re.compile(
    r"^(?P<src>.+?) (?:healed|heals) (?P<dst>.+?) for (?P<eff>\d+)"
    r"(?:\s*\((?P<att>\d+)\))? hit points?(?: by (?P<spell>.+?))?\.?")

# ── Casting interruption ────────────────────────────────────────────────────
# 542 lines in a single session and NOTHING read them. For a class that gets bashed
# mid-cast constantly this is a real metric: how often a cast is broken, and how much
# casting time is lost. Found 2026-08-21 from the owner's own chat-window screenshots.
#
# The pair is not symmetrical and that is the point:
#   "Your Cease spell is interrupted."                        -> the cast was LOST
#   "You regain your concentration and continue your casting." -> it was SAVED
# Counting only the first overstates disruption; counting them together understates it.
# Track both, and report interrupts as lost/(lost+saved).
RX_INTERRUPT = re.compile(r"^Your (?P<spell>.+?) spell is interrupted\.")
RX_REGAIN = re.compile(r"^You regain your concentration and continue your casting\.")

RX_SLAIN = re.compile(r"^You have slain (?P<dst>.+?)!")
RX_DEATH = re.compile(r"^(?P<dst>.+?) has been slain by (?P<src>.+?)!")
RX_ZONE = re.compile(r"^You have entered (?P<zone>.+?)\.")
RX_CAST = re.compile(r"^You begin casting (?P<spell>.+?)\.")

# "Balsummonit`s pet tries to slash ..." — Legends and live both use a BACKTICK here,
# not an apostrophe. Matching only ' silently drops every pet line.
RX_PET = re.compile(r"^(?P<owner>.+?)[`'](?:s)? pet$", re.I)

# ── CHARMED PETS ─────────────────────────────────────────────────────────────
#
# Owner, 2026-08-16: *"charmed pets dps too, its tricky because it looks like an mob name
# but you would see us use a charm on the mob and it will be hitting other mobs."*
#
# Exactly right, and it is the hardest attribution problem in an EQ parser. A charmed mob
# is indistinguishable from a hostile mob BY NAME — "a Pickclaw guard" reads the same
# whether it is killing you or fighting for you. rumstil's parser lists this as a flat
# limitation ("cannot identify charmed creatures"); GamParse does not solve it either.
#
# TWO SIGNALS, and neither alone is enough:
#   1. A charm spell LANDED on it. That is the moment it changed sides.
#   2. It is now damaging things that are ALSO being damaged by the group — a hostile mob
#      does not usually attack another hostile mob.
#
# Signal 1 is authoritative but only while the charm holds; signal 2 catches the window
# after a break before the mob is re-charmed or killed. A charm BREAK ("Your charm spell
# has worn off" / it resumes hitting group members) flips it back to hostile.
#
# ⚠ Damage a charmed pet deals belongs to the CHARMER, the way a summoned pet's does.
# Counting it as a separate "player" inflates the raid list with mob names; dropping it
# entirely under-reports an enchanter by most of their contribution.
CHARM_SPELLS = re.compile(
    r"\b(charm|beguile|allure|dominate|enslave|cajole|befriend|dictate)\b", re.I)
# 🔴 THE REAL LINE IS "has been charmed", NOT "is charmed". Confirmed against the owner's
# own Plane of Sky session 2026-08-16: `a crystaline cloud has been charmed.` The original
# pattern matched neither that nor anything else in 664 charm-related lines, so every
# charmed pet's damage would have been credited to the ENEMY side.
RX_CHARM_LAND = re.compile(
    r"^(?P<dst>.+?) (?:has been|have been) (?:charmed|beguiled|enthralled|dominated|mesmerized)"
    r"|^(?P<dst2>.+?) (?:is|are) (?:charmed|beguiled|enthralled|dominated)")

# ⚠ A CAST IS NOT A CHARM. These three all mean the target is STILL HOSTILE, and treating a
# cast attempt as success would flip a live enemy onto our side and corrupt the whole fight:
#     This NPC cannot be charmed.
#     An azarack resisted your Allure IV!
#     Your Allure spell is interrupted.
# All observed in the same session. The pending-charm flag must be cleared on each.
RX_CHARM_FAIL = re.compile(
    r"cannot be charmed|resisted your |spell is interrupted|Your target resisted", re.I)

RX_CHARM_BREAK = re.compile(
    r"^(?:Your charm spell has worn off"
    r"|(?P<dst>.+?) (?:is no longer charmed|breaks free|has broken free|resumes attacking))")
# "You begin casting Allure V." then the next "<mob> is charmed" ties the two together.
RX_YOU_CAST = re.compile(r"^You begin casting (?P<spell>.+?)\.")

MOD_CRIT = re.compile(r"\((?:[^)]*\b)?Critical", re.I)
MOD_TOKENS = re.compile(r"\(([^)]+)\)")

# Skills that identify a class CONTRIBUTION. In Legends a character has several, so this
# maps to a SET of classes, never to one.
CLASS_SIGNALS = {
    "backstab": "ROG", "assassinate": "ROG",
    "bash": "TANK", "slam": "TANK", "taunt": "TANK",
    "dragon punch": "MNK", "flying kick": "MNK", "tiger claw": "MNK",
    "eagle strike": "MNK", "round kick": "MNK", "feign death": "MNK",
    "safe fall": "MNK", "hand to hand": "MNK", "mend": "MNK",
    "archery": "RNG", "double bow shot": "RNG",
    "frenzy": "BER", "axe throwing": "BER",
    "kick": "MELEE", "riposte": "MELEE", "triple attack": "MELEE",
    "disarm": "ROG", "pick lock": "ROG", "sneak": "ROG", "hide": "ROG",
    "meditate": "CASTER", "channeling": "CASTER", "conjuration": "CASTER",
    "divination": "CASTER", "evocation": "CASTER", "alteration": "CASTER",
    "abjuration": "CASTER",
}
RX_SKILLUP = re.compile(r"^You have become better at (?P<skill>[A-Za-z ]+)!")


@dataclass
class Actor:
    name: str
    is_pet: bool = False
    owner: str = ""
    dmg_melee: int = 0
    dmg_spell: int = 0
    dmg_dot: int = 0
    hits: int = 0
    misses: int = 0
    crits: int = 0
    heal_effective: int = 0
    heal_attempted: int = 0
    heal_self: int = 0          # lifetap / self-sustain, kept OUT of group healing
    dmg_taken: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    verbs: collections.Counter = field(default_factory=collections.Counter)
    spells: collections.Counter = field(default_factory=collections.Counter)
    mods: collections.Counter = field(default_factory=collections.Counter)

    @property
    def damage(self) -> int:
        return self.dmg_melee + self.dmg_spell + self.dmg_dot

    @property
    def active_secs(self) -> float:
        return max(0.0, self.last_ts - self.first_ts)

    @property
    def overheal(self) -> int:
        return max(0, self.heal_attempted - self.heal_effective)


@dataclass
class Fight:
    name: str
    start: float
    end: float = 0.0
    actors: dict = field(default_factory=dict)
    killed: bool = False
    # attacker -> targets it damaged. This is the evidence resolve_friendly() runs on;
    # allegiance is derived from attack DIRECTION, not from names or heal edges.
    targets: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(1.0, self.end - self.start)

    def actor(self, name: str) -> Actor:
        name = canon_actor(name)          # see canon_actor() - YOU / you / You are one actor
        a = self.actors.get(name)
        if a is None:
            m = RX_PET.match(name)
            a = self.actors[name] = Actor(name=name, is_pet=bool(m),
                                          owner=m.group("owner") if m else "")
        return a


def canon_actor(name: str) -> str:
    """Collapse the player's self-reference to one canonical actor.

    🔴 MEASURED BUG, 2026-08-21. EQ writes the player as "You" when you act and
    "YOU" when you are acted upon ("An ice giant magus hits YOU for 274 points..."),
    plus a lowercase "you" in other forms. Uncanonicalised these are THREE actors, and
    the damage is worse than cosmetic: the friendly seed is {"You", "you"}, so "YOU" is
    not friendly - and resolve_friendly() then marks everything that damages YOU as
    FRIENDLY, i.e. every mob hitting you joins your group.

    On an 8 MB slice of the owner's own log that was 26 of 119 fights, with Lady Vox's
    entire ice-giant escort - "An ice giant priest", "An ice giant", "A priest of
    Nagafen" - scored as our own side. This is the same class of failure the heal-graph
    version had; attack-direction resolution is only as good as knowing who "we" are.
    """
    return "You" if name.strip().lower() == "you" else name


def parse_ts(s: str) -> float:
    """EQ stamps `Sat Aug 16 01:47:06 2026`. Only the clock matters for a fight."""
    try:
        hh, mm, ss = s[11:19].split(":")
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    except Exception:
        return 0.0


class LogParser:
    """Streams a log into fights. Memory is bounded by the number of live fights, not
    by log size — a 700 MB log parses without loading it."""

    # A fight ends after this many seconds with no combat line touching it. 10s is the
    # widely used value; too short splits one mob into several fights and inflates DPS,
    # too long merges pulls and deflates it.
    IDLE_TIMEOUT = 10.0

    def __init__(self):
        self.fights: list[Fight] = []
        self.cur: Fight | None = None
        self.zone = ""
        self.skills: collections.Counter = collections.Counter()
        self.classes: collections.Counter = collections.Counter()
        self.unmatched = 0
        self.lines = 0
        self.combat_lines = 0
        # name -> charmer, for mobs currently fighting on our side
        self.charmed: dict[str, str] = {}
        self._pending_charm: str = ""      # a charm cast awaiting its landing line
        # Anyone we have seen HEAL, or be healed by, or who is our pet, is friendly.
        # Healing is the single most reliable friend signal in an EQ log: mobs do not
        # heal players, and the group heals each other constantly.
        self.friendly: set[str] = {"You", "you"}
        self.heal_edges: list[tuple[str, str]] = []   # resolved by resolve_friendly()

    # ── fight lifecycle ──────────────────────────────────────────────────────
    def _touch(self, ts: float, target: str) -> Fight:
        if self.cur is None or (ts - self.cur.end) > self.IDLE_TIMEOUT:
            self.cur = Fight(name=target or "unknown", start=ts, end=ts)
            self.fights.append(self.cur)
        self.cur.end = ts
        return self.cur

    def _hit(self, ts, fight, src, dst, dmg, kind, mods="", verb="", spell=""):
        a = fight.actor(src)
        if not a.first_ts:
            a.first_ts = ts
        a.last_ts = ts
        setattr(a, kind, getattr(a, kind) + dmg)
        a.hits += 1
        if verb:
            a.verbs[verb] += 1
        if spell:
            a.spells[spell] += 1
        if mods:
            if MOD_CRIT.search(mods):
                a.crits += 1
            for tok in MOD_TOKENS.findall(mods):
                a.mods[tok.strip()] += 1
        fight.targets.setdefault(canon_actor(src), set()).add(canon_actor(dst))
        d = fight.actor(dst)
        d.dmg_taken += dmg

    # ── the line pump ────────────────────────────────────────────────────────
    def feed(self, body: str, ts: float):
        self.lines += 1

        m = RX_SKILLUP.match(body)
        if m:
            sk = m.group("skill").strip().lower()
            self.skills[sk] += 1
            if sk in CLASS_SIGNALS:
                self.classes[CLASS_SIGNALS[sk]] += 1
            return

        # ⚠ DoT is checked BEFORE melee. "X has taken N damage from SPELL by CASTER"
        # would otherwise be mangled by a looser pattern, and its name order is inverted.
        m = RX_DOT.match(body)
        if m:
            self.combat_lines += 1
            f = self._touch(ts, m.group("dst"))
            self._hit(ts, f, m.group("src"), m.group("dst"), int(m.group("dmg")),
                      "dmg_dot", m.group("mods") or "", spell=m.group("spell"))
            return

        m = RX_MELEE.match(body)
        if m:
            self.combat_lines += 1
            f = self._touch(ts, m.group("dst"))
            self._hit(ts, f, m.group("src"), m.group("dst"), int(m.group("dmg")),
                      "dmg_melee", m.group("mods") or "", verb=m.group("verb"))
            return

        m = RX_SPELL_DD.match(body)
        if m:
            self.combat_lines += 1
            f = self._touch(ts, m.group("dst"))
            self._hit(ts, f, m.group("src"), m.group("dst"), int(m.group("dmg")),
                      "dmg_spell", m.group("mods") or "", spell=m.group("spell") or "")
            return

        m = RX_MISS.match(body)
        if m:
            f = self._touch(ts, m.group("dst"))
            a = f.actor(m.group("src"))
            a.misses += 1
            if not a.first_ts:
                a.first_ts = ts
            a.last_ts = ts
            return

        m = RX_HEAL.match(body)
        if m:
            f = self._touch(ts, m.group("dst"))
            a = f.actor(m.group("src"))
            eff = int(m.group("eff"))
            att = int(m.group("att")) if m.group("att") else eff
            a.heal_effective += eff
            a.heal_attempted += att
            if not a.first_ts:
                a.first_ts = ts
            a.last_ts = ts
            if m.group("spell"):
                a.spells[m.group("spell")] += 1
            # 🔴 DO NOT mark both as friendly here. That was the first version and it put
            # the raid boss on our DPS chart, because MOBS HEAL THEMSELVES — one
            # "Innoruuk`s Chosen healed itself" line and the boss joins your group.
            #
            # Record a heal EDGE between two DIFFERENT actors instead, and resolve
            # friendliness later by walking outward from "You". A mob healing itself is a
            # self-edge and contributes nothing; a mob healing another mob forms its own
            # cluster that never connects to us.
            # ⚠ Resolve pronouns before recording an edge. EQ writes "Rehn healed himself"
            # and "X healed itself" — 2,064 of the heal lines in a 6 MB sample resolved to
            # the literal strings "himself"/"herself"/"itself", which are not actors. Left
            # raw they create a hub node that every healer connects to, which would merge
            # every mob and every player into one component and mark the raid boss
            # friendly. They are self-heals: drop them.
            src, dst = m.group("src"), m.group("dst")
            selfheal = dst.lower() in ("himself", "herself", "itself",
                                       "themselves", "themself")
            if selfheal:
                dst = src
                # ⚠ LIFETAP IS BOTH. Owner, 2026-08-16: *"lifetap does heal, it damages
                # the mob and gives health to the caster."* So a lifetap emits a DAMAGE
                # line AND a self-heal line, and both are real: the damage belongs in DPS,
                # the heal is genuine sustain that keeps a necro or shadowknight alive.
                #
                # It must NOT count as healing OTHERS — a parser that lumps self-sustain
                # into group healing makes a lifetapping DPS look like a healer, which is
                # exactly the kind of flattery that makes a parse untrustworthy. Tracked
                # separately here, and deliberately kept out of the allegiance signal:
                # mobs lifetap too, and crediting that as friendliness is what put the
                # raid boss on our own side in the first version.
                a.heal_self += eff
            # "X healed Morbid over time for N" is a HEAL-OVER-TIME line; without this the
            # target parses as the actor "Morbid over time".
            if dst.lower().endswith(" over time"):
                dst = dst[: -len(" over time")]
            src, dst = canon_actor(src), canon_actor(dst)
            if src != dst:
                self.heal_edges.append((src, dst))
            return

        # ── charm state ──────────────────────────────────────────────────────
        m = RX_YOU_CAST.match(body)
        if m:
            if CHARM_SPELLS.search(m.group("spell")):
                self._pending_charm = "You"
            return
        # A failed/resisted/interrupted charm leaves the mob HOSTILE — clear the pending
        # flag or the next "has been charmed" line (a different mob) gets misattributed.
        if self._pending_charm and RX_CHARM_FAIL.search(body):
            self._pending_charm = ""
            return
        m = RX_CHARM_LAND.match(body)
        if m and self._pending_charm:
            dst = m.group("dst") or m.group("dst2")
            if dst:
                self.charmed[dst] = self._pending_charm
                self.friendly.add(dst)
            self._pending_charm = ""
            return
        m = RX_CHARM_BREAK.match(body)
        if m:
            dst = m.groupdict().get("dst")
            if dst:
                self.charmed.pop(dst, None)
                self.friendly.discard(dst)
            else:
                # "Your charm spell has worn off" names nobody, so we cannot know WHICH
                # pet broke. Clearing all of ours is the safe direction: mis-crediting a
                # hostile mob's damage to the player is worse than losing a few ticks.
                for k in [k for k, v in self.charmed.items() if v == "You"]:
                    self.charmed.pop(k, None)
                    self.friendly.discard(k)
            return

        m = RX_SLAIN.match(body)
        if m and self.cur:
            self.cur.killed = True
            return
        # Kills by anyone else. Without this, group kills never register and every fight
        # reports "no kill" — which is what the first run did on all 434 fights.
        m = RX_DEATH.match(body)
        if m and self.cur:
            self.cur.killed = True
            return
        m = RX_ZONE.match(body)
        if m:
            self.zone = m.group("zone")
            return

    def resolve_friendly(self) -> set[str]:
        """Decide who is on our side from WHO ATTACKS WHOM.

        🔴 THIS REPLACED A HEAL-GRAPH APPROACH THAT DID NOT WORK. Growing "friendly"
        transitively along heal edges marked 156 actors friendly INCLUDING the raid boss,
        because transitive closure is only as good as its worst edge: one lifetap parsed
        as a heal, or one mob healing a charmed mob, welds the two sides into a single
        component and the whole classification collapses. Measured, not theorised — the
        boss sat at the top of our own DPS chart.

        Attack direction is far more robust because it is ADVERSARIAL BY CONSTRUCTION:
            anything "You" damage is HOSTILE
            anything that damages a HOSTILE is FRIENDLY
            anything a FRIENDLY damages is HOSTILE
        Iterated to a fixed point. A single mis-parse shifts one actor, not the whole set,
        because each classification is re-derived from many independent attacks rather
        than inherited down a chain.

        Heals still help, but only as a tie-breaker for actors that never attacked
        anything (a pure healer), and only toward already-established friendlies.
        """
        # attacker -> set(targets), built once from every fight
        atk: dict[str, set[str]] = {}
        for f in self.fights:
            for a in f.actors.values():
                if a.damage:
                    atk.setdefault(a.name, set())
        for f in self.fights:
            for name, tset in f.targets.items():
                atk.setdefault(name, set()).update(tset)

        friendly = {n for n in ("You", "you") if n in atk or True}
        hostile: set[str] = set()
        for _ in range(6):                       # converges in 2-3; 6 is a safety bound
            before = (len(friendly), len(hostile))
            for n in list(friendly):
                hostile |= atk.get(n, set())
            hostile -= friendly                  # never let an ally flip on one bad line
            for n, tset in atk.items():
                if n in hostile:
                    continue
                if tset & hostile:
                    friendly.add(n)
            if (len(friendly), len(hostile)) == before:
                break

        # A pure healer never attacks. Adopt them only if they healed an established
        # friendly — never the reverse, or a mob lifetapping a player becomes an ally.
        for src, dst in self.heal_edges:
            if dst in friendly and src not in hostile:
                friendly.add(src)

        seen = friendly
        # Pets and charmed mobs inherit their owner's side.
        for f in self.fights:
            for a in f.actors.values():
                if a.is_pet and a.owner in seen:
                    seen.add(a.name)
        seen |= set(self.charmed)
        self.friendly = seen
        return seen

    def parse_file(self, path: str, tail_mb: float = 0.0):
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if tail_mb and size > tail_mb * 1024 * 1024:
                fh.seek(size - int(tail_mb * 1024 * 1024))
                fh.readline()
            for raw in fh:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                m = TS.match(line)
                if not m:
                    continue
                self.feed(m.group("body"), parse_ts(m.group("ts")))


# ── live-facing API ─────────────────────────────────────────────────────────
# The overlay needs three questions answered cheaply, many times a second:
#   what is happening NOW, what just died, and how has the session gone.
# LogParser already holds all of it; this only shapes it for a UI and keeps the
# fight list from growing without bound during an all-night session.

class LiveCombat(LogParser):
    """LogParser plus the snapshot calls the Companion overlay renders from."""

    # An overlay does not need every fight of a 6-hour session in memory. Keep the
    # most recent N; the full history belongs in the log, which is still on disk.
    MAX_FIGHTS = 300

    def __init__(self, me: str = "You"):
        super().__init__()
        # Whose row gets highlighted. The log writes the owner's own actions as "You",
        # so that is the default and it is correct until we learn the character name.
        self.me = me

    def feed(self, body: str, ts: float):
        super().feed(body, ts)
        if len(self.fights) > self.MAX_FIGHTS:
            del self.fights[: len(self.fights) - self.MAX_FIGHTS]

    # ── shaping ─────────────────────────────────────────────────────────────
    def _rows(self, f: Fight, friendly: set[str]) -> list[dict]:
        """One row per actor on OUR side, biggest damage first.

        ⚠ Hostiles are deliberately excluded. The mob's own damage output is not part
        of 'who helped kill it', and including it is exactly the bug resolve_friendly()
        was written to stop.
        """
        total = sum(a.damage for n, a in f.actors.items() if n in friendly) or 1
        rows = []
        for name, a in f.actors.items():
            if name not in friendly or a.damage <= 0:
                continue
            secs = a.active_secs or f.duration
            rows.append({
                "name": name,
                "is_me": name == self.me or name in ("You", "you"),
                "is_pet": a.is_pet,
                "owner": a.owner,
                "damage": a.damage,
                # both definitions, always labelled — see the module docstring
                "encounter_dps": round(a.damage / f.duration),
                "active_dps": round(a.damage / secs) if secs else 0,
                "share": a.damage / total,
                "melee": a.dmg_melee, "spell": a.dmg_spell, "dot": a.dmg_dot,
                "hits": a.hits, "misses": a.misses, "crits": a.crits,
                "heal_effective": a.heal_effective, "heal_self": a.heal_self,
                "overheal": a.overheal,
                "taken": a.dmg_taken,
            })
        rows.sort(key=lambda r: -r["damage"])
        return rows

    def _mob_name(self, f: Fight, friendly: set[str]) -> str:
        """What to CALL this fight.

        🔴 `Fight.name` is whoever was touched FIRST, which is the mob only when we
        opened. If the mob swung first the fight is named after its target - and on a
        real 8 MB sample that named 3 of the 6 most recent fights "YOU". Fine in a
        terminal dump, wrong in an overlay header.

        The mob is the HOSTILE that absorbed the most damage. Falls back to the raw
        name so a fight always has something to show.
        """
        # 1. WHAT WE HIT. Most reliable, because it does not depend on friend/foe
        #    classification at all - and that classification is known to bleed on mobs
        #    that SHARE A NAME (one charmed "An ice giant" marks every ice giant
        #    friendly; see the module docstring's known-limitations note).
        mine = f.targets.get("You") or set()
        ours = [(f.actors[t].dmg_taken, t) for t in mine
                if t in f.actors and canon_actor(t) != "You"]
        if ours:
            return max(ours)[1]
        # 2. Otherwise the hostile that absorbed the most damage.
        hostiles = [(a.dmg_taken, n) for n, a in f.actors.items()
                    if n not in friendly and a.dmg_taken > 0 and canon_actor(n) != "You"]
        if hostiles:
            return max(hostiles)[1]
        # 3. Never label a fight with the player's own name - it reads as nonsense in a
        #    header. Say we do not know instead.
        return "unknown" if canon_actor(f.name) == "You" else f.name

    def _shape(self, f: Fight | None) -> dict | None:
        if f is None:
            return None
        friendly = self.resolve_friendly()
        rows = self._rows(f, friendly)
        total = sum(r["damage"] for r in rows)
        return {
            "mob": self._mob_name(f, friendly),
            "zone": self.zone,
            "duration": f.duration,
            "killed": f.killed,
            "total": total,
            "raid_dps": round(total / f.duration),
            "rows": rows,
            # 🔴 NO MOB HP. The log never states it, so the overlay has nothing to draw a
            # health bar from. Anything shown there would be invented.
        }

    def current(self) -> dict | None:
        """The fight in progress, or None between pulls."""
        return self._shape(self.cur)

    def last_kill(self) -> dict | None:
        """The most recent fight that actually ended in a kill."""
        for f in reversed(self.fights):
            if f.killed and f is not self.cur:
                return self._shape(f)
        return None

    def session(self, limit: int = 50) -> dict:
        """Rollup for the history view. `fights` is newest-first."""
        friendly = self.resolve_friendly()
        out, best, mine, secs = [], 0, 0, 0.0
        for f in reversed(self.fights):
            if len(out) >= limit:
                break
            rows = self._rows(f, friendly)
            # A "fight" where nobody on our side dealt damage is us being hit in passing,
            # not an encounter. Showing those pushes real kills off the history.
            if not rows:
                continue
            me = next((r for r in rows if r["is_me"]), None)
            if me:
                best = max(best, me["encounter_dps"])
                mine += me["damage"]
                secs += f.duration
            out.append({
                "mob": self._mob_name(f, friendly), "duration": f.duration, "killed": f.killed,
                "my_dps": me["encounter_dps"] if me else 0,
                "my_share": me["share"] if me else 0.0,
            })
        return {
            "zone": self.zone,
            "fights": out,
            "count": len(self.fights),
            "kills": sum(1 for f in self.fights if f.killed),
            "best_dps": best,
            "avg_dps": round(mine / secs) if secs else 0,
            # interruption is tracked but has no home in the UI yet; surfaced so the
            # overlay can use it without another pass over the log
            "interrupts": getattr(self, "interrupts", 0),
        }
