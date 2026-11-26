"""`docs-hub delete`: the two-step client of POST /api/delete, one call per run."""
# redefined-outer-name: pytest fixtures are requested by parameter name.
# pylint: disable=redefined-outer-name
import json
import sys

import pytest

from docs_hub_cli import cli

pytestmark = pytest.mark.portable

_MATCH = {"slug": "a/b", "title": "Doc B", "latest_version": 2}
_PREVIEW = {"ok": True, "preview": True, "count": 2, "confirm_token": "tok.1",
            "matched": [_MATCH, {"slug": "c", "title": "Doc C", "latest_version": 11}]}


@pytest.fixture
def run(monkeypatch, capsys):
    def go(*argv, reply=None):
        calls = []

        def request(method, path, *, data=None, headers=None):
            calls.append((method, path, headers, json.loads(data)))
            if reply is None or len(calls) > 1:
                pytest.fail(f"unexpected request #{len(calls)}: {method} {path}")
            status, payload = reply
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return status, raw, "application/json"

        monkeypatch.setattr(cli, "_request", request)
        monkeypatch.setattr(sys, "argv", ["docs-hub", "delete", *argv])
        try:
            code = cli.main()
        except SystemExit as exit_:
            code = exit_.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err, calls

    return go


@pytest.mark.parametrize("argv", [
    (), ("--confirm", "tok.1"), ("--slug", "  "), ("--project", " ", "--q", "\t", "--tag", ""),
])
def test_no_filter_is_a_usage_error_without_a_request(run, argv):
    code, out, err, calls = run(*argv)
    assert (code, out, calls) == (2, "", [])
    assert "docs-hub delete: error: at least one filter" in err


@pytest.mark.parametrize("option, key", [
    ("--slug", "slug"), ("--project", "project"), ("--author", "author"), ("--tag", "tag"),
    ("--q", "q"), ("--updated-before", "updated_before"),
    ("--updated-after", "updated_after"),
])
def test_each_filter_is_sent_as_its_body_key_unstripped(run, option, key):
    code, _, _, calls = run(option, " v ", reply=(200, _PREVIEW))
    assert code == 0
    assert calls == [("POST", "/api/delete", {"Content-Type": "application/json"},
                      {key: " v "})]


def test_blank_filters_are_not_sent_and_confirm_is_sent_as_confirm_token(run):
    code, out, err, calls = run("--slug", "a", "--project", " ", "--confirm", "tok.1",
                                reply=(200, {"ok": True, "preview": False, "deleted": 3}))
    assert (code, out, err) == (0, "deleted 3 document(s)\n", "")
    assert [c[3] for c in calls] == [{"slug": "a", "confirm_token": "tok.1"}]


def test_preview_lists_every_match_and_the_token_and_deletes_nothing(run):
    code, out, err, calls = run("--tag", "x", reply=(200, _PREVIEW))
    assert (code, err) == (0, "")
    assert [c[3] for c in calls] == [{"tag": "x"}]
    assert out.splitlines() == [
        "2 document(s) match; nothing is deleted yet",
        f"{'a/b':<40} v2   Doc B",
        f"{'c':<40} v11  Doc C",
        "confirm token: tok.1 (valid for 5 minutes, and only with the same filter values; "
        "to delete, re-run with --confirm tok.1 and the same filters)",
    ]


def test_zero_matches_still_print_the_count_and_the_token(run):
    code, out, _, _ = run("--slug", "x", reply=(
        200, {"ok": True, "preview": True, "count": 0, "matched": [], "confirm_token": "t"}))
    assert code == 0
    assert out.splitlines()[0] == "0 document(s) match; nothing is deleted yet"
    assert "confirm token: t (" in out.splitlines()[1]


@pytest.mark.parametrize("status, payload, line", [
    (409, {"ok": False, "error": "confirm token invalid or expired"},
     "confirm token invalid or expired"),
    (400, {"ok": False, "error": "at least one filter required"},
     "at least one filter required"),
    (401, {"ok": False, "error": "unauthorized"}, "unauthorized"),
    (500, {"ok": False, "error": "boom"}, "boom"),
    (500, {"ok": False, "error": "failed to remove stored files", "deleted": 2,
           "failed": ["a/b", "c"]},
     "deleted 2 document(s), but failed to remove a/b, c; those documents still exist: "
     "a new preview and confirm retries them"),
])
def test_error_replies_are_one_error_line_on_stderr(run, status, payload, line):
    code, out, err, calls = run("--slug", "a", "--confirm", "t", reply=(status, payload))
    assert (code, out, err) == (1, "", f"ERROR: {line}\n")
    assert len(calls) == 1


_VALID_PREVIEW_ROW = {"slug": "a", "title": "T", "latest_version": 1}


@pytest.mark.parametrize("argv, status, payload", [
    ((), 502, b"<html>Bad gateway</html>"),
    ((), 200, b"[]"),
    ((), 200, {}),
    ((), 200, {"matched": None, "confirm_token": "t"}),
    ((), 200, {"matched": []}),
    ((), 200, {"confirm_token": "t"}),
    ((), 200, {"matched": [], "confirm_token": 7}),
    ((), 200, {"matched": {}, "confirm_token": "t"}),
    ((), 200, {"matched": ["x"], "confirm_token": "t"}),
    ((), 200, {"matched": [{}], "confirm_token": "t"}),
    ((), 200, {"matched": [_VALID_PREVIEW_ROW, None], "confirm_token": "t"}),
    ((), 200, {"matched": [{**_VALID_PREVIEW_ROW, "slug": []}], "confirm_token": "t"}),
    (("--confirm", "t"), 200, {"preview": False}),
    (("--confirm", "t"), 200, {"deleted": "3"}),
    (("--confirm", "t"), 200, {"deleted": True}),
    (("--confirm", "t"), 200, _PREVIEW),
    (("--confirm", "t"), 502, b"<html>Bad gateway</html>"),
    (("--confirm", "t"), 409, {"ok": False}),
    (("--confirm", "t"), 409, {"ok": False, "error": ["x"]}),
    (("--confirm", "t"), 409, []),
    (("--confirm", "t"), 500, {"error": "e", "deleted": "2", "failed": ["a"]}),
    (("--confirm", "t"), 500, {"error": "e", "deleted": 2, "failed": "a"}),
    (("--confirm", "t"), 500, {"error": "e", "deleted": 2, "failed": [1]}),
    (("--confirm", "t"), 500, {"error": "e", "failed": ["a"]}),
])
def test_malformed_replies_are_an_invalid_response_not_a_traceback(
        run, argv, status, payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    code, out, err, _ = run("--slug", "a", *argv, reply=(status, payload))
    assert (code, out) == (1, "")
    assert err == f"ERROR: HTTP {status}: invalid response: {raw.decode()}\n"
