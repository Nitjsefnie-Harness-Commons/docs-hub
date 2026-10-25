"""Execute the tag waiter's Bash and jq against isolated check-run responses."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_REQUIRED = ("aggregate",)


def _check(name: str, conclusion: str | None = "success",
           status: str = "completed",
           html_url: str | None = None) -> dict[str, str | None]:
    return {"name": name, "status": status, "conclusion": conclusion,
            "html_url": html_url}


def _run(run_id: int, event: str) -> dict[str, str | int]:
    return {"id": run_id, "event": event}


def _run_waiter(tmp_path: Path, checks: list[dict[str, str | None]],
                deadline: int = 1,
                workflow_env: dict[str, str] | None = None,
                runs: list[dict[str, str | int]] | None = None
                ) -> subprocess.CompletedProcess[str]:
    with (_REPO / ".github/workflows/tag.yml").open(encoding="utf-8") as stream:
        workflow = yaml.safe_load(stream)
    step = next(step for step in workflow["jobs"]["release-readiness"]["steps"]
                if step.get("name") == "Wait for the other gates on this commit")
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import os
import subprocess
import sys
args = sys.argv[1:]
checks = 'repos/example/repository/commits/' + '0' * 40 + '/check-runs'
runs = 'repos/example/repository/actions/runs?head_sha=' + '0' * 40
if runs in args:
    payload = os.environ.get('RUNS', '{"workflow_runs": []}')
elif checks in args:
    payload = os.environ['CHECKS']
else:
    raise SystemExit('unexpected endpoint: ' + repr(args))
query = args[args.index('--jq') + 1]
result = subprocess.run(['jq', '-r', query], input=payload, text=True)
raise SystemExit(result.returncode)
""", encoding="utf-8")
    gh.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key not in {"REPO", "SHA", "DOCS_HUB_REPO", "DOCS_HUB_SHA"}}
    if workflow_env is None:
        workflow_env = {"DOCS_HUB_REPO": "example/repository", "DOCS_HUB_SHA": "0" * 40}
    env["PATH"] = f"{tmp_path}:{os.environ['PATH']}"
    extra = {**workflow_env, "CHECKS": json.dumps({"check_runs": checks})}
    if runs is not None:
        extra["RUNS"] = json.dumps({"workflow_runs": runs})
    return subprocess.run(
        ["bash", "-e", str(script)], check=False, capture_output=True, text=True,
        timeout=5,
        env={**env, **extra,
             "TAG_WAIT_DEADLINE_SECONDS": str(deadline),
             "TAG_WAIT_SLEEP_SECONDS": "0.05"},
    )


def test_all_required_checks_succeed(tmp_path: Path):
    result = _run_waiter(tmp_path, [_check(name) for name in _REQUIRED])
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("workflow_env", [
    {"DOCS_HUB_REPO": "example/repository", "DOCS_HUB_SHA": "0" * 40},
    {"REPO": "example/repository", "SHA": "0" * 40},
    {"DOCS_HUB_REPO": "example/repository", "DOCS_HUB_SHA": "0" * 40,
     "REPO": "example/legacy", "SHA": "1" * 40},
    {"DOCS_HUB_REPO": "", "DOCS_HUB_SHA": "",
     "REPO": "example/repository", "SHA": "0" * 40},
])
def test_workflow_env_prefers_prefix_and_accepts_legacy(tmp_path: Path,
                                                        workflow_env: dict[str, str]):
    result = _run_waiter(tmp_path, [_check(name) for name in _REQUIRED],
                         workflow_env=workflow_env)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("missing", _REQUIRED)
def test_missing_required_check_waits_until_deadline(tmp_path: Path, missing: str):
    result = _run_waiter(tmp_path, [_check(name) for name in _REQUIRED if name != missing])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Timed out" in result.stderr


@pytest.mark.parametrize("conclusion", ["skipped", "failure", "cancelled", "timed_out", "neutral"])
def test_required_check_must_succeed(tmp_path: Path, conclusion: str):
    result = _run_waiter(tmp_path, [
        _check(name, conclusion) for name in _REQUIRED
    ])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr


def test_failed_tag_prefix_is_not_exempt(tmp_path: Path):
    result = _run_waiter(tmp_path, [_check("tag-security", "failure"), _check("aggregate")])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr


def test_previous_failed_tag_is_exempt(tmp_path: Path):
    checks = [_check(name) for name in _REQUIRED]
    checks.extend([_check("tag", "failure"), _check("tag")])
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("include_tag", [False, True])
def test_previous_failed_readiness_is_exempt(tmp_path: Path, include_tag: bool):
    checks = [_check(name) for name in _REQUIRED]
    checks.append(_check("release-readiness", "failure"))
    if include_tag:
        checks.append(_check("tag", "failure"))
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", ["security-review", "tests / suites (3.15, 16)"])
def test_additional_success_check_is_allowed(tmp_path: Path, name: str):
    checks = [_check(required) for required in _REQUIRED] + [_check(name)]
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_success_cannot_replace_aggregate(tmp_path: Path):
    result = _run_waiter(tmp_path, [_check("tests / suites (3.15, 16)")])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Timed out" in result.stderr


@pytest.mark.parametrize("aggregate_status", [None, "in_progress"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_nested_failure_refuses_before_aggregate_completes(
        tmp_path: Path, aggregate_status: str | None, conclusion: str):
    checks = [_check("tests / suites (3.15, 16)", conclusion)]
    if aggregate_status is not None:
        checks.append(_check("aggregate", None, aggregate_status))
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_additional_failed_check_refuses(tmp_path: Path, conclusion: str):
    checks = [_check(name) for name in _REQUIRED] + [_check("security-review", conclusion)]
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr


def test_failed_readiness_prefix_is_not_exempt(tmp_path: Path):
    checks = [_check(name) for name in _REQUIRED]
    checks.append(_check("release-readiness-security", "failure"))
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr


def test_running_required_check_waits_until_deadline(tmp_path: Path):
    checks = [_check(name, None, "in_progress") for name in _REQUIRED]
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Timed out" in result.stderr


def _gate_row(name: str, run_id: int) -> dict[str, str | None]:
    return _check(name, "failure",
                  html_url=f"https://github.com/example/repository/actions/runs/{run_id}/job/1")


def test_push_produced_checks_are_judged(tmp_path: Path):
    result = _run_waiter(tmp_path, [_check(name) for name in _REQUIRED],
                         runs=[_run(1, "push")])
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event,name", [("push", "security-review"),
                                        ("pull_request", "security-review"),
                                        ("push", "aggregate")])
def test_failed_push_and_pull_request_checks_refuse(tmp_path: Path, event: str,
                                                    name: str):
    checks = [_check(required) for required in _REQUIRED if name != "aggregate"]
    checks.append(_gate_row(name, 2))
    result = _run_waiter(tmp_path, checks, runs=[_run(1, "push"), _run(2, event)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr


@pytest.mark.parametrize("conclusion,status", [("failure", "completed"),
                                               (None, "in_progress")])
def test_issue_comment_rows_are_skipped(tmp_path: Path, conclusion: str | None,
                                        status: str):
    checks = [_check(name) for name in _REQUIRED] + [
        _check("claim", conclusion, status,
               html_url="https://github.com/example/repository/actions/runs/11/job/1")]
    result = _run_waiter(tmp_path, checks, runs=[_run(11, "issue_comment")])
    assert result.returncode == 0, result.stdout + result.stderr


def test_schedule_rows_are_skipped(tmp_path: Path):
    checks = [_check(name) for name in _REQUIRED] + [_gate_row("security-review", 3)]
    result = _run_waiter(tmp_path, checks, runs=[_run(3, "schedule")])
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_aggregate_with_push_rows_times_out(tmp_path: Path):
    row = _check("security-review",
                 html_url="https://github.com/example/repository/actions/runs/1/job/1")
    result = _run_waiter(tmp_path, [row], runs=[_run(1, "push")])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Timed out" in result.stderr


@pytest.mark.parametrize("url,run_id", [
    ("https://github.com/example/repository/runs/11/job/1", 11),
    ("https://github.com/example/repository/actions/runs/99/job/1", 99),
])
def test_unidentified_rows_are_judged(tmp_path: Path, url: str, run_id: int):
    # Fail closed: a row whose producing run cannot be identified (no
    # /actions/runs/<id> in its html_url, or an id the runs listing never
    # returned) is judged even when an identified push run exists.
    checks = [_check(name) for name in _REQUIRED]
    checks.append(_check("security-review", "failure", html_url=url))
    result = _run_waiter(tmp_path, checks, runs=[_run(run_id, "push")])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr
