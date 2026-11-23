"""Consistent SQLite backups for the dispatch database (v1.4.1).

    python backup.py                          # back up DATABASE_URL's file, then prune
    python backup.py --db /data/dispatch.db   # name the file explicitly (what DEPLOY.md runs)
    python backup.py --keep-daily 30 --keep-monthly 12

Three layers protect the production database (DEPLOY.md, "Backups"):

1. **Fly volume snapshots**, daily and automatic, kept 30 days
   (``snapshot_retention`` in ``fly.toml``). They live off the volume, so
   they survive losing it.
2. **A copy before every migration.** ``boot.py`` calls
   :func:`pre_migration_backup` when the image is about to move the schema
   forward, and refuses to migrate if it can't make one.
3. **This command**, on demand (before a risky change, or to download a
   copy off Fly with ``flyctl ssh sftp get``). It keeps 30 daily copies plus
   the first copy of each of the last 12 months.

Why the SQLite backup API and not ``cp``: copying the file while uvicorn
writes to it can capture a half-written page, and in WAL mode the newest
commits sit in ``-wal`` beside it. ``Connection.backup`` copies a
transactionally consistent snapshot, and every copy then has to pass
``PRAGMA integrity_check`` before it takes its final name.

Stdlib only, and no imports from the app, so it runs before the app can
(``boot.py``) and on a machine whose app won't boot.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DAILY_RE = re.compile(r"^dispatch-(\d{4})-(\d{2})-(\d{2})\.db$")
PRE_MIGRATION_PREFIX = "dispatch-pre-migration-"
KEEP_PRE_MIGRATION = 10


class BackupError(RuntimeError):
    """The copy could not be made or did not pass its integrity check."""


def backup_dir_for(db_file: Path) -> Path:
    """Where backups go by default: ``backups/`` beside the database
    (``/data/backups`` on Fly, on the same volume as the database)."""
    return db_file.parent / "backups"


def _match_owner(path: Path, like: Path) -> None:
    """Run as root (``flyctl ssh console``), give ``path`` to the owner of
    ``like`` so the app user can still read the copy and write the folder
    when it makes the next pre-migration backup."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    st = like.stat()
    os.chown(path, st.st_uid, st.st_gid)


def integrity_check(db_file: Path) -> str:
    """``"ok"``, or the first problem ``PRAGMA integrity_check`` reports."""
    con = sqlite3.connect(str(db_file))
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def copy_database(db_file: Path, dest: Path) -> Path:
    """Write a consistent copy of ``db_file`` to ``dest`` and check it.

    The copy is written beside ``dest`` under a temporary name and renamed
    into place only after ``PRAGMA integrity_check`` returns ``ok``, so a
    file with a backup's name is always a whole, readable database.
    """
    db_file = Path(db_file)
    dest = Path(dest)
    if not db_file.is_file():
        raise BackupError(f"no database file at {db_file}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _match_owner(dest.parent, db_file.parent)
    tmp = dest.with_name(dest.name + ".partial")
    tmp.unlink(missing_ok=True)

    src = sqlite3.connect(str(db_file))
    try:
        out = sqlite3.connect(str(tmp))
        try:
            src.backup(out)
        finally:
            out.close()
    except sqlite3.Error as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"backup of {db_file} failed: {exc}") from exc
    finally:
        src.close()

    result = integrity_check(tmp)
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise BackupError(f"the copy of {db_file} failed its integrity check: {result}")

    os.replace(tmp, dest)
    _match_owner(dest, db_file)
    return dest


def daily_backup(db_file: Path, dest_dir: Path | None = None, *, now: datetime | None = None) -> Path:
    """``dispatch-YYYY-MM-DD.db`` (UTC date). A second run the same day
    replaces that day's copy with a newer one."""
    now = now or datetime.now(timezone.utc)
    dest_dir = Path(dest_dir) if dest_dir else backup_dir_for(Path(db_file))
    return copy_database(Path(db_file), dest_dir / f"dispatch-{now:%Y-%m-%d}.db")


