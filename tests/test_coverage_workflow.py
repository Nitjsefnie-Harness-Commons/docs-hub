"""Pin the coverage job's floor plumbing, changed-lines report and raise artifact."""
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_THRESHOLDS = _REPO / ".github/ci-thresholds.json"


def _coverage_job() -> dict[str, Any]:
    path = _REPO / ".github/workflows/tests.yml"
    assert path.is_file(), "The coverage job needs its workflow"
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)["jobs"]["coverage"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _coverage_job()["steps"] if step.get("name") == name)


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
    # Step-to-step consistency, executed: the step must gate on whatever
    # scripts/ci/thresholds.py prints, never on a literal in this file.
    recorder = '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$0.args"\n'
    result = _run_script(tmp_path, _step("Coverage gate")["run"], {}, stubs={
        "python3": recorder + 'echo "94.4"\n',
        "python": recorder + "exit 0\n",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "python3.args").read_text().split() == [
        "scripts/ci/thresholds.py", "--coverage-floor", "python"]
    assert (tmp_path / "python.args").read_text().split() == [
        "-m", "coverage", "report", "--fail-under=94.4", "--precision=1"]


@pytest.mark.parametrize("line_rate,expected", [("0.95735", "95.7"), ("0.96", "96.0")])
def test_the_staging_step_writes_exactly_the_artifact_files(
        tmp_path: Path, line_rate: str, expected: str) -> None:
    (tmp_path / "coverage.xml").write_text(
        f'<coverage line-rate="{line_rate}"></coverage>\n', encoding="utf-8")
    github = tmp_path / ".github"
    github.mkdir()
    (github / "ci-thresholds.json").write_bytes(_THRESHOLDS.read_bytes())
    sha = "a" * 40
    result = _run_script(tmp_path, _step("Stage the coverage raise artifact")["run"],
                         {"GITHUB_SHA": sha})
    assert result.returncode == 0, result.stdout + result.stderr
    staged = tmp_path / "coverage-raise"
    assert sorted(path.name for path in staged.iterdir()) == [
        "base.txt", "ci-thresholds.json", "measured.txt"]
    assert (staged / "measured.txt").read_text() == f"{expected}\n"
    assert (staged / "base.txt").read_text() == f"{sha}\n"
    assert (staged / "ci-thresholds.json").read_bytes() == _THRESHOLDS.read_bytes()


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
    assert (tmp_path / "git.args").read_text().split() == [
        "fetch", "--no-tags", "origin", "master"]
    assert (tmp_path / "pip.args").read_text().split() == [
        "install", "--require-hashes", "-r", "requirements-diff-cover.txt"]
    assert (tmp_path / "diff-cover.args").read_text().split() == [
        "coverage.xml", "--compare-branch=origin/master"]
    assert summary.read_text() == (
        "### Coverage of changed lines (informational)\n\nfixture report\n")


def test_the_report_step_is_information_only() -> None:
    step = _step("Coverage of changed lines (informational)")
    assert step["if"] == ("${{ !cancelled() && steps.measure.conclusion == 'success'"
                          " && github.event_name == 'pull_request' }}")
    assert "--fail-under" not in step["run"]


def test_the_staging_step_runs_only_on_green_master_pushes() -> None:
    condition = ("${{ !cancelled() && steps.measure.conclusion == 'success'"
                 " && github.event_name == 'push' && github.ref == 'refs/heads/master' }}")
    assert _step("Stage the coverage raise artifact")["if"] == condition


def test_no_run_step_carries_a_floor_literal() -> None:
    # The floor is only ever read from the recorded file; a literal here is
    # the old gate's interim value sneaking back in.
    text = (_REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert re.search(r"--fail-under=\d", text) is None


def test_the_artifact_name_matches_the_staged_directory() -> None:
    # Step-to-step consistency: the push workflow downloads by this name, so
    # the uploader and the staged directory must not drift apart.
    steps = _coverage_job()["steps"]
    stage = next(index for index, step in enumerate(steps)
                 if step.get("name") == "Stage the coverage raise artifact")
    upload = steps[stage + 1]
    assert upload["with"]["name"] == "coverage-raise"
    assert upload["if"] == steps[stage]["if"]
    assert "coverage-raise" in steps[stage]["run"]
    order = [step.get("name") for step in steps]
    assert order.index("Coverage of changed lines (informational)") > order.index(
        "Upload coverage.xml")
