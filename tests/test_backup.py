"""SQLite backups (v1.4.1): backup.py, and boot.py's copy before a migration.

The production database is one SQLite file on a Fly volume. These pin:
the copy is consistent (it includes commits still sitting in the WAL), it is
integrity-checked before it takes its final name, the retention policy keeps
what DEPLOY.md promises, and boot never migrates an existing database
without a copy unless told to.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import backup
import boot
from tests.test_deploy_config import HEAD_PREVIOUS, _head, _url, _version


def _make_db(path: Path, rows: int = 3, *, wal: bool = False) -> sqlite3.Connection:
    """A small database; the connection is returned still open so a test can
    keep writing (or keep a WAL un-checkpointed)."""
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE trips (id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO trips (name) VALUES (?)", [(f"trip {i}",) for i in range(rows)])
    con.commit()
    return con


def _names(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT name FROM trips ORDER BY id")]
    finally:
        con.close()


def _touch_daily(folder: Path, d: date) -> Path:
    p = folder / f"dispatch-{d:%Y-%m-%d}.db"
    p.write_bytes(b"")
    return p


# ---------------------------------------------------------------------------
# The copy
# ---------------------------------------------------------------------------

def test_copy_is_a_whole_readable_database(tmp_path):
    src = tmp_path / "dispatch.db"
    _make_db(src).close()
    made = backup.copy_database(src, tmp_path / "out" / "copy.db")
    assert made == tmp_path / "out" / "copy.db"
    assert _names(made) == ["trip 0", "trip 1", "trip 2"]
    assert not list((tmp_path / "out").glob("*.partial"))


def test_copy_includes_commits_still_in_the_wal(tmp_path):
    """A plain file copy of dispatch.db would miss these: they sit in
    dispatch.db-wal until a checkpoint. The backup API doesn't."""
    src = tmp_path / "dispatch.db"
    live = _make_db(src, wal=True)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("INSERT INTO trips (name) VALUES ('committed after the last checkpoint')")
    live.commit()
    assert (tmp_path / "dispatch.db-wal").stat().st_size > 0
    try:
        made = backup.copy_database(src, tmp_path / "copy.db")
    finally:
        live.close()
    assert _names(made)[-1] == "committed after the last checkpoint"


def test_missing_database_is_a_backup_error(tmp_path):
    with pytest.raises(backup.BackupError, match="no database file"):
        backup.copy_database(tmp_path / "nope.db", tmp_path / "copy.db")


def test_a_copy_that_fails_its_integrity_check_never_takes_the_name(tmp_path, monkeypatch):
    src = tmp_path / "dispatch.db"
    _make_db(src).close()
    monkeypatch.setattr(backup, "integrity_check", lambda path: "row 3 missing from index")
    with pytest.raises(backup.BackupError, match="integrity check: row 3 missing"):
        backup.copy_database(src, tmp_path / "copy.db")
    assert not (tmp_path / "copy.db").exists()
    assert not (tmp_path / "copy.db.partial").exists()


def test_integrity_check_reads_a_real_database(tmp_path):
    src = tmp_path / "dispatch.db"
    _make_db(src).close()
    assert backup.integrity_check(src) == "ok"


def test_daily_backup_is_named_by_utc_date_and_a_rerun_replaces_it(tmp_path):
    src = tmp_path / "dispatch.db"
    con = _make_db(src)
    when = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)
    first = backup.daily_backup(src, now=when)
    assert first == tmp_path / "backups" / "dispatch-2026-11-18.db"
    con.execute("INSERT INTO trips (name) VALUES ('later the same day')")
    con.commit()
    con.close()
    second = backup.daily_backup(src, now=when + timedelta(minutes=10))
    assert second == first
    assert _names(second)[-1] == "later the same day"


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_prune_keeps_30_daily_and_the_first_of_12_months(tmp_path):
    start = date(2025, 1, 1)
    days = [start + timedelta(days=i) for i in range(500)]          # 2025-01-01 .. 2026-05-15
    for d in days:
        _touch_daily(tmp_path, d)
    removed = backup.prune_daily(tmp_path, keep_daily=30, keep_monthly=12)
    kept = sorted(p.name for p in tmp_path.glob("dispatch-*.db"))

    newest_30 = {f"dispatch-{d:%Y-%m-%d}.db" for d in days[-30:]}
    assert newest_30 <= set(kept)
    firsts = sorted({f"dispatch-{d:%Y-%m}-01.db" for d in days})[-12:]
    assert set(firsts) <= set(kept)
    assert len(kept) == len(newest_30 | set(firsts))
    assert len(removed) == 500 - len(kept)


