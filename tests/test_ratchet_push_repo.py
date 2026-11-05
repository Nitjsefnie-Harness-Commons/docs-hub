"""The repository name in ratchet-push.yml: prefixed, with the old name as fallback.

Each step reads it in its own script, so each is run against the four
cases of tests/test_tag_waiter.py; the harnesses are test_ratchet_push's.
"""
# pylint: disable=protected-access  # Reuse the sibling suite's harnesses.
from pathlib import Path

import pytest

import test_ratchet_push as f

_REPO_CASES = [
    pytest.param({"DOCS_HUB_REPO": "example/repository"}, id="prefixed-only"),
    pytest.param({"REPO": "example/repository"}, id="old-only"),
    pytest.param({"DOCS_HUB_REPO": "example/repository", "REPO": "example/legacy"},
                 id="prefixed-wins"),
    pytest.param({"DOCS_HUB_REPO": "", "REPO": "example/repository"},
                 id="empty-prefixed-falls-back"),
]

# Distinct per case, so a hardcoded repository fails.
_LIST_CASES = [
    pytest.param({"DOCS_HUB_REPO": "example/prefixed"}, "example/prefixed",
                 id="prefixed-only"),
    pytest.param({"REPO": "example/old"}, "example/old", id="old-only"),
    pytest.param({"DOCS_HUB_REPO": "example/prefixed", "REPO": "example/old"},
                 "example/prefixed", id="prefixed-wins"),
    pytest.param({"DOCS_HUB_REPO": "", "REPO": "example/old"}, "example/old",
                 id="empty-prefixed-falls-back"),
]


@pytest.mark.parametrize("name", ["List the triggering run's artifacts",
                                  "Push the raise commit to master"])
def test_each_step_sets_only_the_prefixed_repository_variable(name: str) -> None:
    env = f._step(name)["env"]
    assert env["DOCS_HUB_REPO"] == "${{ github.repository }}"
    assert "REPO" not in env


@pytest.mark.parametrize("repo_env, expected", _LIST_CASES)
def test_the_repository_reaches_the_artifacts_url(
        tmp_path: Path, repo_env: dict[str, str], expected: str) -> None:
    result = f._run_list_step(tmp_path, {"DOCS_HUB_RUN_ID": "1" * 10, **repo_env})
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / "gh-calls.log").read_text(encoding="utf-8").splitlines()
    assert log == [f"repos/{expected}/actions/runs/" + "1" * 10 + "/artifacts"]


@pytest.mark.parametrize("repo_env", _REPO_CASES)
def test_the_repository_reaches_the_push_remote(
        tmp_path: Path, repo_env: dict[str, str]) -> None:
    origin, seed = f._seed_remote(tmp_path)
    f._raise_data(tmp_path, seed, "97.4")
    result = f._run_push_step(tmp_path, "true", "false", repo_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f._git(origin, "log", "--format=%s", "refs/heads/master").splitlines() == [
        "chore(ratchet): Raise the recorded coverage calibration (automated)", "seed"]
