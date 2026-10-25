"""Check CI ownership and execute the aggregate and conflict-marker guards."""
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_CALLEES = ("tests", "lint", "types", "eslint", "audit", "actionlint", "codeql", "secrets",
            "speed")
_LEGS = (*_CALLEES, "conflict-markers", "base-freshness")
_SCHEDULED = {"audit", "codeql", "secrets"}


def _workflow(filename: str = "ci-gate.yml") -> dict[str, Any]:
    path = _REPO / ".github/workflows" / filename
    assert path.is_file(), f"Missing workflow: {filename}"
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML's YAML 1.1 loader reads an unquoted `on` key as True.
    return workflow.get("on", workflow.get(True, {}))


def _step(job: str, name: str,
          filename: str = "ci-gate.yml") -> dict[str, Any]:
    return next(step for step in _workflow(filename)["jobs"][job]["steps"]
                if step.get("name") == name)


def _fold_step() -> dict[str, Any]:
    return _step("aggregate", "Fold the leg results into one verdict")


def _env_name(leg: str) -> str:
    return "DOCS_HUB_LEG_" + leg.upper().replace("-", "_")


def test_ci_gate_owns_every_push_and_pull_request() -> None:
    workflow = _workflow()
    assert workflow["name"] == "ci gate"
    triggers = _triggers(workflow)
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    filters = {"paths", "paths-ignore", "branches", "branches-ignore", "tags", "tags-ignore"}
    for event in ("push", "pull_request"):
        assert not filters.intersection(triggers[event] or {})
    assert workflow["permissions"] == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert "${{github.ref}}" in "".join(concurrency["group"].split())
    assert concurrency["cancel-in-progress"] is True


def test_ci_gate_calls_all_nine_gates_with_minimal_permissions() -> None:
    jobs = _workflow()["jobs"]
    assert set(jobs) == {*_LEGS, "aggregate"}
    for name in _CALLEES:
        job = jobs[name]
        # The wiring under test is that the callee resolves within this
        # repository; the local-reference prefix's spelling (./ or $/) is
        # not the pin, so a migration of it need not touch this test.
        prefix, slash, remainder = job["uses"].partition("/")
        assert slash and prefix in ("$", ".")
        assert remainder == f".github/workflows/{name}.yml"
        assert "with" not in job and "secrets" not in job
        if name == "codeql":
            assert job["permissions"] == {
                "security-events": "write", "actions": "read", "contents": "read",
            }
        else:
            assert "permissions" not in job


@pytest.mark.parametrize("name", _CALLEES)
def test_callees_leave_push_and_pull_request_to_the_gate(name: str) -> None:
    workflow = _workflow(f"{name}.yml")
    triggers = _triggers(workflow)
    expected = {"workflow_call", "workflow_dispatch"}
    if name in _SCHEDULED:
        expected.add("schedule")
        assert triggers["schedule"]
    assert set(triggers) == expected
    if name == "codeql":
        assert "permissions" not in workflow
    elif "permissions" in workflow:
        assert workflow["permissions"] == {"contents": "read"}
    # `github.ref` inside a called workflow is the caller's ref, so without
    # the event name a callee's own schedule/dispatch run shares the gate's
    # called-leg group on that ref and cancels an in-flight push leg.
    assert "github.event_name" in workflow["concurrency"]["group"]
    for job_name, job in workflow["jobs"].items():
        permissions = job.get("permissions", {})
        if name == "codeql" and job_name == "analyze":
            # Only analysis uploads security events; the caller grants this
            # write scope solely to CodeQL, keeping other callees read-only.
            assert permissions == {
                "security-events": "write", "actions": "read", "contents": "read",
            }
        elif isinstance(permissions, dict):
            assert "write" not in permissions.values()
        else:
            assert permissions in (None, "read-all")


