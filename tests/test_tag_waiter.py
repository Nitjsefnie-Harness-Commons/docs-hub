"""Execute the tag waiter's Bash and jq against isolated check-run responses."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_REQUIRED = (
    "suites (3.13, 16)", "suites (3.13, 17)", "coverage", "pylint",
    "pycodestyle", "pyright", "eslint", "pip-audit", "npm-audit",
    "analyze (python)", "analyze (javascript-typescript)",
)


def _check(name: str, conclusion: str | None = "success",
           status: str = "completed") -> dict[str, str | None]:
    return {"name": name, "status": status, "conclusion": conclusion}


def _run_waiter(tmp_path: Path, checks: list[dict[str, str | None]],
                deadline: int = 1,
                workflow_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
endpoint = 'repos/example/repository/commits/' + '0' * 40 + '/check-runs'
if endpoint not in args:
    raise SystemExit('unexpected check-run endpoint: ' + repr(args))
query = args[args.index('--jq') + 1]
result = subprocess.run(['jq', '-r', query], input=os.environ['CHECKS'], text=True)
raise SystemExit(result.returncode)
""", encoding="utf-8")
    gh.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key not in {"REPO", "SHA", "DOCS_HUB_REPO", "DOCS_HUB_SHA"}}
    if workflow_env is None:
        workflow_env = {"DOCS_HUB_REPO": "example/repository", "DOCS_HUB_SHA": "0" * 40}
    return subprocess.run(
        ["bash", "-e", str(script)], check=False, capture_output=True, text=True,
        timeout=5,
        env={**env, "PATH": f"{tmp_path}:{os.environ['PATH']}", **workflow_env,
             "CHECKS": json.dumps({"check_runs": checks}),
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
        _check(name, conclusion if name == "coverage" else "success") for name in _REQUIRED
    ])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refusing to tag" in result.stderr


def test_failed_tag_prefix_is_not_exempt(tmp_path: Path):
    result = _run_waiter(tmp_path, [_check("tag-security", "failure"), _check("eslint")])
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


def test_additional_success_check_is_allowed(tmp_path: Path):
    checks = [_check(name) for name in _REQUIRED] + [_check("security-review")]
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 0, result.stdout + result.stderr


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
    checks = [_check(name) for name in _REQUIRED if name != "coverage"]
    checks.append(_check("coverage", None, "in_progress"))
    result = _run_waiter(tmp_path, checks)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Timed out" in result.stderr
