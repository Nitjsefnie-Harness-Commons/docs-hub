"""The reaper is a lifespan background task; drive it through the app's
lifespan (TestClient as a context manager runs startup/shutdown) with a
tiny interval, and through `loop` directly for the error path."""
import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from backend import db, docs_repo, reaper
from backend.app import app

KEY = {"x-docs-key": "test-api-key"}


def _backdate(slug):
    with db.docs_conn() as c:
        c.execute("UPDATE docs SET expires_at = now() - interval '1 second' "
                  "WHERE slug=%s", (slug,))
        c.commit()


def _row_count(slug):
    with db.docs_conn() as c:
        return c.execute("SELECT count(*) FROM docs WHERE slug=%s",
                         (slug,)).fetchone()[0]


def test_lifespan_reaper_purges_expired_docs(monkeypatch):
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "0.05")
    with TestClient(app) as c:
        c.post("/api/publish",
               data={"slug": "reap/me", "title": "R", "from": "analyst",
                     "ttl": "1h"},
               files={"file": ("d.html", b"<h1>x</h1>", "text/html")},
               headers=KEY)
        _backdate("reap/me")
        deadline = time.monotonic() + 5
        while _row_count("reap/me") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _row_count("reap/me") == 0
        assert not os.path.exists(
            os.path.join(os.environ["STORE_ROOT"], "reap/me"))


def test_loop_survives_a_failing_purge(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db went away")
        return 0

    monkeypatch.setattr(docs_repo, "purge_expired", boom)

    async def run():
        task = asyncio.get_running_loop().create_task(reaper.loop(0.01))
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert len(calls) >= 2


def test_start_reads_the_interval_from_the_environment(monkeypatch):
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "123.5")
    seen = {}

    async def fake_loop(interval):
        seen["interval"] = interval

    monkeypatch.setattr(reaper, "loop", fake_loop)

    async def run():
        task = reaper.start()
        await task

    asyncio.run(run())
    assert seen["interval"] == 123.5


@pytest.mark.parametrize("raw", ["0", "-5", "soon"])
def test_start_rejects_a_non_positive_or_unparseable_interval(monkeypatch, raw):
    """Zero or a negative interval turns asyncio.sleep into a bare yield, so
    the reaper would spin on the DB forever; an unparseable one used to blow
    up deep inside the lifespan. Both fail loudly at start()."""
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", raw)

    async def fake_loop(interval):
        seen.append(interval)

    seen = []
    monkeypatch.setattr(reaper, "loop", fake_loop)

    async def run():
        reaper.start()

    with pytest.raises(ValueError, match="PURGE_INTERVAL_SECONDS"):
        asyncio.run(run())
    assert not seen


def test_lifespan_closes_the_pools_when_the_interval_is_bad(monkeypatch):
    """reaper.start() runs inside the lifespan's try, so a rejected interval
    still unwinds through db.close_pools() instead of leaving the pools (and
    their non-daemon worker threads) open."""
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError, match="PURGE_INTERVAL_SECONDS"):
        with TestClient(app):
            pass
    # The pool global is the only observable proof the finally ran.
    # pylint: disable-next=protected-access
    assert db._DOCS is None
