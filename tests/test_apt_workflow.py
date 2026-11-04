"""Check the bounds on apt-get steps in every workflow."""
from pathlib import Path
import re
from typing import Any

import pytest
import yaml

_WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github/workflows").glob("*.y*ml"))

_BOUNDS = (r"Acquire::Retries=[1-9]", r"Acquire::http::Timeout=[1-9]",
           r"Acquire::https::Timeout=[1-9]")

_MIN_OUTAGE_WINDOW = 60  # seconds the step's longest download must keep retrying
_TRANSFER_MARGIN = 30  # seconds of package transfer and index work the backoff leaves room for
_APT_TRUE = {"1", "yes", "true", "with", "on", "enable"}


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
    """Read -o options case-insensitively; the last value wins."""
    return {key.lower(): value for key, value in re.findall(r"-o\s*([\w:]+)=(\S*)", command)}


def _apt_bool(text: str) -> bool:
    """Fail safe: only apt's explicit true spellings count as enabled."""
    return text.lower() in _APT_TRUE


def _backoff_windows(command: str) -> tuple[int, int]:
    """Shortest and jittered-longest backoff, per apt-pkg/acquire-worker.cc (HandleFailure).

    Retry i waits min(2**i, maximum), jitter up to min(2**(i+1) - 1, maximum).
    Future Retry-After offsets can stretch each wait up to maximum; not modelled, timeout cuts off.
    """
    options = _apt_options(command)
    delay = options.get("acquire::retries::delay")
    if delay is not None and not _apt_bool(delay):
        return 0, 0
    retries = options.get("acquire::retries", "3")
    maximum = int(options.get("acquire::retries::delay::maximum", "30"))
    count = int(retries) if retries.isdigit() else 0
    return (sum(min(2 ** i, maximum) for i in range(count)),
            sum(min(2 ** (i + 1) - 1, maximum) for i in range(count)))


@pytest.mark.parametrize("delay,enabled", [
    (None, True), ("1", True), ("YES", True), ("true", True), ("WiTh", True),
    ("ON", True), ("enable", True), ("0x0", False), ("0b0", False),
    ("4294967296", False), ('"0"', False), ('"1"', False), ("junk", False),
    ("0x1", False), ("", False),
])
def test_apt_delay_requires_an_explicit_true_value(delay: str | None, enabled: bool) -> None:
    option = "" if delay is None else f" -o Acquire::Retries::Delay={delay}"
    assert _backoff_windows("apt-get install -o Acquire::Retries=10" + option) == (
        (181, 206) if enabled else (0, 0))


@pytest.mark.parametrize("options,expected", [
    (" -o Acquire::Retries::Delay=0 -o acquire::retries::delay=TRUE", (181, 206)),
    (" -o Acquire::Retries::Delay=1 -o acquire::retries::delay=", (0, 0)),
])
def test_apt_delay_uses_the_last_option(options: str, expected: tuple[int, int]) -> None:
    assert _backoff_windows("apt-get install -o Acquire::Retries=10" + options) == expected


def test_apt_get_download_commands_set_retries_and_timeouts() -> None:
    for name, _, step in _apt_steps():
        for command in _apt_commands(step):
            for bound in _BOUNDS:
                assert re.search(bound, command), (name, command, bound)


def test_apt_get_retry_backoff_outlasts_a_short_mirror_outage_inside_the_step_timeout() -> None:
    for name, _, step in _apt_steps():
        windows = [_backoff_windows(command) for command in _apt_commands(step)]
        assert max(shortest for shortest, _ in windows) >= _MIN_OUTAGE_WINDOW, (name, windows)
        jittered_total = sum(jittered for _, jittered in windows)
        assert jittered_total + _TRANSFER_MARGIN <= step["timeout-minutes"] * 60, (name, windows)


def test_every_apt_get_line_in_the_workflows_is_in_a_step_that_was_checked() -> None:
    mentioned = sum("apt-get" in line for path in _WORKFLOWS
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#"))
    checked = sum("apt-get" in line for _, _, step in _apt_steps()
                  for line in step["run"].splitlines())
    assert mentioned == checked
