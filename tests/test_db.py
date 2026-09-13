import os
import psycopg
from backend import db, docs_repo


def test_load_dotenv_sets_missing_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO_DOCS_TEST=bar\n# comment\n\n", encoding="utf-8")
    os.environ.pop("FOO_DOCS_TEST", None)
    db.load_dotenv(str(env))
    assert os.environ["FOO_DOCS_TEST"] == "bar"


def test_load_dotenv_does_not_overwrite(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO_DOCS_TEST2=fromfile\n", encoding="utf-8")
    os.environ["FOO_DOCS_TEST2"] = "preset"
    db.load_dotenv(str(env))
    assert os.environ["FOO_DOCS_TEST2"] == "preset"


def test_close_pools_closes_both_and_allows_reopening():
    """Pools are process-global and their worker threads outlive the code that
    opened them. Without an explicit close, interpreter shutdown stalls 5s per
    thread and an open connection blocks dropping the database."""
    docs, auth = db.docs_pool(), db.auth_pool()
    assert not docs.closed and not auth.closed

    db.close_pools()

    assert docs.closed and auth.closed
    # The globals are reset, so the next use builds a fresh, working pool.
    with db.docs_conn() as c:
        assert c.execute("SELECT 1").fetchone()[0] == 1
        c.commit()


def test_close_pools_is_idempotent():
    """Teardown paths call it unconditionally, including when no pool was
    ever opened."""
    db.close_pools()
    db.close_pools()


def test_pool_construction_emits_no_deprecation_warning():
    """psycopg_pool is changing the default of its 'open' parameter; relying on
    today's default means a future upgrade silently stops opening the pools."""
    import warnings

    db.close_pools()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # The warning fires on the first checkout, not at construction.
        with db.docs_conn() as c:
            c.execute("SELECT 1")
        with db.auth_conn() as c:
            c.execute("SELECT 1")


def test_app_lifespan_closes_pools_on_shutdown():
    """SIGTERM must not leave pool threads hanging: uvicorn runs the lifespan
    shutdown, which is the only hook that can close them in the service."""
    from fastapi.testclient import TestClient
    from backend.app import app

    with TestClient(app):
        docs = db.docs_pool()
        assert not docs.closed

    assert docs.closed


def _kill_pooled_backends() -> int:
    """Terminate every docs-pool backend server-side, the way a Postgres
    restart does. The pool keeps handing out the now-dead connections."""
    db.docs_pool()  # ensure the pool exists and has opened at least one conn
    with db.docs_conn() as c:
        c.execute("SELECT 1").fetchone()
        c.commit()
    with psycopg.connect(os.environ["DATABASE_URL_DOCS"], autocommit=True) as k:
        return k.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()[0]


def test_docs_conn_survives_server_side_disconnect():
    """A Postgres restart must not poison the pool: the next checkout gets a
    live connection instead of raising OperationalError."""
    _kill_pooled_backends()
    with db.docs_conn() as c:
        assert c.execute("SELECT 1").fetchone()[0] == 1
        c.commit()


def test_publish_survives_server_side_disconnect():
    """The failure that actually bit: publish raised HTTP 500 for hours after
    a Postgres restart because the first pooled query died."""
    _kill_pooled_backends()
    res = docs_repo.publish("t/reconnect", "Reconnect", [], None,
                            "tester", b"<p>hi</p>")
    assert res["version"] == 1


def test_migrate_is_idempotent_and_adds_expires_at():
    """migrate() runs at every startup, so it must be safe to re-apply; it is
    what puts expires_at on a docs table created before the column existed."""
    db.migrate()
    db.migrate()
    with db.docs_conn() as c:
        row = c.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='docs' "
            "AND column_name='expires_at'"
        ).fetchone()
    assert row is not None and row[0] == "timestamp with time zone"
