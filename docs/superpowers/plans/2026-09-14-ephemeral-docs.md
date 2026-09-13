# Ephemeral Documents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A publish can carry a time-to-live; when it elapses the document (every version of it) is unreadable, unlisted, and then physically removed.

**Architecture:** One nullable `expires_at TIMESTAMPTZ` column on `docs` (NULL = permanent). Every read/list/find query in `docs_repo` filters to live rows so an expired doc is gone the instant its time passes, independent of the reaper. A `purge_expired()` repo function deletes expired rows + their blob directories; a small asyncio reaper started from the app lifespan runs it at startup and every `PURGE_INTERVAL_SECONDS` (default 60). The API accepts `ttl` as a duration string (`30m`, `2h`, `7d`, bare integer = seconds) on `POST /api/publish`; the CLI exposes it as `--ttl`.

**Tech Stack:** Python 3.13, FastAPI, psycopg3, Postgres; stdlib-only CLI; vanilla-JS SPA; pytest.

**Spec:** No separate spec file. The binding requirements are the Global Constraints and task texts below (this is a bounded change to an existing repo).

## Global Constraints

- **Column:** `docs.expires_at TIMESTAMPTZ NULL`. NULL means permanent. Partial index `idx_docs_expires_at ON docs(expires_at) WHERE expires_at IS NOT NULL`. Both in `backend/schema.sql` AND applied idempotently by `db.migrate()`.
- **TTL grammar:** `^\s*(\d+)\s*([smhdw]?)\s*$`, case-insensitive on the unit; no unit = seconds; `s`=1, `m`=60, `h`=3600, `d`=86400, `w`=604800. Value must be ≥ 1 second after conversion. Anything else → `ValueError("invalid ttl: <text!r>")`. Empty/whitespace-only string means "no ttl" at the API layer (the form default), NOT an error.
- **Publish semantics:** each publish states the doc's lifetime. `ttl` given → `expires_at = now() + ttl` (computed in SQL, `now() + make_interval(secs => %s)`). `ttl` omitted → `expires_at = NULL` (the doc becomes/stays permanent). This is per-publish, NOT sticky like `public`.
- **Expired = nonexistent:** every read path (`get_latest`, `get_version`, `list_docs`, `find_docs`, `list_versions`, `list_tags`, `is_public`, `set_public`) excludes rows where `expires_at IS NOT NULL AND expires_at <= now()`, via the one shared fragment `_LIVE = "(d.expires_at IS NULL OR d.expires_at > now())"` (every query aliases `docs` as `d`).
- **Republishing an expired slug** deletes the stale row (versions cascade) and its blob directory first, then publishes as a brand-new doc at version 1.
- **Reaper:** `backend/reaper.py`; `PURGE_INTERVAL_SECONDS` env (float, default `60`); one purge at startup then every interval; an exception inside a purge is logged and never kills the loop; cancelled on shutdown.
- **API:** `POST /api/publish` form field `ttl: str = Form("")`; invalid → HTTP 400 `{"ok": false, "error": "invalid ttl: ..."}`; success JSON gains `"expires_at"` (ISO-8601 string or `null`). `GET /api/list` docs gain `"expires_at"` (ISO string or `null`). Nothing else on the API surface changes.
- **CLI:** `docs-hub publish ... [--ttl DURATION]`; the field is sent only when given. Publish output appends ` (expires <iso>)` when the response carries a non-null `expires_at`. `docs-hub list` appends ` expires <iso>` to a row when `expires_at` is non-null. Bump `docs_hub_cli.__version__` to `0.2.0`.
- **Invariants from CONTRIBUTING.md hold:** versions append-only (deleting an EXPIRED doc is not a version drop — the doc as a whole has ended); one anonymous surface (an expired public doc is 404/redirect, never served); parameterised SQL only; stored HTML verbatim.
- **Gates:** `.venv/bin/pytest -q` green (coverage floor 90%); `git ls-files '*.py' | xargs .venv/bin/pycodestyle` clean; `git ls-files '*.py' | xargs .venv/bin/pylint --rcfile=.pylintrc` clean; `.venv/bin/pyright` clean; `npx eslint public` clean (Task 4 only). Run them from the worktree root `/tmp/docs-hub-ephemeral`.
- **Commits:** one logical commit per task, explicit paths (never `git add -A`), trailer exactly `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Tests:** pytest, following the existing style in `tests/` (module-level functions, `_client()` helper, `KEY` header). Backdate an expiry in tests by running `UPDATE docs SET expires_at = now() - interval '1 second' WHERE slug=%s` through `backend.db.docs_conn()` (then `c.commit()`), never by sleeping past a real TTL.

---

### Task 1: Schema, TTL parser, and repository semantics

**Files:**
- Modify: `backend/schema.sql`
- Modify: `backend/db.py` (`migrate()`)
- Create: `backend/ttl.py`
- Modify: `backend/docs_repo.py`
- Test: `tests/test_ttl.py` (create), `tests/test_docs_repo.py` (extend), `tests/test_db.py` (extend, migrate)

**Interfaces:**
- Produces: `backend.ttl.parse_ttl(text: str) -> int` (seconds; raises `ValueError`).
- Produces: `docs_repo.publish(slug, title, tags, project, posted_by, html, ttl_seconds: int | None = None) -> dict` — return dict gains `"expires_at": datetime | None`.
- Produces: `docs_repo.purge_expired() -> int` (count of docs removed).
- Produces: every dict from `docs_repo.list_docs()` gains `"expires_at": datetime | None`.

- [ ] **Step 1: Schema + migration**

`backend/schema.sql` — add to `docs`:
```sql
    expires_at      TIMESTAMPTZ
