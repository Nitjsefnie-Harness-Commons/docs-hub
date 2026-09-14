from fastapi.testclient import TestClient

from backend.app import app
from backend import session

KEY = {"x-docs-key": "test-api-key"}


def _client():
    return TestClient(app)


def _publish(c, slug, body=b"<h1>x</h1>"):
    return c.post("/api/publish",
                  data={"slug": slug, "title": slug, "from": "analyst"},
                  files={"file": ("d.html", body, "text/html")}, headers=KEY)


# KEY is a module constant that nothing mutates, and it cannot become a None
# sentinel: `headers=None` is a meaningful argument here -- it is how a test
# asks for an unauthenticated request.
# pylint: disable-next=dangerous-default-value
def _set_public(c, slug, public, headers=KEY):
    return c.post(f"/api/doc/{slug}/public",
                  json={"public": public}, headers=headers)


def test_anonymous_read_public_doc():
    c = _client()
    _publish(c, "pub/doc1", body=b"<h1>open</h1>")
    r = _set_public(c, "pub/doc1", True)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "slug": "pub/doc1", "public": True}
    anon = _client()  # no key, no cookie
    r = anon.get("/d/pub/doc1")
    assert r.status_code == 200
    assert r.content == b"<h1>open</h1>"


def test_anonymous_read_private_doc_redirects_to_login():
    c = _client()
    _publish(c, "priv/doc1")
    anon = _client()
    r = anon.get("/d/priv/doc1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_public_flag_survives_republish():
    c = _client()
    _publish(c, "pub/sticky", body=b"<h1>v1</h1>")
    _set_public(c, "pub/sticky", True)
    _publish(c, "pub/sticky", body=b"<h1>v2</h1>")
    anon = _client()
    r = anon.get("/d/pub/sticky")
    assert r.status_code == 200
    assert r.content == b"<h1>v2</h1>"


def test_toggle_back_to_private():
    c = _client()
    _publish(c, "pub/back", body=b"<h1>b</h1>")
    _set_public(c, "pub/back", True)
    assert _client().get("/d/pub/back").status_code == 200
    r = _set_public(c, "pub/back", False)
    assert r.json()["public"] is False
    r = _client().get("/d/pub/back", follow_redirects=False)
    assert r.status_code == 302


def test_toggle_requires_auth():
    c = _client()
    _publish(c, "pub/tog")
    anon = _client()
    r = anon.post("/api/doc/pub/tog/public", json={"public": True})
    assert r.status_code == 401
    # and the doc stayed private
    r = anon.get("/d/pub/tog", follow_redirects=False)
    assert r.status_code == 302


def test_toggle_accepts_human_cookie():
    c = _client()
    _publish(c, "pub/human")
    c.cookies.set("session", session.make_session_token(1))
    r = _set_public(c, "pub/human", True, headers=None)
    assert r.status_code == 200, r.text
    assert r.json()["public"] is True


def test_toggle_unknown_slug_404():
    r = _set_public(_client(), "nope/nada", True)
    assert r.status_code == 404


def test_anonymous_version_url_still_gated():
    """The SPA iframes /d/<slug>/v<n>; that path stays auth-only even for a
    public slug. Only the hash-less /d/<slug> route opens."""
    c = _client()
    _publish(c, "pub/v", body=b"<h1>v1</h1>")
    _set_public(c, "pub/v", True)
    anon = _client()
    r = anon.get("/d/pub/v/v1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_anonymous_api_doc_path_still_gated():
    c = _client()
    _publish(c, "pub/apidoc")
    _set_public(c, "pub/apidoc", True)
    anon = _client()
    assert anon.get("/api/doc/pub/apidoc").status_code == 401
    assert anon.get("/api/list").status_code == 401
    assert anon.get("/api/versions/pub/apidoc").status_code == 401


def test_anonymous_spa_shell_still_gated():
    c = _client()
    _publish(c, "pub/spa")
    _set_public(c, "pub/spa", True)
    anon = _client()
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_list_includes_public_flag():
    c = _client()
    _publish(c, "pub/listed")
    docs = c.get("/api/list", headers=KEY).json()["docs"]
    d = next(x for x in docs if x["slug"] == "pub/listed")
    assert d["public"] is False
    _set_public(c, "pub/listed", True)
    docs = c.get("/api/list", headers=KEY).json()["docs"]
    d = next(x for x in docs if x["slug"] == "pub/listed")
    assert d["public"] is True


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


def test_public_markdown_doc_renders_anonymously():
    c = _client()
    c.post("/api/publish",
           data={"slug": "pub/md", "title": "Pub MD", "from": "analyst"},
           files={"file": ("d.md", b"# open\n", "text/markdown")}, headers=KEY)
    _set_public(c, "pub/md", True)
    r = _client().get("/d/pub/md")
    assert r.status_code == 200
    assert "<h1>open</h1>" in r.text
