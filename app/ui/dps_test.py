"""DPS benchmark recorder — deliberate, labelled, comparable combat tests.

Owner, 2026-08-25: *"can we setup like a combat testing and let pick our classes we are so
we can do dps testing between different combos remember we need to be able to pick 3 classes
total."*

WHAT THIS IS, AND WHAT IT IS NOT
    It records what actually happened. It does NOT simulate a combo you have not played --
    nothing here can tell you what PAL/WAR/ROG would do until you go and be PAL/WAR/ROG.
    That is the honest boundary, and pretending otherwise would produce numbers with no
    events behind them.

WHY A START/STOP WINDOW rather than tagging fights
    A benchmark is only meaningful against a controlled target. Start, fight the thing you
    chose, stop. The run stores the mobs it covered so a later comparison can refuse to
    compare a gnoll to a dragon.

WHAT MAKES A COMPARISON FAIR — surfaced, not hidden:
    * runs against different mobs are marked, because mob AC and level move DPS more than
      most gear changes;
    * short runs are marked, because one lucky proc chain dominates a 20-second sample;
    * accuracy is shown alongside DPS, since hit rate is this character's dominant term
      (measured 46% -- see the combat parser notes).

Runs are stored under %LOCALAPPDATA%/GnollGuard/data/dps_runs.json — outside this repo,
which is public. Nothing here uploads.
"""
from __future__ import annotations

import json
import logging
import os

import customtkinter as ctk

from app.ui import datapaths, extra_views, theme

log = logging.getLogger(__name__)

RUNS_FILE = "dps_runs.json"
#: Below this many seconds a run is statistically noise; still recorded, but flagged.
SHORT_RUN_SECS = 45


def _runs_path() -> str:
    return datapaths.path(RUNS_FILE)


