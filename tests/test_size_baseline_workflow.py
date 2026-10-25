"""Pin the size-baseline callee's structure and execute its staging step."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent
_WORKFLOW = "size-baseline.yml"
_STAGED = "size-baseline-push"
_GREEN_MASTER_PUSH = {
    "!cancelled()",
    "steps.check.conclusion == 'success'",
    "github.event_name == 'push'",
    "github.ref == 'refs/heads/master'",
}


def _workflow() -> dict[str, Any]:
    path = _REPO / ".github/workflows" / _WORKFLOW
    assert path.is_file(), "The size-baseline callee needs its workflow file"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML's YAML 1.1 loader reads an unquoted `on` key as True.
    return workflow.get("on", workflow.get(True, {}))


def _job() -> dict[str, Any]:
    jobs = _workflow()["jobs"]
    assert set(jobs) == {"size-baseline"}
    return jobs["size-baseline"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _job()["steps"] if step.get("name") == name)


def _conjuncts(expression: str) -> set[str]:
    # The condition must be one conjunction: a disjunction would widen the
    # admission set equality past the designed one.
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()
    assert "||" not in body
    return {part.strip() for part in body.split("&&")}


def _run_script(tmp_path: Path, script: str, env: dict[str, str],
                git_rc: int | None = None) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "step.sh"
    # LF: bash rejects the CRLF write_text emits on Windows.
    path.write_text(script, encoding="utf-8", newline="\n")
    stubs: dict[str, str] = {}
    if git_rc is not None:
        # A git double that answers rev-parse with the real repository and
        # fails the records diff with a given exit status, driving the
        # rc>1 fatal branch without a fixture git can produce honestly.
        real_git = shutil.which("git")
        stubs["git"] = (
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$@" > "$0.args"\n'
            "if [ \"$1\" = rev-parse ]; then exec '" + real_git + "' \"$@\"; fi\n"
            "exit " + str(git_rc) + "\n"
        )
    for name, body in stubs.items():
        shim = tmp_path / name
        shim.write_text(body, encoding="utf-8")
        shim.chmod(0o755)
    inherited = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("DOCS_HUB_BASE_SHA", "RUNNER_TEMP"))
    }
    return subprocess.run(
        ["bash", "-e", str(path)], cwd=tmp_path, check=False,
        capture_output=True, text=True, timeout=10,
        env={**inherited, "PATH": f"{tmp_path}:{os.environ['PATH']}", **env},
    )


def _stage_script() -> str:
    return _step("Stage a records change for the push workflow")["run"]


def test_the_callee_leaves_push_and_pull_request_to_the_gate() -> None:
    workflow = _workflow()
    assert set(_triggers(workflow)) == {"workflow_call", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    concurrency = workflow["concurrency"]
    # A schedule/dispatch run shares no group with the gate's called legs.
    assert "github.event_name" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


def test_the_only_job_declares_no_permissions() -> None:
    assert "permissions" not in _job()


def test_the_conditions_read_a_check_step_that_exists() -> None:
    # The if-conjuncts name steps.check; the id sits on the second step, the
    # first one after the checkout that produces the tree the check judges.
    check = _job()["steps"][1]
    assert check.get("id") == "check"


def test_the_tighten_step_needs_a_green_master_push() -> None:
    tighten = _step("Tighten the records after a green check")
    assert _conjuncts(tighten["if"]) == _GREEN_MASTER_PUSH


def test_the_stage_step_needs_the_same_green_master_push() -> None:
    stage = _step("Stage a records change for the push workflow")
    assert _conjuncts(stage["if"]) == _GREEN_MASTER_PUSH
    # The staged base is this run's sha, through a prefixed variable only.
    assert stage["env"] == {"DOCS_HUB_BASE_SHA": "${{ github.sha }}"}


def test_the_upload_step_runs_when_the_stage_changed_something() -> None:
    upload = _step("Upload a staged records change")
    assert _conjuncts(upload["if"]) == (
        _GREEN_MASTER_PUSH | {"steps.stage.outputs.changed == 'true'"})


def _fixture_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True,
                   capture_output=True)
    for args in (["config", "user.name", "CI test"],
                 ["config", "user.email", "ci@example.invalid"],
                 ["config", "core.hooksPath", "/dev/null"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True)
    records = tmp_path / ".github/size-baseline.json"
    records.parent.mkdir()
    records.write_text('{"schema_version": 1, "baseline": {}}\n', encoding="utf-8")
    subprocess.run(["git", "add", ".github/size-baseline.json"], cwd=tmp_path,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True,
                   capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                          capture_output=True, text=True)
    return head.stdout.strip()


def _stage_env(repo: Path, base_sha: str) -> dict[str, str]:
    return {
        "DOCS_HUB_BASE_SHA": base_sha,
        "RUNNER_TEMP": str(repo / "runner-temp"),
        "GITHUB_OUTPUT": str(repo / "output.txt"),
    }


def _changed_output(repo: Path) -> str:
    # The refusal arms exit before writing anything, so an absent file is
    # the same no-verdict evidence as an empty one.
    output = repo / "output.txt"
    return output.read_text(encoding="utf-8").strip() if output.exists() else ""


def test_the_stage_step_ships_a_changed_records_file(tmp_path: Path) -> None:
    sha = _fixture_repo(tmp_path)
    grown = '{"schema_version": 1, "baseline": {"grown.py": 501}}\n'
    (tmp_path / ".github/size-baseline.json").write_text(grown, encoding="utf-8")
    result = _run_script(tmp_path, _stage_script(), _stage_env(tmp_path, sha))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _changed_output(tmp_path) == "changed=true"
    staged = tmp_path / "runner-temp" / _STAGED
    assert sorted(path.name for path in staged.iterdir()) == [
        "base.txt", "size-baseline.json"]
    assert (staged / "size-baseline.json").read_text(encoding="utf-8") == grown
    assert (staged / "base.txt").read_text(encoding="utf-8") == f"{sha}\n"


def test_the_stage_step_reports_no_change_without_one(tmp_path: Path) -> None:
    sha = _fixture_repo(tmp_path)
    result = _run_script(tmp_path, _stage_script(), _stage_env(tmp_path, sha))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _changed_output(tmp_path) == "changed=false"
    assert not (tmp_path / "runner-temp" / _STAGED).exists()


def test_the_stage_step_refuses_a_drifted_checkout(tmp_path: Path) -> None:
    _fixture_repo(tmp_path)
    (tmp_path / ".github/size-baseline.json").write_text(
        '{"schema_version": 1, "baseline": {"grown.py": 501}}\n', encoding="utf-8")
    result = _run_script(tmp_path, _stage_script(),
                         _stage_env(tmp_path, "f" * 40))
    assert result.returncode != 0
    assert "Checkout does not match" in result.stderr
    assert not (tmp_path / "runner-temp" / _STAGED).exists()
    assert _changed_output(tmp_path) == ""


def test_the_stage_step_treats_a_git_failure_as_fatal(tmp_path: Path) -> None:
    sha = _fixture_repo(tmp_path)
    (tmp_path / ".github/size-baseline.json").write_text(
        '{"schema_version": 1, "baseline": {"grown.py": 501}}\n', encoding="utf-8")
    result = _run_script(tmp_path, _stage_script(), _stage_env(tmp_path, sha),
                         git_rc=2)
    assert result.returncode == 2
    assert "git diff failed with 2" in result.stderr
    assert not (tmp_path / "runner-temp" / _STAGED).exists()
    assert _changed_output(tmp_path) == ""
