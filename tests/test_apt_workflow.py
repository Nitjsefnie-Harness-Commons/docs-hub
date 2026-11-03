"""Check the bounds on apt-get steps in the test workflow."""
from pathlib import Path
import re
from typing import Any

import yaml

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/tests.yml"

_BOUNDS = (r"Acquire::Retries=[1-9]", r"Acquire::http::Timeout=[1-9]",
           r"Acquire::https::Timeout=[1-9]")


def _apt_steps() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = [(name, job, step)
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


def test_apt_get_download_commands_set_retries_and_timeouts() -> None:
    for name, _, step in _apt_steps():
        for line in step["run"].splitlines():
            for command in re.split(r"&&|\|\||;", line):
                if "apt-get" not in command:
                    continue
                assert re.search(r"\b(update|install)\b", command), (name, command)
                for bound in _BOUNDS:
                    assert re.search(bound, command), (name, command, bound)
