"""Unit cover for `docs_hub_cli`, in-process.

tests/test_cli.py drives the CLI as a subprocess against a real server, which
proves the two halves agree but measures no coverage (a child interpreter is
not traced) and cannot reach the failure paths -- a 502 from Cloudflare, a
connection refused, a missing API key. Those live here, with the transport
faked.

THE IMPORT-TIME GOTCHA. `docs_hub_cli.cli` evaluates

    _DEFAULT_URL = setting("DOCS_HUB_URL", "")

at import, so setting DOCS_HUB_URL inside a test changes nothing: the module
object was built when pytest collected this file. Every test here therefore
assigns `cli._DEFAULT_URL` directly, via the autouse `_offline` fixture below,
rather than touching the environment. Reloading the module would work too, but
would also reset anything else a test had patched onto it.

`_offline` also pins the URL to a closed local port. Nothing in this file
should reach a socket at all -- `cli._request` is faked in every test that
issues a request -- but `_settings` falls back to ~/.agent-bundle/settings.json,
which on a fleet machine names the production hub, and a mistake here must fail
as a refused connection rather than as a write to the real docs hub.
"""
# protected-access: the module-private helpers ARE the units under test here —
#   _html_to_text, _multipart, _qs_escape, _base_url, _api_key, _request. The
#   alternative is to widen the CLI's public surface to suit its test file.
# redefined-outer-name: a pytest fixture is requested by writing its own name
#   as a parameter; pylint reads every such parameter as shadowing.
# too-few-public-methods: _Stdout is a two-attribute stand-in for a stream.
# pylint: disable=protected-access,redefined-outer-name,too-few-public-methods
import io
import json
import sys
import urllib.error
import urllib.request

import pytest

from docs_hub_cli import _settings, cli

# Port 9 is `discard`, and nothing listens on it here; see the module docstring.
_DEAD_URL = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(cli, "_DEFAULT_URL", _DEAD_URL)
    # conftest.py exports this already; restated so the file stands alone.
    monkeypatch.setenv("DOCS_HUB_API_KEY", "test-api-key")


