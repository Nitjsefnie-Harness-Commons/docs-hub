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


def test_migrate_adds_format_to_doc_versions():
    """The format column is what tells a stored version apart as Markdown; a
    doc_versions table created before it existed gets it from migrate()."""
    db.migrate()
    db.migrate()
    with db.docs_conn() as c:
        row = c.execute(
            "SELECT data_type, column_default, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='doc_versions' "
            "AND column_name='format'"
        ).fetchone()
    assert row is not None
    assert row[0] == "text" and row[1] == "'html'::text" and row[2] == "NO"


def _column(table: str, column: str):
    """(data_type, column_default, is_nullable) for one column, or None."""
    with db.docs_conn() as c:
        return c.execute(
            "SELECT data_type, column_default, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
            (table, column),
        ).fetchone()


# The two tests below drop a column, which the autouse _clean_tables fixture
# does NOT restore -- it truncates rows, not schema. What restores it is
# migrate() itself, which each test calls in a finally: before its assertions,
# so the table is whole again by the time the test ends and every later test
# sees the full schema. The finally is what makes that unconditional -- a
# failure between the DROP and the migrate() would otherwise hand every
# following test a table missing its column, turning one real failure into a
# cascade of unrelated ones.


def test_migrate_backfills_format_on_a_table_that_already_has_rows():
    """The upgrade path the idempotency tests cannot reach: schema.sql builds
    doc_versions with `format` already on it, so re-running migrate() against
    it proves only that ADD COLUMN IF NOT EXISTS is a no-op. A deployed
    instance instead has a table that predates the column and already holds
    rows, and the NOT NULL DEFAULT has to backfill them -- that is what makes
    every version published before the upgrade read as HTML afterwards."""
    with db.docs_conn() as c:
        c.execute("ALTER TABLE doc_versions DROP COLUMN format")
        c.commit()
    try:
        # Raw SQL rather than publish(): the publishing code writes `format`,
        # so it cannot insert into the pre-migration shape at all.
        with db.docs_conn() as c:
            doc_id = c.execute(
                "INSERT INTO docs (slug, title, latest_version) "
                "VALUES ('m/legacy', 'Legacy', 1) RETURNING id").fetchone()[0]
            c.execute(
                "INSERT INTO doc_versions "
                "(doc_id, version, posted_by, file_path, byte_size, sha256) "
                "VALUES (%s, 1, 'analyst', '/store/m/legacy/v1.html', 3, "
                "'abc123')",
                (doc_id,))
            c.commit()
    finally:
        # In a finally so a failure above still hands the next test a whole
        # table; twice because migrate() re-runs at every startup.
        db.migrate()
        db.migrate()

    assert _column("doc_versions", "format") == ("text", "'html'::text", "NO")
    with db.docs_conn() as c:
        row = c.execute("SELECT format FROM doc_versions WHERE doc_id=%s",
                        (doc_id,)).fetchone()
    assert row is not None and row[0] == "html"


def test_migrate_adds_expires_at_to_a_table_that_already_has_rows():
    """Same upgrade path for the docs table: a row that predates expires_at
    has to come out permanent (NULL), not expired -- a non-NULL backfill here
    would make every pre-upgrade document vanish the moment the reaper ran."""
    with db.docs_conn() as c:
        c.execute("ALTER TABLE docs DROP COLUMN expires_at")
        c.commit()
    try:
        with db.docs_conn() as c:
            doc_id = c.execute(
                "INSERT INTO docs (slug, title, latest_version) "
                "VALUES ('m/preexisting', 'Preexisting', 0) RETURNING id"
            ).fetchone()[0]
            c.commit()
    finally:
        # In a finally so a failure above still hands the next test a whole
        # table; twice because migrate() re-runs at every startup.
        db.migrate()
        db.migrate()

    assert _column("docs", "expires_at") == (
        "timestamp with time zone", None, "YES")
    with db.docs_conn() as c:
        row = c.execute("SELECT expires_at FROM docs WHERE id=%s",
                        (doc_id,)).fetchone()
    assert row is not None and row[0] is None
    # DROP COLUMN takes the partial index with it, so this is the only place
    # migrate()'s CREATE INDEX IF NOT EXISTS is reachable rather than a no-op.
    with db.docs_conn() as c:
        idx = c.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
            "AND tablename='docs' AND indexname='idx_docs_expires_at'"
        ).fetchone()
    assert idx is not None
