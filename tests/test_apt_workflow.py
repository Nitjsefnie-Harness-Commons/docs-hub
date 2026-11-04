"""Check the bounds on apt-get steps in every workflow."""
from pathlib import Path
import re
import shlex
from typing import Any

import yaml

_DIR = Path(__file__).resolve().parent.parent / ".github/workflows"
_WORKFLOWS = sorted(_DIR.glob("*.y*ml"))
_MIN_BUDGET = 60
_STEP_MARGIN = 30


def _workflow(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _apt_steps() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    steps = []
    for path in _WORKFLOWS:
        workflow = _workflow(path)
        steps += [(f"{path.name}:{name}", job, step)
                  for name, job in workflow["jobs"].items()
                  for step in job.get("steps", [])
                  if "apt-get" in step.get("run", "")]
    assert steps, "No apt-get steps found"
    return steps


def test_apt_get_steps_have_a_shorter_timeout_than_their_jobs() -> None:
    for name, job, step in _apt_steps():
        job_limit = job.get("timeout-minutes")
        step_limit = step.get("timeout-minutes")
        assert isinstance(job_limit, int) and not isinstance(job_limit, bool), name
        assert isinstance(step_limit, int) and not isinstance(step_limit, bool), (name, step_limit)
        assert 0 < step_limit < job_limit, (name, step_limit, job_limit)


def _apt_commands(step: dict[str, Any]) -> list[list[str]]:
    return [shlex.split(command) for line in step["run"].replace("\\\n", " ").splitlines()
            for command in re.split(r"&&|\|\|?|;", line) if "apt-get" in command]


def _runner(command: list[str]) -> tuple[int, str, list[str]]:
    if command[0] == "sudo":
        command = command[1:]
    assert command[:2] == ["python3", "scripts/ci/bounded_apt.py"], command
    assert command[2:3] == ["--budget"] and command[3].isdigit(), command
    assert command[4:5] == ["--evidence"] and command[6:8] == ["--", "apt-get"], command
    return int(command[3]), command[5], command[8:]


def _apt_options(args: list[str]) -> dict[str, str]:
    return {option.split("=", 1)[0].lower(): option.split("=", 1)[1]
            for flag, option in zip(args, args[1:]) if flag == "-o" and "=" in option}


def test_each_apt_command_is_bounded_and_has_its_own_evidence() -> None:
    for name, _, step in _apt_steps():
        commands = [_runner(command) for command in _apt_commands(step)]
        for _, evidence, args in commands:
            assert args and args[0] in {"update", "install"}, (name, args)
            assert evidence == f"$EVIDENCE/{args[0]}", (name, evidence)
        assert len({evidence for _, evidence, _ in commands}) == len(commands), name


def test_each_apt_command_disables_apt_retries_and_sets_both_timeouts() -> None:
    for name, _, step in _apt_steps():
        for command in _apt_commands(step):
            _, _, args = _runner(command)
            options = _apt_options(args)
            for key, value in (("acquire::retries", "0"), ("acquire::http::timeout", "30"),
                               ("acquire::https::timeout", "30")):
                assert options.get(key) == value, (name, command, key)
            assert "acquire::retries::handleretryafter" not in options, name


def test_each_apt_update_fails_on_any_fetch_error() -> None:
    updates = [(name, command) for name, _, step in _apt_steps()
               for command in _apt_commands(step) if _runner(command)[2][0] == "update"]
    assert updates, "No apt-get update commands found"
    for name, command in updates:
        assert _runner(command)[2].count("--error-on=any") == 1, (name, command)


def test_budgets_outlast_a_short_outage_and_fit_inside_the_step_timeout() -> None:
    for name, _, step in _apt_steps():
        budgets = [_runner(command)[0] for command in _apt_commands(step)]
        assert all(budget >= _MIN_BUDGET for budget in budgets), (name, budgets)
        assert sum(budgets) + _STEP_MARGIN <= step["timeout-minutes"] * 60, (name, budgets)


def test_failure_upload_reads_the_install_steps_evidence_output() -> None:
    for name, job, step in _apt_steps():
        assert step.get("id"), name
        following = job["steps"][job["steps"].index(step) + 1]
        assert following.get("if") == "failure()", name
        assert following.get("uses", "").partition("@")[0] == "actions/upload-artifact", name
        assert following["with"]["path"] == f"${{{{ steps.{step['id']}.outputs.evidence }}}}", name


def test_artifact_names_are_unique_across_the_ci_gate_run() -> None:
    gate = _workflow(_DIR / "ci-gate.yml")
    callees = [_DIR / Path(job["uses"]).name for job in gate["jobs"].values() if "uses" in job]
    names = []
    for workflow in [gate, *(_workflow(path) for path in callees)]:
        for job in workflow["jobs"].values():
            uploads = [step for step in job.get("steps", [])
                       if step.get("uses", "").partition("@")[0] == "actions/upload-artifact"]
            if not uploads:
                continue
            matrix = job.get("strategy", {}).get("matrix", {})
            assert not matrix or set(matrix) == {"include"}, matrix
            for row in matrix.get("include", [{}]):
                for step in uploads:
                    name = step["with"]["name"]
                    for key, value in row.items():
                        name = name.replace(f"${{{{ matrix.{key} }}}}", str(value))
                    assert "${{" not in name, name
                    names.append(name)
    assert names and len(names) == len(set(names)), names


def test_every_apt_get_line_in_the_workflows_is_in_a_step_that_was_checked() -> None:
    mentioned = sum("apt-get" in line for path in _WORKFLOWS
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#"))
    checked = sum("apt-get" in line for _, _, step in _apt_steps()
                  for line in step["run"].splitlines())
    assert mentioned == checked