```
and after the existing index:
```sql
CREATE INDEX IF NOT EXISTS idx_docs_expires_at ON docs(expires_at)
    WHERE expires_at IS NOT NULL;
```

`backend/db.py` `migrate()` — add, after the `public` ALTER and before `c.commit()`:
```python
        c.execute(
            "ALTER TABLE docs ADD COLUMN IF NOT EXISTS expires_at timestamptz"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_docs_expires_at ON docs(expires_at) "
            "WHERE expires_at IS NOT NULL"
        )
```
Update the docstring of `migrate()` if it enumerates migrations. Add to `tests/test_db.py` a test that calls `db.migrate()` twice and then confirms `information_schema.columns` has `expires_at` on `docs` (look at how the existing migrate test for `public` is written and mirror it).

- [ ] **Step 2: Failing tests for the parser** — `tests/test_ttl.py`:

```python
import pytest

from backend.ttl import parse_ttl


@pytest.mark.parametrize("text,seconds", [
    ("30", 30), ("45s", 45), ("2m", 120), ("1h", 3600), ("3d", 259200),
    ("1w", 604800), (" 10M ", 600), ("1H", 3600),
])
def test_parse_ttl_accepts_each_unit(text, seconds):
    assert parse_ttl(text) == seconds


@pytest.mark.parametrize("text", [
    "", "  ", "0", "0h", "-1", "1x", "h", "1.5h", "1h30m", "abc", "1 h 2",
])
def test_parse_ttl_rejects_bad_input(text):
    with pytest.raises(ValueError, match="invalid ttl"):
        parse_ttl(text)
```

Run: `.venv/bin/pytest tests/test_ttl.py -q` — expected: ImportError / FAIL.

- [ ] **Step 3: Implement `backend/ttl.py`**

```python
"""Parse the `ttl` a publish carries into whole seconds.

Grammar: an integer with an optional unit — s, m, h, d, w (case-insensitive);
no unit means seconds. Compound forms (`1h30m`), fractions and zero are
rejected: a TTL is a coarse "how long should this exist", not a schedule.
"""
from __future__ import annotations

import re

_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_TTL_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)


def parse_ttl(text: str) -> int:
    """Seconds for a duration like `30m`, `2h`, `7d` or `90` (bare = seconds).
    Raises ValueError for anything else, including zero."""
    m = _TTL_RE.match(text or "")
    if m is None:
        raise ValueError(f"invalid ttl: {text!r}")
    seconds = int(m.group(1)) * _UNITS[m.group(2).lower()]
    if seconds < 1:
        raise ValueError(f"invalid ttl: {text!r}")
    return seconds
