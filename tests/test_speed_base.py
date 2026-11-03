"""Tests for the paired run's base resolution (scripts/ci/speed_base.py).

Every case drives a real git repository and the real script; the measured
commit is the checkout's HEAD, as it is in the speed job.
"""
# pylint: disable=protected-access  # The budget-raise suite's git fixtures.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import test_budget_raise as f

_SCRIPT = f._REPO / "scripts/ci/speed_base.py"
_EVENT = ("DOCS_HUB_BASE_SHA", "DOCS_HUB_HEAD_SHA", "DOCS_HUB_BEFORE_SHA", "DOCS_HUB_EVENT_NAME")


def _run(repo: Path, **event: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items()
           if key not in _EVENT and not key.startswith("GIT_")}
    env.update({f"DOCS_HUB_{key.upper()}": value for key, value in event.items()})
    return subprocess.run([sys.executable, str(_SCRIPT)], cwd=repo, capture_output=True,
                          text=True, env=env, check=False)


def _history(tmp_path: Path) -> dict[str, str]:
    """fork <- master <- later; fork <- step <- head; merge = master + head, as
    GitHub builds a pull request's merge commit, then master moves on."""
    repo = f._repo(tmp_path)
    shas = {"fork": f._commit(repo, "fork", **{f.CODE: "print(0)\n"})}
    shas["master"] = f._commit(repo, "master moves", **{"src/other.py": "print(1)\n"})
    f._git(repo, "checkout", "-q", "-b", "side", shas["fork"])
    shas["step"] = f._commit(repo, "side step", **{f.CODE: "print(2)\n"})
    shas["head"] = f._commit(repo, "side head", **{f.CODE: "print(3)\n"})
    f._git(repo, "checkout", "-q", "-b", "merged", shas["master"])
    f._git(repo, "merge", "-q", "--no-ff", "-m", "Merge head into master", shas["head"])
    shas["merge"] = f._git(repo, "rev-parse", "HEAD")
    f._git(repo, "checkout", "-q", "main")
    shas["later"] = f._commit(repo, "master moves again", **{"src/other.py": "print(4)\n"})
    f._git(repo, "checkout", "-q", "--orphan", "orphan")
    shas["orphan"] = f._commit(repo, "unrelated root", **{f.CODE: "print(9)\n"})
    return shas


def _at(tmp_path: Path, sha: str, **event: str) -> subprocess.CompletedProcess:
    f._git(tmp_path / "repo", "checkout", "-q", "--detach", sha)
    return _run(tmp_path / "repo", **event)


def _base(tmp_path: Path, sha: str, **event: str) -> str:
    done = _at(tmp_path, sha, **event)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stderr == ""
    return done.stdout


def test_a_pull_request_measures_the_base_tip_its_merge_commit_was_built_on(
        tmp_path: Path) -> None:
    # merge-base(base, PR head) is fork; the checkout measures master + head.
    shas = _history(tmp_path)
    assert _base(tmp_path, shas["merge"], event_name="pull_request", base_sha=shas["master"],
                 head_sha=shas["head"], before_sha=shas["step"]) == shas["master"] + "\n"


def test_a_pull_request_measures_its_named_base_not_the_first_parent(
        tmp_path: Path) -> None:
    # fork is an ancestor of merge but not its first parent (master).
    shas = _history(tmp_path)
    assert _base(tmp_path, shas["merge"], event_name="pull_request",
                 base_sha=shas["fork"]) == shas["fork"] + "\n"


def test_a_push_measures_the_commit_it_came_from(tmp_path: Path) -> None:
    # head's first parent is step, so only the before route answers fork.
    shas = _history(tmp_path)
    assert _base(tmp_path, shas["head"], event_name="push", before_sha=shas["fork"],
                 head_sha=shas["merge"], base_sha=shas["master"]) == shas["fork"] + "\n"


