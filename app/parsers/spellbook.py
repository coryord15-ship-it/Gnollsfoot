"""Read the game's own `spells_us.txt` and work out what buffs stack.

WHERE THIS DATA COMES FROM
    The EverQuest client ships every spell in the game as a caret-delimited text file next to
    the logs we already read. This module reads that file and nothing else -- same category as
    `Currencies.txt` and the inventory dumps. No network, no packet capture, no hooking. It
    also means the data always matches the patch the player is actually running.

THE COLUMN MAP IS MEASURED, NOT GUESSED
    The file has 173 unlabelled fields. The map below was solved on 2026-08-31 by joining the
    file against 21,636 spells whose values were already known and asking which column
    reproduces each one -- mana, cast time, recast, range and both duration fields each matched
    a single column at 100.00%, and the 16-wide class block at 99.998%. Full derivation and the
    evidence for the columns with no such counterpart:
    GnollLoot-docs/knowledge/23-spellfile-decoded.md

    🔴 The file's layout is NOT guaranteed stable across patches -- the 2026-08-17 patch
    replaced it. `verify()` below re-checks the anchors against the file itself and returns
    False if it no longer looks right, and the UI says so rather than showing wrong buffs.
"""
from __future__ import annotations

import collections
import io
import logging
import os

log = logging.getLogger(__name__)

CLASSES = ["WAR", "CLR", "PAL", "RNG", "SHD", "DRU", "MNK", "BRD",
           "ROG", "SHM", "NEC", "WIZ", "MAG", "ENC", "BST", "BER"]

# Solved column positions. See the module docstring before changing any of these.
C_ID, C_NAME = 0, 1
C_RANGE, C_AOE = 4, 5
C_CAST, C_RECAST = 8, 10
C_DUR_FORMULA, C_DUR_VALUE = 11, 12
C_MANA = 14
C_GOOD, C_RESIST, C_TARGET, C_SKILL = 28, 29, 30, 32
C_CLASS0 = 36                      # 36..51, one per entry in CLASSES
C_GROUP, C_RANK = 133, 134
C_SONG_CAP = 128
C_EFFECTS = 172

# 🔴 Bard songs are NOT identified by the skill column. `skill == 40` ("Singing" in the general
# EQ skill list) matches ZERO rows in this file -- Chant of Battle reads 70, Hymn of Restoration
# 49, Anthem de Arms 41. The signal that works is the bard-only column 128: non-zero on 1,461
# rows, 94.2% of them bard-castable. Measured 2026-08-31.

# Target types a buff can land through. Derived from the spells carrying each value, not from
# a remembered enum -- an earlier pass took 41 to be "Pet Owner" when it is GROUP, which covers
# 1,047 spells including every Pack song.
TARGETS = {
    3: "Group", 41: "Group", 40: "Group",
    5: "Single", 51: "Single", 52: "Single", 56: "Single",
    6: "Self", 14: "Pet", 46: "Pet",
}

# What each effect id does. Only ones whose meaning is settled; anything else shows as its
# number. Several were confirmed directly while decoding -- 3 (Spirit of Wolf caps at 55),
# 11 (Alacrity's 122 = 22% haste), 15 (Clarity caps at 9/tick), 69+79 (Symbol of Transal
# raises max HP and heals the same amount).
SPA_NAMES = {
    0: "Hit Points", 1: "AC", 2: "Attack", 3: "Run Speed",
    4: "STR", 5: "DEX", 6: "AGI", 7: "STA", 8: "INT", 9: "WIS", 10: "CHA",
    11: "Haste", 12: "Invisibility", 13: "See Invisible", 14: "Water Breathing",
    15: "Mana Regen", 28: "Invis vs Undead", 46: "Fire Resist", 47: "Cold Resist",
    48: "Poison Resist", 49: "Disease Resist", 50: "Magic Resist",
    55: "Rune (absorb)", 56: "True North", 57: "Levitate", 58: "Illusion",
    59: "Damage Shield", 69: "Max HP", 74: "Feign Death", 79: "Heal",
    97: "Max Mana", 100: "Regen", 108: "Familiar", 111: "All Resists",
    85: "Melee Proc", 114: "Aggro Mod", 124: "Spell Damage", 127: "Spell Haste",
    148: "Blocks", 149: "Overwrites", 161: "Spell Mitigation", 162: "Melee Mitigation",
}

# 🔴 A judgement call, not a measurement. These weights decide which buff wins a slot when
# two are both castable; they are one person's ranking of what matters and the UI says so.
# Grouped so the optimiser prefers COVERING a new kind of benefit over stacking more of one
# it already has -- six buffs that all add stats are worse than five plus a haste.
SPA_WEIGHT = {
    11: 6.0,    # haste — the single biggest melee multiplier
    15: 5.0,    # mana regen
    69: 4.0,    # max HP
    97: 3.0,    # max mana
    1: 2.5,     # AC
    100: 2.5,   # regen
    2: 2.0,     # attack
    59: 1.5,    # damage shield
    111: 1.5,   # all resists
    127: 1.5,   # spell haste
    3: 1.0,     # run speed
}
STAT_SPAS = (4, 5, 6, 7, 8, 9)          # STR..WIS — useful, individually small
RESIST_SPAS = (46, 47, 48, 49, 50)
UTILITY_SPAS = (12, 13, 14, 28, 56, 57, 74, 108)