```

Run: `.venv/bin/pytest tests/test_ttl.py -q` — expected: PASS.

- [ ] **Step 4: Failing repository tests** — append to `tests/test_docs_repo.py`:

```python
from datetime import datetime, timedelta, timezone

from backend import db


def _backdate(slug):
    with db.docs_conn() as c:
        c.execute("UPDATE docs SET expires_at = now() - interval '1 second' "
                  "WHERE slug=%s", (slug,))
        c.commit()


def test_publish_with_ttl_sets_expires_at_in_the_future():
    before = datetime.now(timezone.utc)
    res = docs_repo.publish("e/one", "E", [], None, "analyst", b"<h1>1</h1>",
                            ttl_seconds=3600)
    assert res["expires_at"] is not None
    assert before + timedelta(minutes=59) < res["expires_at"]
    assert res["expires_at"] < before + timedelta(minutes=61)
    d = next(x for x in docs_repo.list_docs() if x["slug"] == "e/one")
    assert d["expires_at"] == res["expires_at"]


def test_publish_without_ttl_is_permanent():
    res = docs_repo.publish("e/perm", "E", [], None, "analyst", b"<h1>1</h1>")
    assert res["expires_at"] is None
    d = next(x for x in docs_repo.list_docs() if x["slug"] == "e/perm")
    assert d["expires_at"] is None


def test_republish_without_ttl_clears_expiry():
    docs_repo.publish("e/clear", "E", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=60)
    res = docs_repo.publish("e/clear", "E", [], None, "analyst", b"<h1>2</h1>")
    assert res["version"] == 2
    assert res["expires_at"] is None


def test_republish_with_ttl_resets_expiry_from_now():
    docs_repo.publish("e/reset", "E", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=60)
    res = docs_repo.publish("e/reset", "E", [], None, "analyst", b"<h1>2</h1>",
                            ttl_seconds=7200)
    assert res["version"] == 2
    assert res["expires_at"] > datetime.now(timezone.utc) + timedelta(hours=1)


def test_expired_doc_is_invisible_on_every_read_path():
    docs_repo.publish("e/gone", "Gone", ["t"], "proj", "analyst",
                      b"<h1>1</h1>", ttl_seconds=60)
    docs_repo.set_public("e/gone", True)
    _backdate("e/gone")
    assert docs_repo.get_latest("e/gone") is None
    assert docs_repo.get_version("e/gone", 1) is None
    assert docs_repo.list_versions("e/gone") == []
    assert all(d["slug"] != "e/gone" for d in docs_repo.list_docs())
    assert docs_repo.find_docs({"slug": "e/gone"}) == []
    assert docs_repo.find_docs({"tag": "t"}) == []
    assert docs_repo.list_tags() == []
    assert docs_repo.is_public("e/gone") is False
    assert docs_repo.set_public("e/gone", False) is False


def test_live_ttl_doc_is_still_readable():
    docs_repo.publish("e/live", "Live", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=3600)
    assert docs_repo.get_latest("e/live")["html"] == b"<h1>1</h1>"
    assert [d["slug"] for d in docs_repo.list_docs()] == ["e/live"]


def test_republish_of_expired_slug_starts_fresh_at_v1():
    docs_repo.publish("e/again", "A", [], None, "analyst", b"<h1>old</h1>",
                      ttl_seconds=60)
    _backdate("e/again")
    old_blob = os.path.join(os.environ["STORE_ROOT"], "e/again", "v1.html")
    assert os.path.exists(old_blob)
    res = docs_repo.publish("e/again", "A", [], None, "analyst", b"<h1>new</h1>")
    assert res["version"] == 1
    assert docs_repo.get_version("e/again", 1)["html"] == b"<h1>new</h1>"
    assert docs_repo.list_versions("e/again")[0]["version"] == 1
    assert len(docs_repo.list_versions("e/again")) == 1


