"""Alembic migration environment.

Reads DATABASE_URL from the environment (or `.env` via python-dotenv) so the
same migration scripts run against local SQLite, the Fly.io SQLite-on-volume
deployment, and a future Postgres database without config drift between
environments.

target_metadata = Base.metadata — autogenerate diffs against the current
ORM model definitions in `models.py`.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the app modules importable when alembic is invoked from the
# repository root (which is the documented runbook).
HERE = os.path.dirname(os.path.abspath(__file__))
DISPATCH_ROOT = os.path.dirname(HERE)
if DISPATCH_ROOT not in sys.path:
    sys.path.insert(0, DISPATCH_ROOT)

# Load .env so DATABASE_URL is available when running `alembic upgrade head`
# from the shell (not just from inside the FastAPI process).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(DISPATCH_ROOT, ".env"))
except ImportError:
    pass  # python-dotenv is in requirements.txt; this only trips in a broken venv

from database import Base  # noqa: E402  — must follow sys.path tweak
import models  # noqa: F401, E402  — registers all ORM models on Base.metadata

config = context.config

# Configure Python logging from alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the alembic.ini sqlalchemy.url with the env-provided DATABASE_URL.
# This lets the same alembic.ini work across SQLite dev, SQLite-on-Fly, and
# Postgres without environment-specific config files.
database_url = os.getenv("DATABASE_URL", "sqlite:///./dispatch.db")
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render migrations as SQL without connecting to a database.

    Useful for generating SQL scripts to hand to a DBA or to apply in a
    locked-down production environment via psql.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite needs batch mode for ALTER TABLE migrations (renames /
        # column drops are emulated via table-copy). Harmless on Postgres.
        render_as_batch=database_url.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=database_url.startswith("sqlite"),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
