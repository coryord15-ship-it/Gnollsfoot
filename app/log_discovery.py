"""Find EverQuest log files and inventory dumps, including rotated archives.

🔴 THIS MODULE READS FILES THE GAME ITSELF WROTE. NOTHING ELSE.
It opens `eqlog_*.txt`, their `.bak` archives, and `<Character>-Inventory.txt`. There is no
network call here, no external tool is imported, and no path outside the EverQuest install
and this app's own folders is ever searched. That is deliberate and load-bearing: the
Companion is a log reader, and the code must make that obvious by inspection rather than by
promise.

⚠️ LOGS DO NOT ALL LIVE BESIDE THE LIVE LOG. When a log grows past its threshold the app
archives it, and the archive folder has moved between versions — older installs kept it under
`~/Documents/GnollGuard/logs_archive`, far from the game directory. A naive `eqlog_*.txt`
glob therefore misses the overwhelming majority of a long-running player's history, so every
known location is searched and `.bak` files are treated as archive, never as junk.

Search order, all optional and all skipped silently when absent:
  1. whatever the user configured (a file or a directory)
  2. the EverQuest install's `Logs` folder, then the install root
  3. this app's archive folders, current and historical
"""
from __future__ import annotations

import glob
import logging
import os

log = logging.getLogger(__name__)

#: Default EverQuest Legends install. Only used when config gives us nothing.
_EQ_DEFAULT = ("C:/Users/Public/Daybreak Game Company/Installed Games/"
               "EverQuest Legends")

_CFG_KEYS = ("log_file_path", "log_dir", "eq_log_dir", "archive_dir")


def _app_home() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", "GnollGuard")


def candidate_dirs(config=None) -> list:
    """Every directory that might hold logs or archives — existing ones only, deduped."""
    out = []

    def add(p):
        try:
            if p and os.path.isdir(p) and p not in out:
                out.append(p)
        except OSError:
            pass

    cfg = config if hasattr(config, "get") else {}
    for key in _CFG_KEYS:
        v = cfg.get(key)
        if not v:
            continue
        add(v if os.path.isdir(v) else os.path.dirname(v))

    add(os.path.join(_EQ_DEFAULT, "Logs"))
    add(_EQ_DEFAULT)
    add(os.path.join(_app_home(), "logs_archive"))
    add(_app_home())
    return out


def log_files(config=None, live_only: bool = False) -> list:
    """Every EverQuest log, newest first.

    `live_only` restricts to logs the game is actively writing (`.txt`), excluding archives.
    """
    found = {}
    pats = ("eqlog_*.txt",) if live_only else ("eqlog_*.txt", "eqlog_*.bak", "eqlog_*.txt.bak")
    for d in candidate_dirs(config):
        for pat in pats:
            try:
                matches = glob.glob(os.path.join(d, pat))
            except OSError:
                continue
            for p in matches:
                # Guard the loose ".bak" patterns: only ever accept EverQuest logs, never
                # some unrelated backup that happens to share the folder.
                if not os.path.basename(p).lower().startswith("eqlog_"):
                    continue
                try:
                    found[os.path.abspath(p)] = os.path.getmtime(p)
                except OSError:
                    continue
    return [p for p, _ in sorted(found.items(), key=lambda kv: -kv[1])]


def newest_live_log(config=None) -> str:
    """The log currently being written, or "" — never an archive."""
    files = log_files(config, live_only=True)
    return files[0] if files else ""


def inventory_roots(config=None) -> list:
    """Directories EverQuest writes `<Character>-Inventory.txt` into.

    The dump lands in the install ROOT, not in `Logs`, so each candidate's parent is
    searched as well.
    """
    roots = []
    for d in candidate_dirs(config):
        for cand in (d, os.path.dirname(d.rstrip("\\/"))):
            try:
                if cand and os.path.isdir(cand) and cand not in roots:
                    roots.append(cand)
            except OSError:
                continue
    return roots


def inventory_files(config=None) -> list:
    """Every `*-Inventory.txt` dump found, newest first."""
    found = {}
    for d in inventory_roots(config):
        try:
            # 🔴 Currencies.txt TOO, and it is badly named. It is not just coin: wind runes,
            # motes and every EXALTATION SLOT live in it, in the same tab-separated shape as
            # the inventory dump. Reading only *-Inventory.txt made the journal blind to all
            # of it -- a quest step needing a wind rune read as "missing" while the owner was
            # carrying eleven, because runes are CURRENCY and never appear in the bag dump.
            matches = (glob.glob(os.path.join(d, "*-Inventory.txt"))
                       + glob.glob(os.path.join(d, "Currencies.txt"))
                       + glob.glob(os.path.join(d, "*-Currencies.txt")))
        except OSError:
            continue
        for p in matches:
            try:
                found[os.path.abspath(p)] = os.path.getmtime(p)
            except OSError:
                continue
    return [p for p, _ in sorted(found.items(), key=lambda kv: -kv[1])]


def newest_inventory(config=None) -> str:
    files = inventory_files(config)
    return files[0] if files else ""