def test_purge_expired_removes_rows_and_blobs():
    docs_repo.publish("e/p1", "P", [], None, "analyst", b"<h1>1</h1>",
                      ttl_seconds=60)
    docs_repo.publish("e/p1", "P", [], None, "analyst", b"<h1>2</h1>",
                      ttl_seconds=60)
    docs_repo.publish("e/keep", "K", [], None, "analyst", b"<h1>k</h1>",
                      ttl_seconds=60)
    docs_repo.publish("e/perm", "K", [], None, "analyst", b"<h1>k</h1>")
    _backdate("e/p1")
    assert docs_repo.purge_expired() == 1
    assert not os.path.exists(os.path.join(os.environ["STORE_ROOT"], "e/p1"))
    with db.docs_conn() as c:
        assert c.execute("SELECT count(*) FROM docs WHERE slug='e/p1'"
                         ).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM doc_versions").fetchone()[0] == 2
    assert {d["slug"] for d in docs_repo.list_docs()} == {"e/keep", "e/perm"}
    assert docs_repo.purge_expired() == 0
```

(`os` is already imported at the top of this test file.) Run: `.venv/bin/pytest tests/test_docs_repo.py -q` — expected: the new tests FAIL (unexpected kwarg / missing key / missing function).

- [ ] **Step 5: Implement in `backend/docs_repo.py`**

Add near the top:
```python
# A doc is live until its expiry passes; NULL means permanent. Every query
# that returns or mutates a doc applies this so an expired doc is gone the
# moment its time passes, whether or not the reaper has run yet.
_LIVE = "(d.expires_at IS NULL OR d.expires_at > now())"
```

`publish` — new signature and body:
```python
def publish(slug: str, title: str, tags: list[str], project: str | None,
            posted_by: str, html: bytes,
            ttl_seconds: int | None = None) -> dict:
    """Create a new version of `slug` (creating the doc on first publish).

    `ttl_seconds` states the doc's lifetime from now; None makes it
    permanent. Each publish restates it — the expiry is not sticky. A slug
    whose previous life has expired is purged first and starts over at v1.
    Returns {slug, version, doc_id, expires_at}."""
    if not storage.is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    with db.docs_conn() as c:
        stale = c.execute(
            "DELETE FROM docs WHERE slug=%s AND expires_at IS NOT NULL "
            "AND expires_at <= now() RETURNING id", (slug,)).fetchone()
        if stale is not None:
            storage.delete_doc(slug)
        row = c.execute("SELECT id, latest_version FROM docs WHERE slug=%s",
                        (slug,)).fetchone()
        ... (unchanged create-or-bump logic) ...
        path, size, digest = storage.store_blob(slug, version, html)
        c.execute(... doc_versions insert, unchanged ...)
        updated = c.execute(
            "UPDATE docs SET latest_version=%s, title=%s, tags=%s, project=%s, "
            "updated_at=now(), expires_at=now() + make_interval(secs => %s) "
            "WHERE id=%s RETURNING expires_at",
            (version, title, tags, project, ttl_seconds, doc_id),
        ).fetchone()
        c.commit()
    expires_at = updated[0] if updated is not None else None
    return {"slug": slug, "version": version, "doc_id": doc_id,
            "expires_at": expires_at}
```
`make_interval(secs => NULL)` is NULL, so one UPDATE covers both cases. (pyright: `make_interval` takes a float; passing a Python `int | None` is fine for psycopg.)

Apply `_LIVE` everywhere:
- `_version_row`: `... WHERE d.slug=%s AND v.version=%s AND {_LIVE}` — build the string with an f-string ONLY for the constant fragment; values stay `%s` parameters.
- `get_latest`: `SELECT latest_version FROM docs d WHERE d.slug=%s AND {_LIVE}`.
- `is_public`: `SELECT public FROM docs d WHERE d.slug=%s AND {_LIVE}`.
- `set_public`: `UPDATE docs d SET public=%s WHERE d.slug=%s AND {_LIVE} RETURNING id` (Postgres allows an alias on UPDATE's target).
- `list_docs`: add `d.expires_at` to the SELECT list, add `"expires_at": r[9]` to the dict, and seed `clauses = [_LIVE]` so it is always in the WHERE.
- `find_docs`: seed `clauses = [_LIVE]`.
- `list_versions`: `WHERE d.slug=%s AND {_LIVE}`.
- `list_tags`: seed a `clauses = [_LIVE]` list and join it like the others (this query currently has an optional single WHERE; restructure to the clauses pattern).

Add:
```python
def purge_expired() -> int:
    """Delete every doc whose expiry has passed (versions cascade) and its
    blob directory. Returns the number of docs removed. Same crash ordering
    as delete_docs: rows go first, so a crash leaves orphan blobs, never
    orphan rows that a later publish would trip over."""
    with db.docs_conn() as c:
        rows = c.execute(
            "DELETE FROM docs WHERE expires_at IS NOT NULL "
            "AND expires_at <= now() RETURNING slug").fetchall()
        c.commit()
    for (slug,) in rows:
        storage.delete_doc(slug)
    return len(rows)
