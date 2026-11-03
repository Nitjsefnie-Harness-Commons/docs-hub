"""Tests for the paired run's base resolution (scripts/ci/speed_base.py).

Every case drives a real git repository and the real script.
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
    """fork <- master; fork <- step <- head, plus a merge whose parents differ."""
    repo = f._repo(tmp_path)
    shas = {"fork": f._commit(repo, "fork", **{f.CODE: "print(0)\n"})}
    shas["master"] = f._commit(repo, "master moves", **{"src/other.py": "print(1)\n"})
    f._git(repo, "checkout", "-q", "-b", "side", shas["fork"])
    shas["step"] = f._commit(repo, "side step", **{f.CODE: "print(2)\n"})
    shas["head"] = f._commit(repo, "side head", **{f.CODE: "print(3)\n"})
    f._git(repo, "merge", "-q", "--no-ff", "-m", "merge master", shas["master"])
    shas["merge"] = f._git(repo, "rev-parse", "HEAD")
    return shas


def _base(tmp_path: Path, **event: str) -> str:
    done = _run(tmp_path / "repo", **event)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stderr == ""
    return done.stdout


def test_a_pull_request_measures_the_merge_base(tmp_path: Path) -> None:
    shas = _history(tmp_path)
    assert _base(tmp_path, event_name="pull_request", base_sha=shas["master"],
                 head_sha=shas["head"], before_sha=shas["step"]) == shas["fork"] + "\n"


def test_a_push_measures_the_commit_it_came_from(tmp_path: Path) -> None:
    # head's first parent is step, so only the push route answers fork.
    shas = _history(tmp_path)
    assert _base(tmp_path, event_name="push", before_sha=shas["fork"],
                 head_sha=shas["head"], base_sha=shas["master"]) == shas["fork"] + "\n"


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "merge_group"])
def test_any_other_event_measures_the_heads_first_parent(tmp_path: Path, event: str) -> None:
    shas = _history(tmp_path)
    assert _base(tmp_path, event_name=event, head_sha=shas["merge"], base_sha=shas["master"],
                 before_sha=shas["fork"]) == shas["head"] + "\n"


def _refusals(shas: dict[str, str]) -> dict[str, tuple[dict[str, str], str]]:
    head, fork = shas["head"], shas["fork"]
    return {
        "pr-no-base": ({"event_name": "pull_request", "head_sha": head}, "DOCS_HUB_BASE_SHA"),
        "pr-unknown-base": ({"event_name": "pull_request", "base_sha": "nosuchrev",
                             "head_sha": head}, "unknown revision"),
        "pr-unrelated": ({"event_name": "pull_request", "base_sha": shas["orphan"],
                          "head_sha": head}, "no merge base"),
        "pr-no-head": ({"event_name": "pull_request", "base_sha": shas["master"]},
                       "DOCS_HUB_HEAD_SHA"),
        "push-no-before": ({"event_name": "push", "head_sha": head}, "full non-zero SHA"),
        "push-zero": ({"event_name": "push", "before_sha": f.ZERO, "head_sha": head},
                      "full non-zero SHA"),
        "push-abbreviated": ({"event_name": "push", "before_sha": fork[:12], "head_sha": head},
                             "full non-zero SHA"),
        "push-uppercase": ({"event_name": "push", "before_sha": fork.upper(),
                            "head_sha": head}, "full non-zero SHA"),
        "push-unknown": ({"event_name": "push", "before_sha": "f" * 40, "head_sha": head},
                         "unknown revision"),
        "push-not-ancestor": ({"event_name": "push", "before_sha": shas["master"],
                               "head_sha": head}, "not an ancestor"),
        "other-root": ({"event_name": "workflow_dispatch", "head_sha": shas["orphan"]},
                       "no first parent"),
        "other-no-head": ({"event_name": "workflow_dispatch"}, "DOCS_HUB_HEAD_SHA"),
        "no-event": ({"head_sha": head, "base_sha": shas["master"]}, "DOCS_HUB_EVENT_NAME"),
    }


_REFUSALS = ["pr-no-base", "pr-unknown-base", "pr-unrelated", "pr-no-head", "push-no-before",
             "push-zero", "push-abbreviated", "push-uppercase", "push-unknown",
             "push-not-ancestor", "other-root", "other-no-head", "no-event"]


@pytest.mark.parametrize("label", _REFUSALS)
def test_an_unresolvable_base_exits_1_with_a_message(tmp_path: Path, label: str) -> None:
    shas = _history(tmp_path)
    repo = tmp_path / "repo"
    f._git(repo, "checkout", "-q", "--orphan", "orphan")
    shas["orphan"] = f._commit(repo, "unrelated root", **{f.CODE: "print(9)\n"})
    refusals = _refusals(shas)
    assert sorted(refusals) == sorted(_REFUSALS)
    event, message = refusals[label]
    done = _run(repo, **event)
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stdout == ""
    assert done.stderr.startswith("speed-base: ") and message in done.stderr, done.stderr


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_a_shallow_clone_refuses(tmp_path: Path, event: str) -> None:
    shas = _history(tmp_path)
    shallow = tmp_path / "shallow"
    f._git(tmp_path, "clone", "-q", "--depth", "3", "--no-single-branch",
           f"file://{tmp_path / 'repo'}", str(shallow))
    # Every input resolves in the shallow clone, so only the shallow check refuses.
    for sha in (shas["master"], shas["head"], shas["step"], shas["merge"]):
        f._git(shallow, "cat-file", "-e", f"{sha}^{{commit}}")
    done = _run(shallow, event_name=event, base_sha=shas["merge"], head_sha=shas["merge"],
                before_sha=shas["head"])
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stdout == "" and "shallow" in done.stderr