def test_aggregate_always_folds_exactly_eleven_legs_without_checkout() -> None:
    job = _workflow()["jobs"]["aggregate"]
    assert set(job["needs"]) == set(_LEGS)
    assert len(job["needs"]) == len(_LEGS)
    assert job["if"] in ("${{ always() }}", "always()")
    assert "name" not in job
    assert job["permissions"] == {"contents": "read"}
    assert all(step.get("uses", "").partition("@")[0] != "actions/checkout"
               for step in job["steps"])
    assert _fold_step()["env"] == {
        **{_env_name(leg): "${{ needs." + leg + ".result }}" for leg in _LEGS},
        "DOCS_HUB_EVENT_NAME": "${{ github.event_name }}",
    }


def test_local_legs_have_checkout_and_read_only_permissions() -> None:
    for name in ("conflict-markers", "base-freshness"):
        job = _workflow()["jobs"][name]
        assert job["permissions"] == {"contents": "read"}
        assert job["steps"][0].get("uses", "").partition("@")[0] == "actions/checkout"
        assert len(job["steps"]) == 2
    freshness = _workflow()["jobs"]["base-freshness"]
    assert freshness["if"] == "github.event_name == 'pull_request'"
    assert "if" not in freshness["steps"][1]
    assert freshness["steps"][1]["run"] == (
        "python3 scripts/ci/gate_base_freshness.py"
        " --head ${{ github.event.pull_request.head.sha }}")


def _run_script(tmp_path: Path, script: str,
                env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "step.sh"
    path.write_text(script, encoding="utf-8")
    # The prefixed verdict variables are set only through `env`; a stray one
    # in the session environment must not fake a leg result. DOCS_HUB_API_KEY
    # and every other real DOCS_HUB_* variable pass through untouched.
    inherited = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("LEG_", "DOCS_HUB_LEG_"))
        and key != "DOCS_HUB_EVENT_NAME"
    }
    return subprocess.run(
        ["bash", "-e", str(path)], cwd=tmp_path, check=False,
        capture_output=True, text=True, timeout=5,
        env={**inherited, "PATH": f"{tmp_path}:{os.environ['PATH']}", **env},
    )


def _run_fold(tmp_path: Path, results: dict[str, str],
              event: str | None = "push") -> subprocess.CompletedProcess[str]:
    step = _fold_step()
    assert set(results) <= set(step["env"])
    env = dict(results)
    if event is None:
        env.pop("DOCS_HUB_EVENT_NAME", None)
    else:
        env["DOCS_HUB_EVENT_NAME"] = event
    return _run_script(tmp_path, step["run"], env)


def _success_env() -> dict[str, str]:
    return dict.fromkeys(_fold_step()["env"], "success")


def test_fold_accepts_all_eleven_successes(tmp_path: Path) -> None:
    result = _run_fold(tmp_path, _success_env())
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("leg", _LEGS[:-1])
@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_fold_refuses_every_unsuccessful_required_leg(tmp_path: Path, leg: str,
                                                      outcome: str) -> None:
    results = _success_env()
    results[_env_name(leg)] = outcome
    result = _run_fold(tmp_path, results)
    assert result.returncode == 1, result.stdout + result.stderr
    assert leg in result.stdout + result.stderr


@pytest.mark.parametrize("outcome,exit_code", [("failure", 1), ("cancelled", 1), ("skipped", 0)])
def test_fold_accepts_only_the_designed_freshness_skip(tmp_path: Path, outcome: str,
                                                       exit_code: int) -> None:
    results = _success_env()
    results[_env_name("base-freshness")] = outcome
    result = _run_fold(tmp_path, results)
    assert result.returncode == exit_code, result.stdout + result.stderr
    if exit_code:
        assert "base-freshness" in result.stdout + result.stderr


