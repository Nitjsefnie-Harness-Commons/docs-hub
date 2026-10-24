"""Pin the coverage job's floor plumbing, changed-lines report and raise artifact."""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent


def _coverage_job() -> dict[str, Any]:
    path = _REPO / ".github/workflows/tests.yml"
    assert path.is_file(), "The coverage job needs its workflow"
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)["jobs"]["coverage"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _coverage_job()["steps"] if step.get("name") == name)


def _thresholds() -> ModuleType:
    """The store reader, loaded by path: scripts/ci has no package."""
    spec = importlib.util.spec_from_file_location(
        "ci_thresholds_store", _REPO / "scripts/ci/thresholds.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _push_download_name() -> str:
    """The raise artifact's consumer side: the push workflow's download step."""
    workflow = yaml.safe_load(
        (_REPO / ".github/workflows/ratchet-push.yml").read_text(encoding="utf-8"))
    return next(step["with"]["name"] for step in workflow["jobs"]["push"]["steps"]
                if "with" in step)


def _conjuncts(expression: str) -> set[str]:
    # The condition must be one conjunction: a disjunction would widen the
    # admission set past the designed one.
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()
    assert "||" not in body
    return {part.strip() for part in body.split("&&")}


def _run_script(tmp_path: Path, script: str, env: dict[str, str],
                stubs: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    for name, body in (stubs or {}).items():
        shim = tmp_path / name
        shim.write_text(body, encoding="utf-8")
        shim.chmod(0o755)
    return subprocess.run(
        ["bash", "-e", "-"], cwd=tmp_path, check=False, input=script,
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", **env},
    )


def test_the_gate_step_gates_on_the_floor_the_store_prints(tmp_path: Path) -> None:
    # Step-to-step consistency, executed: whatever argv the step reads the
    # floor with must print the recorded floor against the real script and
    # store, and the step must gate on exactly that printed value.
    recorder = '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$0.args"\n'
    first = _run_script(tmp_path, _step("Coverage gate")["run"], {}, stubs={
        "python3": recorder + "echo throwaway\n",
        "python": recorder + "exit 0\n",
    })
    assert first.returncode == 0, first.stdout + first.stderr
    argv = (tmp_path / "python3.args").read_text().split()
    language = argv[argv.index("--coverage-floor") + 1]
    thresholds = _thresholds()
    _measured, floor = thresholds.coverage(thresholds.load(), language)
    printed = subprocess.run([sys.executable, *argv], cwd=_REPO, check=False,
                             capture_output=True, text=True, timeout=20)
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout == f"{floor:.1f}\n"
    stub = recorder + f'echo "{floor:.1f}"\n'
    second = _run_script(tmp_path, _step("Coverage gate")["run"], {}, stubs={
        "python3": stub,
        "python": recorder + "exit 0\n",
    })
    assert second.returncode == 0, second.stdout + second.stderr
    assert f"--fail-under={floor:.1f}" in (tmp_path / "python.args").read_text()


def test_the_gate_step_fails_closed_when_the_store_read_fails(tmp_path: Path) -> None:
    # The floor assignment is the fail-closed control: a store reader that
    # exits nonzero must fail the step, never leave the gate to run on an
    # empty floor string.
    failing = '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$0.args"\nexit 1\n'
    result = _run_script(tmp_path, _step("Coverage gate")["run"], {}, stubs={
        "python3": failing,
        "python": '#!/bin/sh\nexit 0\n',
    })
    assert result.returncode != 0


@pytest.mark.parametrize("line_rate,expected", [("0.95735", "95.7"), ("0.96", "96.0")])
def test_the_staging_step_writes_exactly_the_artifact_files(
        tmp_path: Path, line_rate: str, expected: str) -> None:
    (tmp_path / "coverage.xml").write_text(
        f'<coverage line-rate="{line_rate}"></coverage>\n', encoding="utf-8")
    sha = "a" * 40
    result = _run_script(tmp_path, _step("Stage the coverage raise artifact")["run"],
                         {"GITHUB_SHA": sha})
    assert result.returncode == 0, result.stdout + result.stderr
    staged = tmp_path / _step("Upload the coverage raise artifact")["with"]["path"]
    assert sorted(path.name for path in staged.iterdir()) == [
        "base.txt", "measured.txt"]
    assert (staged / "measured.txt").read_text() == f"{expected}\n"
    assert (staged / "base.txt").read_text() == f"{sha}\n"


def test_the_report_step_appends_the_tool_output_to_the_summary(
        tmp_path: Path) -> None:
    summary = tmp_path / "summary"
    result = _run_script(
        tmp_path, _step("Coverage of changed lines (informational)")["run"],
        {"GITHUB_STEP_SUMMARY": str(summary)}, stubs={
            "git": '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$0.args"\n',
            "pip": '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$0.args"\n',
            "diff-cover": '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$0.args"\necho "fixture report"\n',
        })
    assert result.returncode == 0, result.stdout + result.stderr
    git_args = (tmp_path / "git.args").read_text().split()
    pip_args = (tmp_path / "pip.args").read_text().split()
    diff_args = (tmp_path / "diff-cover.args").read_text().split()
    # The comparison branch is the one the step fetched, not a name of its own.
    fetched = [token for token in git_args
               if not token.startswith("-") and token != "fetch"]
    compare = next(token for token in diff_args
                   if token.startswith("--compare-branch=")).split("=", 1)[1]
    assert compare == "/".join(fetched)
    # Security property only: hashes are verified, and the file named is real.
    assert "--require-hashes" in pip_args
    assert (_REPO / pip_args[pip_args.index("-r") + 1]).is_file()
    assert "fixture report" in summary.read_text()


def test_the_report_step_is_information_only() -> None:
    step = _step("Coverage of changed lines (informational)")
    assert _conjuncts(step["if"]) >= _conjuncts(_step("Coverage summary")["if"]) | {
        "github.event_name == 'pull_request'"}
    assert "--fail-under" not in step["run"]
    # A failing report shows at step level but cannot fail the job.
    assert step.get("continue-on-error") is True


def test_the_staging_step_runs_only_on_green_master_pushes() -> None:
    assert _conjuncts(_step("Stage the coverage raise artifact")["if"]) >= _conjuncts(
        _step("Coverage summary")["if"]) | {
        "github.event_name == 'push'", "github.ref == 'refs/heads/master'"}


def test_no_run_step_carries_a_floor_literal() -> None:
    # The floor is only ever read from the recorded file; a literal here is
    # the old gate's interim value sneaking back in.
    text = (_REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert re.search(r"--fail-under=\d", text) is None


def test_the_artifact_name_matches_the_staged_directory() -> None:
    # The push workflow downloads by the uploader's name, so the two
    # workflows cannot drift apart.
    steps = _coverage_job()["steps"]
    stage = next(index for index, step in enumerate(steps)
                 if step.get("name") == "Stage the coverage raise artifact")
    upload = steps[stage + 1]
    assert upload["with"]["name"] == _push_download_name()
    assert upload["if"] == steps[stage]["if"]
