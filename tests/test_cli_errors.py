"""CLI failures stay readable for bad output paths and malformed replies."""
# redefined-outer-name: pytest fixtures are requested by parameter name.
# pylint: disable=redefined-outer-name
import errno
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
    (b"<html>\r\nbad\nreply</html>", "<html> bad reply</html>"),
])
def test_read_commands_report_malformed_replies(run_cli, capsys, argv, body, preview):
    assert run_cli(*argv, body=body) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: HTTP 200: invalid response: {preview}\n"
