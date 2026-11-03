"""speed.yml's paired base measurement, executed under bash with stubs.

Each branching step runs as GitHub runs it (`bash -e`), with a recording
stub for every interpreter it calls; base resolution runs the real
scripts/ci/speed_base.py against a real repository.
"""
# pylint: disable=protected-access  # Shares the ratchet-push suite's bash and git helpers.
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

import test_budget_raise as f
import test_ratchet_push as rp

_MEASURE = "Measure this commit's workloads"
_MISMATCH = "Ask whether the toolchain left the budget's"
_RESOLVE = "Resolve the base commit beside this one"
_BASE = "Measure the base commit beside this one"
_CHECK = "Check the counts against the committed budget"
_UPLOAD = "Upload the measurement"
_EVENT = ("DOCS_HUB_BASE_SHA", "DOCS_HUB_HEAD_SHA", "DOCS_HUB_BEFORE_SHA", "DOCS_HUB_EVENT_NAME")
# Records its argv, one NUL-joined line per call, then prints $STUB_PRINT.
_RECORDER = """#!/usr/bin/env bash
printf '%s\\0' "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
[ -z "${STUB_PRINT+x}" ] || printf '%s\\n' "$STUB_PRINT"
exit "${STUB_EXIT:-0}"
"""


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load((f._REPO / ".github/workflows/speed.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["speed"]["steps"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _steps() if step.get("name") == name)


def _stub(path: Path, log: Path, body: str = _RECORDER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.replace('"$STUB_LOG"', shlex.quote(str(log))), encoding="utf-8",
                    newline="\n")
    path.chmod(0o755)


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.split("\0")[:-1] for line in log.read_text(encoding="utf-8").splitlines()]


def _run_step(name: str, cwd: Path, runner: Path, env: dict[str, str],
              path: Path | None = None) -> subprocess.CompletedProcess[str]:
    script = runner / "step.sh"
    script.write_text(_step(name)["run"], encoding="utf-8", newline="\n")
    inherited = {key: value for key, value in os.environ.items()
                 if key not in _EVENT and not key.startswith(("GIT_", "STUB_"))}
    if path is not None:
        inherited["PATH"] = f"{path}{os.pathsep}{os.environ['PATH']}"
    return subprocess.run(
        [rp._bash(), "-e", str(script)], cwd=cwd, check=False, capture_output=True,
        text=True, timeout=60,
        env={**inherited, "RUNNER_TEMP": str(runner),
             "GITHUB_OUTPUT": str(runner / "output.txt"),
             "GITHUB_STEP_SUMMARY": str(runner / "summary.md"), **env})


def _outputs(runner: Path) -> dict[str, str]:
    output = runner / "output.txt"
    if not output.exists():
        return {}
    return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())


def _expression(value: str) -> str:
    body = re.sub(r"\s+", " ", value).strip()
    return body[3:-2].strip() if body.startswith("${{") and body.endswith("}}") else body


def test_the_paired_steps_sit_between_the_head_measure_and_the_upload() -> None:
    names = [step.get("name") for step in _steps()]
    paired = [_MEASURE, _MISMATCH, _RESOLVE, _BASE, _CHECK, _UPLOAD]
    assert [name for name in names if name in paired] == paired


@pytest.mark.parametrize("printed, code, expected", [
    ("true", 0, "true"), ("false", 0, "false"),
    ("maybe", 0, None), ("", 0, None), ("true", 1, None),
])
def test_the_mismatch_step_records_only_a_true_or_false_answer(
        tmp_path: Path, printed: str, code: int, expected: str | None) -> None:
    runner, log = tmp_path / "runner", tmp_path / "calls.log"
    _stub(runner / "venv/bin/python", log)
    done = _run_step(_MISMATCH, tmp_path, runner, {"STUB_PRINT": printed, "STUB_EXIT": str(code)})
    assert _calls(log) == [["scripts/ci/speed.py", "mismatch", "--measurements",
                            f"{runner}/measurement.json", "--budget", ".github/speed-budget.json"]]
    if expected is None:
        assert done.returncode != 0, done.stdout + done.stderr
        assert not _outputs(runner)
    else:
        assert done.returncode == 0, done.stdout + done.stderr
        assert _outputs(runner) == {"mismatch": expected}


