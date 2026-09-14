"""Postgres pools + a tiny dotenv loader.

Two pools, never joined:
- docs_pool  -> the docs DB (this app's tables)
- auth_pool  -> the shared authdb DB (read users.config for human login)

Both pools check a connection's liveness before handing it out. Without that,
a Postgres restart leaves every idle pooled connection dead and the next
request raises OperationalError instead of reconnecting — publishes returned
HTTP 500 for hours after the 2026-08-06 restart.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

_DOCS: ConnectionPool | None = None
_AUTH: ConnectionPool | None = None


def load_dotenv(path: str = ".env") -> None:
    """Set env vars from a .env file. First '=' splits key/value; quotes are
    NOT stripped; existing vars are never overwritten; '#' lines skipped."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def docs_pool() -> ConnectionPool:
    global _DOCS
    if _DOCS is None:
        _DOCS = ConnectionPool(
            os.environ["DATABASE_URL_DOCS"],
            min_size=1, max_size=8, timeout=10, open=True,
            check=ConnectionPool.check_connection,
            kwargs={"autocommit": False},
        )
    return _DOCS


def auth_pool() -> ConnectionPool:
    global _AUTH
    if _AUTH is None:
        _AUTH = ConnectionPool(
            os.environ["DATABASE_URL_AUTH"],
            min_size=1, max_size=4, timeout=10, open=True,
            check=ConnectionPool.check_connection,
            kwargs={"autocommit": True},
        )
    return _AUTH


def close_pools() -> None:
    """Close both pools and reset the globals so a later call rebuilds them.

    Pool worker threads are non-daemon, so an unclosed pool stalls interpreter
    shutdown 5s per thread, and its still-open connections block dropping the
    database. Safe to call when no pool was ever opened.
    """
    global _DOCS, _AUTH
    for pool in (_DOCS, _AUTH):
        if pool is not None:
            pool.close()
    _DOCS = _AUTH = None


@contextmanager
def docs_conn():
    with docs_pool().connection() as conn:
        yield conn


@contextmanager
def auth_conn():
    with auth_pool().connection() as conn:
        yield conn


def migrate() -> None:
    """Idempotent schema migrations, applied at startup before schema_check."""
    with docs_conn() as c:
        c.execute(
            "ALTER TABLE docs ADD COLUMN IF NOT EXISTS "
            "public boolean NOT NULL DEFAULT false"
        )
        c.execute(
            "ALTER TABLE docs ADD COLUMN IF NOT EXISTS expires_at timestamptz"
        )
        c.execute(
            "ALTER TABLE doc_versions ADD COLUMN IF NOT EXISTS "
            "format text NOT NULL DEFAULT 'html'"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_docs_expires_at ON docs(expires_at) "
            "WHERE expires_at IS NOT NULL"
        )
        c.commit()


def schema_check() -> None:
    """Fail fast at startup if the docs DB or auth DB shape is missing."""
    with docs_conn() as c:
        row = c.execute("SELECT to_regclass('public.doc_versions')").fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("docs.doc_versions missing — run backend/schema.sql")
    with auth_conn() as c:
        row = c.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' "
            "AND column_name='config'"
        ).fetchone()
        if row is None or row[0] != "jsonb":
            raise RuntimeError("auth DB users.config (jsonb) missing")