def pre_migration_backup(db_file: Path, from_revision: str, *, now: datetime | None = None) -> Path:
    """The copy ``boot.py`` makes before ``alembic upgrade head`` moves the
    schema off ``from_revision``. Restoring it puts the data back exactly
    as the previous release left it."""
    now = now or datetime.now(timezone.utc)
    safe_rev = re.sub(r"[^A-Za-z0-9_]", "_", from_revision)
    dest = backup_dir_for(Path(db_file)) / f"{PRE_MIGRATION_PREFIX}{safe_rev}-{now:%Y%m%dT%H%M%SZ}.db"
    made = copy_database(Path(db_file), dest)
    prune_pre_migration(dest.parent)
    return made


def prune_daily(dest_dir: Path, *, keep_daily: int = 30, keep_monthly: int = 12) -> list[Path]:
    """Delete daily copies outside the retention policy; return what went.

    Kept: the newest ``keep_daily`` copies, plus the earliest copy of each
    of the newest ``keep_monthly`` months. Only files named exactly
    ``dispatch-YYYY-MM-DD.db`` are touched; anything else in the folder
    (pre-migration copies, a file an operator put there) is left alone.
    """
    if keep_daily < 1 or keep_monthly < 0:
        raise ValueError("keep_daily must be at least 1 and keep_monthly at least 0")
    dated: list[tuple[str, Path]] = []
    for p in Path(dest_dir).glob("dispatch-*.db"):
        m = DAILY_RE.match(p.name)
        if m:
            dated.append(("-".join(m.groups()), p))
    dated.sort(reverse=True)                      # newest first

    keep = {p for _, p in dated[:keep_daily]}
    first_of_month: dict[str, Path] = {}
    for day, p in sorted(dated):                  # oldest first: first one wins
        first_of_month.setdefault(day[:7], p)
    for month in sorted(first_of_month, reverse=True)[:keep_monthly]:
        keep.add(first_of_month[month])

    removed = [p for _, p in dated if p not in keep]
    for p in removed:
        p.unlink()
    return removed


def prune_pre_migration(dest_dir: Path, *, keep: int = KEEP_PRE_MIGRATION) -> list[Path]:
    """Keep the newest ``keep`` pre-migration copies (the timestamp in the
    name sorts them)."""
    copies = sorted(Path(dest_dir).glob(f"{PRE_MIGRATION_PREFIX}*.db"), key=lambda p: p.name, reverse=True)
    removed = copies[keep:]
    for p in removed:
        p.unlink()
    return removed


def _db_file_from_env() -> Path:
    url = os.environ.get("DATABASE_URL", "sqlite:///./dispatch.db")
    if not url.startswith("sqlite:///"):
        raise SystemExit(f"backup: DATABASE_URL is not a SQLite file ({url.split(':', 1)[0]}); pass --db")
    path = url[len("sqlite:///"):]
    if not path or path == ":memory:":
        raise SystemExit("backup: DATABASE_URL names an in-memory database; pass --db")
    return Path(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consistent SQLite backup of the dispatch database.")
    parser.add_argument("--db", type=Path, help="database file (default: the file DATABASE_URL names)")
    parser.add_argument("--dest", type=Path, help="backup folder (default: backups/ beside the database)")
    parser.add_argument("--keep-daily", type=int, default=30)
    parser.add_argument("--keep-monthly", type=int, default=12)
    args = parser.parse_args(argv)

    db_file = args.db or _db_file_from_env()
    dest_dir = args.dest or backup_dir_for(db_file)
    try:
        made = daily_backup(db_file, dest_dir)
    except BackupError as exc:
        print(f"backup: FAILED: {exc}", file=sys.stderr)
        return 1
    removed = prune_daily(dest_dir, keep_daily=args.keep_daily, keep_monthly=args.keep_monthly)
    print(f"backup: wrote {made} ({made.stat().st_size:,} bytes, integrity ok)")
    if removed:
        print(f"backup: pruned {len(removed)} old copies: {', '.join(p.name for p in removed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