def test_prune_leaves_other_files_alone(tmp_path):
    for i in range(40):
        _touch_daily(tmp_path, date(2026, 10, 1) + timedelta(days=i))
    others = [
        tmp_path / "dispatch-pre-migration-47830a2466c9-20261101T000000Z.db",
        tmp_path / "dispatch-manual-before-repair.db",
        tmp_path / "notes.txt",
    ]
    for p in others:
        p.write_bytes(b"")
    backup.prune_daily(tmp_path, keep_daily=5, keep_monthly=0)
    assert all(p.exists() for p in others)
    assert len(list(tmp_path.glob("dispatch-2026-*.db"))) == 5


def test_prune_rejects_a_policy_that_keeps_nothing(tmp_path):
    with pytest.raises(ValueError):
        backup.prune_daily(tmp_path, keep_daily=0)


def test_pre_migration_copies_keep_the_newest_ten(tmp_path):
    src = tmp_path / "dispatch.db"
    _make_db(src).close()
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for i in range(12):
        backup.pre_migration_backup(src, "47830a2466c9", now=t0 + timedelta(hours=i))
    copies = sorted(p.name for p in (tmp_path / "backups").glob("dispatch-pre-migration-*.db"))
    assert len(copies) == backup.KEEP_PRE_MIGRATION == 10
    assert copies[0].endswith("20261101T020000Z.db")              # the two oldest went


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def test_cli_backs_up_prunes_and_reports(tmp_path, capsys):
    src = tmp_path / "dispatch.db"
    _make_db(src).close()
    dest = tmp_path / "copies"
    dest.mkdir()
    for i in range(3):
        _touch_daily(dest, date(2020, 1, 1) + timedelta(days=i))
    assert backup.main(["--db", str(src), "--dest", str(dest), "--keep-daily", "1", "--keep-monthly", "0"]) == 0
    out = capsys.readouterr().out
    assert "integrity ok" in out and "pruned 3 old copies" in out
    [kept] = dest.glob("dispatch-*.db")
    assert _names(kept) == ["trip 0", "trip 1", "trip 2"]


def test_cli_reads_the_database_from_database_url(tmp_path, monkeypatch):
    src = tmp_path / "dispatch.db"
    _make_db(src).close()
    monkeypatch.setenv("DATABASE_URL", _url(src))
    assert backup.main([]) == 0
    assert len(list((tmp_path / "backups").glob("dispatch-*.db"))) == 1


def test_cli_fails_loudly_without_a_database(tmp_path, capsys):
    assert backup.main(["--db", str(tmp_path / "missing.db")]) == 1
    assert "FAILED" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["postgresql://u:p@db/x", "sqlite://", "sqlite:///:memory:"])
def test_cli_refuses_a_database_url_without_a_file(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(SystemExit, match="pass --db"):
        backup.main([])


# ---------------------------------------------------------------------------
# boot.py: a copy before every migration of an existing database
# ---------------------------------------------------------------------------

def _older_db(tmp_path, monkeypatch) -> Path:
    from alembic import command
    db = tmp_path / "dispatch.db"
    monkeypatch.setenv("DATABASE_URL", _url(db))
    command.upgrade(boot._alembic_config(), HEAD_PREVIOUS)
    monkeypatch.delenv("DATABASE_URL")
    return db


def test_boot_copies_the_database_before_migrating_it(tmp_path, monkeypatch, capsys):
    db = _older_db(tmp_path, monkeypatch)
    assert boot.migrate(_url(db)) == "upgraded"
    [copy] = (tmp_path / "backups").glob(f"dispatch-pre-migration-{HEAD_PREVIOUS}-*.db")
    assert _version(copy) == HEAD_PREVIOUS          # the copy is the pre-migration schema
    assert _version(db) == _head()
    assert "backed up the database before migrating" in capsys.readouterr().err


def test_boot_makes_no_copy_when_nothing_migrates(tmp_path):
    db = tmp_path / "dispatch.db"
    boot.migrate(_url(db))                           # fresh: built at head, nothing to save
    boot.migrate(_url(db))                           # restart: already at head
    assert not (tmp_path / "backups").exists()


def test_boot_refuses_to_migrate_when_the_copy_fails(tmp_path, monkeypatch):
    db = _older_db(tmp_path, monkeypatch)

    def broken(*a, **kw):
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "pre_migration_backup", broken)
    with pytest.raises(SystemExit, match="Nothing was migrated"):
        boot.migrate(_url(db))
    assert _version(db) == HEAD_PREVIOUS


def test_boot_can_be_told_to_migrate_without_a_copy(tmp_path, monkeypatch, capsys):
    db = _older_db(tmp_path, monkeypatch)
    monkeypatch.setenv("SKIP_PRE_MIGRATION_BACKUP", "1")
    assert boot.migrate(_url(db)) == "upgraded"
    assert not (tmp_path / "backups").exists()
    assert "without a copy" in capsys.readouterr().err