@pytest.mark.parametrize("before", ["zero", "master", "unknown"])
def test_a_branch_creating_or_rewriting_push_measures_the_first_parent(
        tmp_path: Path, before: str) -> None:
    # all-zero: a new branch; master: a force push past a non-ancestor;
    # unknown: a force push whose old tip the clone never fetched.
    shas = _history(tmp_path)
    value = {"zero": f.ZERO, "master": shas["master"], "unknown": "f" * 40}[before]
    assert _base(tmp_path, shas["head"], event_name="push", before_sha=value,
                 head_sha=shas["merge"]) == shas["step"] + "\n"


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "merge_group"])
def test_any_other_event_measures_the_first_parent(tmp_path: Path, event: str) -> None:
    shas = _history(tmp_path)
    assert _base(tmp_path, shas["merge"], event_name=event, head_sha=shas["head"],
                 base_sha=shas["fork"], before_sha=shas["fork"]) == shas["master"] + "\n"


def _refusals(shas: dict[str, str]) -> dict[str, tuple[str, dict[str, str], str]]:
    merge, head, fork = shas["merge"], shas["head"], shas["fork"]
    return {
        "pr-no-base": (merge, {"event_name": "pull_request", "head_sha": head},
                       "DOCS_HUB_BASE_SHA"),
        "pr-unknown-base": (merge, {"event_name": "pull_request", "base_sha": "nosuchrev"},
                            "unknown revision"),
        "pr-base-moved-on": (merge, {"event_name": "pull_request", "base_sha": shas["later"],
                                     "head_sha": head}, "not an ancestor"),
        "pr-unrelated": (merge, {"event_name": "pull_request", "base_sha": shas["orphan"]},
                         "not an ancestor"),
        "push-no-before": (head, {"event_name": "push"}, "not a full SHA"),
        "push-abbreviated": (head, {"event_name": "push", "before_sha": fork[:12]},
                             "not a full SHA"),
        "push-uppercase": (head, {"event_name": "push", "before_sha": fork.upper()},
                           "not a full SHA"),
        "push-root": (shas["orphan"], {"event_name": "push", "before_sha": f.ZERO},
                      "no first parent"),
        "other-root": (shas["orphan"], {"event_name": "workflow_dispatch"}, "no first parent"),
        "no-event": (merge, {"head_sha": head, "base_sha": shas["master"]},
                     "DOCS_HUB_EVENT_NAME"),
    }


_REFUSALS = ["pr-no-base", "pr-unknown-base", "pr-base-moved-on", "pr-unrelated",
             "push-no-before", "push-abbreviated", "push-uppercase", "push-root", "other-root",
             "no-event"]


@pytest.mark.parametrize("label", _REFUSALS)
def test_an_unresolvable_base_exits_1_with_a_message(tmp_path: Path, label: str) -> None:
    refusals = _refusals(_history(tmp_path))
    assert sorted(refusals) == sorted(_REFUSALS)
    checkout, event, message = refusals[label]
    done = _at(tmp_path, checkout, **event)
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stdout == ""
    assert done.stderr.startswith("speed-base: ") and message in done.stderr, done.stderr


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_a_shallow_clone_refuses(tmp_path: Path, event: str) -> None:
    shas = _history(tmp_path)
    shallow = tmp_path / "shallow"
    f._git(tmp_path, "clone", "-q", "--depth", "3", "--no-single-branch",
           f"file://{tmp_path / 'repo'}", str(shallow))
    f._git(shallow, "checkout", "-q", "--detach", shas["merge"])
    # Every input resolves in the shallow clone, so only the shallow check refuses.
    for sha in (shas["master"], shas["head"], shas["step"], shas["merge"]):
        f._git(shallow, "cat-file", "-e", f"{sha}^{{commit}}")
    done = _run(shallow, event_name=event, base_sha=shas["master"], before_sha=shas["master"])
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stdout == "" and "shallow" in done.stderr
