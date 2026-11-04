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
_APT_WORDS = {"no": False, "false": False, "without": False, "off": False, "disable": False,
              "yes": True, "true": True, "with": True, "on": True, "enable": True}


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


def _apt_bool(text: str, default: bool) -> bool:
    """Read a boolean option the way apt's StringToBool does: a fully consumed strtol (base 0)
    of 0 or 1 first, then the case-insensitive words, else the default."""
    number = re.fullmatch(r"\s*([+-]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)", text)
    if number:
        digits = number[2]
        value = int(digits, 16 if digits[:2].lower() == "0x" else 8 if digits[0] == "0" else 10)
        if (-value if number[1] == "-" else value) in (0, 1):
            return bool(value)
    return _APT_WORDS.get(text.lower(), default)


def _backoff_windows(command: str) -> tuple[int, int]:
    """Shortest and jittered-longest seconds apt sleeps across one download's retries.

    Per apt-pkg/acquire-worker.cc (HandleFailure), retry i waits min(2**i, maximum), and
    Retry-After jitter stretches it to at most min(2**(i+1) - 1, maximum). The longest figure
    assumes no future Retry-After offset (the schedule the failed run shows); an offset can
    stretch every wait to maximum, and the step timeout then cuts the retries off.
    """
    options = _apt_options(command)
    delay = options.get("acquire::retries::delay")
    if delay is not None and not _apt_bool(delay, True):
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
        jittered_total = sum(jittered for _, jittered in windows)
        assert jittered_total + _TRANSFER_MARGIN <= step["timeout-minutes"] * 60, (name, windows)


def test_every_apt_get_line_in_the_workflows_is_in_a_step_that_was_checked() -> None:
    mentioned = sum("apt-get" in line for path in _WORKFLOWS
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#"))
    checked = sum("apt-get" in line for _, _, step in _apt_steps()
                  for line in step["run"].splitlines())
    assert mentioned == checked