class _Transport:
    """Stand-in for `cli._request`: records calls, replays queued responses.

    One queued response is reused for every call; several are consumed in
    order, which is what the multi-request paths need.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, *, data=None, headers=None):
        self.calls.append((method, path, data, headers or {}))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    @property
    def paths(self):
        return [c[1] for c in self.calls]


class _Stdout:
    """A stdout with a byte buffer and no `reconfigure`.

    cmd_get writes bytes through `sys.stdout.buffer`, which pytest's own
    capture does not expose as a readable artefact. Missing `reconfigure` is
    deliberate: it exercises main()'s except branch for a stream that cannot
    be reconfigured.
    """

    def __init__(self):
        self.buffer = io.BytesIO()


def _json(obj):
    return json.dumps(obj).encode()


def _main(monkeypatch, transport, *argv):
    """Parse `argv` with the real parser and run the command against `transport`."""
    monkeypatch.setattr(cli, "_request", transport)
    monkeypatch.setattr(sys, "argv", ["docs-hub", *argv])
    return cli.main()


# --- the --text-only HTML-to-text pass ---------------------------------


def test_text_only_drops_script_and_style_content():
    html = (b"<style>body{color:red}</style><p>kept</p>"
            b"<script>var x = 'dropped';</script>")
    assert cli._html_to_text(html) == "kept\n"


def test_text_only_drops_head_and_title():
    html = b"<html><head><title>Title</title></head><body><p>body</p></body></html>"
    assert cli._html_to_text(html) == "body\n"


def test_text_only_breaks_at_block_tags():
    # A block tag emits a newline on BOTH its start and its end, so adjacent
    # blocks are separated by a blank line; runs of three or more collapse to
    # two. That is the rendered shape --text-only actually produces.
    html = b"<h1>one</h1><p>two</p><ul><li>three</li><li>four</li></ul>"
    assert cli._html_to_text(html) == "one\n\ntwo\n\nthree\n\nfour\n"


def test_text_only_keeps_inline_tags_on_one_line():
    assert cli._html_to_text(b"<p>a <b>bold</b> word</p>") == "a bold word\n"


def test_text_only_unescapes_entities():
    # convert_charrefs is HTMLParser's default; this is the assertion that
    # would catch someone turning it off.
    assert cli._html_to_text(b"<p>a &amp; b &lt;c&gt; &#233;</p>") == "a & b <c> é\n"


def test_text_only_collapses_whitespace_and_blank_runs():
    html = b"<p>a   \t  b</p><div></div><div></div><div></div><p>c</p>"
    assert cli._html_to_text(html) == "a b\n\nc\n"


def test_text_only_of_an_empty_document_is_the_empty_string():
    assert cli._html_to_text(b"") == ""
    assert cli._html_to_text(b"<div>  </div>") == ""


def test_text_only_tolerates_an_unbalanced_skip_end_tag():
    # A stray </script> with no opening tag must not drive _skip_depth
    # negative, which would swallow the rest of the document.
    assert cli._html_to_text(b"</script><p>still here</p>") == "still here\n"


def test_text_only_decodes_invalid_utf8_without_raising():
    assert cli._html_to_text(b"<p>caf\xff</p>") == "caf�\n"


# --- the multipart body builder ----------------------------------------


def test_multipart_carries_every_field_and_the_file():
    body, ctype = cli._multipart({"slug": "a/b", "title": "T"},
                                 "file", "doc.html", b"<h1>x</h1>")
    boundary = ctype.split("boundary=")[1]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="slug"\r\n\r\na/b\r\n' in body
    assert b'name="title"\r\n\r\nT\r\n' in body
    assert b'name="file"; filename="doc.html"' in body
    assert b"Content-Type: text/html" in body
    assert b"<h1>x</h1>" in body
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())


def test_multipart_guesses_the_content_type_from_the_filename():
    body, _ = cli._multipart({}, "file", "d.txt", b"x")
    assert b"Content-Type: text/plain" in body


def test_multipart_falls_back_to_text_html_for_an_unknown_extension():
    body, _ = cli._multipart({}, "file", "d.qqq", b"x")
    assert b"Content-Type: text/html" in body


def test_multipart_escapes_the_filename_against_header_injection():
    body, _ = cli._multipart({}, "file", 'a"b\r\nX-Evil: 1.html', b"x")
    assert b'filename="a\\"bX-Evil: 1.html"' in body
    assert b"\r\nX-Evil" not in body


def test_qs_escape_strips_newlines_and_escapes_backslashes():
    assert cli._qs_escape('a\r\nb\\c"d') == 'ab\\\\c\\"d'


# --- base URL and API key ----------------------------------------------


def test_base_url_strips_a_trailing_slash(monkeypatch):
    monkeypatch.setattr(cli, "_DEFAULT_URL", "https://example.test/")
    assert cli._base_url() == "https://example.test"


def test_base_url_without_a_url_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_DEFAULT_URL", "")
    with pytest.raises(SystemExit) as exc:
        cli._base_url()
    assert exc.value.code == 2
    assert "DOCS_HUB_URL not set" in capsys.readouterr().err


def test_api_key_reads_the_environment():
    assert cli._api_key() == "test-api-key"


def test_api_key_without_a_key_exits_2(monkeypatch, capsys):
    # `cli.setting` is replaced rather than the env var emptied: an empty env
    # var falls through to ~/.agent-bundle/settings.json, so on a machine with
    # the fleet bundle installed the real key would answer and this would pass
    # for the wrong reason.
    monkeypatch.setattr(cli, "setting", lambda name, default=None: "")
    with pytest.raises(SystemExit) as exc:
        cli._api_key()
    assert exc.value.code == 2
    assert "DOCS_HUB_API_KEY not set" in capsys.readouterr().err


# --- the transport ------------------------------------------------------


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_request_sends_the_key_header_and_returns_the_body(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.method
        seen["key"] = req.get_header("X-docs-key")
        seen["extra"] = req.get_header("Content-type")
        seen["timeout"] = timeout
        return _Resp(200, b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert cli._request("POST", "/api/publish", data=b"body",
                        headers={"Content-Type": "text/plain"}) == (200, b"ok")
    assert seen == {"url": _DEAD_URL + "/api/publish", "method": "POST",
                    "key": "test-api-key", "extra": "text/plain", "timeout": 30}


def test_request_returns_the_body_of_an_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {},
                                     io.BytesIO(b"<html>502</html>"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert cli._request("GET", "/api/list") == (503, b"<html>502</html>")


def test_request_exits_1_when_the_connection_fails(monkeypatch, capsys):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as exc:
        cli._request("GET", "/api/list")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "connection to " + _DEAD_URL + " failed" in err
    assert "Connection refused" in err


# --- publish ------------------------------------------------------------


def _doc(tmp_path, body=b"<h1>x</h1>"):
    path = tmp_path / "d.html"
    path.write_bytes(body)
    return str(path)


def test_publish_posts_the_form_and_reports_the_url(tmp_path, monkeypatch, capsys):
    tr = _Transport((200, _json({"ok": True, "slug": "a/b", "version": 3,
                                 "url": "/d/a/b"})))
    rc = _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a/b",
               "--title", "T", "--tags", "spec", "--project", "p",
               "--from", "analyst")
    assert rc == 0
    method, path, data, headers = tr.calls[0]
    assert (method, path) == ("POST", "/api/publish")
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    for field in (b'name="slug"\r\n\r\na/b', b'name="tags"\r\n\r\nspec',
                  b'name="project"\r\n\r\np', b'name="from"\r\n\r\nanalyst'):
        assert field in data
    assert capsys.readouterr().out == f"published a/b v3 -> {_DEAD_URL}/d/a/b\n"


def test_publish_sends_empty_strings_for_the_optional_fields(tmp_path, monkeypatch):
    tr = _Transport((200, _json({"ok": True, "slug": "a", "version": 1, "url": "/d/a"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
                 "--title", "T", "--from", "analyst") == 0
    data = tr.calls[0][2]
    assert b'name="tags"\r\n\r\n\r\n' in data
    assert b'name="project"\r\n\r\n\r\n' in data


def test_publish_reports_a_non_json_error_body(tmp_path, monkeypatch, capsys):
    # The Cloudflare-502 case: an HTML error page must produce a readable
    # message, not a JSONDecodeError traceback.
    tr = _Transport((502, b"<html><body>Bad gateway</body></html>"))
    rc = _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
               "--title", "T", "--from", "analyst")
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: HTTP 502: non-JSON response: <html>")


def test_publish_truncates_a_long_non_json_body_to_200_bytes(tmp_path, monkeypatch, capsys):
    tr = _Transport((500, b"x" * 5000))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
                 "--title", "T", "--from", "analyst") == 1
    err = capsys.readouterr().err
    assert err.count("x") == 200


def test_publish_reports_a_json_error_payload(tmp_path, monkeypatch, capsys):
    tr = _Transport((400, _json({"ok": False, "error": "slug is required"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
                 "--title", "T", "--from", "analyst") == 1
    assert capsys.readouterr().err == "ERROR: slug is required\n"


def test_publish_reports_a_200_that_is_not_ok(tmp_path, monkeypatch, capsys):
    tr = _Transport((200, _json({"ok": False})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
                 "--title", "T", "--from", "analyst") == 1
    assert "ERROR:" in capsys.readouterr().err


def test_publish_of_a_missing_file_reports_the_path_and_sends_nothing(
        monkeypatch, tmp_path, capsys):
    # The same one-line shape every other publish failure uses: no traceback,
    # and nothing is sent, since the read happens before the request.
    missing = tmp_path / "nope.html"
    tr = _Transport((200, _json({"ok": True})))
    assert _main(monkeypatch, tr, "publish", str(missing),
                 "--slug", "a", "--title", "T", "--from", "analyst") == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: "), err
    assert str(missing) in err, err
    assert not tr.calls


def test_publish_sends_ttl_when_given(tmp_path, monkeypatch):
    tr = _Transport((200, _json({"ok": True, "slug": "a", "version": 1,
                                 "url": "/d/a"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
                 "--title", "T", "--from", "analyst", "--ttl", "1h") == 0
    assert b'name="ttl"\r\n\r\n1h\r\n' in tr.calls[0][2]


def test_publish_omits_ttl_when_not_given(tmp_path, monkeypatch):
    # Absent, not blank. The server reads a whitespace-only ttl as no ttl, so
    # both spellings land the same today; sending nothing keeps "permanent"
    # stated by the wire format rather than by a parse rule on the far side.
    tr = _Transport((200, _json({"ok": True, "slug": "a", "version": 1,
                                 "url": "/d/a"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
                 "--title", "T", "--from", "analyst") == 0
    assert b'name="ttl"' not in tr.calls[0][2]


def test_publish_omits_a_whitespace_only_ttl(tmp_path, monkeypatch):
    # `--ttl "  "` is a fumbled shell quote, not a lifetime. Stating it blank
    # on the wire would ask the server to parse its way back to "permanent".
    tr = _Transport((200, _json({"ok": True, "slug": "a", "version": 1,
                                 "url": "/d/a"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a",
                 "--title", "T", "--from", "analyst", "--ttl", "   ") == 0
    assert b'name="ttl"' not in tr.calls[0][2]


def test_publish_reports_the_expiry_when_the_server_returns_one(
        tmp_path, monkeypatch, capsys):
    tr = _Transport((200, _json({"ok": True, "slug": "a/b", "version": 1,
                                 "url": "/d/a/b",
                                 "expires_at": "2026-09-14T12:00:00+00:00"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a/b",
                 "--title", "T", "--from", "analyst", "--ttl", "1h") == 0
    assert capsys.readouterr().out == (
        f"published a/b v1 -> {_DEAD_URL}/d/a/b"
        " (expires 2026-09-14T12:00:00+00:00)\n")


def test_publish_output_is_unchanged_without_an_expiry(
        tmp_path, monkeypatch, capsys):
    tr = _Transport((200, _json({"ok": True, "slug": "a/b", "version": 1,
                                 "url": "/d/a/b", "expires_at": None})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a/b",
                 "--title", "T", "--from", "analyst") == 0
    assert capsys.readouterr().out == f"published a/b v1 -> {_DEAD_URL}/d/a/b\n"


def test_publish_warns_when_a_requested_ttl_is_not_confirmed(
        tmp_path, monkeypatch, capsys):
    # No `expires_at` key at all -- a server predating the ttl field, which
    # ignored it and stored the document permanently. The publish succeeded,
    # so this is a warning on stderr and rc 0, not a failure; without it the
    # operator reads "published" and believes the document will expire.
    tr = _Transport((200, _json({"ok": True, "slug": "a/b", "version": 1,
                                 "url": "/d/a/b"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a/b",
                 "--title", "T", "--from", "analyst", "--ttl", "1h") == 0
    out, err = capsys.readouterr()
    assert out == f"published a/b v1 -> {_DEAD_URL}/d/a/b\n"
    assert err == ("WARNING: server did not confirm an expiry (it predates "
                   "--ttl); the document is permanent\n")


def test_publish_does_not_warn_about_an_expiry_nobody_asked_for(
        tmp_path, monkeypatch, capsys):
    # The same response, no --ttl: a permanent document is what was asked
    # for, so the missing key says nothing and there is nothing to warn about.
    tr = _Transport((200, _json({"ok": True, "slug": "a/b", "version": 1,
                                 "url": "/d/a/b"})))
    assert _main(monkeypatch, tr, "publish", _doc(tmp_path), "--slug", "a/b",
                 "--title", "T", "--from", "analyst") == 0
    assert capsys.readouterr().err == ""


# --- get ----------------------------------------------------------------


def test_get_writes_bytes_to_the_output_file(tmp_path, monkeypatch, capsys):
    out = tmp_path / "got.html"
    tr = _Transport((200, b"<h1>doc</h1>"))
    assert _main(monkeypatch, tr, "get", "a/b", "-o", str(out)) == 0
    assert tr.paths == ["/api/doc/a/b"]
    assert out.read_bytes() == b"<h1>doc</h1>"
    assert capsys.readouterr().out == f"wrote {out} (12 bytes)\n"


def test_get_writes_bytes_to_stdout_without_an_output_file(monkeypatch):
    fake = _Stdout()
    monkeypatch.setattr(sys, "stdout", fake)
    tr = _Transport((200, b"<h1>\xc3\xa9</h1>"))
    assert _main(monkeypatch, tr, "get", "a/b") == 0
    assert fake.buffer.getvalue() == b"<h1>\xc3\xa9</h1>"


def test_get_with_a_version_uses_the_versioned_route(monkeypatch):
    fake = _Stdout()
    monkeypatch.setattr(sys, "stdout", fake)
    tr = _Transport((200, b"old"))
    assert _main(monkeypatch, tr, "get", "a/b", "--version", "2") == 0
    assert tr.paths == ["/d/a/b/v2"]


def test_get_text_only_writes_the_stripped_text(tmp_path, monkeypatch):
    out = tmp_path / "got.txt"
    tr = _Transport((200, b"<h1>Head</h1><script>x()</script><p>Body</p>"))
    assert _main(monkeypatch, tr, "get", "a/b", "--text-only", "-o", str(out)) == 0
    assert out.read_bytes() == b"Head\n\nBody\n"


def test_get_reports_an_http_error_status(monkeypatch, capsys):
    tr = _Transport((404, b""))
    assert _main(monkeypatch, tr, "get", "missing") == 1
    assert capsys.readouterr().err == "ERROR: HTTP 404\n"


# --- list ---------------------------------------------------------------


_DOCS = {"docs": [
    {"slug": "a/one", "latest_version": 2, "posted_by": "analyst",
     "tags": ["spec"], "title": "One"},
    {"slug": "a/two", "latest_version": 1, "posted_by": "builder",
     "tags": [], "title": "Two"},
]}


def test_list_renders_a_row_per_doc(monkeypatch, capsys):
    tr = _Transport((200, _json(_DOCS)))
    assert _main(monkeypatch, tr, "list") == 0
    assert tr.paths == ["/api/list"]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["a/one", "v2", "analyst", "[spec]", "One"]
    # No tags renders as a literal dash rather than an empty bracket pair.
    assert lines[1].split() == ["a/two", "v1", "builder", "[-]", "Two"]


def test_list_filters_by_project(monkeypatch, capsys):
    tr = _Transport((200, _json({"docs": []})))
    assert _main(monkeypatch, tr, "list", "--project", "docs-hub") == 0
    assert tr.paths == ["/api/list?project=docs-hub"]
    assert capsys.readouterr().out == ""


def test_list_filters_by_agent(monkeypatch):
    tr = _Transport((200, _json({"docs": []})))
    assert _main(monkeypatch, tr, "list", "--agent", "analyst") == 0
    assert tr.paths == ["/api/list?agent=analyst"]


def test_list_combines_both_filters(monkeypatch):
    tr = _Transport((200, _json({"docs": []})))
    assert _main(monkeypatch, tr, "list", "--project", "p", "--agent", "a") == 0
    assert tr.paths == ["/api/list?project=p&agent=a"]


def test_list_untagged_keeps_only_docs_with_no_tags(monkeypatch, capsys):
    tr = _Transport((200, _json(_DOCS)))
    assert _main(monkeypatch, tr, "list", "--untagged") == 0
    # Filtered client-side: the query string is unchanged.
    assert tr.paths == ["/api/list"]
    out = capsys.readouterr().out
    assert "a/two" in out and "a/one" not in out


def test_list_untagged_also_drops_a_doc_with_a_null_tags_field(monkeypatch, capsys):
    tr = _Transport((200, _json({"docs": [
        {"slug": "a", "latest_version": 1, "posted_by": "x",
         "tags": None, "title": "T"}]})))
    assert _main(monkeypatch, tr, "list", "--untagged") == 0
    assert "[-]" in capsys.readouterr().out


def test_list_reports_an_http_error_status(monkeypatch, capsys):
    tr = _Transport((500, b""))
    assert _main(monkeypatch, tr, "list") == 1
    assert capsys.readouterr().err == "ERROR: HTTP 500\n"


def test_list_appends_the_expiry_when_present(monkeypatch, capsys):
    # Three shapes a server can return: an expiry set, an explicit null, and
    # the key missing entirely (a server older than the ttl feature). Only
    # the first prints a suffix.
    tr = _Transport((200, _json({"docs": [
        {"slug": "a/ttl", "latest_version": 1, "posted_by": "analyst",
         "tags": [], "title": "Ttl", "expires_at": "2026-09-14T12:00:00+00:00"},
        {"slug": "a/null", "latest_version": 1, "posted_by": "analyst",
         "tags": [], "title": "Null", "expires_at": None},
        {"slug": "a/old", "latest_version": 1, "posted_by": "analyst",
         "tags": [], "title": "Old"},
    ]})))
    assert _main(monkeypatch, tr, "list") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith(" Ttl expires 2026-09-14T12:00:00+00:00")
    assert lines[1].endswith(" Null")
    assert lines[2].endswith(" Old")


# --- versions -----------------------------------------------------------


def test_versions_renders_a_row_per_version(monkeypatch, capsys):
    tr = _Transport((200, _json({"versions": [
        {"version": 2, "created_at": "2026-08-23T00:00:00", "posted_by": "a",
         "byte_size": 44},
        {"version": 1, "created_at": "2026-08-22T00:00:00", "posted_by": "a",
         "byte_size": 12}]})))
    assert _main(monkeypatch, tr, "versions", "a/b") == 0
    assert tr.paths == ["/api/versions/a/b"]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["v2", "2026-08-23T00:00:00", "a", "44", "bytes"]
    assert lines[1].startswith("v1 ")


def test_versions_reports_an_http_error_status(monkeypatch, capsys):
    tr = _Transport((404, b""))
    assert _main(monkeypatch, tr, "versions", "gone") == 1
    assert capsys.readouterr().err == "ERROR: HTTP 404\n"


# --- tags ---------------------------------------------------------------


def test_tags_renders_count_and_tag(monkeypatch, capsys):
    tr = _Transport((200, _json({"tags": [{"tag": "spec", "count": 7},
                                          {"tag": "plan", "count": 2}]})))
    assert _main(monkeypatch, tr, "tags") == 0
    assert tr.paths == ["/api/tags"]
    assert capsys.readouterr().out == "    7  spec\n    2  plan\n"


def test_tags_filters_by_project(monkeypatch):
    tr = _Transport((200, _json({"tags": []})))
    assert _main(monkeypatch, tr, "tags", "--project", "docs-hub") == 0
    assert tr.paths == ["/api/tags?project=docs-hub"]


def test_tags_reports_an_http_error_status(monkeypatch, capsys):
    tr = _Transport((503, b""))
    assert _main(monkeypatch, tr, "tags") == 1
    assert capsys.readouterr().err == "ERROR: HTTP 503\n"


# --- the parser itself --------------------------------------------------


def test_main_requires_a_subcommand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["docs-hub"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_rejects_publish_without_its_required_options(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["docs-hub", "publish", _doc(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_accepts_the_ttl_option(monkeypatch, tmp_path):
    seen = {}

    def _capture(args):
        seen["ttl"] = args.ttl
        return 0

    monkeypatch.setattr(cli, "cmd_publish", _capture)
    monkeypatch.setattr(sys, "argv", [
        "docs-hub", "publish", _doc(tmp_path), "--slug", "a/b",
        "--title", "T", "--from", "analyst", "--ttl", "2d"])
    assert cli.main() == 0
    assert seen["ttl"] == "2d"


# --- the vendored settings reader ---------------------------------------
#
# _settings.py is vendored byte-identical from the fleet bundle and is part of
# the wheel, so it is measured with the rest of the package. Each test resets
# the module-level cache first (monkeypatch restores it afterwards) and points
# both settings directories at tmp_path, so nothing reads the real ~/.claude
# or ~/.agent-bundle.


@pytest.fixture
def settings_dirs(tmp_path, monkeypatch):
    legacy, neutral = tmp_path / "legacy", tmp_path / "neutral"
    legacy.mkdir()
    neutral.mkdir()
    monkeypatch.setattr(_settings, "LEGACY_SETTINGS_DIR", legacy)
    monkeypatch.setattr(_settings, "SETTINGS_DIR", neutral)
    monkeypatch.setattr(_settings, "_cache", None)
    return legacy, neutral


def _write_env(directory, name, env):
    (directory / name).write_text(json.dumps({"env": env}), encoding="utf-8")


def test_setting_prefers_the_environment(settings_dirs, monkeypatch):
    _, neutral = settings_dirs
    _write_env(neutral, "settings.json", {"DOCS_HUB_URL": "https://file.test"})
    monkeypatch.setenv("DOCS_HUB_URL", "https://env.test")
    assert _settings.setting("DOCS_HUB_URL") == "https://env.test"


def test_setting_treats_a_blank_environment_variable_as_unset(settings_dirs, monkeypatch):
    _, neutral = settings_dirs
    _write_env(neutral, "settings.json", {"DOCS_HUB_URL": "https://file.test"})
    monkeypatch.setenv("DOCS_HUB_URL", "")
    assert _settings.setting("DOCS_HUB_URL") == "https://file.test"


def test_setting_prefers_local_over_base_and_neutral_over_legacy(settings_dirs, monkeypatch):
    legacy, neutral = settings_dirs
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)
    monkeypatch.delenv("C", raising=False)
    _write_env(legacy, "settings.json", {"A": "legacy", "B": "legacy", "C": "legacy"})
    _write_env(neutral, "settings.json", {"A": "neutral", "B": "neutral"})
    _write_env(neutral, "settings.local.json", {"A": "local"})
    assert _settings.setting("A") == "local"
    assert _settings.setting("B") == "neutral"
    assert _settings.setting("C") == "legacy"


@pytest.mark.usefixtures("settings_dirs")
def test_setting_falls_back_to_the_default(monkeypatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    assert _settings.setting("ABSENT_KEY", "fallback") == "fallback"
    assert _settings.setting("ABSENT_KEY") is None


def test_setting_skips_unreadable_and_non_string_entries(settings_dirs, monkeypatch):
    _, neutral = settings_dirs
    monkeypatch.delenv("N", raising=False)
    (neutral / "settings.json").write_text("{not json", encoding="utf-8")
    _write_env(neutral, "settings.local.json", {"N": 5, "S": "str"})
    assert _settings.setting("N", "default") == "default"
    assert _settings.setting("S") == "str"


def test_file_env_is_cached_until_forced(settings_dirs, monkeypatch):
    _, neutral = settings_dirs
    monkeypatch.delenv("K", raising=False)
    _write_env(neutral, "settings.json", {"K": "first"})
    assert _settings.setting("K") == "first"
    _write_env(neutral, "settings.json", {"K": "second"})
    assert _settings.setting("K") == "first"
    assert _settings._file_env(force=True)["K"] == "second"


@pytest.mark.usefixtures("settings_dirs")
def test_required_returns_a_present_value(monkeypatch):
    monkeypatch.setenv("PRESENT", "yes")
    assert _settings.required("PRESENT") == "yes"


@pytest.mark.usefixtures("settings_dirs")
def test_required_raises_naming_the_key_and_the_file(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        _settings.required("MISSING_KEY", hint="ask the operator")
    message = str(exc.value)
    assert "MISSING_KEY is not set" in message
    assert "settings.json" in message
    assert "ask the operator" in message
