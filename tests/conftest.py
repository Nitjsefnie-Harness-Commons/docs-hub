import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCHEMA = _REPO / "backend" / "schema.sql"


@pytest.fixture(scope="session", autouse=True)
def _test_env(tmp_path_factory):
    # Recreate a clean docs_test DB.
    subprocess.run(["dropdb", "--if-exists", "docs_test"], check=True)
    subprocess.run(["createdb", "docs_test"], check=True)
    subprocess.run(["psql", "-q", "-d", "docs_test", "-f", str(_SCHEMA)], check=True)
    # The auth pool needs a users table with a jsonb config column; host it
    # in the same test DB so tests are self-contained (no external auth DB).
    subprocess.run(
        ["psql", "-q", "-d", "docs_test", "-c",
         "CREATE TABLE IF NOT EXISTS users ("
         "user_id BIGINT PRIMARY KEY, "
         "config JSONB NOT NULL DEFAULT '{}'::jsonb)"],
        check=True)
    store = tmp_path_factory.mktemp("store")
    os.environ["DOCS_HUB_DATABASE_URL_DOCS"] = "postgresql:///docs_test"
    os.environ["DOCS_HUB_DATABASE_URL_AUTH"] = "postgresql:///docs_test"
    os.environ["DOCS_HUB_STORE_ROOT"] = str(store)
    os.environ["DOCS_HUB_SESSION_SECRET"] = "test-session-secret"
    os.environ["DOCS_HUB_API_KEY"] = "test-api-key"
    os.environ["DOCS_HUB_COOKIE_SECURE"] = "0"
    yield
    # Close the pools first: their open connections would make dropdb fail,
    # leaving docs_test behind, and their worker threads stall interpreter exit.
    from backend import db
    db.close_pools()
    subprocess.run(["dropdb", "--if-exists", "docs_test"], check=True)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncate docs tables before each test for isolation."""
    from backend import db
    with db.docs_conn() as c:
        c.execute("TRUNCATE docs RESTART IDENTITY CASCADE")
        c.commit()
    yield