def load_runs() -> list:
    try:
        with open(_runs_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d.get("runs", []) if isinstance(d, dict) else []
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("dps runs unreadable")
        return []


def save_runs(runs: list) -> bool:
    try:
        os.makedirs(os.path.dirname(_runs_path()), exist_ok=True)
        with open(_runs_path(), "w", encoding="utf-8") as fh:
            json.dump({"runs": runs}, fh, indent=1)
        return True
    except Exception:
        log.exception("could not save dps runs")
        return False


def summarise(lc, since_ts: float, until_ts: float) -> dict:
    """Aggregate the player's damage across fights inside a log-time window.

    ⚠ Bounded on the FIGHT's own timestamps, not on wall-clock. The two differ by however
    much history was primed at startup, and by the parser's timezone-naive epoch.
    """
    out = {"fights": 0, "kills": 0, "duration": 0.0, "damage": 0, "hits": 0, "misses": 0,
           "mobs": [], "breakdown": {k: 0 for k in
                                     ("melee", "skill", "ranged", "proc", "spell", "dot", "pet")}}
    if lc is None:
        return out
    mobs = {}
    for f in lc.fights:
        if f.start < since_ts or f.start > until_ts:
            continue
        shaped = lc._shape(f)
        me = next((r for r in shaped["rows"] if r.get("is_me")), None)
        if me is None:
            continue
        out["fights"] += 1
        out["kills"] += 1 if f.killed else 0
        out["duration"] += f.duration
        out["damage"] += me.get("damage", 0)
        out["hits"] += me.get("hits", 0)
        out["misses"] += me.get("misses", 0)
        for k in out["breakdown"]:
            out["breakdown"][k] += me.get("pet_damage", 0) if k == "pet" else me.get(k, 0)
        mobs[f.name] = mobs.get(f.name, 0) + 1
    out["mobs"] = sorted(mobs.items(), key=lambda kv: -kv[1])
    swings = out["hits"] + out["misses"]
    out["accuracy"] = (out["hits"] / swings) if swings else None
    out["dps"] = (out["damage"] / out["duration"]) if out["duration"] else 0.0
    return out


def tab_dps_test(tab, app):
    """Pick 3 classes, run a timed test, compare every run you have recorded."""
    state = {"running": False, "since": 0.0, "combo": list(extra_views.MY_CLASSES)}

    head = extra_views.card(tab)
    head.pack(fill="x", padx=10, pady=(10, 4))
    extra_views.lab(head, "DPS BENCHMARK", extra_views.F_SMALL, extra_views.GOLD)\
        .pack(fill="x", padx=12, pady=(10, 2))
    extra_views.wrap(
        head,
        "Pick the 3 classes you are running, hit Start, fight, hit Stop. The run is saved "
        "with its damage breakdown so combos can be compared later. It records what you "
        "actually did — it cannot predict a combo you have not played.",
        extra_views.T3).pack(fill="x", padx=12, pady=(0, 8))

    pick = ctk.CTkFrame(head, fg_color="transparent")
    pick.pack(fill="x", padx=12, pady=(0, 8))
    extra_views.lab(pick, "COMBO", extra_views.F_SMALL, extra_views.T3).pack(side="left", padx=(0, 8))
    menus = []
    for i in range(3):
        m = ctk.CTkOptionMenu(
            pick, width=100, height=28, values=["-"] + extra_views.CLASSES,
            fg_color=theme.PANEL, button_color=theme.PANEL_HOVER,
            button_hover_color=theme.GOLD, text_color=theme.TEXT_PRIMARY,
            dropdown_fg_color=theme.PANEL, dropdown_text_color=theme.TEXT_PRIMARY,
            dropdown_hover_color=theme.PANEL_HOVER, font=theme.FONT_BODY_SMALL,
            dropdown_font=theme.FONT_BODY_SMALL, corner_radius=7)
        m.set(state["combo"][i] if i < len(state["combo"]) else "-")
        m.pack(side="left", padx=3)
        menus.append(m)

    ctrl = ctk.CTkFrame(head, fg_color="transparent")
    ctrl.pack(fill="x", padx=12, pady=(0, 11))
    btn = ctk.CTkButton(ctrl, text="Start test", width=110, height=30, corner_radius=8,
                        fg_color=theme.PANEL_HOVER, hover_color=theme.PANEL,
                        text_color=theme.GOLD, font=theme.FONT_BODY_SMALL)
    btn.pack(side="left")
    status = extra_views.lab(ctrl, "not running", extra_views.F_SMALL, extra_views.T3)
    status.pack(side="left", padx=12)

    body = ctk.CTkFrame(tab, fg_color="transparent")
    body.pack(fill="both", expand=True)

    def feed():
        return getattr(app, "tail", None)

    def toggle():
        f = feed()
        if f is None or getattr(f, "lc", None) is None:
            status.configure(text="no combat parser", text_color=theme.DANGER)
            return
        if not state["running"]:
            state["running"] = True
            state["since"] = f.last_ts
            state["combo"] = [m.get() for m in menus if m.get() and m.get() != "-"]
            btn.configure(text="Stop test")
            status.configure(text="recording — %s" % ("/".join(state["combo"]) or "no combo set"),
                             text_color=theme.GREEN)
            return

        state["running"] = False
        btn.configure(text="Start test")
        s = summarise(f.lc, state["since"], f.last_ts + 1)
        if not s["fights"]:
            # Refuse to save an empty run rather than storing a 0-dps row that will sit in
            # the comparison forever looking like a real result.
            status.configure(text="no fights in that window — nothing saved",
                             text_color=theme.DANGER)
            return
        runs = load_runs()
        runs.append({
            "combo": state["combo"],
            "zone": getattr(f, "zone", ""),
            "fights": s["fights"], "kills": s["kills"],
            "duration": round(s["duration"], 1), "damage": s["damage"],
            "dps": round(s["dps"], 1),
            "hits": s["hits"], "misses": s["misses"],
            "accuracy": s["accuracy"],
            "breakdown": s["breakdown"],
            "mobs": s["mobs"][:6],
        })
        ok = save_runs(runs)
        status.configure(
            text=("saved — %d dps over %d fights" % (round(s["dps"]), s["fights"])) if ok
            else "could not save (see log)",
            text_color=theme.GREEN if ok else theme.DANGER)
        redraw()

    btn.configure(command=toggle)

    def redraw():
        extra_views.clear(body)
        runs = load_runs()
        if not runs:
            extra_views.lab(body, "no test runs recorded yet", extra_views.F_BODY,
                            extra_views.T3).pack(padx=12, pady=12)
            return

        # A comparison is only fair against the same target. Group by mob so the table does
        # not invite reading a gnoll run against a dragon run as a combo difference.
        extra_views.section(body, "recorded runs — newest first")
        peak = max(r.get("dps", 0) for r in runs) or 1
        seen_mobs = {}
        for r in runs:
            top = r.get("mobs") or []
            seen_mobs[top[0][0] if top else "?"] = True
        multi = len(seen_mobs) > 1

        for r in reversed(runs[-25:]):
            c = extra_views.card(body)
            row = ctk.CTkFrame(c, fg_color="transparent")
            row.pack(fill="x", padx=11, pady=(8, 0))
            combo = "/".join(r.get("combo") or []) or "no combo recorded"
            extra_views.lab(row, combo, extra_views.F_BODY, extra_views.T1).pack(side="left")
            extra_views.lab(row, "%s dps" % format(int(r.get("dps", 0)), ","),
                            extra_views.F_MONO, extra_views.GOLD).pack(side="right")

            top = r.get("mobs") or []
            sub = "%d fights · %.0fs · %s damage" % (
                r.get("fights", 0), r.get("duration", 0), format(r.get("damage", 0), ","))
            acc = r.get("accuracy")
            if acc is not None:
                sub += " · %.0f%% hit" % (acc * 100)
            if top:
                sub += "   vs %s" % top[0][0]
            extra_views.lab(c, sub, extra_views.F_SMALL, extra_views.T3)\
                .pack(fill="x", padx=11)

            warn = []
            if r.get("duration", 0) < SHORT_RUN_SECS:
                warn.append("short run — one proc chain can dominate this")
            if multi and top:
                warn.append("different target than some other runs")
            if warn:
                extra_views.lab(c, "⚠ " + "; ".join(warn), extra_views.F_SMALL,
                                extra_views.WARN).pack(fill="x", padx=11)

            b = r.get("breakdown") or {}
            parts = [(k, v) for k, v in b.items() if v]
            if parts:
                extra_views.lab(
                    c, "   ".join("%s %s" % (k, format(v, ",")) for k, v in parts),
                    extra_views.F_SMALL, extra_views.T2).pack(fill="x", padx=11)
            holder = ctk.CTkFrame(c, fg_color="transparent")
            holder.pack(fill="x", padx=11, pady=(0, 9))
            extra_views.bar(holder, r.get("dps", 0) / peak, extra_views.GOLD, height=5)

        if multi:
            extra_views.lab(
                body,
                "Runs above cover more than one target. Mob level and AC move DPS more than "
                "most gear changes — compare like against like.",
                extra_views.F_SMALL, extra_views.WARN).pack(fill="x", padx=14, pady=(8, 12))

    app.redraw_dps_test = redraw
