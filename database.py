"""SQLAlchemy engine and session setup."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dispatch.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_session():
    """FastAPI dependency that yields a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Create tables from current models, and add columns we've introduced
    since the original schema. Idempotent.

    `create_all` only creates *missing* tables; it does NOT add columns to
    tables that already exist. For SQLite (dev + tests/e2e DBs), we patch
    in the v0.9.9 `auth_token` column with a defensive `ALTER TABLE` so
    upgrading an existing dispatch.db doesn't require manual surgery.
    Production deploys on Postgres should use a real migration tool
    (Alembic); SQLite is dev-only.
    """
    from sqlalchemy import inspect, text

    import models  # noqa: F401 — importing the module registers every ORM table on Base.metadata before create_all()

    Base.metadata.create_all(bind=engine)

    # Defensive in-place migration for SQLite-backed dev/test DBs: add the
    # `auth_token` column on `drivers` if it isn't there yet.
    if DATABASE_URL.startswith("sqlite"):
        inspector = inspect(engine)
        if "drivers" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("drivers")}
            if "auth_token" not in cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE drivers ADD COLUMN auth_token VARCHAR(64)"))