def test_the_base_and_check_steps_read_the_mismatch_steps_own_answer(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    _stub(runner / "venv/bin/python", tmp_path / "calls.log")
    assert _run_step(_MISMATCH, tmp_path, runner, {"STUB_PRINT": "true"}).returncode == 0
    (key,) = _outputs(runner)
    reference = f"steps.{_step(_MISMATCH)['id']}.outputs.{key}"
    for name in (_RESOLVE, _BASE):
        assert _expression(_step(name)["if"]) == f"{reference} == 'true'", name
    assert _expression(_step(_CHECK)["env"]["DOCS_HUB_SPEED_MISMATCH"]) == reference


def test_the_resolve_step_reads_the_budget_raise_steps_event_variables() -> None:
    # The measured commit is the checkout's HEAD, never DOCS_HUB_HEAD_SHA.
    raise_env = _step("Refuse an undeclared budget raise")["env"]
    assert _step(_RESOLVE)["env"] == {name: raise_env[name] for name in _EVENT
                                      if name != "DOCS_HUB_HEAD_SHA"}


def test_both_measure_steps_count_under_the_jobs_env_alone() -> None:
    # speed.py hands os.environ to every counted child, so a step-level
    # variable on one side only would sit in that side's counts alone.
    assert "env" not in _step(_MEASURE) and "env" not in _step(_BASE)


def _checkout(tmp_path: Path) -> dict[str, str]:
    """A pull request's merge commit checked out: master + head, whose lock
    differs from master's, with master past the fork point."""
    repo = f._repo(tmp_path)
    for name in ("speed_base.py", "budget_raise.py", "budget_chain.py"):
        (repo / "scripts/ci").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f._REPO / "scripts/ci" / name, repo / "scripts/ci" / name)
    shas = {"fork": f._commit(repo, "fork", **{"requirements-speed.txt": "lock at fork\n"})}
    shas["master"] = f._commit(repo, "master moves", **{"src/other.py": "print(1)\n"})
    f._git(repo, "checkout", "-q", "-b", "side", shas["fork"])
    shas["head"] = f._commit(repo, "head", **{"requirements-speed.txt": "lock at head\n"})
    f._git(repo, "checkout", "-q", "--detach", shas["master"])
    f._git(repo, "merge", "-q", "--no-ff", "-m", "Merge head into master", shas["head"])
    return shas


def _interpreters(tmp_path: Path) -> Path:
    """`python -m venv DIR` plants a recorder at DIR/bin/python; python3 is real."""
    bin_dir = tmp_path / "bin"
    log = tmp_path / "python.log"
    _stub(bin_dir / "python", log, _RECORDER.replace(
        'exit "${STUB_EXIT:-0}"',
        f'[ "$1 $2" = "-m venv" ] && mkdir -p "$3/bin" && '
        f'cp {shlex.quote(str(bin_dir / "recorder"))} "$3/bin/python"\nexit 0\n'))
    _stub(bin_dir / "recorder", tmp_path / "base-venv.log")
    (bin_dir / "python3").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
                                     encoding="utf-8", newline="\n")
    (bin_dir / "python3").chmod(0o755)
    return bin_dir


