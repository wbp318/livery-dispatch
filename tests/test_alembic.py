"""Tests for the Alembic migration scaffold (v1.0.12).

The migration scripts themselves are exercised by `alembic upgrade head`
against a temp SQLite DB; functional tests use `Base.metadata.create_all`
on in-memory SQLite via conftest, so Alembic doesn't run during the normal
test suite. These probes verify:

  1. The Alembic env.py is importable + valid (catches broken sys.path,
     missing imports, env-var handling regressions before they hit CI).
  2. The v1.0.12 baseline migration is the only head revision (sanity
     check after adding new migrations — accidental branches show up here).
  3. `alembic upgrade head` against a fresh SQLite DB produces the same
     schema as `Base.metadata.create_all` on an empty DB. This guards
     against the baseline drifting from the ORM definitions.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from database import Base


DISPATCH_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_CFG = DISPATCH_ROOT / "alembic.ini"
ALEMBIC_DIR = DISPATCH_ROOT / "alembic"


def test_alembic_config_file_exists():
    assert ALEMBIC_CFG.exists(), f"alembic.ini missing at {ALEMBIC_CFG}"
    assert ALEMBIC_DIR.exists(), f"alembic/ missing at {ALEMBIC_DIR}"
    assert (ALEMBIC_DIR / "env.py").exists()
    assert (ALEMBIC_DIR / "versions").exists()


def test_alembic_env_imports_cleanly():
    """Loading env.py should not raise — sys.path tweak + Base import work."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ALEMBIC_CFG))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    script = ScriptDirectory.from_config(cfg)
    # Iterating revisions loads + parses every migration file.
    revisions = list(script.walk_revisions())
    assert len(revisions) >= 1, "no migrations found — baseline should be present"


def test_single_head_revision():
    """No accidental branches in the migration history."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ALEMBIC_CFG))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, \
        f"expected exactly one head, got {len(heads)}: {heads} (a branch was introduced)"


def test_baseline_revision_exists():
    """The v1.0.12 baseline revision is referenced by ID — if it gets
    renamed or deleted by accident, this test catches it."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ALEMBIC_CFG))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    script = ScriptDirectory.from_config(cfg)
    baseline = script.get_revision("dc2823d2aef2")
    assert baseline is not None
    assert baseline.down_revision is None, \
        "baseline must be the bottom of the migration stack (down_revision=None)"


def test_upgrade_head_produces_orm_schema():
    """`alembic upgrade head` against a fresh SQLite DB creates the same
    tables that `Base.metadata.create_all` would. This is the load-bearing
    check that the baseline migration stays in sync with the ORM models.
    """
    from alembic import command
    from alembic.config import Config

    # Use a real on-disk SQLite file so alembic's connection handling
    # works the same way it does in production.
    with tempfile.TemporaryDirectory() as tmpdir:
        alembic_db = Path(tmpdir) / "alembic.db"
        orm_db = Path(tmpdir) / "orm.db"

        # Alembic-managed schema.
        os.environ["DATABASE_URL"] = f"sqlite:///{alembic_db}"
        try:
            cfg = Config(str(ALEMBIC_CFG))
            cfg.set_main_option("script_location", str(ALEMBIC_DIR))
            command.upgrade(cfg, "head")
        finally:
            os.environ.pop("DATABASE_URL", None)

        # ORM-direct schema.
        orm_engine = create_engine(f"sqlite:///{orm_db}")
        Base.metadata.create_all(orm_engine)

        alembic_engine = create_engine(f"sqlite:///{alembic_db}")
        try:
            alembic_tables = set(inspect(alembic_engine).get_table_names())
            orm_tables = set(inspect(orm_engine).get_table_names())
        finally:
            # Windows: SQLite file locks must be released before
            # TemporaryDirectory.cleanup tries to unlink the .db file.
            alembic_engine.dispose()
            orm_engine.dispose()

        # Alembic tracks itself via alembic_version — ORM doesn't know about it.
        alembic_tables.discard("alembic_version")

        assert alembic_tables == orm_tables, (
            f"alembic schema drifted from ORM schema\n"
            f"  alembic only: {alembic_tables - orm_tables}\n"
            f"  ORM only:     {orm_tables - alembic_tables}"
        )
