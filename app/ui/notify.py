"""Attention-getting for alerts: a sound, and a flashing taskbar button.

WHY THIS EXISTS
    Owner, 2026-08-16: *"if we are going to alart the person its there we need some
    kinda alart sound plus a blinking taskbar/alart tab indicating theres an alart in
    this window (the taskbar) and on this section of the app (alart window tab)."*

    A quest-item alert that only appears in a list the player is not looking at is not
    an alert. The player is in-game, full-screen, with Gnoll Guard behind it — the only
    two channels that reach them there are audio and the taskbar button.

DESIGN RULES, both learned the hard way on this project
    1. **Never fire when the player is already looking at it.** If the window is focused
       AND the Alerts section is open, the row appearing IS the notification. Beeping at
       someone who is watching the thing beep is how a feature gets switched off.
    2. **Never let this raise.** These are cosmetic. A ctypes call that fails on some
       Windows build must not take down loot handling — every entry point swallows.

Everything degrades to a no-op off Windows, so the Linux port keeps working.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"

# FlashWindowEx flags (winuser.h)
_FLASHW_STOP = 0x00000000
_FLASHW_CAPTION = 0x00000001
_FLASHW_TRAY = 0x00000002
_FLASHW_TIMERNOFG = 0x0000000C      # flash until the window comes to the foreground


_SAMPLE_RATE = 44100
_wav_cache: dict[int, bytes] = {}


def _chime_wav(volume: int) -> bytes:
    """A two-tone rising chime as an in-memory WAV, rendered at `volume` (0-100).

    🔴 WHY SYNTHESISE INSTEAD OF winsound.Beep. Owner asked for a volume slider, and
    Beep has NO volume control — it is a fixed-level square wave on the system speaker.
    MessageBeep is worse: it plays whatever generic sound the user has assigned, so it
    is indistinguishable from every other notification on the machine, and the whole
    point is that the player recognises THIS sound while staring at the game.

    Rendering a WAV also lets us play it with SND_ASYNC, which returns immediately —
    so unlike Beep it never stalls the Tk main thread and needs no helper thread.

    Sine, not square: a square wave at any useful volume is genuinely unpleasant to
    hear repeatedly, and this fires every time a quest item drops.
    """
    import math
    import struct

    vol = max(0, min(100, int(volume))) / 100.0
    # SQUARE the fader, not cube. Perceived loudness is roughly logarithmic, so a raw
    # linear slider feels dead until ~70% and then blasts. But cubic overcorrects: it
    # put 25% at 1.6% of full amplitude, i.e. the bottom half of the slider was
    # inaudible — measured, not guessed. Squared puts 25% at ~6% and 50% at 25%, which
    # keeps the whole travel useful. This is an ALERT; a setting the player can't hear
    # is the same as off, and they already have a switch for off.
    amp = int(32767 * 0.85 * (vol ** 2))

    frames = bytearray()
    for freq, ms in ((880, 110), (1175, 170)):      # A5 -> D6, reads as "look at me"
        n = int(_SAMPLE_RATE * ms / 1000)
        for i in range(n):
            # Fade both ends ~6ms, or the discontinuity clicks audibly on every play.
            edge = int(_SAMPLE_RATE * 0.006)
            env = min(1.0, i / edge, (n - i) / edge) if edge else 1.0
            s = int(amp * env * math.sin(2 * math.pi * freq * i / _SAMPLE_RATE))
            frames += struct.pack("<h", s)

    data = bytes(frames)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " \
        + struct.pack("<IHHIIHH", 16, 1, 1, _SAMPLE_RATE, _SAMPLE_RATE * 2, 2, 16) \
        + b"data" + struct.pack("<I", len(data))
    return hdr + data


def play_alert_sound(volume: int = 70) -> None:
    """Play the alert chime at `volume` (0-100). 0 is silent. Never raises.

    Rendering ~12k samples takes a few ms, so each volume level is cached — the sound
    fires on every quest-item drop and re-rendering it each time would be wasteful for
    no benefit.
    """
    if not _IS_WINDOWS:
        return
    vol = max(0, min(100, int(volume)))
    if vol <= 0:
        return                                   # slider at zero == muted
    try:
        import winsound
        wav = _wav_cache.get(vol)
        if wav is None:
            wav = _wav_cache[vol] = _chime_wav(vol)
        # SND_ASYNC returns immediately; SND_NODEFAULT means a failure is silent rather
        # than substituting the Windows "ding", which would be confusing.
        winsound.PlaySound(wav, winsound.SND_MEMORY | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
    except Exception:
        log.debug("alert sound unavailable", exc_info=True)


def flash_taskbar(window, count: int = 0) -> None:
    """Flash the taskbar button until the user brings the window to the foreground.

    count=0 with FLASHW_TIMERNOFG means "keep flashing until focused", which is the
    behaviour asked for — the player may be mid-fight and come back in a minute, and a
    flash that stopped after three blinks would be gone by then.
    """
    if not _IS_WINDOWS or window is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        hwnd = window.winfo_id()
        # winfo_id() is the Tk child HWND; the taskbar button belongs to the top-level
        # owner, so walk up. Flashing the child silently does nothing.
        root = ctypes.windll.user32.GetAncestor(hwnd, 2)   # GA_ROOT
        info = FLASHWINFO(
            ctypes.sizeof(FLASHWINFO), root or hwnd,
            _FLASHW_TRAY | _FLASHW_CAPTION | _FLASHW_TIMERNOFG, count, 0,
        )
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        log.debug("taskbar flash failed", exc_info=True)


def stop_flashing(window) -> None:
    """Cancel a pending flash — call when the user actually looks at the alerts."""
    if not _IS_WINDOWS or window is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        hwnd = window.winfo_id()
        root = ctypes.windll.user32.GetAncestor(hwnd, 2)
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), root or hwnd, _FLASHW_STOP, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        log.debug("stop flash failed", exc_info=True)


# ── On-screen toast, over the game ───────────────────────────────────────────
#
# Owner, 2026-08-16: *"maybe a quick 10sec overlay popup on the screen they have
# everquest legends playing on about the alart as an optional turn on off by default"*,
# then *"make it 5 seconds"*.
#
# 🔴 THREE THINGS THIS MUST NEVER DO, all of which would be worse than no feature:
#   1. **Take focus.** Stealing focus from a game mid-fight is unforgivable. The window
#      is created with WS_EX_NOACTIVATE and never calls focus_force/grab_set.
#   2. **Appear by default.** It is opt-in; see the settings switch.
#   3. **Outlive its welcome.** It self-destructs after `seconds`, and clicking it
#      dismisses it early.
#
# ⚠ HONEST LIMITATION: if the game runs in EXCLUSIVE full-screen, Windows composites
# nothing above it and this will not be visible. That is an OS behaviour, not a bug to
# fix here — borderless/windowed works, and the settings text says so plainly rather
# than letting the user think the feature is broken.

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080     # keep it out of alt-tab and the taskbar

_EQ_TITLE_HINTS = ("everquest", "eqgame")


def _game_monitor_rect():
    """(left, top, right, bottom) of the monitor EverQuest is on, or None.

    Finds the game window by title, then asks Windows which monitor holds it and for
    that monitor's WORK area. Falls back to None so the caller can use the primary
    screen — a toast on the wrong monitor still beats no toast.
    """
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value.lower()
            if any(h in t for h in _EQ_TITLE_HINTS):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(_enum, 0)
        if not found:
            return None

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        hmon = user32.MonitorFromWindow(found[0], 2)      # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        log.debug("could not locate the game monitor", exc_info=True)
        return None


def show_toast(parent, title: str, body: str, seconds: int = 5, color: str = "#C8960C"):
    """Borderless, non-focusing pop-up on the game's monitor. Auto-closes."""
    try:
        import tkinter as tk

        top = tk.Toplevel(parent)
        top.overrideredirect(True)              # no title bar, no chrome
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.94)
        except Exception:
            pass

        frame = tk.Frame(top, bg="#12100F", highlightbackground=color,
                         highlightthickness=2)
        frame.pack(fill="both", expand=True)
        tk.Frame(frame, bg=color, height=3).pack(fill="x")
        # ⚠ padx/pady on a plain tk.Label must be INTEGERS. A (top, bottom) tuple is a
        # pack() option, not a widget option, and tk rejects it at runtime with
        # `bad screen distance "10 2"` — which compiles perfectly and only fails when
        # the widget is actually built. Asymmetric spacing goes in pack().
        tk.Label(frame, text=title, bg="#12100F", fg=color,
                 font=("Segoe UI", 13, "bold"), anchor="w",
                 ).pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(frame, text=body, bg="#12100F", fg="#D8D2C8",
                 font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=380).pack(fill="x", padx=14, pady=(0, 12))

        top.update_idletasks()
        w = max(320, min(440, top.winfo_reqwidth()))
        h = top.winfo_reqheight()

        rect = _game_monitor_rect()
        if rect:
            left, topc, right, bottom = rect
        else:
            left, topc = 0, 0
            right, bottom = top.winfo_screenwidth(), top.winfo_screenheight()
        # Top-right of the game's monitor: out of the way of the action, and where
        # every other notification on Windows appears, so it reads as a notification.
        x = right - w - 24
        y = topc + 24
        top.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

        # Must be applied AFTER the window exists, or the style is overwritten.
        if _IS_WINDOWS:
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
                cur = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, _GWL_EXSTYLE, cur | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
            except Exception:
                log.debug("could not set no-activate style", exc_info=True)

        def _close():
            try:
                top.destroy()
            except Exception:
                pass

        for w_ in (top, frame):
            w_.bind("<Button-1>", lambda _e: _close())       # click to dismiss early
        top.after(max(1, int(seconds)) * 1000, _close)
        return top
    except Exception:
        log.debug("toast failed", exc_info=True)
        return None


def window_has_focus(window) -> bool:
    """True if this app currently owns keyboard focus.

    Used to decide whether an alert needs to shout. Tk's focus_displayof() returns None
    when the focus is in another application, which is exactly the case we care about.
    """
    try:
        return window.focus_displayof() is not None
    except Exception:
        return False        # unknown => assume not focused => do notify