```

- [ ] **Step 6: Run the gates**

`.venv/bin/pytest -q` — expected all green (the existing tests keep passing because `expires_at` defaults NULL). Then pycodestyle, pylint, pyright per Global Constraints.

- [ ] **Step 7: Commit**

```bash
git add backend/schema.sql backend/db.py backend/ttl.py backend/docs_repo.py tests/test_ttl.py tests/test_docs_repo.py tests/test_db.py
git commit -m "Add a per-publish TTL so a document can expire"
```

---

### Task 2: API `ttl` field, reaper, and lifespan wiring

**Files:**
- Modify: `backend/api.py` (`publish`, `api_list`)
- Create: `backend/reaper.py`
- Modify: `backend/app.py` (lifespan)
- Modify: `README.md` (Deployment env list)
- Test: `tests/test_app.py` (extend), `tests/test_public.py` (extend), `tests/test_reaper.py` (create)

**Interfaces:**
- Consumes: `backend.ttl.parse_ttl`, `docs_repo.publish(..., ttl_seconds=)`, `docs_repo.purge_expired()`.
- Produces: `reaper.start(interval: float | None = None) -> asyncio.Task`; `reaper.loop(interval: float)` coroutine; `PURGE_INTERVAL_SECONDS` env.

- [ ] **Step 1: Failing API tests** — append to `tests/test_app.py`:

```python
def _backdate(slug):
    from backend import db
    with db.docs_conn() as c:
        c.execute("UPDATE docs SET expires_at = now() - interval '1 second' "
                  "WHERE slug=%s", (slug,))
        c.commit()


