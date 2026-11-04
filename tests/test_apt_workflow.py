"""Check the bounds on apt-get steps in every workflow."""
from pathlib import Path
import re
from typing import Any

import yaml

_WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github/workflows").glob("*.y*ml"))

_BOUNDS = (r"Acquire::Retries=[1-9]", r"Acquire::http::Timeout=[1-9]",
           r"Acquire::https::Timeout=[1-9]")

_MIN_OUTAGE_WINDOW = 60  # seconds the step's longest download must keep retrying
_TRANSFER_MARGIN = 30  # seconds of package transfer and index work the backoff leaves room for
_APT_FALSE = ("0", "no", "false", "without", "off", "disable")  # apt's StringToBool spellings


def _apt_steps() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    steps = []
    for path in _WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
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


def _apt_commands(step: dict[str, Any]) -> list[str]:
    return [command for line in step["run"].splitlines()
            for command in re.split(r"&&|\|\|?|;", line) if "apt-get" in command]


def _apt_options(command: str) -> dict[str, str]:
    """The -o options as apt reads them: keys case-insensitive, the last one given wins."""
    return {key.lower(): value for key, value in re.findall(r"-o\s*([\w:]+)=(\S+)", command)}


def _backoff_windows(command: str) -> tuple[int, int]:
    """Shortest and worst-case seconds apt sleeps across one download's retries.

    Retry i waits min(2**i, maximum); Retry-After jitter lengthens it to at most
    min(2**(i+1) - 1, maximum).
    """
    options = _apt_options(command)
    if options.get("acquire::retries::delay", "").lower() in _APT_FALSE:
        return 0, 0
    retries = options.get("acquire::retries", "3")
    maximum = int(options.get("acquire::retries::delay::maximum", "30"))
    count = int(retries) if retries.isdigit() else 0
    return (sum(min(2 ** i, maximum) for i in range(count)),
            sum(min(2 ** (i + 1) - 1, maximum) for i in range(count)))


def test_apt_get_download_commands_set_retries_and_timeouts() -> None:
    for name, _, step in _apt_steps():
        for command in _apt_commands(step):
            for bound in _BOUNDS:
                assert re.search(bound, command), (name, command, bound)


def test_apt_get_retry_backoff_outlasts_a_short_mirror_outage_inside_the_step_timeout() -> None:
    for name, _, step in _apt_steps():
        windows = [_backoff_windows(command) for command in _apt_commands(step)]
        assert max(shortest for shortest, _ in windows) >= _MIN_OUTAGE_WINDOW, (name, windows)
        worst_total = sum(worst for _, worst in windows)
        assert worst_total + _TRANSFER_MARGIN <= step["timeout-minutes"] * 60, (name, windows)


def test_every_apt_get_line_in_the_workflows_is_in_a_step_that_was_checked() -> None:
    mentioned = sum("apt-get" in line for path in _WORKFLOWS
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#"))
    checked = sum("apt-get" in line for _, _, step in _apt_steps()
                  for line in step["run"].splitlines())
    assert mentioned == checked
