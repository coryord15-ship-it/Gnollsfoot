# Gnoll Guard

A lightweight **Quest Journal** and **Item Database** companion for *EverQuest Legends* (EQL).

Gnoll Guard sits quietly next to your game, reads your character log, and helps you
keep track of the quests you're working on and the items you find — nothing more.

---

## What it does

### 📜 Quest Journal
- Track the quests you're actively working on with a step-by-step checklist.
- When you loot a required item, the matching step ticks off automatically — no manual bookkeeping.
- Completed turn-ins clear themselves from the journal.
- An optional always-on-top overlay keeps your active quests visible while you play.

### 🗂️ Item Database
- Look up items — stats, drop locations, and vendor info — from the community database.
- As you loot and inspect items in-game, Gnoll Guard silently contributes what it sees to the
  shared database so everyone's lookups get better over time.
- Optional on-screen capture reads item stats from a tooltip and pre-fills a submission for you.

That's the whole scope: **a quest tracker and an item reference tool.**

---

## Installing

Download the latest release from the
[Releases page](https://github.com/coryord15-ship-it/Gnollsfoot/releases/latest):

- **GnollGuard-Setup.exe** — installer (recommended).
- **GnollGuard.exe** — standalone portable build.

On first launch, a short setup wizard helps you point Gnoll Guard at your EverQuest
log file. That's the only setup required.

---

## Running from source

Requires **Python 3.11** on Windows.

```sh
py -3.11 -m pip install -r requirements.txt
py -3.11 app/main.py
```

Or use the helper: `Run-Dev.bat`.

---

## Building the executable

The Windows build is produced with [PyInstaller](https://pyinstaller.org/) from
`GnollGuard.spec`:

```sh
py -3.11 -m pip install pyinstaller
py -3.11 -m PyInstaller GnollGuard.spec
```

The build bundles only the `app/` package and its declared dependencies. Development-only
helpers under `tools/` are not included in the shipped executable.

---

## Project layout

```
app/            Application source
  main.py         Entry point + wiring
  log_watcher.py  Reads the EverQuest log for loot/quest/zone events
  quest_progress.py  Ticks off Quest Journal steps as items are looted
  parsers/        Log-line + item-tooltip parsers
  db/             Local SQLite item/quest cache
  sync/           Community database sync (Supabase)
  ui/             Main window, overlay, and settings
  alerts/         In-app quest-item notifications
assets/         Icons (no sound files)
config/         Default configuration
installer/      Windows installer scripts
supabase/       Community database schema/migrations
tests/          Test suite (pytest)
tools/          Development-only utilities (not shipped)
```

---

## Running the tests

```sh
py -3.11 -m pytest
```

---

## License

See repository settings for licensing details.
```
