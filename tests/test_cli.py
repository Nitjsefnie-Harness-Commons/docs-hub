"""End-to-end: the real CLI, as a subprocess, against a real server.

The client lives in this repository now (`docs_hub_cli/`), so this file drives
it directly instead of the copy the fleet bundle installs. It runs everywhere
the rest of the suite runs -- a test that only passed on a machine with the
bundle installed was the thing this replaced.

The CLI is invoked as `python -m docs_hub_cli.cli`, not through the `docs-hub`
console script: the console script only exists once the wheel is installed, and
CI runs the suite from the checkout. `-m` on the submodule keeps the package
context, so `from ._settings import setting` resolves.
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

_REPO = Path(__file__).resolve().parent.parent
# `python -m docs_hub_cli.cli` rather than a path to cli.py: run as a plain
# script the relative import of ._settings has no package to resolve against.
CLI = [sys.executable, "-m", "docs_hub_cli.cli"]

_PORT = 8099
_URL = f"http://127.0.0.1:{_PORT}"

_SERVER = None


def _spawn_server():
    """Start the test server once per session. The thread is a daemon and is
    never joined, so re-binding port 8099 for a second test always failed."""
    global _SERVER
    if _SERVER is not None:
        return _SERVER
    from backend.app import app
    cfg = uvicorn.Config(app, host="127.0.0.1", port=_PORT, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        time.sleep(0.1)
        if server.started:
            _SERVER = server
            return server
    raise RuntimeError("server did not start")


def _env():
    """Environment for the child CLI.

    DOCS_HUB_URL must be in the ENVIRONMENT rather than set afterwards: the
    module reads it at import time into `_DEFAULT_URL`, so a value that arrives
    later is never seen. Pointing it at the local test server is also what
    keeps this suite off the real hub -- `docs_hub_cli._settings` would
    otherwise fall back to ~/.agent-bundle/settings.json, which on a fleet
    machine names production.
    """
    return {**os.environ,
            "DOCS_HUB_URL": _URL,
            "DOCS_HUB_API_KEY": "test-api-key",
            "PYTHONPATH": str(_REPO)}


def _run(*args, check=False):
    return subprocess.run([*CLI, *args], capture_output=True, text=True,
                          env=_env(), cwd=str(_REPO), check=check)


def test_cli_publish_get_list(tmp_path):
    _spawn_server()
    doc = tmp_path / "d.html"
    doc.write_text("<h1>cli</h1>", encoding="utf-8")
    pub = _run("publish", str(doc), "--slug", "cli/demo",
               "--title", "CLI Demo", "--from", "analyst")
    assert pub.returncode == 0, pub.stderr
    assert "cli/demo" in pub.stdout
    out = tmp_path / "got.html"
    get = _run("get", "cli/demo", "-o", str(out))
    assert get.returncode == 0, get.stderr
    assert out.read_text(encoding="utf-8") == "<h1>cli</h1>"
    lst = _run("list")
    assert "cli/demo" in lst.stdout


def test_cli_publish_with_a_ttl_reports_and_lists_the_expiry(tmp_path):
    """`--ttl` through the real server: the timestamp is whatever the server
    computed, so only the shape of the suffix is asserted."""
    _spawn_server()
    doc = tmp_path / "d.html"
    doc.write_text("<h1>ttl</h1>", encoding="utf-8")
    pub = _run("publish", str(doc), "--slug", "cli/ttl", "--title", "CLI Ttl",
               "--from", "analyst", "--ttl", "1h")
    assert pub.returncode == 0, pub.stderr
    assert "(expires " in pub.stdout, pub.stdout
    lst = _run("list")
    row = [ln for ln in lst.stdout.splitlines() if ln.startswith("cli/ttl ")]
    assert len(row) == 1, lst.stdout
    assert " expires " in row[0], row[0]


def test_cli_tags(tmp_path):
    _spawn_server()
    doc = tmp_path / "d.html"
    doc.write_text("<h1>t</h1>", encoding="utf-8")
    for slug, tags in (("cli/a", "spec,draft"), ("cli/b", "spec")):
        _run("publish", str(doc), "--slug", slug, "--title", slug,
             "--tags", tags, "--from", "analyst", check=True)
    out = _run("tags")
    assert out.returncode == 0, out.stderr
    # "<count>  <tag>" lines, most-used first
    lines = [ln.strip() for ln in out.stdout.strip().splitlines()]
    assert lines[0].endswith("spec") and lines[0].startswith("2")
    assert any(ln.endswith("draft") and ln.startswith("1") for ln in lines)


def test_cli_versions_and_text_only(tmp_path):
    """A second publish under one slug, then `versions` and `get --text-only`.

    These two subcommands have no other end-to-end cover, and --text-only is
    the only path where the CLI transforms the bytes the server returned
    instead of passing them through.
    """
    _spawn_server()
    doc = tmp_path / "d.html"
    for body in ("<h1>one</h1>", "<h1>two</h1><script>ignore()</script>"):
        doc.write_text(body, encoding="utf-8")
        _run("publish", str(doc), "--slug", "cli/vers", "--title", "Versions",
             "--from", "analyst", check=True)
    vers = _run("versions", "cli/vers")
    assert vers.returncode == 0, vers.stderr
    # Newest first, one "v<n> <created_at> <posted_by> <n> bytes" line each.
    lines = vers.stdout.strip().splitlines()
    assert [ln.split()[0] for ln in lines] == ["v2", "v1"]
    assert all(ln.endswith("bytes") and "analyst" in ln for ln in lines)
    text = _run("get", "cli/vers", "--text-only")
    assert text.returncode == 0, text.stderr
    assert text.stdout.strip() == "two"


def test_cli_rejects_a_missing_api_key(tmp_path):
    """No key -> exit 2 before any request is made.

    DOCS_HUB_API_KEY is emptied AND the settings-file fallback is neutralised
    by pointing HOME at an empty directory; an exported-but-blank value alone
    would still let ~/.agent-bundle/settings.json supply the real key on a
    fleet machine.
    """
    _spawn_server()
    env = {**_env(), "DOCS_HUB_API_KEY": "", "HOME": str(tmp_path)}
    got = subprocess.run([*CLI, "list"], capture_output=True, text=True,
                         env=env, cwd=str(_REPO), check=False)
    assert got.returncode == 2
    assert "DOCS_HUB_API_KEY not set" in got.stderr
