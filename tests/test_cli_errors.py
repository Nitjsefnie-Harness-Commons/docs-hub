"""CLI failures stay readable for bad output paths and malformed replies."""
# redefined-outer-name: pytest fixtures are requested by parameter name.
# pylint: disable=redefined-outer-name
import errno
import json
import os
import sys

import pytest

from docs_hub_cli import cli


@pytest.fixture
def run_cli(monkeypatch):
    def run(*argv, body=b"<h1>doc</h1>"):
        monkeypatch.setattr(cli, "_request", lambda *_args, **_kwargs: (200, body, "text/html"))
        monkeypatch.setattr(sys, "argv", ["docs-hub", *argv])
        return cli.main()

    return run


@pytest.mark.parametrize("target, error_number", [
    ("missing/got.html", errno.ENOENT),
    (".", errno.EISDIR),
])
def test_get_reports_an_unwritable_output_path(
        tmp_path, run_cli, capsys, target, error_number):
    out = tmp_path / target
    assert run_cli("get", "a/b", "-o", str(out)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: cannot write {out}: {os.strerror(error_number)}\n"


def test_get_reports_permission_denied_for_the_output_path(tmp_path, monkeypatch, run_cli, capsys):
    out = tmp_path / "denied.html"

    def denied_open(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(out))

    # chmod cannot deny writes to root, so inject the OS failure at open.
    monkeypatch.setattr(cli, "open", denied_open, raising=False)
    assert run_cli("get", "a/b", "-o", str(out)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: cannot write {out}: {os.strerror(errno.EACCES)}\n"
    assert not out.exists()


@pytest.mark.parametrize("argv", [("list",), ("versions", "a/b"), ("tags",)])
@pytest.mark.parametrize("body, preview", [
    (b"<html>Bad gateway</html>", "<html>Bad gateway</html>"),
    (b"{}", "{}"),
    (b"[]", "[]"),
    (b"null", "null"),
    (b"x" * 199 + "é".encode() + b"hidden", "x" * 199 + "�"),
    (b"<html>\r\nbad\nreply</html>", r"<html>\r\nbad\nreply</html>"),
    (b"\x1b[2J\x1b[Hforged message\x00\t\x7f\xc2\x85",
     r"\x1b[2J\x1b[Hforged message\x00\t\x7f\x85"),
    ("bad\u202ereply\u200b".encode(), r"bad\u202ereply\u200b"),
    (b"x" * 199 + b"\x1bhidden", "x" * 199 + r"\x1b"),
])
def test_read_commands_report_malformed_replies(run_cli, capsys, argv, body, preview):
    assert run_cli(*argv, body=body) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: HTTP 200: invalid response: {preview}\n"


_READ_COMMANDS = [
    (("list",), "docs", {"slug": "a/b", "latest_version": 2,
                         "posted_by": "writer", "tags": [], "title": "Doc"}),
    (("versions", "a/b"), "versions", {"version": 2, "created_at": "2026-10-23",
                                       "posted_by": "writer", "byte_size": 12}),
    (("tags",), "tags", {"tag": "audit", "count": 3}),
]


@pytest.mark.parametrize("argv, key, _valid_row", _READ_COMMANDS)
@pytest.mark.parametrize("rows", [None, {}, "", "bad", 7, False, [{}], [None], [[]], ["bad"]])
def test_read_commands_reject_invalid_containers_and_rows(
        run_cli, capsys, argv, key, _valid_row, rows):
    body = json.dumps({key: rows}).encode()
    assert run_cli(*argv, body=body) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: HTTP 200: invalid response: {body.decode()}\n"


@pytest.mark.parametrize("argv, key, valid_row", _READ_COMMANDS)
@pytest.mark.parametrize("invalid_row", [{}, None, [], "bad"])
def test_read_commands_validate_all_rows_before_printing(
        run_cli, capsys, argv, key, valid_row, invalid_row):
    body = json.dumps({key: [valid_row, invalid_row]}).encode()
    assert run_cli(*argv, body=body) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: HTTP 200: invalid response: {body.decode()}\n"


@pytest.mark.parametrize("argv, key, _valid_row", _READ_COMMANDS)
def test_read_commands_accept_empty_lists(run_cli, capsys, argv, key, _valid_row):
    assert run_cli(*argv, body=json.dumps({key: []}).encode()) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("argv, key, valid_row", _READ_COMMANDS)
def test_read_commands_reject_unformattable_rows(run_cli, capsys, argv, key, valid_row):
    field = {"docs": "slug", "versions": "version", "tags": "count"}[key]
    body = json.dumps({key: [{**valid_row, field: []}]}).encode()
    assert run_cli(*argv, body=body) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: HTTP 200: invalid response: {body.decode()}\n"


def test_list_validates_filtered_rows_before_printing(run_cli, capsys):
    body = b'{"docs": [{"tags": ["hidden"]}]}'
    assert run_cli("list", "--untagged", body=body) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: HTTP 200: invalid response: {body.decode()}\n"


def test_get_help_describes_heading_and_bold_markers(run_cli, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli("get", "--help")
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "#" in help_text and "######" in help_text
    assert "heading" in help_text and "**bold**" in help_text
