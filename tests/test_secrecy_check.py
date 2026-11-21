"""Exercise the secrecy check's literal sources without credentials or live databases."""
from pathlib import Path
import subprocess

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts/secrecy-check.sh"
_ERROR = "secrecy-check: ERROR — no literals available; add .secrecy-literals or .env\n"


def _check(cwd: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(_SCRIPT), *flags], cwd=cwd,
                          check=False, capture_output=True, text=True)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   cwd=cwd, check=True)


def _plant(tree: Path, text: str) -> None:
    (tree / "candidate").write_text(text, encoding="utf-8")
    _git(tree, "add", "candidate")


def _make_repos(tmp_path: Path, bare: bool = False) -> Path:
    """`main` (or a bare clone of it, `bare`) with one commit; returns its linked worktree."""
    _git(tmp_path, "init", "--quiet", "main")
    _git(tmp_path / "main", "commit", "--quiet", "--allow-empty", "-m", "base")
    if bare:
        _git(tmp_path, "clone", "--quiet", "--bare", "main", "bare")
    _git(tmp_path / ("bare" if bare else "main"), "worktree", "add", "--quiet", "../linked")
    return tmp_path / "linked"


@pytest.mark.parametrize("settings,detected,ignored", [
    ("DOCS_HUB_DATABASE_URL_AUTH=postgresql:///preferred_auth_fixture\n",
     "preferred_auth_fixture", "legacy_auth_fixture"),
    ("DATABASE_URL_AUTH=postgresql:///legacy_auth_fixture\n",
     "legacy_auth_fixture", "preferred_auth_fixture"),
    ("DATABASE_URL_AUTH=postgresql:///legacy_auth_fixture\n"
     "DOCS_HUB_DATABASE_URL_AUTH=postgresql:///preferred_auth_fixture\n",
     "preferred_auth_fixture", "legacy_auth_fixture"),
    ("DOCS_HUB_DATABASE_URL_AUTH=\nDATABASE_URL_AUTH=postgresql:///legacy_auth_fixture\n",
     "legacy_auth_fixture", "preferred_auth_fixture"),
    (" export DOCS_HUB_DATABASE_URL_AUTH = "
     "'postgresql:///preferred_auth_fixture?sslmode=require'\n",
     "preferred_auth_fixture", "legacy_auth_fixture"),
])
def test_auth_database_name_prefers_prefix_and_accepts_legacy(tmp_path: Path, settings: str,
                                                              detected: str, ignored: str):
    _git(tmp_path, "init", "--quiet")
    (tmp_path / ".env").write_text(settings, encoding="utf-8")
    _plant(tmp_path, detected)
    result = _check(tmp_path, "--tree")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "the auth-DB name" in result.stderr
    assert "candidate" in result.stderr
    assert detected not in result.stderr
    (tmp_path / "candidate").write_text(ignored, encoding="utf-8")
    result = _check(tmp_path, "--tree")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("files,literals,label", [
    ({"main/.secrecy-literals": "main_list_fixture\n"}, ["main_list_fixture"], "a listed literal"),
    ({"main/.env": "DOCS_HUB_DATABASE_URL_AUTH=postgresql:///main_auth_fixture\n"},
     ["main_auth_fixture"], "the auth-DB name"),
    ({"main/.secrecy-literals": "main_list_fixture\n",
      "linked/.secrecy-literals": "own_list_fixture\n"},
     ["main_list_fixture", "own_list_fixture"], "a listed literal"),
])
def test_a_linked_worktree_checks_the_main_worktrees_and_its_own_literals(
        tmp_path: Path, files: dict[str, str], literals: list[str], label: str):
    linked = _make_repos(tmp_path)
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert _check(linked, "--tree").stdout.endswith(f" clean ({len(literals)} literal(s))\n")
    for literal in literals:
        _plant(linked, literal)
        result = _check(linked, "--tree")
        assert result.returncode == 1 and label in result.stderr and "candidate" in result.stderr
        assert literal not in result.stderr
    _git(linked, "commit", "--quiet", "-m", "leak")
    _git(linked, "rm", "--quiet", "candidate")
    result = _check(linked)
    assert result.returncode == 1 and "is in committed history" in result.stderr
    assert "working tree" not in result.stderr and literals[-1] not in result.stderr


@pytest.mark.parametrize("bare,source,run_in,flags,expected", [
    (False, "main", "main", ["--tree"], (0, "secrecy-check: tree clean (1 literal(s))\n", "")),
    (False, "main", "main", [], (0, "secrecy-check: clean (tree + history, 1 literal(s))\n", "")),
    (False, None, "linked", ["--tree"], (1, "", _ERROR)),
    (True, "bare", "linked", ["--tree"], (1, "", _ERROR)),  # read only if `bare` is not skipped
])
def test_the_main_checkout_reads_its_literals_once_and_none_anywhere_is_an_error(
        tmp_path: Path, bare: bool, source: str | None, run_in: str, flags: list[str],
        expected: tuple[int, str, str]):
    _make_repos(tmp_path, bare)
    if source:
        (tmp_path / source / ".secrecy-literals").write_text("listed_fixture\n", encoding="utf-8")
    result = _check(tmp_path / run_in, *flags)
    assert (result.returncode, result.stdout, result.stderr) == expected
