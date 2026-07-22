"""Shared quest-journal step display.

Used by BOTH the shipped app's Quest Journal tab (app/ui/main_window.py) and the private
Officer Console's "My Journal" tab. Renders one journaled quest's steps against a live
quest_matcher.QuestMatcher — so a step shows done/active exactly when the SAME log-driven
matching logic the shipped app uses considers it done. That parity is the point: it lets an
officer see whether a quest's steps actually tick from real play, or are silently broken,
using the identical engine a player runs.

Kept deliberately NARROW — presentation + quest_matcher reads only:
  - No devkit imports (quest_board, bank_harvest, command_center, officer_console).
  - No board-posting, no DB writes beyond what the caller does via matcher.mark_done/
    mark_undone.
  - The Officer Console layers board-posting and add-a-quest UI on top by passing an
    `extra_header` callback; this module must never grow that logic itself.

Import direction: devkit -> app only (Officer Console imports this module). The app NEVER
imports anything from devkit/GnollLoot-docs.
"""
from __future__ import annotations

import customtkinter as ctk

from app import quest_matcher

# Default palette — the app's dark-gold HUD colors. Callers (e.g. the Officer Console's
# steel-cyan theme) can override any of these via the `theme` dict param.
_DEFAULT_THEME = {
    "panel": "#1B2530",
    "panel_hover": "#263340",
    "gold": "#C8960C",
    "green": "#3FB950",
    "text": "#E6EDF3",
    "text_secondary": "#8899A8",
    "border": "#263340",
    "font_body": ("Segoe UI", 12),
    "font_body_small": ("Segoe UI", 11),
    "font_subheader": ("Segoe UI Semibold", 14),
}


def _theme(overrides):
    t = dict(_DEFAULT_THEME)
    if overrides:
        t.update(overrides)
    return t


