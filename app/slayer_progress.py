"""Local log-only progress for slayer-style achievement kill targets.

Counts come from bundled client data (achievement_step_targets.json) or
step fields when present. Progress is per-character JSON under AppData.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_TARGETS: Optional[dict] = None

# Strip common EQ article/prefixes for matching.
_STRIP = re.compile(r"^(?:a|an|the)\s+", re.I)
_SPLIT = re.compile(r"\s*(?:,|;|\band\b)\s*", re.I)


def _data_dir() -> str:
    base = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "GnollGuard",
    )
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.path.expanduser("~")
    return base


def _progress_path(char: str = "default") -> str:
    safe = re.sub(r"[^\w\-]+", "_", (char or "default"))[:64] or "default"
    return os.path.join(_data_dir(), f"slayer_progress_{safe}.json")


def _bundled_targets_path() -> Optional[str]:
    # Prefer next to package / site data copies when shipping with app later.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "data", "achievement_step_targets.json"),
        os.path.join(here, "..", "data", "achievement_step_targets.json"),
        os.path.join(
            os.path.expanduser("~"),
            "Documents",
            "INTERNETSTUFF",
            "codex",
            "_migrate",
            "site",
            "public",
            "data",
            "achievement_step_targets.json",
        ),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def load_targets() -> dict:
    """achievement_id(str) -> list of step target dicts."""
    global _TARGETS
    if _TARGETS is not None:
        return _TARGETS
    path = _bundled_targets_path()
    if not path:
        _TARGETS = {}
        return _TARGETS
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _TARGETS = data.get("achievements") or {}
    except Exception:
        log.debug("load slayer targets failed", exc_info=True)
        _TARGETS = {}
    return _TARGETS


def load_progress(char: str = "default") -> dict:
    """{ 'kill_counts': {token: n}, 'by_achievement': {aid: {step_order: n}} }"""
    try:
        with open(_progress_path(char), encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {"kill_counts": {}, "by_achievement": {}}
        d.setdefault("kill_counts", {})
        d.setdefault("by_achievement", {})
        return d
    except Exception:
        return {"kill_counts": {}, "by_achievement": {}}


def save_progress(state: dict, char: str = "default") -> None:
    try:
        path = _progress_path(char)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=0)
        os.replace(tmp, path)
    except Exception:
        log.debug("save slayer progress failed", exc_info=True)


def _tokens_from_mobs(text: str) -> list[str]:
    if not text:
        return []
    parts = _SPLIT.split(text.strip().rstrip("."))
    out = []
    for p in parts:
        t = _STRIP.sub("", p.strip().lower())
        t = re.sub(r"[^a-z0-9\s\-']", "", t).strip()
        if t and len(t) > 1:
            out.append(t)
    return out


def mob_matches_targets(mob: str, target_mobs: str) -> bool:
    """True if slain mob name matches any race/token in the target list."""
    m = _STRIP.sub("", (mob or "").strip().lower())
    m = re.sub(r"[^a-z0-9\s\-']", "", m)
    if not m:
        return False
    for tok in _tokens_from_mobs(target_mobs or ""):
        # whole-token / substring for plurals: "froglok" in "a froglok scout"
        if tok in m or m in tok:
            return True
        # singular/plural soft match
        if tok.endswith("s") and tok[:-1] in m:
            return True
        if not tok.endswith("s") and (tok + "s") in m:
            return True
    return False


def enrich_achievement(ach: dict, progress: dict | None = None) -> dict:
    """Attach target counts + local progress onto a journal achievement dict."""
    aid = ach.get("achievement_id")
    targets = load_targets().get(str(aid), []) if aid is not None else []
    by_order = {t.get("step_order"): t for t in targets}
    prog = progress or {}
    by_a = (prog.get("by_achievement") or {}).get(str(aid), {})

    steps = list(ach.get("steps") or [])
    if not steps and targets:
        steps = [
            {
                "step_order": t.get("step_order", 0),
                "description": t.get("description") or t.get("target_mobs"),
            }
            for t in targets
        ]
    out_steps = []
    for s in sorted(steps, key=lambda x: x.get("step_order", 0)):
        d = dict(s)
        t = by_order.get(d.get("step_order")) or {}
        # DB fields win if already present
        if d.get("target_count") is None and t.get("target_count") is not None:
            d["target_count"] = t.get("target_count")
            d["target_kind"] = t.get("target_kind")
            d["target_mobs"] = t.get("target_mobs") or t.get("description")
        order = d.get("step_order")
        if d.get("target_count") and d.get("target_kind") == "kill":
            cur = int(by_a.get(str(order), 0) or 0)
            d["progress_count"] = min(cur, int(d["target_count"]))
        out_steps.append(d)
    ach = dict(ach)
    ach["steps"] = out_steps
    return ach


def on_kill(
    mob: str,
    journal_achs: list,
    progress: dict,
    char: str = "default",
) -> list[dict]:
    """
    Increment counters for journaled slayer achievements matching this kill.
    Returns list of {achievement_id, name, progress, target, step_order} that advanced.
    """
    if not mob or not journal_achs:
        return []
    advanced = []
    by_a = progress.setdefault("by_achievement", {})
    changed = False
    for ach in journal_achs:
        aid = ach.get("achievement_id")
        if aid is None:
            continue
        enriched = enrich_achievement(ach, progress)
        key = str(aid)
        step_prog = by_a.setdefault(key, {})
        for s in enriched.get("steps") or []:
            if s.get("target_kind") != "kill" or not s.get("target_count"):
                continue
            if not mob_matches_targets(mob, s.get("target_mobs") or s.get("description") or ""):
                continue
            order = str(s.get("step_order", 0))
            cur = int(step_prog.get(order, 0) or 0)
            target = int(s["target_count"])
            if cur >= target:
                continue
            cur += 1
            step_prog[order] = cur
            changed = True
            advanced.append({
                "achievement_id": aid,
                "name": ach.get("name") or "Achievement",
                "step_order": s.get("step_order"),
                "progress": cur,
                "target": target,
                "mobs": s.get("target_mobs") or s.get("description"),
            })
    if changed:
        with _LOCK:
            save_progress(progress, char)
    return advanced


def rescan_kills_from_lines(
    lines: list[str],
    journal_achs: list,
    kill_re: re.Pattern,
    char: str = "default",
) -> int:
    """Re-parse kill lines and rebuild progress (max over existing). Returns # kills matched."""
    progress = load_progress(char)
    # Reset only achievement counters we track, then re-apply from full rescan
    # so rescan is authoritative for the scanned window. Keep higher of old/new.
    matched = 0
    temp = {"kill_counts": {}, "by_achievement": {}}
    for line in lines:
        m = kill_re.search(line)
        if not m:
            continue
        mob = (m.groupdict().get("mob") or m.group(1) if m.lastindex else "") or ""
        mob = mob.strip()
        if not mob:
            continue
        matched += 1
        on_kill(mob, journal_achs, temp, char=char)
    # Merge: take max per step
    by_a = progress.setdefault("by_achievement", {})
    for aid, steps in (temp.get("by_achievement") or {}).items():
        dest = by_a.setdefault(aid, {})
        for order, n in steps.items():
            dest[order] = max(int(dest.get(order, 0) or 0), int(n or 0))
    save_progress(progress, char)
    return matched
