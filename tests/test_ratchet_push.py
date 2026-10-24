"""Structure pins over ratchet-push.yml, related across the two workflows."""
import re
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent


def _workflow() -> dict[str, Any]:
    path = _REPO / ".github/workflows/ratchet-push.yml"
    assert path.is_file(), "The push workflow needs its workflow file"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML's YAML 1.1 loader reads an unquoted `on` key as True.
    return workflow.get("on", workflow.get(True, {}))


def _push_job() -> dict[str, Any]:
    jobs = _workflow()["jobs"]
    assert set(jobs) == {"push"}
    return jobs["push"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _push_job()["steps"] if step.get("name") == name)


def _conjuncts(expression: str) -> set[str]:
    # One conjunction: a disjunction would widen the admission set.
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()
    assert "||" not in body
    return {part.strip() for part in body.split("&&")}


def test_the_trigger_keys_on_the_gate_workflow_itself() -> None:
    gate = yaml.safe_load(
        (_REPO / ".github/workflows/ci-gate.yml").read_text(encoding="utf-8"))
    run = _triggers(_workflow())["workflow_run"]
    assert run["workflows"] == [gate["name"]]
    assert run["types"] == ["completed"]


def test_the_push_job_admits_only_green_master_pushes() -> None:
    # The measure step exits 0 even when tests fail, so the green-only
    # guarantee lives in this admission set, exactly.
    assert _conjuncts(_push_job()["if"]) == {
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'master'",
        "github.event.workflow_run.conclusion == 'success'",
    }


def test_the_push_job_runs_in_the_master_push_environment() -> None:
    job = _push_job()
    assert job["environment"] == "master-push"
    assert _workflow()["permissions"] == {}
    assert job["permissions"] == {"actions": "read"}


def test_the_push_never_cancels_a_run_mid_push() -> None:
    concurrency = _workflow()["concurrency"]
    # Keyed on the triggering run, so a pull request's completion can never
    # supersede a pending master raise.
    for field in ("event", "head_branch"):
        assert f"github.event.workflow_run.{field}" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_the_deploy_key_leaves_the_environment_before_fetched_code_runs() -> None:
    lines = [line.strip() for line in
             _step("Push the raise commit to master")["run"].splitlines()]
    loaded = next(i for i, line in enumerate(lines) if line.startswith("ssh-add"))
    unset = lines.index("unset MASTER_PUSH_DEPLOY_KEY")
    python = next(i for i, line in enumerate(lines) if "python3" in line)
    assert loaded < unset < python
    assert not any("MASTER_PUSH_DEPLOY_KEY" in line for line in lines[unset + 1:])


def test_the_download_names_the_uploader_artifact() -> None:
    tests = yaml.safe_load(
        (_REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    upload = next(step for step in tests["jobs"]["coverage"]["steps"]
                  if step.get("name") == "Upload the coverage raise artifact")
    download = _step("Download the coverage raise artifact")
    assert download["with"]["name"] == upload["with"]["name"]
    assert f"grep -qx '{upload['with']['name']}'" in _step(
        "List the triggering run's artifacts")["run"]
    assert download["if"] == _step("Push the raise commit to master")["if"]


def test_the_push_step_consumes_exactly_the_staged_files() -> None:
    tests = yaml.safe_load(
        (_REPO / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    stager = next(step["run"] for step in tests["jobs"]["coverage"]["steps"]
                  if step.get("name") == "Stage the coverage raise artifact")
    push = _step("Push the raise commit to master")["run"]
    staged = set(re.findall(r'out / "([^"]+)"', stager))
    consumed = set(re.findall(r"raise-data/([A-Za-z0-9._-]+)", push))
    # Both directions: every staged file is read, and nothing else is.
    assert staged and staged == consumed


def test_the_push_step_runs_the_fetched_trees_own_ratchet() -> None:
    push = _step("Push the raise commit to master")["run"]
    joined = push.replace("\\\n", " ").replace('"', " ")
    init = next(line for line in joined.splitlines() if line.lstrip().startswith("git init"))
    clone = init.split()[-1]
    command = next(line for line in joined.splitlines() if "ratchet.py" in line)
    tokens = command.split()
    script = next(token for token in tokens if token.endswith("scripts/ci/ratchet.py"))
    # The script and the document it rewrites both live in the fetched tree,
    # executed isolated, restating the staleness inputs.
    assert clone in script
    assert clone in tokens[tokens.index("--thresholds") + 1]
    assert tokens[tokens.index("python3") + 1] == "-I"
    assert {"--base", "--tip", "--measured", "--thresholds"} <= set(tokens)