@pytest.mark.parametrize("event,exit_code", [
    ("push", 0), ("workflow_dispatch", 0), ("pull_request", 1), ("", 1), (None, 1),
])
def test_freshness_skip_exemption_needs_a_non_pull_request_event(
        tmp_path: Path, event: str | None, exit_code: int) -> None:
    results = _success_env()
    results[_env_name("base-freshness")] = "skipped"
    result = _run_fold(tmp_path, results, event)
    assert result.returncode == exit_code, result.stdout + result.stderr
    if exit_code:
        assert "base-freshness" in result.stdout + result.stderr


@pytest.mark.parametrize("leg", _LEGS)
@pytest.mark.parametrize("empty", [False, True])
def test_fold_refuses_unset_and_empty_results(tmp_path: Path, leg: str, empty: bool) -> None:
    results = _success_env()
    if empty:
        results[_env_name(leg)] = ""
    else:
        del results[_env_name(leg)]
    result = _run_fold(tmp_path, results)
    assert result.returncode == 1, result.stdout + result.stderr
    assert leg in result.stdout + result.stderr


def test_fold_honours_the_legacy_name_when_the_prefixed_one_is_unset(
        tmp_path: Path) -> None:
    env = _success_env()
    del env[_env_name("tests")]
    env["LEG_TESTS"] = "failure"
    result = _run_script(tmp_path, _fold_step()["run"], env)
    assert result.returncode == 1, result.stdout + result.stderr
    # Naming the leg with its legacy-carried result is the discriminator:
    # with the fallback dropped the same leg is reported as missing instead.
    assert "tests: failure" in result.stdout + result.stderr


def test_fold_prefers_the_prefixed_name_when_both_names_are_set(
        tmp_path: Path) -> None:
    env = _success_env()
    env["LEG_TESTS"] = "failure"
    result = _run_script(tmp_path, _fold_step()["run"], env)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fold_reports_every_offending_leg(tmp_path: Path) -> None:
    result = _run_fold(tmp_path, dict.fromkeys(_fold_step()["env"], "failure"))
    assert result.returncode == 1, result.stdout + result.stderr
    for leg in _LEGS:
        assert leg in result.stdout + result.stderr


