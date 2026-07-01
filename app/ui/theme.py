"""
Central palette and font constants.
All widget code must import from here — no hex values inline.

Two themes are supported (selectable in Settings):
  * "default" — the original dark-fantasy gold palette.
  * "loggy"   — high-density slate + mint-green accent (the Loggy style guide).

Widgets read `theme.X` at construction time, so calling `theme.apply(name)`
BEFORE the UI is built switches the whole app. A live switch needs a UI rebuild
(restart), which is what Settings tells the user to do.
"""

# ── Theme palettes ─────────────────────────────────────────────────────────────
# Every key listed here is swapped by apply(); keep both palettes in sync.

_DEFAULT = {
    "BG": "#0D0A0B",
    "PANEL": "#1A1416",
    "PANEL_HOVER": "#221C1E",
    "BORDER": "#2E2428",
    "GOLD": "#C8960C",
    "GREEN": "#2D6A2D",
    "TEXT_PRIMARY": "#E8E0D0",
    "TEXT_SECONDARY": "#9E8E7E",
    "TEXT_MUTED": "#5E4E4E",
    "ALERT_ITEM_VERIFIED": "#4CAF50",
    "ALERT_ITEM_UNVERIFIED": "#FFA726",
    "ALERT_QUEST_VERIFIED": "#FFD700",
    "ALERT_QUEST_UNVERIFIED": "#7E57C2",
    "ALERT_RESEARCHING": "#5B8DB8",
    "STATUS_IDLE": "#4CAF50",
    "STATUS_QUEUED": "#FFC107",
    "STATUS_RESEARCHING": "#F44336",
    "STATUS_LOG_WATCHING": "#4CAF50",
    "STATUS_LOG_READING": "#FFC107",
    "STATUS_LOG_DISCONNECTED": "#F44336",
    "DANGER": "#F44336",
    "FONT_HEADER": ("Georgia", 18, "bold"),
    "FONT_SUBHEADER": ("Georgia", 13, "bold"),
    "FONT_BODY": ("Segoe UI", 11),
    "FONT_BODY_SMALL": ("Segoe UI", 9),
    "FONT_MONO": ("Consolas", 10),
    "RADIUS": 8,
    "PAD": 10,
    "PAD_SM": 6,
}

# Light theme — warm off-white surfaces, dark text, deeper gold for contrast.
# Same layout/fonts as the dark (Default) theme; colors only.
_LIGHT = {
    "BG": "#f5f3ee",
    "PANEL": "#ffffff",
    "PANEL_HOVER": "#ece8df",
    "BORDER": "#ddd6c8",
    "GOLD": "#8a6d00",
    "GREEN": "#2f7d32",
    "TEXT_PRIMARY": "#1d1b17",
    "TEXT_SECONDARY": "#5d574c",
    "TEXT_MUTED": "#8c8576",
    "ALERT_ITEM_VERIFIED": "#2e7d32",
    "ALERT_ITEM_UNVERIFIED": "#b8860b",
    "ALERT_QUEST_VERIFIED": "#8a6d00",
    "ALERT_QUEST_UNVERIFIED": "#6b46c1",
    "ALERT_RESEARCHING": "#2563eb",
    "STATUS_IDLE": "#2e7d32",
    "STATUS_QUEUED": "#b8860b",
    "STATUS_RESEARCHING": "#c0392b",
    "STATUS_LOG_WATCHING": "#2e7d32",
    "STATUS_LOG_READING": "#b8860b",
    "STATUS_LOG_DISCONNECTED": "#c0392b",
    "DANGER": "#c0392b",
    "FONT_HEADER": ("Georgia", 18, "bold"),
    "FONT_SUBHEADER": ("Georgia", 13, "bold"),
    "FONT_BODY": ("Segoe UI", 11),
    "FONT_BODY_SMALL": ("Segoe UI", 9),
    "FONT_MONO": ("Consolas", 10),
    "RADIUS": 8,
    "PAD": 10,
    "PAD_SM": 6,
}

# "default" = our dark (gold) theme; "light" = the light theme.
_THEMES = {"default": _DEFAULT, "light": _LIGHT}

# Active theme name (updated by apply()).
ACTIVE = "default"


def apply(name: str):
    """Swap the module-level palette/font constants to the named theme."""
    global ACTIVE
    palette = _THEMES.get((name or "default").lower(), _DEFAULT)
    globals().update(palette)
    ACTIVE = (name or "default").lower() if name in _THEMES else "default"


# Apply the default palette at import so every constant exists immediately.
apply("default")

# ── Fixed (theme-independent) constants ────────────────────────────────────────
ALERT_WIDTH      = 344
ALERT_HEIGHT     = 172
ALERT_MAX_HEIGHT = 172
