"""Archived log filenames must state the date range they cover.

Owner, 2026-08-08, after log rotation moved 12 files out of his game folder:
"im worried people are gonna look for their logs and not know where they are".
The answer chosen was to RENAME IN PLACE — the archive stays in the EQ Logs
folder beside the live log — with a name that shows the span at a glance:

    eqlog_Morbid_freeport.txt
    eqlog_Morbid_freeport_2026-07-11_to_2026-08-07.bak

Two rules these tests exist to defend:
  * the ".bak" extension is load-bearing. Archives live in the SAME folder as
    live logs and the watcher globs "eqlog_*.txt". If an archive ever matched
    that glob the app would re-ingest its own history and double-count.
  * never mix a date parsed from the log with the file's mtime. Doing so
    invented a "Jul 11 to Aug 08" span for a log whose lines were all Jul 11,
    just because the file had been touched today.
"""
import fnmatch
import os

from app.log_watcher import LogWatcher

TS = "[Sat Jul 11 13:11:40 2026] first line"
TS_LATER = "[Fri Aug 07 23:47:35 2026] last line"


def _write(tmp_path, name, lines, pad=0):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n" + ("x" * pad), encoding="utf-8")
    return str(p)


def test_name_shows_the_full_span(tmp_path):
    p = _write(tmp_path, "eqlog_Morbid_freeport.txt", [TS, TS_LATER])
    assert LogWatcher.archive_name_for(p) == \
        "eqlog_Morbid_freeport_2026-07-11_to_2026-08-07.bak"


def test_single_day_log_is_not_written_as_a_range(tmp_path):
    p = _write(tmp_path, "eqlog_Morbid_freeport.txt",
               [TS, "[Sat Jul 11 23:00:00 2026] later same day"])
    assert LogWatcher.archive_name_for(p) == "eqlog_Morbid_freeport_2026-07-11.bak"


def test_parsed_date_is_never_mixed_with_mtime(tmp_path):
    """Only the START parses; the tail is junk. The span must stay Jul 11 — NOT
    'Jul 11 to <today>', which is what mixing the two sources produced."""
    p = _write(tmp_path, "eqlog_Morbid_freeport.txt", [TS], pad=200_000)
    assert LogWatcher.archive_name_for(p) == "eqlog_Morbid_freeport_2026-07-11.bak"


def test_archive_never_matches_the_watchers_glob(tmp_path):
    """The whole safety of renaming in place rests on this."""
    p = _write(tmp_path, "eqlog_Morbid_freeport.txt", [TS, TS_LATER])
    assert not fnmatch.fnmatch(LogWatcher.archive_name_for(p), "eqlog_*.txt")


def test_only_oversized_logs_are_archived(tmp_path):
    """One big log used to drag every other character's log along with it."""
    big = _write(tmp_path, "eqlog_Big_freeport.txt", [TS, TS_LATER], pad=200_000)
    small = _write(tmp_path, "eqlog_Small_freeport.txt", [TS, TS_LATER])
    w = LogWatcher({"log_dir": str(tmp_path)})
    w.rotate_to(None, min_bytes=100_000)
    left = sorted(os.listdir(tmp_path))
    assert "eqlog_Small_freeport.txt" in left, "small log must be left alone"
    assert not os.path.exists(big), "oversized log should have been archived"
    assert any(n.endswith(".bak") for n in left)


def test_archive_stays_in_the_same_folder(tmp_path):
    p = _write(tmp_path, "eqlog_Morbid_freeport.txt", [TS, TS_LATER], pad=200_000)
    w = LogWatcher({"log_dir": str(tmp_path)})
    dest = w.rotate_to(None, min_bytes=1)
    assert os.path.dirname(dest) == str(tmp_path), \
        "archives must never leave the folder the user is looking in"
