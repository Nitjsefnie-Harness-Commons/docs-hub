"""Rewritten side-branch pushes defer window enforcement to the PR route."""
# pylint: disable=protected-access  # Use real commits and a local bare remote.
import json
from pathlib import Path

import pytest

import test_budget_raise as f


@pytest.mark.parametrize("branch, default, expected", [
    ("feature", "main", 0), ("main", "main", 2), ("feature", "feature", 2),
])
@pytest.mark.parametrize("unknown_before", [False, True])
@pytest.mark.parametrize("source", ["env", "flags"])
def test_force_push_window_is_checked_only_on_the_default_branch(
        tmp_path: Path, branch: str, default: str, expected: int,
        unknown_before: bool, source: str) -> None:
    repo, _base = f._seeded(tmp_path)
    remote = tmp_path / "remote.git"
    f._git(repo, "clone", "--bare", str(repo), str(remote))
    f._git(repo, "remote", "add", "origin", str(remote))
    f._git(repo, "checkout", "-B", branch)
    before = f._commit(repo, "old tip", **{f.CODE: "print(2)\n"})
    f._git(repo, "push", "-u", "origin", branch)
    (repo / f.CODE).write_text("print(3)\n", encoding="utf-8")
    f._git(repo, "add", f.CODE)
    f._git(repo, "commit", "--amend", "-m", "rewritten tip")
    f._git(repo, "push", "--force-with-lease", "origin", branch)
    assert f._git(repo, "rev-parse", "HEAD^") != before
    if unknown_before:
        before = "f" * 40
    flags = ["--event", "push", "--before", before, "--head", "HEAD"]
    env = {"DOCS_HUB_REF_NAME": branch, "DOCS_HUB_DEFAULT_BRANCH": default}
    if source == "flags":
        flags += ["--ref-name", branch, "--default-branch", default]
        env = {"DOCS_HUB_REF_NAME": "wrong", "DOCS_HUB_DEFAULT_BRANCH": "wrong"}
    result = f._run(repo, *flags, env=env)
    assert result.returncode == expected, result.stdout + result.stderr
    if expected == 0:
        assert "skipping" in result.stdout and "pull_request" in result.stdout
    else:
        assert ("unknown revision" if unknown_before else "not an ancestor") in result.stderr


def test_side_branch_context_never_skips_a_pull_request(tmp_path: Path) -> None:
    repo, base = f._seeded(tmp_path)
    f._commit(repo, "undeclared raise", **{f.BUDGET: json.dumps(f._budget(
        {"w.pub": 300, "w.list": 200}))})
    result = f._run(repo, "--event", "pull_request", "--base", base, "--head", "HEAD",
                    env={"DOCS_HUB_REF_NAME": "feature", "DOCS_HUB_DEFAULT_BRANCH": "main"})
    assert result.returncode == 1, result.stdout + result.stderr