def test_a_mismatch_measures_the_base_tip_with_its_own_lock(tmp_path: Path) -> None:
    shas = _checkout(tmp_path)
    runner = tmp_path / "runner"
    runner.mkdir()
    done = _run_step(_RESOLVE, tmp_path / "repo", runner, {
        "DOCS_HUB_EVENT_NAME": "pull_request", "DOCS_HUB_BASE_SHA": shas["master"],
        "DOCS_HUB_HEAD_SHA": shas["head"], "DOCS_HUB_BEFORE_SHA": ""}, _interpreters(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    # The measure step gets no event variables (_run_step drops inherited ones).
    done = _run_step(_BASE, tmp_path / "repo", runner, {}, _interpreters(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    tree = runner / "base-tree"
    assert f._git(tree, "rev-parse", "HEAD") == shas["master"]
    assert (tmp_path / "repo/requirements-speed.txt").read_text(encoding="utf-8") \
        == "lock at head\n"
    assert (tree / "requirements-speed.txt").read_text(encoding="utf-8") == "lock at fork\n"
    assert _calls(tmp_path / "python.log") == [["-m", "venv", f"{runner}/base-venv"]]
    assert _calls(tmp_path / "base-venv.log") == [
        ["-m", "pip", "install", "--require-hashes", "-r", f"{tree}/requirements-speed.txt"],
        ["scripts/ci/speed.py", "measure", "--tree", str(tree), "--database", "docs_speed",
         "--out", f"{runner}/base-measurement.json", "--base-sha", shas["master"]]]


@pytest.mark.parametrize("event", [
    {"DOCS_HUB_EVENT_NAME": "push", "DOCS_HUB_BEFORE_SHA": "abc1234"},
    {"DOCS_HUB_EVENT_NAME": "pull_request", "DOCS_HUB_BASE_SHA": "nosuchrev"},
])
def test_an_unresolved_base_fails_the_step_before_anything_is_built(
        tmp_path: Path, event: dict[str, str]) -> None:
    shas = _checkout(tmp_path)
    runner = tmp_path / "runner"
    runner.mkdir()
    done = _run_step(_RESOLVE, tmp_path / "repo", runner,
                     {"DOCS_HUB_HEAD_SHA": shas["head"], **event}, _interpreters(tmp_path))
    assert done.returncode != 0
    assert "speed-base: " in done.stderr
    assert not (runner / "base-tree").exists()
    assert _calls(tmp_path / "python.log") == [] and _calls(tmp_path / "base-venv.log") == []


@pytest.mark.parametrize("mismatch, event, expected", [
    ("true", "push", ["--base-measurements", "{runner}/base-measurement.json"]),
    ("false", "push", []),
    ("true", "pull_request", ["--base-measurements", "{runner}/base-measurement.json",
                              "--refuse-mismatch"]),
    ("false", "pull_request", ["--refuse-mismatch"]),
    ("true", "workflow_dispatch", ["--base-measurements", "{runner}/base-measurement.json"]),
    ("false", "pull_request_target", []),
    ("false", "", []),
    ("", "pull_request", None), ("maybe", "push", None),
])
def test_the_check_step_passes_its_flags_by_mismatch_and_event(
        tmp_path: Path, mismatch: str, event: str, expected: list[str] | None) -> None:
    runner, log = tmp_path / "runner", tmp_path / "calls.log"
    _stub(runner / "venv/bin/python", log)
    for name in ("speed_budget.py", "speed_paired.py"):
        (tmp_path / "scripts/ci").mkdir(parents=True, exist_ok=True)
        (tmp_path / "scripts/ci" / name).write_text("", encoding="utf-8")
    done = _run_step(_CHECK, tmp_path, runner,
                     {"DOCS_HUB_SPEED_MISMATCH": mismatch, "DOCS_HUB_EVENT_NAME": event})
    if expected is None:
        assert done.returncode != 0 and _calls(log) == [], done.stdout + done.stderr
        return
    assert done.returncode == 0, done.stdout + done.stderr
    assert _calls(log) == [["scripts/ci/speed.py", "check", "--measurements",
                            f"{runner}/measurement.json", "--budget", ".github/speed-budget.json",
                            "--summary", f"{runner}/summary.md",
                            *(arg.format(runner=runner) for arg in expected)]]


def test_the_check_step_reads_the_event_name_through_its_env_alone() -> None:
    step = _step(_CHECK)
    assert _expression(step["env"]["DOCS_HUB_EVENT_NAME"]) == "github.event_name"
    assert "${{" not in step["run"]