def render_quest_card(
    parent,
    quest: dict,
    matcher: "quest_matcher.QuestMatcher",
    prog: set,
    given: set,
    theme: dict | None = None,
    on_toggle_step=None,
    on_copy=None,
    extra_header=None,
):
    """Build one quest card (steps, done/active state, required items) into `parent`.

    quest: a journaled quest dict — {"id", "quest_name", "zone", "steps": [...], ...} shaped
        like the Supabase journal rows the app already renders.
    matcher: the live QuestMatcher whose persisted step state drives done/active.
    prog: set of lower-cased item names the player has looted (for the ✓/○ item markers).
    given: set of lower-cased item names already turned in (✔ marker).
    theme: optional dict of color/font overrides (see _DEFAULT_THEME).
    on_toggle_step(quest, step, was_done): called on the Mark done/undone click. Omit to
        hide the button (read-only display).
    on_copy(text): clipboard copy for the /waypoint button. Omit to hide the button.
    extra_header(card, title_row): optional hook to add caller-specific controls (e.g. the
        Officer Console's "I'll build the walkthrough" board button) to the title row.

    Returns the card frame.
    """
    t = _theme(theme)
    card = ctk.CTkFrame(parent, fg_color=t["panel"], corner_radius=8)
    card.pack(fill="x", padx=8, pady=4)

    steps = sorted(quest.get("steps", []) or [], key=lambda s: s.get("step_order", 0))
    structured = [s for s in steps if s.get("action_type")]

    title_row = ctk.CTkFrame(card, fg_color="transparent")
    title_row.pack(fill="x", padx=10, pady=(8, 0))
    ctk.CTkLabel(
        title_row, text=quest.get("quest_name", "Quest"), font=t["font_subheader"],
        text_color=t["gold"], anchor="w",
    ).pack(side="left")
    if structured and matcher:
        done_n, total_n = matcher.progress(quest)
        ctk.CTkLabel(
            title_row, text=f"  {done_n}/{total_n}", font=t["font_body_small"],
            text_color=t["text_secondary"],
        ).pack(side="left")
    if extra_header:
        try:
            extra_header(card, title_row)
        except Exception:
            pass
    if quest.get("zone"):
        ctk.CTkLabel(
            card, text=quest["zone"], font=t["font_body_small"],
            text_color=t["text_secondary"], anchor="w",
        ).pack(anchor="w", padx=10)

    # The active step = the first not-yet-done structured step whose prerequisites (if
    # any) are already done — mirrors quest_matcher's own eligibility rule so the
    # highlighted step is always the one that can actually complete next.
    done_by_order = {}
    if matcher:
        for s in structured:
            done_by_order[s.get("step_order")] = matcher.is_step_done(quest.get("id"), s.get("step_order"))
    active_order = None
    for s in structured:
        order = s.get("step_order")
        if done_by_order.get(order):
            continue
        prereqs = s.get("prerequisite_step_orders") or []
        if prereqs and not all(done_by_order.get(p) for p in prereqs):
            continue
        active_order = order
        break

    for s in steps:
        num = s.get("step_order", "")
        npc = s.get("npc_name") or ""
        is_structured = bool(s.get("action_type"))
        is_done = is_structured and done_by_order.get(num, False)
        is_active = is_structured and num == active_order

        row = ctk.CTkFrame(
            card, corner_radius=6,
            fg_color=(t["panel_hover"] if is_done else "transparent"),
            border_width=2 if is_active else 0,
            border_color=t["gold"],
        )
        row.pack(fill="x", padx=10, pady=(6, 0))

        mark = "✓ " if is_done else ("► " if is_active else "")
        head = f"{mark}{num}. {npc}" if npc else f"{mark}{num}."
        head_color = t["green"] if is_done else (t["gold"] if is_active else t["text"])
        ctk.CTkLabel(
            row, text=head, font=t["font_body_small"],
            text_color=head_color, anchor="w",
        ).pack(anchor="w", padx=(4, 4), pady=(2, 0))
        if s.get("instruction"):
            ctk.CTkLabel(
                row, text="   " + s["instruction"], font=t["font_body_small"],
                text_color=t["text_secondary"], anchor="w", justify="left", wraplength=440,
            ).pack(anchor="w", padx=(4, 4))

        req = s.get("required_items") or [i.get("item_name") for i in (s.get("items") or []) if i.get("item_name")]
        if req:
            irow = ctk.CTkFrame(row, fg_color="transparent")
            irow.pack(anchor="w", padx=(4, 4), pady=(2, 0))
            ctk.CTkLabel(
                irow, text="   Items:", font=t["font_body_small"], text_color=t["text_secondary"],
            ).pack(side="left")
            for it in req:
                low = it.lower()
                if low in given:
                    imark, col = "  ✔ ", t["green"]
                elif low in prog:
                    imark, col = "  ✓ ", t["gold"]
                else:
                    imark, col = "  ○ ", t["text_secondary"]
                ctk.CTkLabel(
                    irow, text=imark + it, font=t["font_body_small"], text_color=col,
                ).pack(side="left")

        if is_structured and (on_toggle_step or on_copy):
            btn_row = ctk.CTkFrame(row, fg_color="transparent")
            btn_row.pack(anchor="w", padx=(4, 4), pady=(2, 6))
            entity = s.get("entities")
            wp = quest_matcher.waypoint_command(entity) if entity else None
            if wp and on_copy:
                ctk.CTkButton(
                    btn_row, text="📍 Copy /waypoint", width=140, height=22,
                    font=t["font_body_small"], fg_color=t["panel_hover"],
                    text_color=t["text_secondary"], hover_color=t["border"],
                    command=lambda cmd=wp: on_copy(cmd),
                ).pack(side="left", padx=(0, 6))
            if on_toggle_step:
                toggle_text = "Mark undone" if is_done else "Mark done"
                ctk.CTkButton(
                    btn_row, text=toggle_text, width=100, height=22,
                    font=t["font_body_small"], fg_color=t["panel_hover"],
                    text_color=t["text_secondary"], hover_color=t["border"],
                    command=lambda qq=quest, ss=s, done=is_done: on_toggle_step(qq, ss, done),
                ).pack(side="left")

    if quest.get("reward_items"):
        ctk.CTkLabel(
            card, text="Rewards: " + ", ".join(quest["reward_items"]),
            font=t["font_body_small"], text_color=t["gold"], anchor="w",
        ).pack(anchor="w", padx=10, pady=(6, 8))
    else:
        ctk.CTkLabel(card, text="", font=t["font_body_small"]).pack(pady=(0, 8))

    return card
