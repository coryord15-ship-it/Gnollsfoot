r"""Combat parser for the GnollGuard Companion overlay — EverQuest Legends.

MOVED HERE 2026-08-21 at the owner's request, to combine the DPS parser with the
Companion rather than keep a separate tool. The parsing logic
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
               "gore", "burn", "freeze", "smite", "shoot", "cleave", "backstab",
               # "Endyr reaves a pledge familiar for 24 points of damage." -- 1,038
               # hits in older logs going uncounted. Same defect as `frenzies` and
               # `smite` before it: a real damage verb absent from the stem list.
               "reave")
# third person adds -s or -es; "hit"/"hits" and "bash"/"bashes" both have to work.
MELEE_VERBS = tuple(v for stem in MELEE_STEMS
                    for v in (stem, stem + ("es" if stem.endswith(("s", "sh", "ch", "x", "z")) else "s")))
_VERB_ALT = "|".join(sorted(set(MELEE_VERBS), key=len, reverse=True))

RX_MELEE = re.compile(
    rf"^(?P<src>.+?) (?P<verb>{_VERB_ALT}) (?P<dst>.+?) for (?P<dmg>\d+) points? of damage\.?"
    r"(?P<mods>.*)$")

# "Aeaadyene hit a tormented spirit for 80680 points of cold damage by Gelid Claw XVIII."
# 🔴 THE SAME TRAILING-GROUP BUG THE DOT PATTERN BELOW WARNS ABOUT, still live here.
# `(?P<spell>.+?)` followed by an optional `\.?` and a `.*` that accepts anything lets the
# non-greedy group collapse to ONE CHARACTER, with the rest landing in `mods`:
#     "You hit an initiate familiar for 56 points of fire damage by Flame Bolt."
#     -> spell = "F"
# Measured 2026-08-22: every spell name in the file truncated to a single letter. It hid
# for months because nothing read Actor.spells until the combat tab needed a breakdown.
# Fix is the same as RX_DOT's: forbid the dot inside the name and anchor the tail.
RX_SPELL_DD = re.compile(
    r"^(?P<src>.+?) hit (?P<dst>.+?) for (?P<dmg>\d+) points? of (?P<type>[a-z]+) damage"
    r"(?: by (?P<spell>[^.]+?))?\.?(?P<mods>(?:\s*\([^)]*\))*)\s*$")

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
# 🔴 TWO gaps found 2026-08-23 by counting damage-shaped lines no pattern claimed:
#   * "You HAVE taken 42 damage from Tainted Breath by a pledge familiar."  (613 lines)
#     — second person again, the fourth instance of that family tonight.
#   * "<X> has taken 30 damage from Sicken."                                 (12 lines)
#     — a DoT tick with NO caster named. Requiring " by <src>" dropped it entirely.
# The caster group is optional now; an unattributed tick still counts as damage taken.
RX_DOT = re.compile(
    r"^(?P<dst>.+?) ha(?:s|ve) taken (?P<dmg>\d+) damage from (?P<spell>[^.]+?)"
    r"(?: by (?P<src>[^.]+?))?\.?"
    + _MODS)

# 🔴 FRENZY WAS INVISIBLE — 1,103 hits in one 12 MB slice (993 plain, 100 Critical,
# 10 Finishing Blow). It is a Berserker SKILL and the log writes it with a preposition:
#     "Zuuluu frenzies on an initiate familiar for 84 points of damage."
# RX_MELEE expects "<verb> <target>", so "frenzies on" never matched and every point of
# Frenzy damage went uncounted. Kept as its own pattern rather than loosening RX_MELEE,
# which would let the non-greedy source group swallow a word on every ordinary swing.
# Damage we can see but cannot credit to anyone. Kept as a real actor so totals stay
# honest — a fight's damage should add up even when part of it has no owner.
# Special attacks, as distinct from ordinary weapon swings. Both land as "dmg_melee" in the
# log and in storage; this is what separates them for display.
#
# EVIDENCE, not vibes: the owner's own skill-up lines name Frenzy and Archery outright
# ("You have become better at Frenzy!" x30, "Archery" x4). kick/bash/backstab/slam are
# unambiguous EQ special attacks. Everything else -- slash, pierce, smite, strike, punch,
# crush -- is treated as a weapon swing.
# ⚠ Ambiguous verbs default to WEAPON on purpose. Over-claiming "skill damage" would flatter
# the split; under-claiming is merely incomplete, and the per-verb breakdown shows the truth
# either way.
SKILL_VERBS = {"kick", "kicks", "bash", "bashes", "frenzy", "frenzies", "backstab",
               "backstabs", "slam", "slams", "eagle strike", "tiger claw", "dragon punch",
               "flying kick", "round kick"}
RANGED_VERBS = {"shoot", "shoots"}

#: Monk Mend. The client's own string table (eqstr_us.txt) is authoritative here and gives
#: EXACTLY three outcomes -- there is no separate "poor mend" message:
#:     349  You magically mend your wounds and heal considerable damage.
#:     350  You mend your wounds and heal some damage.
#:     352  You have failed to mend your wounds.
#:
#: 🔴 NONE OF THEM STATE AN AMOUNT. Mend heals a PERCENTAGE of max health, and the log
#: carries neither the percentage nor the player's max HP. So the COUNT and the TIER are
#: measured facts; the healed HP is not derivable and must never be fabricated into a
#: healing total. Owner's own estimate, explicitly uncertain: normal ~27%, considerable
#: ~40% ("just because im not positive"). Kept as a labelled estimate, never as data.
#: A rune soaking damage that would otherwise have landed on us. Mitigation, and until now
#: completely invisible -- we counted damage dealt and damage taken, never damage PREVENTED.
RX_RUNE_ABSORB = re.compile(
    r"^You gain a rune for (?P<dmg>\d+) points? of absorption")

#: Stun lockout. Owner's swing time is ~10% of fight wall-clock, and 21,000+ stun lines were
#: unparsed -- this measures how much of that dead time is being stunned rather than choosing
#: not to swing. Paired: "You are stunned!" -> "You are no longer stunned."
#: Auto-attack state. Not a mystery line -- it is exactly when the player is and is not
#: swinging, which is the direct measurement of a question that had only been guessed at.
#: Measured across 88.7 hours: ON 76.0% of tracked time, 1,050 toggles.
RX_ATTACK_ON = re.compile(r"^Auto attack is on")
RX_ATTACK_OFF = re.compile(r"^Auto attack is off")

RX_STUN_ON = re.compile(r"^You are stunned!")
RX_STUN_OFF = re.compile(r"^You are no longer stunned")

RX_MEND = re.compile(
    r"^You (?P<good>magically )?mend your wounds and heal (?:considerable|some) damage"
    r"|^You have (?P<failed>failed to mend) your wounds")

UNATTRIBUTED = "(unattributed)"

#: Appended to a charmed pet whose name COLLIDES with the mob being fought.
#: 🔴 Owner, 2026-08-25, screenshot: his charmed pet was `an ice giant` while he was killing
#: `an ice giant`. Actors are keyed by NAME, so the pet and the target collapsed into a single
#: actor -- the pet's damage vanished into the enemy's row, and `_owner_of`'s "never treat the
#: fight's own target as a pet" guard then correctly refused to credit it to him. Both halves
#: behaved as designed and the result was still wrong.
#: The tell is unambiguous: `an ice giant -> an ice giant` appeared 77 times in one fight, and
#: a mob does not attack itself. When source and target share a name, the SOURCE is the pet.
PET_SUFFIX = " (your pet)"

RX_SKILL_ON = re.compile(
    r"^(?P<src>.+?) (?P<verb>frenzies) on (?P<dst>.+?) for (?P<dmg>\d+) points? of damage\.?"
    r"(?P<mods>.*)$")

RX_NONMELEE = re.compile(
    r"^(?P<dst>.+?) was hit by non-melee for (?P<dmg>\d+) points? of damage")

# Damage shields, which are written PASSIVELY and name the victim first:
#     "An initiate familiar is burned by YOUR flames for 5 points of non-melee damage."
#     "YOU are pierced by a pledge familiar's thorns for 7 points of non-melee damage!"
# The first is damage we DEAL and was being counted as zero; the second is damage we TAKE
# and must not be credited to us. The possessive tells them apart — "YOUR" bare, everyone
# else with "'s" - so the two arms of the alternation are what decide direction.
#
# Small but real: 226 hits for 1,220 damage over a two-day slice. Worth having because a
# shield ticks on EVERY incoming swing, so its share grows with the number of things
# hitting you - exactly the fights where the number matters most.
RX_SHIELD = re.compile(
    r"^(?P<dst>.+?) (?:is|are) (?P<verb>[a-z]+) by (?:(?P<mine>YOUR)|(?P<src>.+?)'s) "
    r"(?P<noun>[a-z]+) for (?P<dmg>\d+) points? of non-melee damage", re.I)

# 🔴 SECOND PERSON AGAIN, measured 2026-08-22. This matched only "X tries to ..." and
# the log writes YOUR misses as "You try to slash a ghoul, but miss!" — 13,571 of them in
# one 12 MB slice, every one invisible. Consequence: your own hit chance read a flat 100%
# (58 hits / 58 swings) because the denominator never saw a miss.
#
# This is the SAME defect that was fixed for melee HITS (verb stems accepting both forms)
# and it was left in the miss pattern, because nothing consumed accuracy until now.
RX_MISS = re.compile(
    rf"^(?P<src>.+?) (?:tries|try) to (?P<verb>\w+) (?P<dst>.+?), but (?P<how>.+?)!")

# "Risith healed itself for 0 (196) hit points by Curate's Channeled Mark."
# The bare number is EFFECTIVE healing; the parenthesised one is the ATTEMPTED amount.
# Overheal is the difference — reporting the attempt as healing is how a parser flatters
# a healer who is spamming into a full-health tank.
RX_HEAL = re.compile(
    r"^(?P<src>.+?) (?:healed|heals) (?P<dst>.+?) for (?P<eff>\d+)"
    # 🔴 Third instance of the same trailing-group bug (see RX_SPELL_DD above). A lifetap
    # emits a heal line, so "Vampiric Embrace" was landing in Actor.spells as "V".
    r"(?:\s*\((?P<att>\d+)\))? hit points?(?: by (?P<spell>[^.]+?))?\.?\s*$")

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
# 🔴 EVERY ALTERNATIVE HERE MUST BE ANCHORED TO *YOUR* CAST.
# Owner, 2026-08-25: *"i noticed my pet didnt come back after i recharmed it."*
# The bare `spell is interrupted` matched ANY actor's interrupted spell -- and his logs are
# full of `a glyphed sentry's Beguile spell is interrupted.` A mob fizzling its own spell in
# the ~3 seconds between his Allure cast and `<mob> has been charmed.` wiped `_pending_charm`,
# so the landing was ignored and the pet was never registered as his. In a busy fight that is
# most recharms, which is exactly the reported symptom.
# `resisted your` and `Your target resisted` were already correctly player-scoped; only the
# interrupt clause leaked.
RX_CHARM_FAIL = re.compile(
    r"cannot be charmed|resisted your |^Your .{0,40}spell is interrupted|Your target resisted",
    re.I)

# 🔴 A PET THAT NAMES ITSELF. This is how we learn about a pet we never saw charmed.
# Owner, 2026-08-25: *"we need to find away to see if i have a pet already charmed like right
# now i have 1 charmed but its not showing up because i didnt have to cast the charm spell."*
#
# The cast->landing handshake only works if the app was running and parsing when the charm
# was cast. Start the app mid-session, zone in already charmed, or re-charm outside the
# primed window and the pet is invisible -- its damage silently goes to nobody.
#
# But a pet answering a pet command addresses its OWNER and names ITSELF:
#     Innoruuk`s Chosen told you, 'Attacking Lord of Ire Master.'
# "told you" means it was directed at this player, and only a pet calls anyone Master. That
# is two independent facts in one line -- who the pet is, and that it is ours -- with no
# dependency on having seen the charm land.
#
# ⚠ Deliberately NOT matched: "Your pet prefers what it already has equipped..." confirms a
# pet exists but never names it, so it cannot attribute damage.
RX_PET_SPEAK = re.compile(
    r"^(?P<pet>.+?) tells? you, '(?P<msg>.*?(?:Master|Attacking|Following you|"
    r"Guarding|At your service|No longer taunting|Sorry).*)'", re.I)

RX_CHARM_BREAK = re.compile(
    r"^(?:Your charm spell has worn off"
    r"|(?P<dst>.+?) (?:is no longer charmed|breaks free|has broken free|resumes attacking))")
# "You begin casting Allure V." then the next "<mob> is charmed" ties the two together.
RX_YOU_CAST = re.compile(r"^You begin casting (?P<spell>.+?)\.")
#: ANY actor starting a cast. We only ever recorded our own, which is what makes proc-vs-cast
#: work for the player -- but it left every groupmate's damage unsplittable and every enemy
#: cast invisible. "You begin casting X." vs "Talenel begins casting X." -- both forms.
RX_ANY_CAST = re.compile(r"^(?P<src>.+?) begins? casting (?P<spell>.+?)\.")

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
    dmg_shield: int = 0        # damage shield ticks — small, but they are ours
    # Monk Mend, counted by outcome. Heals a % of max HP that the log never states, so these
    # are COUNTS only -- they are deliberately NOT added to any healing total.
    mend_normal: int = 0
    mend_good: int = 0
    mend_failed: int = 0
    # 🔴 "misses" conflated THREE different outcomes. Measured over 341,865 of the owner's
    # swings: 133,165 true misses, 16,323 the TARGET dodging/parrying, 389 absorbed by a
    # rune. Only the first is his accuracy; the second is the mob's avoidance and no amount
    # of +hit fixes it. One number could not answer either question.
    miss_clean: int = 0        # "but miss!"  -- our accuracy
    avoided: int = 0           # target dodged / parried / blocked / riposted
    absorbed: int = 0          # rune ate the swing entirely
    # Damage prevented by a rune on US. Mitigation we were entirely blind to.
    dmg_absorbed: int = 0
    stun_events: int = 0       # times we were stunned
    stunned_secs: float = 0.0  # measured lockout, stunned -> no longer stunned
    dmg_taken: int = 0
    best_hit: int = 0          # biggest single hit; nothing was tracking it
    # target -> [effective, attempted, casts, best single heal]. Healing totals
    # alone cannot answer "who am I having to heal, and how often" — which is the
    # question a healer actually has, and a tank-stress signal besides.
    heal_out: dict = field(default_factory=dict)
    heal_spells: collections.Counter = field(default_factory=collections.Counter)
    first_ts: float = 0.0
    last_ts: float = 0.0
    verbs: collections.Counter = field(default_factory=collections.Counter)
    spells: collections.Counter = field(default_factory=collections.Counter)
    # The counters above hold HIT COUNTS. Splitting melee into weapon-vs-skill, and spell
    # into cast-vs-proc, needs DAMAGE per verb and per spell, so they are tracked alongside
    # rather than by changing the shape of the existing counters (which several call sites
    # already read as plain counts).
    verb_dmg: collections.Counter = field(default_factory=collections.Counter)
    spell_dmg: collections.Counter = field(default_factory=collections.Counter)
    mods: collections.Counter = field(default_factory=collections.Counter)

    @property
    def damage(self) -> int:
        return self.dmg_melee + self.dmg_spell + self.dmg_dot + self.dmg_shield

    @property
    def active_secs(self) -> float:
        return max(0.0, self.last_ts - self.first_ts)

    @property
    def heal_others(self) -> int:
        """Healing that actually went to someone else.

        🔴 `heal_effective` counts every heal INCLUDING self-sustain, so a lifetapping
        DPS reads as a healer if a UI shows it raw. Measured 2026-08-23: the owner's
        92,657 "healing" was 83,941 lifetap self-heal — 91% of it. The module already
        warned about exactly this; the number just had nowhere honest to live.
        Show THIS in a healing column; keep heal_effective for the total.
        """
        return max(0, self.heal_effective - self.heal_self)

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


# The character the log belongs to. EQ writes the player as "You" when they act, but
# names them outright in some lines — "You healed Morbid for 42 hit points by Lifedraw."
# Without knowing that Morbid IS You, that parses as healing a DIFFERENT actor.
#
# 🔴 MEASURED 2026-08-23, and it is not cosmetic: on one 12 MB slice it split the owner
# into two actors, credited him with 92,657 of GROUP HEALING that is really lifetap
# self-sustain (self-heal read zero), and created 551 heal edges between him and himself.
# The module already warns that lumping self-sustain into group healing "makes a
# lifetapping DPS look like a healer" — this is how it happened anyway.
#
# Set from the log filename (eqlog_<Character>_<server>.txt) via player_name_from_log().
_PLAYER = {"name": ""}


def set_player_name(name: str):
    """Teach the parser which character IS "You". Safe to call repeatedly."""
    _PLAYER["name"] = (name or "").strip()


def player_name_from_log(path: str) -> str:
    """EQ names logs eqlog_<Character>_<server>.txt — free, and always correct."""
    m = re.match(r"eqlog_([A-Za-z]+)_([A-Za-z]+)", os.path.basename(path or ""))
    return m.group(1) if m else ""


_ARTICLE = re.compile(r"^(A|An|The) ")


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
    n = name.strip()
    low = n.lower()
    if low == "you":
        return "You"
    # the player's own character name is the same actor as "You"
    if _PLAYER["name"] and low == _PLAYER["name"].lower():
        return "You"
    # 🔴 SECOND CASING SPLIT, same class, one level up. EQ capitalises a mob when it
    # is the SUBJECT of the sentence and lowercases it as the OBJECT:
    #     "You slash a fetid fiend ..."      -> "a fetid fiend"
    #     "A fetid fiend hits YOU ..."       -> "A fetid fiend"
    # Those are one mob. Measured on the owner's log: 84 names appear in BOTH casings
    # inside a single fight ("a pledge familiar" in 60 fights), which splits one actor
    # into two - halving its damage and its damage-taken - and breaks charm lookups,
    # because self.charmed stores whichever casing the charm-landing line happened to use.
    #
    # Only the leading ARTICLE is folded. EQ mob names begin with "a"/"an"/"the"; player
    # names do not, so "Wakeblade" is never touched and no two players can collide.
    m = _ARTICLE.match(n)
    if m:
        n = m.group(1).lower() + n[len(m.group(1)):]
    return n


_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
_DAY_CACHE: dict[str, int] = {}


def parse_ts(s: str) -> float:
    """EQ stamps `Sat Aug 16 01:47:06 2026` — parse the DATE too, not just the clock.

    🔴 The first version read only `s[11:19]` on the reasoning that "only the clock
    matters for a fight". It does not. At midnight the clock falls from 86399 back to 0,
    so every duration computed across that boundary is negative by most of a day:

      * `(ts - f.end) > IDLE_TIMEOUT` is FALSE for a -86398 delta, so the fight never
        closes — it swallows everything after midnight into one encounter.
      * `duration = end - start` then goes negative and clamps to 0, and DPS is
        `damage / duration`, so the number is garbage in both directions.

    Measured 2026-08-23 on a 12 MB slice: it reported a span of **-2.2 hours**. Any
    late-night session — which is most of them — was affected.

    The date part is cached because consecutive lines almost always share it; this stays
    a few string slices per line rather than a datetime construction.
    """
    try:
        key = s[4:11] + s[20:24]
        base = _DAY_CACHE.get(key)
        if base is None:
            from datetime import date
            # 719163 = date(1970, 1, 1).toordinal(). Timezone-naive on purpose: we only
            # ever subtract two of these, so a consistent origin is all that is required.
            base = (date(int(s[20:24]), _MONTHS[s[4:7]], int(s[8:10])).toordinal()
                    - 719163) * 86400
            _DAY_CACHE[key] = base
        return base + int(s[11:13]) * 3600 + int(s[14:16]) * 60 + int(s[17:19])
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
        # Spells the player was SEEN to cast ("You begin casting X"). A damaging spell that
        # never appears here fired off an item -- that is a PROC. Measured on one 12 MB
        # slice: Smiting Strike dealt 136,850 damage over 905 hits with zero cast lines,
        # i.e. 87% of his non-melee damage was proc damage being displayed as if he had
        # cast it. Kept on the parser, not the fight: casting a spell once proves it is
        # castable for the whole session.
        self.cast_spells: set[str] = set()
        self._stun_since: float = 0.0
        # Auto-attack uptime, session-wide rather than per-fight: toggling it is a habit, and
        # the interesting figure is how much of a session it was actually enabled.
        self._attack_on = None
        self._attack_since: float = 0.0
        self.attack_on_secs: float = 0.0
        self.attack_off_secs: float = 0.0
        self.attack_toggles: int = 0
        self.cur: Fight | None = None
        # one open encounter PER MOB, so chain-pulling does not merge them
        self.open: dict[str, Fight] = {}
        # 🔴 The mob WE are swinging at, which is not the same as the last
        # encounter any line touched. Owner, 2026-08-23: "we need to parse only
        # the mob we are attacking at the time." With several encounters open at
        # once, an add that merely hits you would otherwise steal the readout
        # from the mob you are actually fighting.
        self.attacking: Fight | None = None
        self.attacking_ts: float = 0.0
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
    def _touch(self, ts: float, target: str, other: str = "") -> Fight:
        """Get (or open) the encounter for THIS mob.

        🔴 REWRITTEN 2026-08-23. The old version kept ONE current fight and started a new
        one only after IDLE_TIMEOUT of total quiet. While chain-pulling a camp there is
        never 10 idle seconds, so every kill merged into a single enormous "fight":
        measured on the owner's log, one encounter held **7 distinct hostiles over 524
        seconds**, named after whichever mob happened to be touched first.

        The damage was right; the DENOMINATOR was not. His DPS was his real damage divided
        by an entire camp session including all the downtime between pulls, which is why
        it read 36 when he knew it was higher.

        Encounters are now keyed PER MOB and time out independently.

        ⚠ Pick the mob, not the target. A line can be us hitting it or it hitting us, so
        when one side is the player the OTHER side names the encounter — otherwise every
        incoming swing opens a fight called "You".
        """
        a, b = canon_actor(target or ""), canon_actor(other or "")
        key = a
        if a == "You" and b and b != "You":
            key = b
        if not key or key == "You":
            key = a or "unknown"

        # Drop encounters that have gone quiet. Without this `open` accumulates one
        # entry per mob ever seen — 80 of them on a single 12 MB slice — and a respawn
        # would rejoin a fight that ended minutes ago.
        if len(self.open) > 24:
            for k in [k for k, v in self.open.items() if (ts - v.end) > self.IDLE_TIMEOUT]:
                del self.open[k]

        f = self.open.get(key)
        if f is None or (ts - f.end) > self.IDLE_TIMEOUT:
            f = Fight(name=key, start=ts, end=ts)
            self.fights.append(f)
            self.open[key] = f
        f.end = ts
        self.cur = f
        return f

    def close_fight(self, name: str):
        """A mob died — its encounter is over, so a respawn starts a fresh one."""
        f = self.open.pop(canon_actor(name or ""), None)
        if f is not None and f is self.attacking:
            self.attacking = None

    def _hit(self, ts, fight, src, dst, dmg, kind, mods="", verb="", spell=""):
        # every damage path lands here, so this is the one place that always knows
        # whether the swing was ours
        if canon_actor(src) == "You":
            self.attacking = fight
            self.attacking_ts = ts
        # Same name on both sides of a damage line means a charmed pet is hitting a mob that
        # shares its name. Split the SOURCE into its own actor so the two stop merging.
        if src and dst and canon_actor(src) == canon_actor(dst):
            base = canon_actor(src)
            owner = self.charmed.get(base)
            if owner:
                src = base + PET_SUFFIX
                # Register the split name too, so _owner_of folds it to the charmer instead
                # of tripping the same-name guard.
                self.charmed[src] = owner
                self.friendly.add(src)
            else:
                # Not a pet we know about. Still split it: leaving it merged would credit
                # the attacker's damage to the thing it is attacking, which is worse than an
                # unowned extra row.
                src = base + " (unknown pet)"

        a = fight.actor(src)
        if not a.first_ts:
            a.first_ts = ts
        a.last_ts = ts
        setattr(a, kind, getattr(a, kind) + dmg)
        a.hits += 1
        if dmg > a.best_hit:
            a.best_hit = dmg
        if verb:
            a.verbs[verb] += 1
            a.verb_dmg[verb] += dmg
        if spell:
            a.spells[spell] += 1
            a.spell_dmg[spell] += dmg
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
            f = self._touch(ts, m.group("dst"), m.group("src") or "")
            # 🔴 The caster group is OPTIONAL now (a tick can name no caster), so this
            # can be None. Credit it to a sentinel rather than crashing or dropping it:
            # the damage is real and the TARGET's damage-taken must still be right, we
            # just cannot say who dealt it. Never invent an attacker to fill the hole.
            # "A glyphed ghoul has taken 8 damage from YOUR Leech." — the caster IS
            # named, in the possessive, and there is no trailing "by <name>". Reading it
            # as unattributed threw away your own DoT ticks. Found 2026-08-23 by asking
            # what damage we could see but not credit: it was 12 lines, and all 12 were
            # the owner's own.
            spell_raw = (m.group("spell") or "")
            src = m.group("src")
            if not src and spell_raw.lower().startswith("your "):
                src = "You"
            src = src or UNATTRIBUTED
            self._hit(ts, f, src, m.group("dst"), int(m.group("dmg")),
                      "dmg_dot", m.group("mods") or "",
                      spell=spell_raw[5:].strip() if spell_raw.lower().startswith("your ")
                      else spell_raw)
            return

        m = RX_MELEE.match(body)
        if m:
            self.combat_lines += 1
            f = self._touch(ts, m.group("dst"), m.group("src"))
            self._hit(ts, f, m.group("src"), m.group("dst"), int(m.group("dmg")),
                      "dmg_melee", m.group("mods") or "", verb=m.group("verb"))
            return

        # Frenzy — a skill, and its own line shape. Same bookkeeping as a melee hit.

        m = RX_SKILL_ON.match(body)

        if m:

            self.combat_lines += 1

            fight = self._touch(ts, m.group('dst'), m.group('src'))

            self._hit(ts, fight, m.group('src'), m.group('dst'),

                      int(m.group('dmg')), 'dmg_melee',

                      mods=m.group('mods') or '', verb=m.group('verb'))

            return


        m = RX_SPELL_DD.match(body)
        if m:
            self.combat_lines += 1
            f = self._touch(ts, m.group("dst"), m.group("src"))
            self._hit(ts, f, m.group("src"), m.group("dst"), int(m.group("dmg")),
                      "dmg_spell", m.group("mods") or "", spell=m.group("spell") or "")
            return

        m = RX_RUNE_ABSORB.match(body)
        if m:
            (self.cur or self._touch(ts, "You", "You")).actor("You").dmg_absorbed +=                 int(m.group("dmg"))
            return

        if RX_ATTACK_ON.match(body) or RX_ATTACK_OFF.match(body):
            on = bool(RX_ATTACK_ON.match(body))
            if self._attack_since and self._attack_on is not None:
                d = max(0.0, ts - self._attack_since)
                if self._attack_on:
                    self.attack_on_secs += d
                else:
                    self.attack_off_secs += d
            if self._attack_on is not None and on != self._attack_on:
                self.attack_toggles += 1
            self._attack_on, self._attack_since = on, ts
            return

        if RX_STUN_ON.match(body):
            self._stun_since = ts
            (self.cur or self._touch(ts, "You", "You")).actor("You").stun_events += 1
            return
        if RX_STUN_OFF.match(body):
            if self._stun_since:
                a = (self.cur or self._touch(ts, "You", "You")).actor("You")
                a.stunned_secs += max(0.0, ts - self._stun_since)
                self._stun_since = 0.0
            return

        m = RX_MEND.match(body)
        if m:
            a = (self.cur or self._touch(ts, "You", "You")).actor("You")
            if m.group("failed"):
                a.mend_failed += 1
            elif m.group("good"):
                a.mend_good += 1
            else:
                a.mend_normal += 1
            return

        m = RX_SHIELD.match(body)
        if m:
            # Only OUR shield is ours. "YOU are pierced by <mob>'s thorns" is damage
            # taken — it still belongs to the fight, credited to the mob, never to us.
            src = "You" if m.group("mine") else (m.group("src") or UNATTRIBUTED)
            self.combat_lines += 1
            f = self._touch(ts, m.group("dst"), src)
            self._hit(ts, f, src, m.group("dst"), int(m.group("dmg")),
                      "dmg_shield", verb=m.group("verb"),
                      spell=(m.group("noun") or "").strip())
            return

        m = RX_MISS.match(body)
        if m:
            f = self._touch(ts, m.group("dst"), m.group("src"))
            a = f.actor(m.group("src"))
            a.misses += 1
            # Split WHY the swing did nothing -- see the Actor field comment.
            how = (m.group("how") or "").lower()
            if "absorb" in how:
                a.absorbed += 1
            elif any(w in how for w in ("dodge", "parr", "block", "riposte", "shield")):
                a.avoided += 1
            else:
                a.miss_clean += 1
            if not a.first_ts:
                a.first_ts = ts
            a.last_ts = ts
            return

        m = RX_HEAL.match(body)
        if m:
            # 🔴 Resolve the target BEFORE opening a fight on it. The raw dst can be a
            # PRONOUN ("healed himself") or carry a HoT suffix ("Zuuluu over time"), and
            # _touch() names the fight after whatever it is handed. Measured 2026-08-23:
            # 64 of 300 "fights" were one-second phantoms named `himself`, `herself`,
            # `itself` — created by nearby players self-healing, nothing to do with us.
            _d = m.group("dst")
            if _d.lower() in ("himself", "herself", "itself", "themselves", "themself"):
                _d = m.group("src")
            if _d.lower().endswith(" over time"):
                _d = _d[: -len(" over time")]
            f = self._touch(ts, canon_actor(_d))
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
            # Pronoun form, OR the same actor under two names ("You healed Morbid").
            # canon_actor folds the character name into "You", so compare canonically.
            selfheal = (dst.lower() in ("himself", "herself", "itself",
                                        "themselves", "themself")
                        or canon_actor(src) == canon_actor(dst))
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

            # 🔴 PER-TARGET LEDGER GOES HERE, NOT EARLIER. dst is only trustworthy after
            # the two rewrites above: "healed himself" resolves to the caster, and a
            # heal-over-time tick arrives as "<name> over time". Recording before them
            # produced literal targets called "himself" and "Zuuluu over time" — a healer
            # split across three phantom people. Found 2026-08-23 the first time the
            # healing view was rendered.
            _t = canon_actor(dst)
            _rec = a.heal_out.setdefault(_t, [0, 0, 0, 0])
            _rec[0] += eff
            _rec[1] += att
            _rec[2] += 1
            _rec[3] = max(_rec[3], eff)
            if m.group("spell"):
                a.heal_spells[m.group("spell").strip()] += 1
            src, dst = canon_actor(src), canon_actor(dst)
            if src != dst:
                self.heal_edges.append((src, dst))
            return

        # ── charm state ──────────────────────────────────────────────────────
        m = RX_YOU_CAST.match(body)
        if m:
            sp = (m.group("spell") or "").strip()
            if sp:
                self.cast_spells.add(sp.lower())
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
                self.charmed[canon_actor(dst)] = self._pending_charm
                self.friendly.add(dst)
            self._pending_charm = ""
            return
        # A pet speaking identifies itself and its owner without any charm handshake.
        m = RX_PET_SPEAK.match(body)
        if m:
            pet = canon_actor(m.group("pet") or "")
            if pet and pet != "You":
                self.charmed[pet] = "You"
                self.friendly.add(pet)
            return

        m = RX_CHARM_BREAK.match(body)
        if m:
            dst = m.groupdict().get("dst")
            if dst:
                self.charmed.pop(canon_actor(dst), None)
                self.friendly.discard(canon_actor(dst))
            else:
                # "Your charm spell has worn off" names nobody, so we cannot know WHICH
                # pet broke. Clearing all of ours is the safe direction: mis-crediting a
                # hostile mob's damage to the player is worse than losing a few ticks.
                for k in [k for k, v in self.charmed.items() if v == "You"]:
                    self.charmed.pop(k, None)
                    self.friendly.discard(k)
            return

        m = RX_SLAIN.match(body)
        if m:
            f = self.open.get(canon_actor(m.group("dst")))
            (f or self.cur) and setattr(f or self.cur, "killed", True)
            self.close_fight(m.group("dst"))
            return
        # Kills by anyone else. Without this, group kills never register and every fight
        # reports "no kill" — which is what the first run did on all 434 fights.
        m = RX_DEATH.match(body)
        if m:
            f = self.open.get(canon_actor(m.group("dst")))
            (f or self.cur) and setattr(f or self.cur, "killed", True)
            self.close_fight(m.group("dst"))
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
    # ── damage-kind splits ──────────────────────────────────────────────────
    # These read the per-verb and per-spell ledgers so the parts always sum back to the
    # stored total; nothing is estimated and nothing is double counted.
    @staticmethod
    def _skill_dmg(a) -> int:
        if not a:
            return 0
        return sum(d for v, d in a.verb_dmg.items() if v.lower() in SKILL_VERBS)

    @staticmethod
    def _ranged_dmg(a) -> int:
        if not a:
            return 0
        return sum(d for v, d in a.verb_dmg.items() if v.lower() in RANGED_VERBS)

    def _weapon_dmg(self, a) -> int:
        """Ordinary swings: melee total minus the special attacks and archery."""
        if not a:
            return 0
        return max(0, a.dmg_melee - self._skill_dmg(a) - self._ranged_dmg(a))

    def _proc_dmg(self, a) -> int:
        """Damage from spells we never saw cast -- i.e. fired off a weapon or item."""
        if not a:
            return 0
        return sum(d for sp, d in a.spell_dmg.items()
                   if sp and sp.lower() not in self.cast_spells)

    def _cast_dmg(self, a) -> int:
        """Direct damage from spells the player actually cast."""
        if not a:
            return 0
        # Anything in dmg_spell that the per-spell ledger cannot attribute stays here
        # rather than being silently dropped -- an unnamed spell is a cast until proven
        # otherwise, and the totals must still reconcile.
        return max(0, a.dmg_spell - self._proc_dmg(a))

    def _owner_of(self, name: str, mob: str, a: Actor) -> str:
        """Who a pet's damage belongs to, or "" if it is not anyone's pet.

        🔴 THIS IS THE WHOLE ENCHANTER PROBLEM. Owner, 2026-08-16: *"charmed pets dps
        too, its tricky because it looks like an mob name but you would see us use a charm
        on the mob and it will be hitting other mobs."* A charmed mob keeps its own name,
        so `is_pet` - which only matches "X`s pet" - never fires on one.

        Measured on the owner's own logs 2026-08-22, before this existed: **633,828 damage
        from charmed pets credited to nobody**, and on single fights the pet out-damaged
        him 4x to 32x (Bzzazzt: him 684, pet 21,931). BOTH failure modes the module comment
        warns about were live at once - the raid list carried mob names as though they were
        players, AND his own contribution was understated by most of it.

        ⚠ GUARD: never treat the fight's own target as a pet. Charm is tracked BY NAME and
        mobs share names, so a charmed "a fetid fiend" would otherwise credit the fetid
        fiend you are currently killing to you as well.
        """
        if canon_actor(name) == canon_actor(mob):
            return ""
        if a.is_pet and a.owner:                 # summoned: "Balsummonit`s pet"
            return canon_actor(a.owner)
        if name in self.charmed:                 # charmed: keyed by mob name
            return canon_actor(self.charmed[name])
        return ""

    def _rows(self, f: Fight, friendly: set[str]) -> list[dict]:
        """One row per PLAYER on our side, biggest damage first, pets folded into their owner.

        ⚠ Hostiles are deliberately excluded. The mob's own damage output is not part
        of 'who helped kill it', and including it is exactly the bug resolve_friendly()
        was written to stop.

        Pet damage is credited to the owner AND kept visible in `pets`, so the overlay can
        show "you 18,449 (+83,404 pet)" instead of either hiding the pet or listing it as a
        separate player. `own_damage` is the actor's unaided contribution.
        """
        mob = self._mob_name(f, friendly)

        # 🔴 "WHO HELPED KILL IT" IS OBSERVED, NOT INFERRED. Membership here is
        # ATTACKED-THE-TARGET, which is direct evidence, with resolve_friendly() only as a
        # fallback for someone who fought the adds instead.
        #
        # Why not trust the classifier alone: in a charm-heavy zone it is genuinely
        # fragile. Measured 2026-08-22 on a Plane of Fear pull - the player `Wakeblade`
        # dealt 45,861 damage to the scareling we killed and was classified HOSTILE,
        # because one of our charmed pets hit them (4,985 taken) and the rule "anything a
        # friendly damages is hostile" flipped them. They vanished from their own kill.
        #
        # Attacking the thing we killed cannot be faked by a stray cross-hit, so it is the
        # stronger signal for this specific question.
        helped = {src for src, tgts in f.targets.items() if mob in tgts}

        # 1) split the contributors into owners and pets
        pets: dict[str, list] = {}
        own: dict[str, Actor] = {}
        for name, a in f.actors.items():
            if a.damage <= 0:
                continue
            if name not in helped and name not in friendly:
                continue
            # 🔴 The mob being killed is never on the "who helped" list, even when
            # resolve_friendly() has marked its NAME friendly. That happens whenever you
            # charm one mob and fight another of the same name (Bzzazzt / Bazzzazzt in
            # the owner's Plane of Sky logs), and charm is tracked by name.
            #
            # Direction of the error is chosen deliberately, matching the rule already
            # stated for charm breaks: mis-crediting a HOSTILE's damage to the player is
            # worse than losing a few ticks of a same-named pet. Under-report, never
            # inflate.
            if canon_actor(name) == canon_actor(mob):
                continue
            owner = self._owner_of(name, mob, a)
            if owner:
                pets.setdefault(owner, []).append((name, a))
            else:
                own[name] = a

        # 2) a pet whose owner never appeared still has to be counted - an enchanter who
        #    only charms and never swings would otherwise vanish from their own parse.
        for owner in pets:
            own.setdefault(owner, None)

        total = sum(a.damage for a in own.values() if a)               + sum(pa.damage for lst in pets.values() for _, pa in lst)
        total = total or 1

        rows = []
        for name, a in own.items():
            mine = a.damage if a else 0
            plist = pets.get(name, [])
            pet_dmg = sum(pa.damage for _, pa in plist)
            dmg = mine + pet_dmg
            secs = (a.active_secs if a else 0) or f.duration
            rows.append({
                "name": name,
                "is_me": canon_actor(name) == canon_actor(self.me) or canon_actor(name) == "You",
                "is_pet": False,
                "owner": "",
                "damage": dmg,
                "own_damage": mine,
                "pet_damage": pet_dmg,
                "pets": [{
                    "name": pn,
                    "damage": pa.damage,
                    "charmed": pn in self.charmed,
                    "encounter_dps": round(pa.damage / f.duration),
                } for pn, pa in sorted(plist, key=lambda x: -x[1].damage)],
                # both definitions, always labelled - see the module docstring
                "encounter_dps": round(dmg / f.duration),
                "active_dps": round(dmg / secs) if secs else 0,
                "share": dmg / total,
                # 🔴 dmg_melee lumps weapon swings, special attacks and archery together;
                # dmg_spell lumps casts and item procs together. Both are one number in
                # storage and FOUR different questions on screen, so split them here from
                # the per-verb / per-spell ledgers rather than adding storage columns.
                # Split the swing outcomes: our accuracy is a different problem from their
                # avoidance, and one "hit chance" number answered neither.
                "miss_clean": a.miss_clean if a else 0,
                "avoided": a.avoided if a else 0,
                "absorbed_swings": a.absorbed if a else 0,
                "dmg_absorbed": a.dmg_absorbed if a else 0,
                "stunned_secs": a.stunned_secs if a else 0.0,
                "mend_normal": a.mend_normal if a else 0,
                "mend_good": a.mend_good if a else 0,
                "melee": self._weapon_dmg(a),
                "skill": self._skill_dmg(a),
                "ranged": self._ranged_dmg(a),
                "proc": self._proc_dmg(a),
                "spell": self._cast_dmg(a),
                "dot": a.dmg_dot if a else 0,
                "hits": a.hits if a else 0,
                "misses": a.misses if a else 0,
                "crits": a.crits if a else 0,
                "heal_effective": a.heal_effective if a else 0,
                "heal_others": a.heal_others if a else 0,
                "heal_self": a.heal_self if a else 0,
                "overheal": a.overheal if a else 0,
                "taken": a.dmg_taken if a else 0,
                "best_hit": a.best_hit if a else 0,
                # ⚠ misses are only logged for MELEE, so this is melee accuracy and
                # the UI must say so — calling it "accuracy" flat would overstate a caster.
                # 🔴 None, not 1.0, when no misses were recorded. Measured 2026-08-23:
                # OTHER players' miss lines only start appearing in the owner's logs around
                # 2026-08-16 (0-131/day before, 12k-28k/day after), so before that every
                # other actor would read a flat 100% hit chance. Zero misses means we did
                # not observe them, which is not the same as never missing — and a UI must
                # not print a number it cannot support.
                "accuracy": (a.hits / (a.hits + a.misses)) if (a and a.misses) else None,
                "crit_rate": (a.crits / a.hits) if (a and a.hits) else 0.0,
                "avg_hit": (a.damage / a.hits) if (a and a.hits) else 0.0,
                # verbs/spells/mods were counted all along and nothing ever read them
                "verbs": a.verbs.most_common(6) if a else [],
                "spells": a.spells.most_common(6) if a else [],
                "mods": a.mods.most_common(4) if a else [],
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

    def engaged(self, f: Fight) -> bool:
        """Did WE actually take part in this fight?

        Owner, 2026-08-23: "i dont want it showing me the dps to a fight that i havent
        engaged in ... like look for my auto attack to be on or something."

        Auto-attack state is the wrong signal — it can be on while you are nowhere near
        the mob that died. Participation is directly observable instead: we dealt damage,
        we took damage, or we healed in it. Anything else is somebody else's fight that
        happened within earshot, and a DPS readout for it is noise.

        ⚠ Damage-taken counts deliberately: being beaten on by an add you never hit is
        still your fight, and a tank who never lands a swing is still in it.
        """
        a = f.actors.get("You")
        if not a:
            return False
        if a.damage or a.dmg_taken or a.heal_effective:
            return True
        # our pets and charmed mobs fighting for us count as us
        for name, act in f.actors.items():
            if act.damage <= 0:
                continue
            if act.is_pet and canon_actor(act.owner) == "You":
                return True
            if name in self.charmed and canon_actor(self.charmed[name]) == "You":
                return True
        return False

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

    def healing(self, f: Fight | None = None) -> dict | None:
        """Healing shaped for a healer, not for a damage meter.

        Owner, 2026-08-23: "HPS heals per second type thing for our healers just kinda who
        youve healed and for how much and how often you are having to heal them."

        That last clause is the interesting one and no parser I know of shows it. Healing
        totals tell you how much you poured out; **heals per minute on a given target**
        tells you who is in trouble. Cross-referenced against what that target actually
        TOOK, it answers the real question: am I keeping up, and on whom.

        ⚠ HPS is computed on healing to OTHERS. Including lifetap self-sustain would put a
        necro at the top of the healing chart, which is the same flattery `heal_others`
        exists to prevent.
        """
        f = f or self.cur or (self.fights[-1] if self.fights else None)
        if f is None:
            return None
        friendly = self.resolve_friendly()
        dur = f.duration
        out = []
        for name, a in f.actors.items():
            if name not in friendly or not a.heal_effective:
                continue
            others = a.heal_others
            targets = []
            for t, (eff, att, casts, best) in a.heal_out.items():
                ta = f.actors.get(t)
                targets.append({
                    "name": t,
                    "healed": eff,
                    "casts": casts,
                    "best": best,
                    "overheal": max(0, att - eff),
                    "overheal_pct": ((att - eff) / att) if att else 0.0,
                    # 🔴 TWO DIFFERENT QUESTIONS, and one label cannot carry both.
                    # Owner, 2026-08-23: "is that how many times i basicly had to cast on
                    # him or is it the amount of hp we healed in that min". It was casts,
                    # labelled "heals/min", which reads as HP. Both are useful, so both
                    # ship, named for what they are:
                    #   casts_per_min — how OFTEN you are having to heal them
                    #   hps           — how MUCH healing per second they needed
                    #   secs_between  — the same cadence stated the way a healer feels it
                    "casts_per_min": (casts / dur * 60.0) if dur else 0.0,
                    "hps": (eff / dur) if dur else 0.0,
                    "secs_between": (dur / casts) if casts else 0.0,
                    # and what they were taking while you did it. `covered` over 1.0 means
                    # you out-healed the damage we can see; under it, you did not keep up.
                    "took": ta.dmg_taken if ta else 0,
                    "covered": (eff / ta.dmg_taken) if (ta and ta.dmg_taken) else None,
                    "is_self": canon_actor(t) == canon_actor(name),
                })
            targets.sort(key=lambda x: -x["healed"])
            out.append({
                "name": name,
                "is_me": canon_actor(name) == canon_actor(self.me) or canon_actor(name) == "You",
                "hps": round(others / dur) if dur else 0,
                "hps_total": round(a.heal_effective / dur) if dur else 0,
                "healed_others": others,
                "self_sustain": a.heal_self,
                "total": a.heal_effective,
                "overheal": a.overheal,
                "overheal_pct": (a.overheal / a.heal_attempted) if a.heal_attempted else 0.0,
                "casts": sum(t["casts"] for t in targets),
                "spells": a.heal_spells.most_common(6),
                "targets": targets,
            })
        out.sort(key=lambda r: -r["healed_others"])
        return {"mob": self._mob_name(f, friendly), "duration": dur,
                "killed": f.killed, "healers": out}

    def current(self) -> dict | None:
        """The fight in progress that WE are in, or None.

        ⚠ Gated on engaged(). Without it a scrap happening next to you becomes "your"
        live fight the moment someone nearby swings, and the DPS readout belongs to
        strangers. The owner asked for this directly.
        """
        # The mob we are ATTACKING, not merely the last one any line mentioned. Stale
        # once the encounter has run on past our last swing by more than the idle window
        # — at that point we have stopped hitting it and it is no longer "current".
        f = self.attacking
        if f is None or (f.end - self.attacking_ts) > self.IDLE_TIMEOUT:
            return None
        return self._shape(f)

    def last_kill(self) -> dict | None:
        """The most recent fight that actually ended in a kill."""
        for f in reversed(self.fights):
            if f.killed and f is not self.cur and self.engaged(f):
                return self._shape(f)
        return None

    def session(self, limit: int = 50) -> dict:
        """Rollup for the history view. `fights` is newest-first."""
        friendly = self.resolve_friendly()
        out, best, mine, secs = [], 0, 0, 0.0
        for f in reversed(self.fights):
            if len(out) >= limit:
                break
            if not self.engaged(f):
                continue
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
