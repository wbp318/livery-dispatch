"""The production image and its Fly.io config (v1.1.21).

``Dockerfile``, ``fly.toml``, ``.dockerignore`` and ``boot.py`` have to agree
with each other and with the app: one port, one volume holding the SQLite
file, one process, production mode, no secrets in git. These tests pin that
without Docker.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import tomllib
from pathlib import Path

import pytest

import boot

DISPATCH = Path(__file__).resolve().parent.parent
HEAD_PREVIOUS = "47830a2466c9"          # the v1.1.0 revision, two before head

SECRET_NAMES = {
    "ADMIN_USERNAME", "ADMIN_PASSWORD_HASH", "ADMIN_SESSION_SECRET", "DRIVER_CSRF_SECRET",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY",
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _dockerfile_instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, args) pairs, with backslash continuations joined."""
    out: list[tuple[str, str]] = []
    buf = ""
    for raw in (DISPATCH / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        line = buf + line
        buf = ""
        instr, _, args = line.partition(" ")
        out.append((instr.upper(), args.strip()))
    return out


def _dockerfile_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for instr, args in _dockerfile_instructions():
        if instr == "ENV":
            for pair in args.split():
                key, _, value = pair.partition("=")
                env[key] = value
    return env


def _exec_form(name: str) -> list[str]:
    [args] = [a for i, a in _dockerfile_instructions() if i == name]
    return json.loads(args)


def _fly() -> dict:
    return tomllib.loads((DISPATCH / "fly.toml").read_text(encoding="utf-8"))


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        for sep in ("[", ">", "<", "=", "~", "!"):
            line = line.split(sep, 1)[0]
        names.add(line.strip().lower())
    return names


def _dockerignored(rel: str) -> bool:
    """Close enough to Docker's matcher for the patterns we use: a path is
    excluded if it, or any parent directory, matches a pattern."""
    patterns = [
        p.strip().rstrip("/")
        for p in (DISPATCH / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if p.strip() and not p.strip().startswith("#")
    ]
    parts = rel.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pat in patterns:
        if pat.startswith("**/"):
            tail = pat[3:]
            if any(fnmatch.fnmatch(part, tail) for part in parts):
                return True
        elif any(fnmatch.fnmatch(prefix, pat) for prefix in prefixes):
            return True
    return False


# ---------------------------------------------------------------------------
# Dockerfile <-> fly.toml <-> app
# ---------------------------------------------------------------------------

def test_image_defaults_to_production_on_the_volume():
    env = _dockerfile_env()
    assert env["DISPATCH_ENV"] == "production"
    db = boot.sqlite_path(env["DATABASE_URL"])
    assert db is not None and db.as_posix().startswith("/"), "the image's DB must be an absolute path"


def test_fly_mounts_the_volume_where_the_database_lives():
    db = boot.sqlite_path(_dockerfile_env()["DATABASE_URL"])
    assert _fly()["mounts"]["destination"] == db.parent.as_posix()


def test_fly_keeps_30_days_of_volume_snapshots():
    """v1.4.1: the off-volume layer of the backup posture (DEPLOY.md, Backups).
    Fly's default is 5 days; 1-60 is the allowed range."""
    mounts = _fly()["mounts"]
    assert mounts["scheduled_snapshots"] is True
    assert mounts["snapshot_retention"] == 30


def test_backups_live_on_the_volume_beside_the_database():
    import backup
    db = boot.sqlite_path(_dockerfile_env()["DATABASE_URL"])
    assert backup.backup_dir_for(db).as_posix() == f"{_fly()['mounts']['destination']}/backups"


def test_entrypoint_is_boot_and_cmd_is_one_uvicorn_worker():
    assert _exec_form("ENTRYPOINT") == ["python", "boot.py"]
    cmd = _exec_form("CMD")
    assert cmd[:2] == ["uvicorn", "app:app"]
    # Rate-limit buckets, the /osrm cache and SQLite all assume one process.
    assert cmd[cmd.index("--workers") + 1] == "1"


def test_fly_internal_port_matches_the_uvicorn_port():
    cmd = _exec_form("CMD")
    assert _fly()["http_service"]["internal_port"] == int(cmd[cmd.index("--port") + 1])


def test_fly_keeps_exactly_one_machine_up_and_checks_health():
    svc = _fly()["http_service"]
    assert svc["force_https"] is True
    assert svc["auto_stop_machines"] == "off"       # calls, SMS, GPS at any hour
    assert svc["min_machines_running"] == 1
    [check] = svc["checks"]
    assert check["path"] == "/health"
    from app import app
    assert "/health" in {getattr(r, "path", None) for r in app.routes}


def test_fly_env_holds_no_secrets_and_the_proxy_settings():
    env = _fly()["env"]
    assert not SECRET_NAMES & set(env), "secrets go in `flyctl secrets set`, never fly.toml"
    assert env["CLIENT_IP_HEADER"] == "Fly-Client-IP"
    assert env["NOTIFICATION_PROVIDER"] == "twilio"
    # One source of truth: these two belong to the image.
    assert not {"DISPATCH_ENV", "DATABASE_URL"} & set(env)


# ---------------------------------------------------------------------------
# What goes into the image
# ---------------------------------------------------------------------------

def test_image_installs_runtime_requirements_only():
    runs = " ".join(a for i, a in _dockerfile_instructions() if i == "RUN")
    assert "pip install -r requirements-runtime.txt" in runs
    assert "requirements.txt" not in runs.replace("requirements-runtime.txt", "")


def test_requirements_txt_is_runtime_plus_dev_tools():
    text = (DISPATCH / "requirements.txt").read_text(encoding="utf-8")
    assert "-r requirements-runtime.txt" in text
    runtime = _requirement_names(DISPATCH / "requirements-runtime.txt")
    assert {"fastapi", "uvicorn", "sqlalchemy", "alembic", "bcrypt", "twilio"} <= runtime
    assert not {"pytest", "bandit", "pip-audit"} & runtime
    assert {"pytest", "bandit", "pip-audit"} <= _requirement_names(DISPATCH / "requirements.txt")


def test_dockerignore_keeps_the_app_and_drops_the_rest():
    needed = [p.name for p in DISPATCH.glob("*.py")]
    needed += ["alembic.ini", "requirements-runtime.txt", "alembic/env.py"]
    needed += [f"alembic/versions/{p.name}" for p in (DISPATCH / "alembic" / "versions").glob("*.py")]
    missing = [rel for rel in needed if _dockerignored(rel)]
    assert not missing, f".dockerignore would drop runtime files: {missing}"
    for rel in ("tests/conftest.py", "osrm/Dockerfile", ".env", "dispatch.db",
                ".e2e_test.db", "alembic/__pycache__/env.cpython-311.pyc"):
        assert _dockerignored(rel), f"{rel} should stay out of the image"


# ---------------------------------------------------------------------------
# boot.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("sqlite:////data/dispatch.db", "/data/dispatch.db"),
    ("sqlite:///./dispatch.db", "dispatch.db"),
    ("sqlite://", None),
    ("sqlite:///:memory:", None),
    ("postgresql://u:p@db.internal/dispatch", None),
])
def test_sqlite_path(url, expected):
    got = boot.sqlite_path(url)
    assert (got.as_posix() if got else None) == (Path(expected).as_posix() if expected else None)