def test_publish_with_ttl_returns_expires_at():
    c = _client()
    r = c.post("/api/publish",
               data={"slug": "ttl/one", "title": "T", "from": "analyst",
                     "ttl": "2h"},
               files={"file": ("d.html", b"<h1>x</h1>", "text/html")},
               headers=KEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["expires_at"] is not None
    from datetime import datetime, timedelta, timezone
    exp = datetime.fromisoformat(body["expires_at"])
    assert exp > datetime.now(timezone.utc) + timedelta(minutes=110)
    listed = c.get("/api/list", headers=KEY).json()["docs"]
    d = next(x for x in listed if x["slug"] == "ttl/one")
    assert d["expires_at"] == body["expires_at"]


def test_publish_without_ttl_reports_null_expiry():
    c = _client()
    r = _publish(c, "ttl/none")
    assert r.json()["expires_at"] is None
    listed = c.get("/api/list", headers=KEY).json()["docs"]
    assert next(x for x in listed if x["slug"] == "ttl/none")["expires_at"] is None


def test_publish_rejects_a_bad_ttl():
    c = _client()
    r = c.post("/api/publish",
               data={"slug": "ttl/bad", "title": "T", "from": "analyst",
                     "ttl": "soon"},
               files={"file": ("d.html", b"<h1>x</h1>", "text/html")},
               headers=KEY)
    assert r.status_code == 400
    assert r.json()["error"].startswith("invalid ttl")
    assert c.get("/api/doc/ttl/bad", headers=KEY).status_code == 404


def test_expired_doc_is_404_everywhere():
    c = _client()
    c.post("/api/publish",
           data={"slug": "ttl/exp", "title": "T", "from": "analyst", "ttl": "1h"},
           files={"file": ("d.html", b"<h1>x</h1>", "text/html")}, headers=KEY)
    _backdate("ttl/exp")
    assert c.get("/api/doc/ttl/exp", headers=KEY).status_code == 404
    assert c.get("/d/ttl/exp", headers=KEY).status_code == 404
    assert c.get("/d/ttl/exp/v1", headers=KEY).status_code == 404
    assert c.get("/api/versions/ttl/exp", headers=KEY).status_code == 404
    assert all(d["slug"] != "ttl/exp"
               for d in c.get("/api/list", headers=KEY).json()["docs"])
```

Append to `tests/test_public.py`:
```python
def test_expired_public_doc_is_not_served_anonymously():
    c = _client()
    c.post("/api/publish",
           data={"slug": "pub/ttl", "title": "T", "from": "analyst", "ttl": "1h"},
           files={"file": ("d.html", b"<h1>x</h1>", "text/html")}, headers=KEY)
    _set_public(c, "pub/ttl", True)
    assert _client().get("/d/pub/ttl").status_code == 200
    from backend import db
    with db.docs_conn() as conn:
        conn.execute("UPDATE docs SET expires_at = now() - interval '1 second' "
                     "WHERE slug=%s", ("pub/ttl",))
        conn.commit()
    r = _client().get("/d/pub/ttl", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
```
(Read `backend/session.py`'s middleware first: the anonymous `/d/<slug>` path consults `docs_repo.is_public`, which now returns False for an expired slug, so the middleware redirects to login exactly as for a private doc. If the middleware turns out to work differently, make the assertion match "not served" — never a 200 with the body.)

Run: `.venv/bin/pytest tests/test_app.py tests/test_public.py -q` — expected: new tests FAIL.

- [ ] **Step 2: Implement `backend/api.py`**

`publish`: add parameter `ttl: str = Form("")`. Before calling `docs_repo.publish`:
```python
    ttl_seconds: int | None = None
    if ttl.strip():
        try:
            ttl_seconds = parse_ttl(ttl)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
```
pass `ttl_seconds=ttl_seconds`, and add to the success JSON:
```python
        "expires_at": res["expires_at"].isoformat() if res["expires_at"] else None,
```
Import `from backend.ttl import parse_ttl`.

`api_list`: alongside the `updated_at` isoformat loop add
```python
        if d["expires_at"] is not None:
            d["expires_at"] = d["expires_at"].isoformat()
```

Run the two test files — expected: PASS.

- [ ] **Step 3: Failing reaper test** — `tests/test_reaper.py`:

```python
"""The reaper is a lifespan background task; drive it through the app's
lifespan (TestClient as a context manager runs startup/shutdown) with a
tiny interval, and through `loop` directly for the error path."""
import asyncio
import os
import time

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
```

Run: `.venv/bin/pytest tests/test_reaper.py -q` — expected: ImportError.

- [ ] **Step 4: Implement `backend/reaper.py`**

```python
"""Background purge of expired documents.

Reads already hide an expired doc the moment its time passes (docs_repo's
live filter); this loop is what actually removes the rows and the blobs.
Runs once at startup, then every PURGE_INTERVAL_SECONDS (default 60). One
failed purge is logged and the loop carries on — a transient DB outage must
not leave the service without a reaper until the next restart.
"""
from __future__ import annotations

import asyncio
import logging
import os

from backend import docs_repo

_log = logging.getLogger(__name__)
DEFAULT_INTERVAL = 60.0


async def loop(interval: float) -> None:
    while True:
        try:
            removed = await asyncio.to_thread(docs_repo.purge_expired)
            if removed:
                _log.info("purged %d expired doc(s)", removed)
        except Exception:  # pylint: disable=broad-exception-caught
            # A reaper that dies on the first transient error is worse than
            # none: the loop is the only thing that ever frees the blobs.
            _log.exception("purge_expired failed; will retry")
        await asyncio.sleep(interval)


def start(interval: float | None = None) -> asyncio.Task:
    """Schedule the loop on the running event loop and return the task so the
    caller can cancel it at shutdown."""
    if interval is None:
        interval = float(os.environ.get("PURGE_INTERVAL_SECONDS",
                                        DEFAULT_INTERVAL))
    return asyncio.get_running_loop().create_task(loop(interval))
```

`backend/app.py` lifespan:
```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.migrate()
    db.schema_check()
    purge_task = reaper.start()
    try:
        yield
    finally:
        purge_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task
        db.close_pools()
```
(imports: `asyncio`, `from contextlib import asynccontextmanager, suppress`, and `reaper` in the `from backend import ...` line.)

Run: `.venv/bin/pytest tests/test_reaper.py -q` — expected: PASS.

- [ ] **Step 5: README deployment env** — add to the "Required environment" list in `README.md`:
```
- `PURGE_INTERVAL_SECONDS` — how often the reaper deletes expired
  documents (default `60`). Optional.
```

- [ ] **Step 6: Gates, then commit**

Full `.venv/bin/pytest -q`, pycodestyle, pylint, pyright — all clean.

```bash
git add backend/api.py backend/reaper.py backend/app.py README.md tests/test_app.py tests/test_public.py tests/test_reaper.py
git commit -m "Accept ttl on publish and reap expired documents"
```

---

### Task 3: CLI `--ttl`, version bump, and the agent-facing docs

**Files:**
- Modify: `docs_hub_cli/cli.py` (`cmd_publish`, `cmd_list`, `main`)
- Modify: `docs_hub_cli/__init__.py` (`__version__ = "0.2.0"`)
- Modify: `README.md` ("What it does", CLI table), `AGENTS.md` (Architecture)
- Test: `tests/test_cli_unit.py` (extend), `tests/test_cli.py` (extend, one end-to-end case)

**Interfaces:**
- Consumes: `POST /api/publish` `ttl` form field; `expires_at` in publish and list JSON (Task 2).

- [ ] **Step 1: Failing unit tests** — read the top ~100 lines of `tests/test_cli_unit.py` for the `_Transport` fake and the existing `test_publish_posts_the_form_and_reports_the_url` / `test_list_renders_a_row_per_doc` tests, then add, in the same style:

- `test_publish_sends_ttl_when_given`: run `cmd_publish` with `ttl="1h"`; assert the multipart body sent contains a `name="ttl"` part with `1h`.
- `test_publish_omits_ttl_when_not_given`: body contains no `name="ttl"` part.
- `test_publish_reports_the_expiry_when_the_server_returns_one`: response `{"ok": True, "slug": "a/b", "version": 1, "url": "/d/a/b", "expires_at": "2026-09-14T12:00:00+00:00"}` → stdout line ends with ` (expires 2026-09-14T12:00:00+00:00)`.
- `test_publish_output_is_unchanged_without_an_expiry`: `expires_at: None` → no `(expires` in stdout.
- `test_list_appends_the_expiry_when_present`: one doc with `expires_at` set → its row ends with ` expires <iso>`; a doc with `expires_at: None` and a doc lacking the key entirely (older server) → no suffix.
- `test_main_accepts_the_ttl_option`: `main()` with argv `publish f.html --slug a/b --title T --from x --ttl 2d` reaches `cmd_publish` with `args.ttl == "2d"` (mirror how `test_main_rejects_publish_without_its_required_options` drives `main`).

Run: `.venv/bin/pytest tests/test_cli_unit.py -q` — expected: new tests FAIL.

- [ ] **Step 2: Implement**

`cmd_publish`:
```python
    fields = {"slug": args.slug, "title": args.title,
              "tags": args.tags or "", "project": args.project or "",
              "from": getattr(args, "from")}
    if getattr(args, "ttl", ""):
        fields["ttl"] = args.ttl
    ...
    suffix = f" (expires {payload['expires_at']})" if payload.get("expires_at") else ""
    print(f"published {payload['slug']} v{payload['version']} "
          f"-> {_base_url()}{payload['url']}{suffix}")
```
`cmd_list` row:
```python
        expires = f" expires {d['expires_at']}" if d.get("expires_at") else ""
        print(f"{d['slug']:<40} v{d['latest_version']:<3} "
              f"{d['posted_by']:<14} [{tags}] {d['title']}{expires}")
```
`main`, publish parser:
```python
    p.add_argument("--ttl", default="",
                   help="lifetime after which the document vanishes, e.g. "
                        "30m, 2h, 7d (bare number = seconds); omit = permanent")
```
`docs_hub_cli/__init__.py`: `__version__ = "0.2.0"`.

- [ ] **Step 3: One end-to-end case** — in `tests/test_cli.py`, following its existing publish test, publish with `--ttl 1h` through the subprocess CLI and assert the stdout contains `(expires ` and that `list` output for that slug contains ` expires `.

- [ ] **Step 4: Docs**

`README.md` "What it does": add a bullet
```
- A publish may carry a time-to-live (`ttl`, e.g. `30m`, `2h`, `7d`).
  When it elapses the document and all its versions vanish: reads and
  listings hide it immediately, and a background reaper deletes it. Each
  publish restates the lifetime; republishing without `ttl` makes the
  document permanent.
```
CLI table: `docs-hub publish FILE --slug S --title T --from AGENT [--tags a,b] [--project P] [--ttl DURATION]`.

`AGENTS.md` Architecture: add a bullet after the "Public docs" one:
```
- Ephemeral docs: a nullable `expires_at` on the `docs` row, set per publish
  from the `ttl` form field (`backend/ttl.py` parses `30m`/`2h`/`7d`/seconds).
  NOT sticky — every publish restates it, and omitting it makes the doc
  permanent. `docs_repo` filters every read to live rows (`_LIVE`), so an
  expired doc is 404/unlisted the moment its time passes; `backend/reaper.py`
  runs `purge_expired()` at startup and every `PURGE_INTERVAL_SECONDS`
  (default 60) to delete the rows and blobs. Republishing an expired slug
  purges it first and starts again at v1.
```

- [ ] **Step 5: Gates, then commit**

Full pytest, pycodestyle, pylint, pyright clean.

```bash
git add docs_hub_cli/cli.py docs_hub_cli/__init__.py README.md AGENTS.md tests/test_cli_unit.py tests/test_cli.py
git commit -m "Expose --ttl in the CLI and document ephemeral documents"
```

---

### Task 4: Show the expiry in the SPA

**Files:**
- Modify: `public/app.js` (`renderIndex` row, `renderDocViewer` meta, `sigIndex`)
- Modify: `public/styles.css`

**Interfaces:**
- Consumes: `expires_at` (ISO string or null) on each doc from `/api/list`.

- [ ] **Step 1: Read the existing code**

`renderIndex` (the row template), `renderDocViewer` (the topbar meta where `public`/`private` is shown), `relTime`/`absTime`, `sigIndex`, and the `.tag`/`.slug` styles in `styles.css`. Match their conventions exactly (template literals, `esc()` on every interpolated value, no new dependencies).

- [ ] **Step 2: Implement**

- Add a helper next to `relTime`:
```js
function relUntil(iso) {
  const d = new Date(iso).getTime() - Date.now();
  if (d <= 0) return 'expired';
  const mins = d / 60_000;
  if (mins < 1) return 'in <1m';
  if (mins < 60) return 'in ' + Math.round(mins) + 'm';
  const hours = mins / 60;
  if (hours < 24) return 'in ' + Math.round(hours) + 'h';
  const days = hours / 24;
  if (days < 14) return 'in ' + Math.round(days) + 'd';
  return 'in ' + Math.round(days / 7) + 'w';
}
```
- Index row: inside the `.slug` line of the title cell, after the slug text, render when `d.expires_at` is set:
  `<span class="ttl" title="expires ${esc(absTime(d.expires_at))} UTC">⏳ ${esc(relUntil(d.expires_at))}</span>`
- Doc viewer: next to the public/private control, when the doc has `expires_at`, a non-interactive `<span class="ttl" title="expires ${esc(absTime(doc.expires_at))} UTC">⏳ expires ${esc(relUntil(doc.expires_at))}</span>`.
- `sigIndex`: append `'|' + (d.expires_at || '')` to each row so a republish that changes only the expiry re-renders.
- `styles.css`: a `.ttl` rule in the same visual family as `.tag` (small, muted, monospace if the slug line is monospace) — read the neighbouring rules and match them.

- [ ] **Step 3: Gate**

`npx eslint public` clean. There is no JS test suite; state that in the report.

- [ ] **Step 4: Commit**

```bash
git add public/app.js public/styles.css
git commit -m "Show a document's expiry in the SPA"
```