# 🔴 SPA 58 is Illusion and its "base" is a MODEL id, not a magnitude — 142 means a gnome, not
# +142 of anything. Never score it.
#
# 🔴 HEALING IS NOT A BUFF. Owner, 2026-08-31: *"heals wouldnt go on the quick buff now would
# they?"* — correct, and the first cut had Blooming Heal, Celestial Elixir and Sloths Healing
# taking slots. SPA 0 (hit points) and SPA 79 (the instant-heal half of a max-HP buff) are heal
# components; a Quick Buff slot is for something you pre-load and leave up, so they score
# nothing. SPA 100 (regen) is deliberately NOT here — a regen BUFF like Regeneration is a real
# buff; what disqualifies a heal-over-time is its short duration, which MIN_BUFF_TICKS handles.
#
# 🔴 SPA 55 is a RUNE. Owner, 2026-08-31: *"we can ignore runes too fyi they are temoporay"* —
# a rune absorbs a fixed pool of damage and then pops, so it is a consumable, not something
# that improves your stats for a period. Same reasoning that removed heals. Note this does NOT
# apply to SPA 59, a damage shield, which runs for its whole duration and is not used up.
#
# 🔴 VISION AND UTILITY are not worth a gem. Owner, 2026-08-31: *"i dont think vision
# enhancements are worth it"*. See Invisible, Invis vs Undead, Levitate, True North, Water
# Breathing, Familiar and Feign Death are things you cast when you need them, not things you
# burn a permanent Quick Buff slot on. They still show in a buff's effect list if it has one
# alongside real stats — they just do not earn the slot.
IGNORE_SPAS = (58, 0, 79, 55) + UTILITY_SPAS

#: A Quick Buff slot is wasted on anything short. 30 ticks = 3 minutes. This is what separates
#: a regen BUFF from a heal-over-time, without having to guess a spell's intent from its name.
MIN_BUFF_TICKS = 30

#: Entries in the file that are not memorisable spells. `Illusion:` is excluded because an
#: illusion is not something anyone loads a Quick Buff gem with (they also overwrite each other
#: and change your model); `Item Benefit:` and `Spell:` are item clickies and spell scrolls.
#: `Ancient:` is deliberately NOT here — those are real, and Ancient: Gift of Aegolism is the
#: best buff in the game for the slot it fills.
SKIP_PREFIXES = ("Illusion:", "Item Benefit:", "Spell:", "BetaTestSpell")

_EQ_DEFAULT = ("C:/Users/Public/Daybreak Game Company/Installed Games/"
               "EverQuest Legends")

#: 🔴 EverQuest Legends caps at 50. Owner, 2026-08-31: *"level 50 pally level 60 doesnt exisit
#: yet"*. `spells_us.txt` ships every spell in EverQuest's HISTORY, so planning at 60 pulled in
#: things that cannot exist here — Austerity is a level-55 paladin buff and was being handed out
#: as a recommendation. Same trap as the imported wiki data describing classic EQ rather than
#: Legends.
LEVEL_CAP = 50


def eq_roots(config=None) -> list:
    """Every directory that might BE the player's EverQuest install, best guess first.

    🔴 Uses the app's own `log_discovery`, which already resolves this from config and
    from where the game actually writes. Do NOT re-implement it here: a second copy of
    "where is EverQuest" drifts from the first, and the developer path that used to be
    hardcoded in this file would have shipped to every user whose game lives elsewhere.
    """
    roots = []
    try:
        from app import log_discovery
        roots.extend(log_discovery.inventory_roots(config))
    except Exception:
        log.debug("log_discovery unavailable; using config + default", exc_info=True)

    cfg = config if hasattr(config, "get") else {}
    for key in ("spell_file", "eq_dir", "log_dir", "log_file_path"):
        v = cfg.get(key)
        if not v:
            continue
        base = v if os.path.isdir(v) else os.path.dirname(v)
        for cand in (base, os.path.dirname(base.rstrip(chr(92) + "/"))):
            if cand and os.path.isdir(cand) and cand not in roots:
                roots.append(cand)
    if _EQ_DEFAULT not in roots:
        roots.append(_EQ_DEFAULT)          # last resort, never the first assumption
    return roots


def spell_file(config=None) -> str:
    """The player's own `spells_us.txt`, found in their install."""
    for root in eq_roots(config):
        cand = os.path.join(root, "spells_us.txt")
        if os.path.isfile(cand):
            return cand
    return os.path.join(_EQ_DEFAULT, "spells_us.txt")


def known_spells(config=None) -> set:
    """Every spell this CHARACTER knows, across all their equipped classes.

    🔴 THE DUMP IS CHARACTER-WIDE, NOT CLASS-WIDE. The filename carries a class
    (`<char>-PAL-Spellbook.txt`) and that is misleading: Legends is multiclass, so the file
    holds the spells of every class the character has equipped. The owner's "PAL" book is 752
    spells, of which 142 are ENCHANTER-ONLY and 11 are paladin-only.

    Keying it by the filename's class -- which `spellbook_files()` does -- therefore reports
    an enchanter as having no spellbook at all, and a multiclass character's book gets filed
    under one third of itself. Use THIS for "do I know that spell"; use `spellbook_files()`
    only when you genuinely want the per-file split.
    """
    out = set()
    for names in spellbook_files(config).values():
        out |= names
    return out