def _head() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(boot._alembic_config()).get_current_head()


def _version(db: Path) -> str | None:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        con.close()


def _url(db: Path) -> str:
    return f"sqlite:///{db.as_posix()}"


def test_migrate_builds_a_fresh_database_at_head(tmp_path):
    db = tmp_path / "fresh.db"
    assert boot.migrate(_url(db)) == "upgraded"
    assert _version(db) == _head()
    assert boot.migrate(_url(db)) == "upgraded"      # idempotent: a restart is a no-op
    assert _version(db) == _head()


def test_migrate_upgrades_an_older_revision(tmp_path, monkeypatch):
    from alembic import command
    db = tmp_path / "old.db"
    monkeypatch.setenv("DATABASE_URL", _url(db))
    command.upgrade(boot._alembic_config(), HEAD_PREVIOUS)
    assert _version(db) == HEAD_PREVIOUS
    assert boot.migrate(_url(db)) == "upgraded"
    assert _version(db) == _head()
    con = sqlite3.connect(db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(trips)")}
    con.close()
    assert "completed_at" in cols


def test_migrate_refuses_an_unstamped_create_all_database(tmp_path):
    from sqlalchemy import create_engine
    import models  # noqa: F401  (registers every table on Base.metadata)
    from database import Base
    db = tmp_path / "legacy.db"
    engine = create_engine(_url(db))
    Base.metadata.create_all(engine)
    engine.dispose()
    with pytest.raises(SystemExit, match="alembic stamp"):
        boot.migrate(_url(db))


def test_migrate_skips_when_the_database_is_ahead_of_the_image(tmp_path, capsys):
    """A rollback to an older release must still boot."""
    db = tmp_path / "ahead.db"
    boot.migrate(_url(db))
    con = sqlite3.connect(db)
    con.execute("UPDATE alembic_version SET version_num = ?", ("ffffffffffff",))
    con.commit()
    con.close()
    assert boot.migrate(_url(db)) == "ahead"
    assert _version(db) == "ffffffffffff"
    assert "Skipping migrations" in capsys.readouterr().err


def test_migrate_leaves_database_url_as_it_found_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./sentinel.db")
    boot.migrate(_url(tmp_path / "x.db"))
    assert os.environ["DATABASE_URL"] == "sqlite:///./sentinel.db"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="as root, main() would drop privileges for real")
def test_main_migrates_then_execs_the_command(tmp_path, monkeypatch):
    db = tmp_path / "main.db"
    monkeypatch.setenv("DATABASE_URL", _url(db))
    calls = []
    monkeypatch.setattr(boot.os, "execvp", lambda file, argv: calls.append((file, argv)))
    boot.main(["uvicorn", "app:app", "--port", "8080"])
    assert _version(db) == _head()
    assert calls == [("uvicorn", ["uvicorn", "app:app", "--port", "8080"])]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="as root, main() would drop privileges for real")
def test_main_without_a_command_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _url(tmp_path / "noargs.db"))
    with pytest.raises(SystemExit, match="no command"):
        boot.main([])
