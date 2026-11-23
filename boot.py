"""Container entrypoint for the production image (v1.1.21).

    ENTRYPOINT ["python", "boot.py"]
    CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

In order:

1. **Hand the data volume to the app user.** Fly.io mounts volumes owned by
   root. The container starts as root only so this step can ``chown`` the
   SQLite directory (and any existing database files in it) to the
   unprivileged ``dispatch`` user, then drops to that user for good.
2. **Bring the schema to Alembic head.** This runs here, on the machine that
   has the volume, and not in a ``fly.toml`` ``release_command``: Fly runs
   release commands on a temporary machine *without* the app's volumes, so
   with SQLite-on-volume it would migrate a throwaway empty file. Running at
   boot also means no deploy can skip ``alembic upgrade head`` (the v1.1.20
   ``completed_at`` column broke every trip page until someone ran it).
   When a migration is pending on an existing database, ``backup.py`` copies
   it to ``/data/backups/`` first (v1.4.1), and boot stops if it can't.
3. **exec the real command** (uvicorn by default; anything passed to
   ``docker run`` replaces it, so ``docker run IMAGE python cli.py rates list``
   migrates first, then runs the CLI as the app user).

Two database states are handled on purpose:

* **Tables but no ``alembic_version``** (built by ``create_all`` and never
  stamped) refuses to start. Guessing a revision could skip a migration, so
  the operator stamps the right one by hand (DEPLOY.md § 6).
* **A revision this image doesn't know** means the database is ahead of the
  code: a rollback to an older release. Migrations are skipped with a warning
  and the app starts, because older code runs fine on a newer additive
  schema, and a rollback has to be able to boot.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_USER = "dispatch"
DEFAULT_DATABASE_URL = "sqlite:///./dispatch.db"   # same default as database.py


def _log(msg: str) -> None:
    print(f"boot: {msg}", file=sys.stderr, flush=True)


def sqlite_path(url: str) -> Path | None:
    """The database file for a file-backed SQLite URL, else None."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    if u.get_backend_name() != "sqlite" or not u.database or u.database == ":memory:":
        return None
    return Path(u.database)


def prepare_volume(db_file: Path, uid: int, gid: int) -> None:
    """Create the database directory and give it, plus the database file and
    its journal/WAL siblings, to ``uid:gid``. Runs as root, before the drop.

    The ``backups/`` folder and its copies get the same owner (v1.4.1), so a
    copy someone made as root over ``flyctl ssh console`` can't stop the app
    user from writing the next pre-migration backup."""
    data_dir = db_file.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chown(data_dir, uid, gid)
    for f in data_dir.iterdir():
        if f.name.startswith(db_file.name):
            os.chown(f, uid, gid)
    backups = data_dir / "backups"
    if backups.is_dir():
        os.chown(backups, uid, gid)
        for f in backups.iterdir():
            os.chown(f, uid, gid)


def drop_privileges(user: str = APP_USER) -> None:
    """Switch the process to ``user`` for good (groups, then gid, then uid)."""
    import pwd

    pw = pwd.getpwnam(user)
    os.setgroups([])
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir
    os.environ["USER"] = os.environ["LOGNAME"] = user


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(HERE / "alembic.ini"))
    cfg.set_main_option("script_location", str(HERE / "alembic"))
    return cfg


def _backup_before_migrating(url: str, current: str) -> None:
    """Copy the database before ``alembic upgrade`` moves it off ``current``
    (v1.4.1). A migration that goes wrong can then be undone by restoring
    the copy (DEPLOY.md, "Backups"). Fails closed: if the copy can't be
    made, the image refuses to migrate, unless the operator sets
    ``SKIP_PRE_MIGRATION_BACKUP=1`` on purpose. Only file-backed SQLite has
    a file to copy; Postgres brings its own point-in-time recovery."""
    db_file = sqlite_path(url)
    if db_file is None or not db_file.is_file():
        return
    if os.environ.get("SKIP_PRE_MIGRATION_BACKUP", "").strip().lower() in ("1", "true", "yes"):
        _log(f"WARNING: SKIP_PRE_MIGRATION_BACKUP is set: migrating off {current} without a copy")
        return
    import backup

    try:
        made = backup.pre_migration_backup(db_file, current)
    except (backup.BackupError, OSError) as exc:
        raise SystemExit(
            f"boot: couldn't back up the database before migrating it off revision {current}: "
            f"{exc}. Nothing was migrated. Free space on the volume and redeploy, or set "
            "SKIP_PRE_MIGRATION_BACKUP=1 to migrate without a copy (DEPLOY.md, Backups)."
        ) from exc
    _log(f"backed up the database before migrating off {current}: {made}")


def migrate(url: str) -> str:
    """Upgrade the database at ``url`` to head. Returns what happened:
    ``"upgraded"`` or ``"ahead"`` (rollback; skipped). Raises SystemExit for
    an unstamped ``create_all`` database.

    ``alembic/env.py`` reads ``DATABASE_URL`` from the environment, so it is
    set to ``url`` for the duration of the call.
    """
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        current = None
        if "alembic_version" in tables:
            with engine.connect() as conn:
                current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()

    if tables - {"alembic_version"} and "alembic_version" not in tables:
        raise SystemExit(
            "boot: the database has tables but no alembic_version, so it was built by "
            "create_all and never stamped. Refusing to guess its revision. Stamp the "
            "revision that matches its schema, then redeploy: `alembic stamp <revision>` "
            "(DEPLOY.md § 6)."
        )

    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    known = {rev.revision for rev in script.walk_revisions()}
    if current and current not in known:
        _log(
            f"WARNING: the database is at revision {current}, which this image doesn't "
            "know: it is newer than this release (a rollback?). Skipping migrations and "
            "starting anyway. If this release needs the older schema, run "
            "`alembic downgrade` from the newer release first (DEPLOY.md, Rollback)."
        )
        return "ahead"

    if current and current != script.get_current_head():
        _backup_before_migrating(url, current)

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    return "upgraded"


def main(argv: list[str]) -> None:
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    db_file = sqlite_path(url)

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        import pwd

        try:
            pw = pwd.getpwnam(APP_USER)
        except KeyError:
            raise SystemExit(
                f"boot: running as root but there is no '{APP_USER}' user to drop to. "
                "Refusing to run the app as root."
            ) from None
        if db_file is not None and db_file.is_absolute():
            prepare_volume(db_file, pw.pw_uid, pw.pw_gid)
        drop_privileges(APP_USER)
        _log(f"dropped root; running as {APP_USER} (uid {pw.pw_uid})")

    outcome = migrate(url)
    if outcome == "upgraded":
        _log("schema at alembic head")

    if not argv:
        raise SystemExit("boot: no command to run (the image's CMD starts uvicorn)")
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