def _commit_file(tmp_path: Path, text: str) -> None:
    (tmp_path / "example.md").write_text(text, encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.name", "CI test"],
                 ["config", "user.email", "ci@example.invalid"], ["add", "example.md"],
                 ["-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


@pytest.mark.parametrize("text,exit_code", [
    ("# Clean document\n", 0),
    ("<<<<<<< HEAD\n", 1),
    (">>>>>>> branch\n", 1),
    ("=======\n", 1),
    ("<<<<<<<\n", 1),
    (">>>>>>>\n", 1),
    ("========\n<<<<<<<prose\n>>>>>>>prose\n", 0),
])
def test_conflict_marker_scan_checks_tracked_markdown(
        tmp_path: Path, text: str, exit_code: int) -> None:
    _commit_file(tmp_path, text)
    step = _step("conflict-markers", "Check no tracked file carries a merge-conflict marker")
    result = _run_script(tmp_path, step["run"], {})
    assert result.returncode == exit_code, result.stdout + result.stderr
    if exit_code:
        assert f"example.md:1:{text.strip()}" in result.stdout
        assert "rebase" in result.stdout.lower()


def test_conflict_marker_scan_propagates_grep_error(tmp_path: Path) -> None:
    _commit_file(tmp_path, "# Clean document\n")
    shim = tmp_path / "git"
    shim.write_text('#!/bin/sh\n[ "$1" = grep ] || exit 99\nexit 2\n', encoding="utf-8")
    shim.chmod(0o755)
    step = _step("conflict-markers", "Check no tracked file carries a merge-conflict marker")
    result = _run_script(tmp_path, step["run"], {})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no marker was looked for" in result.stdout


# One line of real pip-audit output per failure class the audit step's retry
# classifier names. Every retryable fixture matches exactly one retry branch
# and none of the others, so removing that branch is what fails the case.
_AUDIT_RETRYABLE = {
    "connection-reset": "Connection reset by peer",
    "http-429": ("requests.exceptions.HTTPError: 429 Client Error: Too Many"
                 " Requests for url: https://pypi.org/simple/urllib3/"),
    "http-5xx": ("requests.exceptions.HTTPError: 500 Server Error: Internal"
                 " Server Error for url: https://pypi.org/simple/urllib3/"),
    "ssl-error": ("pip._vendor.urllib3.exceptions.SSLError:"
                  " certificate verify failed"),
    "service-error": "pip_audit._service.interface.ServiceError: boom",
}

# The immediate fixtures. The 403 arrives wrapped in a ServiceError — the
# compound shape the classifier's ordering exists for: the non-429 client-error
# check runs before the retry set, so a client error through the service layer
# exits immediately instead of retrying. The finding table matches nothing at
# all and leaves through the findings report.
_AUDIT_IMMEDIATE_FIXTURE_403 = (
    "pip_audit._service.interface.ServiceError: 403 Client Error: Forbidden"
    " for url: https://pypi.org/simple/private-package/")
_AUDIT_IMMEDIATE_FIXTURE_FINDING = (
    "Package Version ID Fix Versions\n"
    "------- ------- -------- ------------\n"
    "pyyaml 5.4 GHSA-8q59-q68h-6hv4 >=6.0.1\n"
    "Found 1 known vulnerability in 1 package")


def _run_audit_step(tmp_path: Path, first_failure_output: str
                    ) -> tuple[subprocess.CompletedProcess[str], int]:
    step = _step("pip-audit", "Audit the Python dependencies", "audit.yml")
    # The pip-audit double writes the fixture and fails on its first
    # invocation and exits 0 on every later one, counting its calls beside
    # itself; the sleep double makes the attempt-scaled backoff a no-op.
    shim = tmp_path / "pip-audit"
    shim.write_text(
        "#!/bin/sh\n"
        'calls_file="$0.calls"\n'
        'calls=$(cat "$calls_file" 2>/dev/null || echo 0)\n'
        'calls=$((calls + 1))\n'
        'echo "$calls" > "$calls_file"\n'
        'if [ "$calls" -gt 1 ]; then exit 0; fi\n'
        "cat <<'FIXTURE'\n"
        f"{first_failure_output}\n"
        "FIXTURE\n"
        "exit 1\n",
        encoding="utf-8")
    shim.chmod(0o755)
    sleeper = tmp_path / "sleep"
    sleeper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleeper.chmod(0o755)
    result = _run_script(tmp_path, step["run"], {})
    calls = int((tmp_path / "pip-audit.calls").read_text(encoding="utf-8"))
    return result, calls


@pytest.mark.parametrize("failure_output", _AUDIT_RETRYABLE.values(),
                         ids=_AUDIT_RETRYABLE)
def test_audit_step_retries_each_retryable_failure_class(
        tmp_path: Path, failure_output: str) -> None:
    result, calls = _run_audit_step(tmp_path, failure_output)
    # Attempt 2 ran (and succeeded), so the manifest loop completes green.
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls >= 2


@pytest.mark.parametrize("failure_output,refusal", [
    pytest.param(_AUDIT_IMMEDIATE_FIXTURE_403, "non-retryable 4xx",
                 id="http-403"),
    pytest.param(_AUDIT_IMMEDIATE_FIXTURE_FINDING, "reported findings",
                 id="finding"),
])
def test_audit_step_reports_each_immediate_failure_once(
        tmp_path: Path, failure_output: str, refusal: str) -> None:
    result, calls = _run_audit_step(tmp_path, failure_output)
    assert result.returncode == 1, result.stdout + result.stderr
    assert calls == 1
    # The refusal names its branch: the 4xx case must leave through the
    # non-retryable-4xx check — which runs before the retry checks — not
    # through the findings report a fall-through into them would produce.
    assert refusal in result.stderr
