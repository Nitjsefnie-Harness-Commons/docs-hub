"""The operator-supplied privacy notice: /privacy page and the injected link.

`DOCS_HUB_PRIVACY_NOTICE_FILE` is unset suite-wide (tests/conftest.py never
sets it), so every absence test here holds against the suite's default
environment; the configured tests monkeypatch it per test with throwaway
fixture text under tmp_path.
"""
import re

from fastapi.testclient import TestClient

from backend import session, storage
from backend.app import app

KEY = {"x-docs-key": "test-api-key"}

ENV = "DOCS_HUB_PRIVACY_NOTICE_FILE"
LINK = b'<a href="/privacy">Privacy notice</a>'


def _client():
    return TestClient(app)


def _notice_file(tmp_path, text: str) -> str:
    path = tmp_path / "notice.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


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


def _anon_public_doc(slug="pub/notice", body=b"<h1>pub</h1>"):
    """Publish + public `slug`, then fetch it with no credentials at all."""
    c = _client()
    assert _publish(c, slug, body=body).status_code == 200
    assert _set_public(c, slug, True).status_code == 200
    return _client().get(f"/d/{slug}")


def _assert_notice_absent_everywhere():
    """Unconfigured shape: /privacy 404s and no link renders anywhere.

    The three surfaces are asserted together because the page and every
    link site must agree: one availability check drives them all."""
    r = _client().get("/privacy")
    assert r.status_code == 404
    login = _client().get("/login")
    assert login.status_code == 200
    assert LINK not in login.content
    doc = _anon_public_doc()
    assert doc.status_code == 200
    assert LINK not in doc.content


def test_privacy_page_serves_the_operator_markdown(tmp_path, monkeypatch):
    """Configured: /privacy renders the file; raw HTML in it is escaped, not
    executed, and the page is sandboxed without scripts."""
    monkeypatch.setenv(ENV, _notice_file(
        tmp_path,
        "# Notice fixture\n\nReach the fixture desk at "
        "privacy-fixture@example.invalid.\n\n<script>alert(1)</script>\n"))
    r = _client().get("/privacy")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "sandbox"
    assert "<h1>Notice fixture</h1>" in r.text
    assert "&lt;script&gt;" in r.text
    assert "<script>" not in r.text


def test_absent_when_env_unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    _assert_notice_absent_everywhere()


def test_absent_when_env_empty(monkeypatch):
    monkeypatch.setenv(ENV, "")
    _assert_notice_absent_everywhere()


def test_absent_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "not-there.md"))
    _assert_notice_absent_everywhere()


def test_absent_when_file_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _notice_file(tmp_path, ""))
    _assert_notice_absent_everywhere()


def test_absent_when_path_is_a_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path))
    _assert_notice_absent_everywhere()


def test_sign_in_form_links_the_notice(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    login = _client().get("/login")
    assert login.status_code == 200
    assert LINK in login.content


def test_invalid_credentials_response_links_the_notice(tmp_path, monkeypatch):
    """The second login render site (the 401 body) carries the link too."""
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    r = _client().post("/login",
                       data={"user_id": "1", "password": "fixture-not-it"})
    assert r.status_code == 401
    assert LINK in r.content


def test_anonymous_public_html_doc_gains_the_link_before_its_last_body_close(
        tmp_path, monkeypatch):
    """The link lands before the FINAL </body> (the fixture's first `</body>`
    is literal text inside the document) and appears exactly once."""
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    body = (b"<html><body><h1>pub</h1><p>about the </body> tag</p>"
            b"</body></html>")
    r = _anon_public_doc(slug="pub/lastclose", body=body)
    assert r.status_code == 200
    assert r.content.endswith(LINK + b"</body></html>")
    assert r.content.count(LINK) == 1


def test_link_injection_reads_a_uppercase_body_close(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    r = _anon_public_doc(slug="pub/caps", body=b"<h1>caps</h1></BODY>")
    assert r.status_code == 200
    assert r.content.endswith(LINK + b"</BODY>")


def test_link_is_appended_when_the_document_has_no_body_close(
        tmp_path, monkeypatch):
    """Fragment HTML (no closing body tag) gets the link appended after the
    document's own bytes."""
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    r = _anon_public_doc(slug="pub/frag", body=b"<h1>fragment</h1>")
    assert r.status_code == 200
    assert r.content == b"<h1>fragment</h1>" + LINK


def test_anonymous_public_markdown_doc_renders_with_the_link(
        tmp_path, monkeypatch):
    """The Markdown render path gets the same injection: the link sits after
    the rendered article, before the page's own closing body tag."""
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    c = _client()
    assert c.post("/api/publish",
                  data={"slug": "pub/mdn", "title": "MD", "from": "analyst"},
                  files={"file": ("d.md", b"# open md\n", "text/markdown")},
                  headers=KEY).status_code == 200
    assert _set_public(c, "pub/mdn", True).status_code == 200
    r = _client().get("/d/pub/mdn")
    assert r.status_code == 200
    assert "<h1>open md</h1>" in r.text
    assert r.text.endswith(LINK.decode() + "</body>\n</html>\n")


def test_the_injected_link_opens_the_page(tmp_path, monkeypatch):
    """Span: the link every consumer injects and the route behind /privacy
    are one contract — the anchor is walked end to end, and it is the ONLY
    anchor the anonymous response carries."""
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    r = _anon_public_doc(slug="pub/walk")
    assert r.status_code == 200
    hrefs = re.findall(rb'<a href="([^"]+)">', r.content)
    assert hrefs == [b"/privacy"]
    page = _client().get(hrefs[0].decode())
    assert page.status_code == 200
    assert "Notice fixture" in page.text


def test_stored_blob_is_untouched_by_the_anonymous_view(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    body = b"<h1>verbatim</h1>"
    c = _client()
    assert _publish(c, "pub/stored", body=body).status_code == 200
    assert _set_public(c, "pub/stored", True).status_code == 200
    path = storage.blob_path("pub/stored", 1)
    with open(path, "rb") as f:
        before = f.read()
    assert before == body
    r = _client().get("/d/pub/stored")
    assert LINK in r.content
    with open(path, "rb") as f:
        assert f.read() == body


def test_keyed_reader_gets_the_document_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    body = b"<h1>agent view</h1></body>"
    c = _client()
    assert _publish(c, "pub/agentview", body=body).status_code == 200
    assert _set_public(c, "pub/agentview", True).status_code == 200
    assert c.get("/d/pub/agentview", headers=KEY).content == body


def test_signed_in_reader_gets_the_document_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    body = b"<h1>human view</h1></body>"
    c = _client()
    assert _publish(c, "pub/humanview", body=body).status_code == 200
    assert _set_public(c, "pub/humanview", True).status_code == 200
    c.cookies.set("session", session.make_session_token(1))
    assert c.get("/d/pub/humanview").content == body


def test_privacy_subpaths_stay_gated_even_when_configured(
        tmp_path, monkeypatch):
    """The public-path entry is exact: /privacy/ and /privacy/x must not
    inherit /privacy's exemption."""
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    anon = _client()
    for path in ("/privacy/", "/privacy/x"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_version_route_stays_gated_while_the_notice_is_configured(
        tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    c = _client()
    assert _publish(c, "pub/gatever").status_code == 200
    assert _set_public(c, "pub/gatever", True).status_code == 200
    r = _client().get("/d/pub/gatever/v1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_private_docs_and_the_toggle_stay_protected_while_configured(
        tmp_path, monkeypatch):
    """Visibility negative: the notice widens nothing else — an anonymous
    visitor still cannot flip a doc public or read a private one."""
    monkeypatch.setenv(ENV, _notice_file(tmp_path, "# Notice fixture\n"))
    c = _client()
    assert _publish(c, "pub/gate").status_code == 200
    anon = _client()
    assert anon.post("/api/doc/pub/gate/public",
                     json={"public": True}).status_code == 401
    r = anon.get("/d/pub/gate", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