def spellbook_files(config=None) -> dict:
    """Raw per-FILE spellbook dumps: {class-in-filename: {spell names}}.

    ⚠ The class in the key is the class in the FILENAME, not the class that can cast each
    spell -- see `known_spells()`. Prefer that unless you specifically need the per-file view.

    🔴 THIS IS THE GROUND TRUTH and it beats anything derived from `spells_us.txt`. The client
    writes `<char>-<CLASS>-Spellbook.txt` (one `level<TAB>name` per line) listing what that
    character actually knows — 748 lines for the owner's paladin. The spell FILE lists every
    spell EverQuest has ever had, which is how a level-55 Planes-of-Power buff ended up being
    recommended to a level-50 character on a classic-era server.

    Returns {} when no dump is present, and the caller then falls back to class/level from the
    file and says so, rather than pretending.
    """
    import glob
    out = {}
    for root in eq_roots(config):
        for path in glob.glob(os.path.join(root, "*-Spellbook.txt")):
            # `<character>-<CLASS>-Spellbook.txt`. Read the class from the filename; the
            # character name is deliberately not retained.
            parts = os.path.basename(path)[:-len("-Spellbook.txt")].split("-")
            ab = parts[-1].upper()
            if ab not in CLASSES:
                continue
            names = out.setdefault(ab, set())
            try:
                with io.open(path, encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        bits = ln.rstrip().split(chr(9))
                        if len(bits) >= 2 and bits[1].strip():
                            names.add(bits[1].strip().lower())
            except OSError:
                continue
        if out:
            break
    return out


def _n(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# Duration is `min(formula(level), dur_value)` -- dur_value is a CAP, not the answer. At high
# level most buffs sit at their cap, which is why reading dur_value alone looked right on
# Clarity (270 ticks = 27 min) and Spirit of Wolf (360 = 36 min). It is not right for
# short/level-scaled ones.
_DUR_FORMULA = {
    1: lambda lv: lv // 2,        2: lambda lv: lv // 2 + 5,
    3: lambda lv: lv * 30,        4: lambda lv: 50,
    5: lambda lv: 2,              6: lambda lv: lv // 2,
    7: lambda lv: lv,             8: lambda lv: lv + 10,
    9: lambda lv: lv * 2 + 10,    10: lambda lv: lv * 3 + 10,
    11: lambda lv: lv * 30,       12: lambda lv: max(lv // 4, 1),
    50: lambda lv: 72000,         51: lambda lv: 72000,
}


class Spell:
    __slots__ = ("id", "name", "mana", "cast", "recast", "dur", "dur_formula", "good",
                 "target", "skill", "group", "rank", "song_cap", "effects", "classes")

    def __init__(self, f):
        self.id = _n(f[C_ID])
        self.name = f[C_NAME].strip()
        self.mana = _n(f[C_MANA])
        self.cast = _n(f[C_CAST])
        self.recast = _n(f[C_RECAST])
        self.dur = _n(f[C_DUR_VALUE])
        self.dur_formula = _n(f[C_DUR_FORMULA])
        self.good = _n(f[C_GOOD])
        self.target = _n(f[C_TARGET])
        self.skill = _n(f[C_SKILL])
        self.group = _n(f[C_GROUP])
        self.rank = _n(f[C_RANK])
        self.song_cap = _n(f[C_SONG_CAP]) if len(f) > C_SONG_CAP else 0
        self.effects = _parse_effects(f[C_EFFECTS] if len(f) > C_EFFECTS else "")
        self.classes = {}
        for i, ab in enumerate(CLASSES):
            lv = _n(f[C_CLASS0 + i], 255)
            if lv < 255:
                self.classes[ab] = lv

    @property
    def is_song(self) -> bool:
        return bool(self.song_cap)

    def ticks(self, level: int = 60) -> int:
        fn = _DUR_FORMULA.get(self.dur_formula)
        v = fn(level) if fn else self.dur
        return min(v, self.dur) if self.dur else v

    def minutes(self, level: int = 60) -> float:
        return self.ticks(level) * 6 / 60.0        # a tick is six seconds

    def __repr__(self):
        return "<Spell %d %s>" % (self.id, self.name)


def _parse_effects(raw: str) -> list:
    """`slot|spa|base|base2|calc|max`, slots joined by `$`.

    🔴 A slot of SPA 10 with base 0 is PADDING, not a charisma buff. It is the most common
    slot in the whole file; counting it would make nearly every buff look like it touches CHA.
    """
    out = []
    for chunk in (raw or "").split("$"):
        p = chunk.split("|")
        if len(p) != 6:
            continue
        spa, base, mx = _n(p[1]), _n(p[2]), _n(p[5])
        if spa == 10 and base == 0 and mx == 0:
            continue
        out.append({"slot": _n(p[0]), "spa": spa, "base": base, "max": mx})
    return out


def load(config=None, path: str = "") -> list:
    """Every spell in the client file. Returns [] if the file is missing or unreadable."""
    path = path or spell_file(config)
    out = []
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                f = ln.rstrip("\r\n").split("^")
                if len(f) <= C_EFFECTS or not f[0].isdigit():
                    continue
                out.append(Spell(f))
    except OSError:
        log.warning("could not read spell file: %s", path)
        return []
    return out


def verify(spells: list) -> bool:
    """Cheap sanity check that the columns still mean what we think.

    A patch can reorder this file. Rather than silently recommend nonsense, the caller shows a
    warning when this fails. The checks are things that must hold for ANY correct read:
    durations in a plausible tick range, most spells usable by at least one class, and the
    effect column actually parsing.
    """
    if len(spells) < 5000:
        return False
    sample = spells[:8000]
    with_class = sum(1 for s in sample if s.classes)
    with_effect = sum(1 for s in sample if s.effects)
    sane_dur = sum(1 for s in sample if 0 <= s.dur <= 20000)
    return (with_class > len(sample) * 0.25
            and with_effect > len(sample) * 0.50
            and sane_dur > len(sample) * 0.98)


# ── what counts as a buff ────────────────────────────────────────────────────────────────
def can_self_heal(picks) -> bool:
    return any(ab in SELF_HEAL_CLASSES for ab, _ in picks)


def castable_buffs(spells: list, picks: list, min_ticks: int = MIN_BUFF_TICKS,
                   known: dict = None, self_heal: bool = None) -> list:
    """Buffs the given classes can cast at the given levels.

    `picks` is [(class_abbrev, level), ...]. A spell qualifies once, for the FIRST picked class
    that can cast it -- who casts it is recorded so the plan can say so.

    Excludes heals and anything too short to be worth a gem: a Quick Buff loadout is what you
    pre-load and leave up, not what you cast in a fight.

    `known` is {CLASS: {names}} from `spellbook_files()`. When a class has a spellbook dump,
    ONLY spells in it are offered — that is what the character actually has, as opposed to what
    EverQuest has ever shipped.
    """
    known = known or {}
    if self_heal is None:
        self_heal = can_self_heal(picks)
    out = []
    for s in spells:
        # 🔴 Do NOT test `s.dur > 0`. `dur_value == 0` with `dur_formula == 50` means
        # PERMANENT, not "no duration" — that test silently discarded every permanent buff in
        # the game, which is why Yaulp (durF 50) and the self-proc buffs Instrument of Nife and
        # Call of Sky never appeared no matter how they were prioritised or pinned.
        if s.good <= 0 or not s.effects:
            continue
        if s.target not in TARGETS:
            continue
        if s.name.startswith(SKIP_PREFIXES):
            continue
        for ab, lvl in picks:
            need = s.classes.get(ab)
            if need is None or need > min(lvl, LEVEL_CAP):
                continue
            # Character-wide, not per-class: a multiclass character's dump is filed under one
            # class name but contains every class they have equipped.
            if known and s.name.lower() not in known:
                continue                       # the character does not have this spell
            if s.ticks(lvl) < min_ticks:      # a heal-over-time, not a buff (permanent passes)
                continue
            spas = _by_spa(s)
            if self_heal:
                spas = {k: v for k, v in spas.items() if not (k in (69, 100) and v < 0)}
            if not any(value_of(k, v) > 0 for k, v in spas.items()):
                continue                       # nothing left once heals are discounted
            out.append((s, ab, need))
            break
    return out




def _by_spa(s) -> dict:
    """spa -> magnitude, KEEPING THE SIGN.

    🔴 Do not take abs() here. A negative value on a beneficial spell is a real cost, and
    dropping the sign turns one into a headline benefit: Minor Illusion carries `SPA 3 base
    -7000` (it pins you in place) and came out top of the list as "Run Speed +7000%". Torpor's
    `SPA 11 base 70` is 70-100 = **-30% attack speed**, the price of its huge regen, and showed
    as a harmless "+0%".

    ⚠ `max` of 0 means "no cap recorded", not "no effect" — fall back to base.
    """
    d = {}
    for e in s.effects:
        if e["spa"] in IGNORE_SPAS:
            continue
        v = e["max"] if e["max"] else e["base"]
        if e["spa"] == 11:                 # haste is stored as percent + 100
            v = v - 100
        if e["spa"] in d and abs(d[e["spa"]]) >= abs(v):
            continue
        d[e["spa"]] = v
    return d


#: Classes that can put their own health back — heals, or lifetap. Owner, 2026-09-01:
#: *"mana is more imporent than hp in classes that have ways to heal themselfs and necros can
#: lifetap which is a way for them to heal themselfs"*. For these, a self-buff that trades HP
#: for mana (Lich, Divine Purpose, Dark Temptation) is a good deal, not a penalty.
SELF_HEAL_CLASSES = {"CLR", "PAL", "DRU", "SHM", "NEC", "RNG", "SHD", "BST"}


def value_of(spa: int, mag: int) -> float:
    """Magnitude of ONE effect, used only to compare buffs WITHIN the same priority.

    🔴 This no longer decides which priority wins — `PRIORITIES` does, in order. Its only job
    is "given two buffs that both give HP, which gives more". Effects outside the owner's stat
    list score zero and cannot earn a gem.
    """
    if spa not in SCORED_SPAS:
        return 0.0
    # 🔴 SPA 85's "base" is the SPELL ID of the proc it grants (2729 = Instrument of Nife's
    # proc), not an amount. Scaling by it would score that buff at 28 and bury everything else
    # — the same trap as SPA 58, whose base is a character model id. Flat value.
    if spa == 85:
        return 1.6
    # 🔴 A damage shield's SIGN IS MEANINGLESS in this file — 14 of the 30 at level <=50 are
    # positive and 16 negative, and the two strongest disagree with each other (MAG Shield of
    # Lava is -25, DRU Shield of Thorns +24). Magnitude is the value. Reading the sign made
    # every coat spell look like a penalty: Spikecoat is AC 84 AND 4 damage back per hit.
    if spa == 59:
        return 1.0 + abs(mag) / 100.0
    sign = -1.0 if mag < 0 else 1.0
    return sign * (1.0 + abs(mag) / 100.0)


# ── the slot model ────────────────────────────────────────────────────────────────────────
# 🔴 THIS IS THE CENTRAL CONSTRAINT, not a detail. A buff does not occupy "AC" — it occupies
# SLOT n, and the game blocks a second buff only when it writes the SAME EFFECT IN THE SAME
# SLOT. Everything else about this planner is preference; this is the rule.
#
# It was got wrong first time by comparing bare effect types, which is stricter than the game:
# Skin like Steel (AC slot 1) appeared to block Spikecoat (AC slot 2), and Armor of Faith
# (AC slot 4) appeared to block Yaulp II (AC slot 5). All four actually coexist. Owner,
# 2026-09-01: *"skin like steal and spike coat should stack one is an HP buff of a differnt
# type than the damage shield spikecoat no?"* — he was right.
#
# It matters most exactly where this tool is used: pick THREE classes and their buff pools
# overlap heavily, so which combination fits together is the whole question.
MAX_SLOTS = 12


def slots_used(sp) -> set:
    """Every (slot, effect) pair this spell OCCUPIES — scored or not.

    🔴 OCCUPYING A SLOT AND BEING WORTH POINTS ARE DIFFERENT QUESTIONS, and conflating them
    was a real bug. This filtered to SCORED_SPAS, so an effect excluded from scoring became
    invisible to stacking — and every shapeshift carries SPA 58 (illusion) in slot 1:

        Wolf Form         slot 1 spa 58 model 796
        Form of the Bear  slot 1 spa 58 model 43

    Both occupy slot 1 and cannot both be up, but the planner happily stacked them. Owner,
    2026-09-01: *"theres a few buffs you are stacking that wouldnt actuall work because they
    are also an illusion like wolf form and form of the bear"*.

    SPA 58 is still excluded from VALUE (its number is a character model id, not a magnitude)
    and vision/runes/heals still earn no gem — they just block correctly now. The SPA-10
    padding slots never reach here; they are dropped at parse.
    """
    return {(e["slot"], e["spa"]) for e in sp.effects if (e["max"] or e["base"])}


def slot_map(chosen: list) -> dict:
    """(slot, effect) -> the buff holding it, across a whole plan."""
    out = {}
    for r in chosen:
        for key in slots_used(r["spell"]):
            out.setdefault(key, r["spell"].name)
    return out


def collision_report(cands: list, picks: list) -> dict:
    """How well a class combination actually fits together.

    Answers the question the class pickers raise: does adding a third class give you more
    buffs, or just more buffs fighting over the same slots? Counts, per class, how many of
    its buffs are blocked by a buff another picked class already holds better.
    """
    best = {}
    for sp, ab, need in cands:
        for key in slots_used(sp):
            cur = best.get(key)
            if cur is None or _slot_strength(sp, key) > _slot_strength(cur[0], key):
                best[key] = (sp, ab)
    holders = collections.Counter(ab for _, ab in best.values())
    blocked = collections.Counter()
    for sp, ab, _ in cands:
        keys = slots_used(sp)
        if keys and all(best.get(k, (None, None))[0] is not sp for k in keys):
            if any(best.get(k) and best[k][1] != ab for k in keys):
                blocked[ab] += 1
    return {"holds": dict(holders), "blocked": dict(blocked),
            "slots_filled": len(best)}


def _slot_strength(sp, key) -> float:
    """How strongly one spell writes ONE (slot, effect) pair.

    When two buffs collide, the bigger number wins — owner, 2026-09-01. Only ever called on a
    key both spells share, so this compares like with like (AC against AC in the same slot),
    never Haste against a resist.
    """
    slot, spa = key
    for e in sp.effects:
        if e["slot"] == slot and e["spa"] == spa:
            return abs(e["max"] or e["base"])
    return 0.0


def conflicts(a, b) -> bool:
    """Would casting both waste one of them?

    EQ blocks a buff that would write an effect another buff already holds, so two buffs that
    share a real effect are a wasted slot even when one is strictly better.

    🔴 COMPARE ONLY REAL STAT EFFECTS. SPA 148 and 149 are stacking DIRECTIVES (block /
    overwrite), not things a buff grants -- and they sit on a large share of buffs. Counting
    them as shared effects made almost everything conflict with almost everything: once Symbol
    of Pinzarn was chosen, every other buff carrying a 148 was ruled out, so the planner
    reported "nothing found" for AC, Mana and Attack Speed and silently ignored the priority
    order. Restrict the comparison to the stats we actually score.

    ⚠ Bard songs are the exception -- they occupy their own window and stack alongside spells.
    The owner's words, 2026-08-30: *"bard songs always stack they have their own window"*,
    immediately followed by *"well that might not always be true but mostly true"*. So this is
    treated as true between a song and a spell, and songs are still checked against each other.
    """
    if a.id == b.id:
        return True
    if a.is_song != b.is_song:
        return False
    if a.group and a.group == b.group:      # same spell line, higher rank replaces lower
        return True
    # 🔴 EQ STACKS PER SLOT POSITION, NOT PER EFFECT TYPE. Two buffs conflict when the same
    # effect sits in the same SLOT INDEX; the same effect in different slots coexists. The
    # first version compared bare SPA sets, which is strictly harsher than the game and made
    # Skin like Steel (AC in one slot) block Spikecoat (AC in another) — the owner caught it:
    # *"skin like steal and spike coat should stack one is an HP buff of a differnt type than
    # the damage shield spikecoat no?"*
    return bool(slots_used(a) & slots_used(b))


#: Below this a buff is not worth a permanent gem — you would spend the session re-firing it.
#: A Quick Buff slot is for something you set and forget.
GOOD_DURATION_TICKS = 150      # 15 minutes


def duration_factor(s, level: int = 60) -> float:
    """How much a buff's own length is worth to a SET-AND-FORGET slot.

    🔴 Without this the planner put "Resistant Discipline, 5 min" and "Trickster's Augmentation,
    6 min" into permanent gems ahead of two-hour buffs. Duration is not a tiebreak here — it is
    most of the point. Full credit at 15 minutes and above, falling away sharply below.
    """
    t = s.ticks(level)
    if t >= GOOD_DURATION_TICKS:
        return 1.0
    return max(0.15, (t / float(GOOD_DURATION_TICKS)) ** 1.5)


# ── what a player actually cares about, IN ORDER ──────────────────────────────────────────
# 🔴 Priorities are ORDERED, not weighted. Owner, 2026-08-31: *"why would i care about resist
# magic if i didnt includr an hp buff first kinda thing"*. Any linear scoring lets a big resist
# buff outbid HP, which is exactly wrong — so the optimiser walks this list top-down and only
# reaches resists once everything above is covered. Reorder it in the UI.
#
# The stats are the owner's own list. Regen is included because he separately confirmed it
# counts (*"heal over times dont count but regen does"*) even though it is not in that list.
PRIORITIES = [
    ("HP",           (69,)),
    ("ATK",          (2,)),
    ("AC",           (1,)),
    ("Mana",         (97, 15)),
    ("Attack Speed", (11,)),
    ("Regen",        (100,)),
    ("STR",          (4,)),
    ("STA",          (7,)),
    ("INT",          (8,)),
    ("WIS",          (9,)),
    ("AGI",          (6,)),
    ("DEX",          (5,)),
    # 🔴 Above resists on purpose. Owner, 2026-09-01: *"before resists if theres a self proc
    # you should put that value higher"*. SPA 85 adds a proc to your own melee swings — the
    # owner has Instrument of Nife (PAL 15) and Call of Sky (RNG 36), both permanent.
    ("Self Proc",    (85,)),
    ("Resists",      (46, 47, 48, 49, 50, 111)),
    # 🔴 Damage shields were never cut by the owner — I inferred it from his stat list and he
    # corrected it 2026-09-01: *"I dont recall us cutting damage shields but the only imporent
    # classes to use damage shields are mage druid ranger ... resists may be better at this
    # point since mage and druid ones are so much better"*. Hence: back in, but BELOW resists.
    # The magnitudes bear him out — MAG Shield of Lava 25 and DRU Shield of Thorns 24 against
    # NEC Banshee Aura 12 and ENC Feedback 11.
    ("Damage Shield", (59,)),
]
DEFAULT_ORDER = [name for name, _ in PRIORITIES]

#: Classes that live in melee range. Owner, 2026-09-01: *"AC Then HP for melee"* — mitigation
#: beats a bigger health pool when you are the one being hit, because AC reduces every swing
#: while HP only lets you absorb more of them.
MELEE_CLASSES = {"WAR", "PAL", "SHD", "RNG", "MNK", "ROG", "BER", "BST"}


def default_order(picks=()) -> list:
    """The starting priority order for a loadout. AC leads for melee, HP for everyone else."""
    order = list(DEFAULT_ORDER)
    if any(ab in MELEE_CLASSES for ab, _ in picks):
        order.remove("AC")
        order.insert(order.index("HP"), "AC")
    return order
_SPA_TO_PRIORITY = {spa: name for name, spas in PRIORITIES for spa in spas}

#: Every effect NOT in the list above is ignored entirely. Owner: *"lets stick to
#: HP/ATK/AC/MANA/ATTACK SPEED/STR/STA/INT/WIS/AGI/DEX/Resists"*. Damage shield, run speed,
#: spell haste, aggro and mitigation all drop out here — they were padding the list.
SCORED_SPAS = {spa for _, spas in PRIORITIES for spa in spas}


def priority_of(spa: int):
    return _SPA_TO_PRIORITY.get(spa)


def provides(row) -> set:
    """Which priority names this buff actually delivers, positively."""
    return {priority_of(k) for k, v in row["spas"].items()
            if priority_of(k) and v > 0}


def delivers(row, want: str) -> float:
    """HOW MUCH of one priority this buff gives. Coverage is not binary.

    🔴 Treating a priority as simply "covered" let a RIDER lock out a far better dedicated
    buff: Skin like Steel carries AC +35 alongside its HP, which blocked Spikecoat's AC +84,
    and Yaulp II's AC +30 blocked Armor of Faith's +85. Now that stacking is known to be
    per-slot, those all coexist — so what matters is how much is already covered, not whether.
    """
    return sum(value_of(k, v) for k, v in row["spas"].items()
               if priority_of(k) == want and v > 0)


def magnitude(row, want: str) -> float:
    """RAW size of what this buff gives for one priority — for upgrade comparisons only.

    🔴 Do not use value_of() to decide an upgrade. It is deliberately compressive
    (`1 + mag/100`), so AC 35 scores 1.35 and AC 84 scores 1.84 — a stat 2.4x bigger looks
    only 1.36x better and never clears any sane margin. That is why Spikecoat (AC 84) and
    Armor of Faith (AC 85) kept losing to a rider worth AC 30-35.
    """
    return sum(abs(v) for k, v in row["spas"].items()
               if priority_of(k) == want and v > 0)


def total_worth(row, order: list, cover_val: dict, level: int = 60) -> float:
    """What this buff is worth OVERALL — every priority it touches, weighted by your order.

    🔴 THE OVERWRITE TRAP. Owner, 2026-09-01: *"what if only 1 number is bigger that slot and
    the rest are non existent or much smaller ... 100ac 100hp 100sta vs 101ac 40hp 40sta"*.
    Ranking a slot by that slot's number alone picks the 101 AC spell and throws away 60 HP
    and 60 STA to gain 1 AC — and in game you do not get to reconsider, the wider buff is gone
    the instant the narrow one lands.

    So a candidate is scored on the MARGINAL gain across every priority it delivers, weighted
    by where that priority sits in the owner's order (earlier = worth more). Only what it adds
    beyond what is already covered counts, so a duplicate is worth nothing.
    """
    total = 0.0
    for i, name in enumerate(order):
        gain = magnitude(row, name) - cover_val.get(name, 0.0)
        if gain <= 0:
            continue
        rank_w = 1.0 / (1.0 + 0.35 * i)          # top of the list matters most
        total += rank_w * value_of_scale(name, gain)
    return total * duration_factor(row["spell"], level)


def value_of_scale(priority: str, gain: float) -> float:
    """Turn a raw magnitude gain into something comparable ACROSS priorities.

    Raw numbers are not commensurable — 40 disease resist is not 40 hit points. Compressing
    with a log keeps a big number ahead of a small one without letting one huge stat drown
    out three useful ones, which is the whole point of the owner's example.
    """
    import math
    return math.log1p(max(gain, 0.0) / 10.0)


#: A priority already covered is revisited only if something delivers meaningfully more.
#: Without a margin the planner would spend every gem on marginal upgrades to line one.
UPGRADE_MARGIN = 1.6


#: Spells that go in a gem no matter what the optimiser thinks, matched on the name STEM so the
#: best version you own wins (Yaulp / Yaulp II / Yaulp III). Owner, 2026-09-01: *"spells like
#: Yelp should always be casted as well"*. Yaulp is `durF=50` — PERMANENT — which is why it is
#: always worth a gem, but it kept losing its slot because its AC collides with a bigger AC buff
#: that was picked first. A pin says "I want this regardless", and that is a judgement the tool
#: should not be second-guessing.
ALWAYS_CAST = ("yaulp",)


def pinned_matches(cands: list, stems=ALWAYS_CAST) -> list:
    """Best castable spell per pinned stem.

    🔴 BIGGER NUMBER WINS, not higher level. Owner, 2026-09-01: *"if two spells dont stack
    because one has bigger number the bigger number wins"*. Level is usually a proxy for that
    and occasionally is not — a higher-level spell in a line can trade raw magnitude for
    utility. Rank on what the spell actually delivers; level only breaks a tie.
    """
    best = {}
    for s, ab, need in cands:
        low = s.name.lower()
        for stem in stems:
            if not (low == stem or low.startswith(stem + " ")):
                continue
            score = (sum(abs(v) for v in _by_spa(s).values() if v > 0), need)
            cur = best.get(stem)
            if cur is None or score > cur[1]:
                best[stem] = ((s, ab, need), score)
    return [v[0] for v in best.values()]


#: 🔴 Only for the leftover-gem pass. It must NOT gate the priority pass: under the ordered
#: model the priority list is itself the quality filter — if you said Attack Speed matters, a
#: haste buff earns a gem, full stop. A leftover threshold of 2.0 (calibrated for the old
#: weighted scoring, where values ran 1.5-6.0) silently rejected Swift Like the Wind at 1.60,
#: Gift of Magic at 1.50 and Armor of Faith at 1.85 — so haste only ever appeared by accident,
#: riding along on a multi-stat buff at the bottom of the list.
MIN_EXTRA_VALUE = 1.4


def optimise(cands: list, slots: int = 6, level: int = 60, order: list = None,
             pins=ALWAYS_CAST, self_heal: bool = False) -> tuple:
    """Fill gems by PRIORITY ORDER, not by score.

    Walks `order` top-down. For each priority not yet covered, takes the strongest buff that
    delivers it and does not clash with anything already chosen. Resists therefore get a gem
    only when HP, AC, haste and the rest above them are already handled -- which is the whole
    point (owner: *"why would i care about resist magic if i didnt includr an hp buff first"*).

    A buff that covers several priorities at once covers them all, so a HP+AC buff spends one
    gem on two lines. Any gems still free after every priority is covered go to the best
    remaining buffs.

    Returns (chosen, rejected); each chosen row carries `for_priority` -- why it was taken.
    """
    order = order or DEFAULT_ORDER

    scored = []
    for s, ab, need in cands:
        spas = _by_spa(s)
        if self_heal:
            # 🔴 A self-buff that BURNS HP to make mana is a trade, not a penalty, for anyone
            # who can put the health back. Lich reads mana +20/tick, HP -22/tick and is a
            # necromancer staple precisely because they lifetap it back. Drop the negative HP
            # term so the mana is judged on its own merits.
            spas = {k: v for k, v in spas.items() if not (k in (69, 100) and v < 0)}
        total = sum(value_of(k, v) for k, v in spas.items()) * duration_factor(s, level)
        if total > 0:
            scored.append({"spell": s, "caster": ab, "req_level": need,
                           "spas": spas, "value": total})
    scored.sort(key=lambda r: (-r["value"], r["spell"].mana))

    chosen, rejected, covered = [], [], set()
    cover_val: dict = {}                 # priority -> best magnitude delivered so far

    def clash(r):
        return next((c for c in chosen if conflicts(c["spell"], r["spell"])), None)

    # Pinned spells take their gem BEFORE anything is optimised. That is the point of a pin:
    # the owner has decided he wants it, and the tool should not out-argue him. Yaulp is the
    # example -- permanent, self-only, and it kept losing its slot to a bigger AC buff.
    by_id = {r["spell"].id: r for r in scored}
    for sp_, ab_, need_ in pinned_matches(cands, pins):
        if len(chosen) >= slots:
            break
        row = by_id.get(sp_.id)
        if row is None:
            # Pinned but unscored (every effect fell outside the stat list). Still honour it.
            row = {"spell": sp_, "caster": ab_, "req_level": need_,
                   "spas": _by_spa(sp_), "value": 0.0}
        if clash(row):
            continue
        row = dict(row, for_priority="always")
        chosen.append(row)
        covered |= provides(row)
        for pz in provides(row):
            cover_val[pz] = max(cover_val.get(pz, 0.0), magnitude(row, pz))

    for want in order:
        if len(chosen) >= slots:
            break
        have = cover_val.get(want, 0.0)
        best = None
        for r in scored:
            if r in chosen or want not in provides(r):
                continue
            if clash(r):
                continue
            # Rank within the priority by how much of THAT stat it gives, not by its total --
            # otherwise a buff loaded with other stats wins the HP slot over a bigger HP buff.
            # Rank by TOTAL worth, not by this one slot. A buff that wins the slot by a
            # point while giving up everything else is a bad trade, and the game will not let
            # you take it back. This also subsumes the earlier "riders are free" rule
            # (owner: *"if the shaman shield is small but comes with another type of buff ...
            # that one 100% gets used"*) — a rider is simply more marginal gain.
            here = total_worth(r, order, cover_val, level)
            if best is None or here > best[1]:
                best = (r, here)
        if best is None:
            continue
        row, gain = best
        # Already covered, and this is not a clear improvement -> leave the gem for something
        # further down the list.
        if have > 0 and magnitude(row, want) < have * UPGRADE_MARGIN:
            continue
        row = dict(row, for_priority=want)
        chosen.append(row)
        covered |= provides(row)
        for pz in provides(row):
            cover_val[pz] = max(cover_val.get(pz, 0.0), magnitude(row, pz))

    # Any gem still free goes to the best thing left that holds alongside the rest.
    for r in scored:
        if len(chosen) >= slots:
            break
        if r in chosen or clash(r) or r["value"] < MIN_EXTRA_VALUE:
            continue
        if provides(r) <= covered:
            continue                    # adds nothing new
        r = dict(r, for_priority="extra")
        chosen.append(r)
        covered |= provides(r)

    for r in scored[:60]:
        if any(c["spell"].id == r["spell"].id for c in chosen):
            continue
        c = clash(r)
        if c and len(rejected) < 25:
            rejected.append({**r, "because": "overlaps " + c["spell"].name})
        elif len(rejected) < 25 and len(chosen) >= slots:
            rejected.append({**r, "because": "no gem left"})
    return chosen, rejected


def describe(spa: int, mag: int) -> str:
    """Human text for one effect. A penalty must READ like a penalty."""
    name = SPA_NAMES.get(spa, "Effect %d" % spa)
    # 🔴 SPA 85's value is the SPELL ID of the proc it grants, so printing it as "+2729" reads
    # as a huge bonus. Name it and show no number.
    if spa == 85:
        return name
    sign = "-" if mag < 0 else "+"
    m = abs(mag)
    if spa in (11, 3):
        return "%s %s%d%%" % (name, sign, m)
    if spa in UTILITY_SPAS:
        return name
    if spa in (15, 100):
        return "%s %s%d/tick" % (name, sign, m)
    if spa == 59:
        return "%s %d" % (name, m)          # sign carries no meaning here
    return "%s %s%d" % (name, sign, m)


def summary(row: dict, limit: int = 4) -> str:
    """The headline effects of one chosen buff, biggest first."""
    items = sorted(row["spas"].items(), key=lambda kv: -value_of(kv[0], kv[1]))
    good = [(k, v) for k, v in items if value_of(k, v) > 0]
    bad = [(k, v) for k, v in items if value_of(k, v) < 0]
    parts = [describe(k, v) for k, v in good[:limit]]
    extra = len(good) - len(parts)
    if extra > 0:
        parts.append("+%d more" % extra)
    # 🔴 Downsides are never truncated away. Torpor's -30% attack speed is the whole reason
    # somebody might not want it in a slot.
    parts += [describe(k, v) for k, v in bad]
    return ", ".join(parts)
