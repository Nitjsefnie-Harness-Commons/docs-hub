"""Check the bounds on apt-get steps in the test workflow."""
from itertools import groupby
from pathlib import Path
import shlex
from typing import Any

import yaml

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/tests.yml"


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
            if "apt-get" not in line:
                continue
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            for separator, words in groupby(
                    lexer, lambda word: word in {";", "&&", "||", "|", "&"}):
                command = list(words)
                if separator or "apt-get" not in command:
                    continue
                args = command[command.index("apt-get") + 1:]
                if not {"update", "install"}.intersection(args):
                    continue
                options = dict(value.split("=", 1) for flag, value in zip(args, args[1:])
                               if flag == "-o" and "=" in value)
                for key in ("Acquire::Retries", "Acquire::http::Timeout",
                            "Acquire::https::Timeout"):
                    value = options.get(key, "")
                    assert value.isdecimal() and int(value) > 0, (name, line, key)
