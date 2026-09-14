from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from backend.app import app
from backend import db, docs_repo, session

KEY = {"x-docs-key": "test-api-key"}


def _client():
    return TestClient(app)


def test_health_open():
    assert _client().get("/health").json() == {"ok": True}


def test_publish_requires_key():
    c = _client()
    r = c.post("/api/publish", data={"slug": "a/b", "title": "T", "from": "analyst"},
               files={"file": ("d.html", b"<h1>x</h1>", "text/html")})
    assert r.status_code == 401


def test_publish_then_read_roundtrip():
    c = _client()
    r = c.post("/api/publish",
               data={"slug": "analyst/demo", "title": "Demo", "tags": "x,y",
                     "project": "p", "from": "analyst"},
               files={"file": ("d.html", b"<h1>hello</h1>", "text/html")},
               headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    # agent read
    got = c.get("/api/doc/analyst/demo", headers=KEY)
    assert got.status_code == 200
    assert got.content == b"<h1>hello</h1>"
    # browser render
    rendered = c.get("/d/analyst/demo", headers=KEY)
    assert rendered.content == b"<h1>hello</h1>"


def test_republish_and_version_history():
    c = _client()
    for body in (b"<h1>1</h1>", b"<h1>2</h1>"):
        c.post("/api/publish",
               data={"slug": "analyst/v", "title": "V", "from": "analyst"},
               files={"file": ("d.html", body, "text/html")}, headers=KEY)
    assert c.get("/d/analyst/v", headers=KEY).content == b"<h1>2</h1>"
    assert c.get("/d/analyst/v/v1", headers=KEY).content == b"<h1>1</h1>"
    vs = c.get("/api/versions/analyst/v", headers=KEY).json()
    assert [v["version"] for v in vs["versions"]] == [2, 1]


def test_no_auth_redirects_to_login():
    c = _client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_bad_login_rejected():
    c = _client()
    r = c.post("/login", data={"user_id": "999999999", "password": "nope"},
               follow_redirects=False)
    assert r.status_code == 401


def _publish(c, slug, project="p", author="analyst", tags="", body=b"<h1>x</h1>"):
    return c.post("/api/publish",
                  data={"slug": slug, "title": slug, "tags": tags,
                        "project": project, "from": author},
                  files={"file": ("d.html", body, "text/html")}, headers=KEY)


def test_delete_requires_a_filter():
    r = _client().post("/api/delete", json={}, headers=KEY)
    assert r.status_code == 400


def test_delete_requires_auth():
    r = _client().post("/api/delete", json={"project": "analyst"})
    assert r.status_code == 401


def test_delete_preview_then_confirm():
    c = _client()
    _publish(c, "analyst/d1", project="analyst")
    _publish(c, "analyst/d2", project="analyst")
    prev = c.post("/api/delete", json={"project": "analyst"}, headers=KEY)
    assert prev.status_code == 200, prev.text
    pj = prev.json()
    assert pj["preview"] is True and pj["count"] == 2
    assert docs_repo.get_latest("analyst/d1") is not None  # preview deletes nothing
    conf = c.post("/api/delete",
                  json={"project": "analyst", "confirm_token": pj["confirm_token"]},
                  headers=KEY)
    assert conf.status_code == 200, conf.text
    cj = conf.json()
    assert cj["preview"] is False and cj["deleted"] == 2
    assert docs_repo.get_latest("analyst/d1") is None


def test_delete_bad_token_rejected():
    c = _client()
    _publish(c, "analyst/d1", project="analyst")
    r = c.post("/api/delete",
               json={"project": "analyst", "confirm_token": "1.deadbeef"},
               headers=KEY)
    assert r.status_code == 409
    assert docs_repo.get_latest("analyst/d1") is not None


def test_delete_token_bound_to_filters():
    c = _client()
    _publish(c, "analyst/d1", project="analyst")
    _publish(c, "beta/d2", project="beta")
    prev = c.post("/api/delete", json={"project": "analyst"}, headers=KEY)
    token = prev.json()["confirm_token"]
    # token from project=analyst must not confirm a project=beta delete
    r = c.post("/api/delete",
               json={"project": "beta", "confirm_token": token}, headers=KEY)
    assert r.status_code == 409


def test_delete_accepts_human_cookie():
    c = _client()
    _publish(c, "analyst/d1", project="analyst")
    c.cookies.set("session", session.make_session_token(1))
    r = c.post("/api/delete", json={"project": "analyst"})
    assert r.status_code == 200
    assert r.json()["preview"] is True


def test_whoami_agent():
    r = _client().get("/api/whoami", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["principal"] == "agent"


def test_root_serves_spa_when_authed():
    c = _client()
    c.cookies.set("session", session.make_session_token(1))
    r = c.get("/")
    assert r.status_code == 200
    assert '<div id="app"' in r.text


def test_static_assets_served_when_authed():
    c = _client()
    c.cookies.set("session", session.make_session_token(1))
    assert c.get("/app.js").status_code == 200
    assert c.get("/styles.css").status_code == 200


def test_doc_render_still_works():
    c = _client()
    _publish(c, "analyst/r1")
    assert c.get("/d/analyst/r1", headers=KEY).content == b"<h1>x</h1>"


def test_index_stamps_asset_mtime():
    c = _client()
    c.cookies.set("session", session.make_session_token(1))
    r = c.get("/")
    assert r.status_code == 200
    assert "styles.css?v=" in r.text
    assert "app.js?v=" in r.text
    assert 'href="styles.css"' not in r.text
    assert 'src="app.js"' not in r.text


def test_api_tags():
    c = _client()
    _publish(c, "alpha/x", tags="spec,draft")
    _publish(c, "alpha/y", tags="spec")
    _publish(c, "beta/z", project="beta", tags="report")
    r = c.get("/api/tags", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    by = {t["tag"]: t["count"] for t in body["tags"]}
    assert by == {"spec": 2, "draft": 1, "report": 1}
    # most-used first
    assert body["tags"][0]["tag"] == "spec" and body["tags"][0]["count"] == 2
    # project-scoped
    r2 = c.get("/api/tags?project=beta", headers=KEY)
    assert r2.json()["tags"] == [{"tag": "report", "count": 1}]


def test_api_tags_requires_auth():
    assert _client().get("/api/tags").status_code == 401


def test_api_list_serialises_expires_at():
    """A doc with an expiry carries a datetime out of list_docs; JSONResponse
    cannot dump one, so /api/list must render it as an ISO string — and null
    for a permanent doc. Set the column directly: the publish endpoint grows
    its own ttl field later, and this must not wait for it."""
    c = _client()
    _publish(c, "exp/soon")
    _publish(c, "exp/never")
    with db.docs_conn() as conn:
        conn.execute("UPDATE docs SET expires_at = now() + interval '1 hour' "
                     "WHERE slug=%s", ("exp/soon",))
        conn.commit()
    r = c.get("/api/list", headers=KEY)
    assert r.status_code == 200, r.text
    by = {d["slug"]: d["expires_at"] for d in r.json()["docs"]}
    assert by["exp/never"] is None
    assert isinstance(by["exp/soon"], str)
    assert datetime.fromisoformat(by["exp/soon"]) > datetime.now(timezone.utc)


def _backdate(slug):
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
