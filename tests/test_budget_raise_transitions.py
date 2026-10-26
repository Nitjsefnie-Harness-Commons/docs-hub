"""Real-history regressions for workload and toolchain composition."""
from pathlib import Path
import json

import pytest

import test_budget_raise as fixtures


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("first, pure_first, last, pure_last, expected", [
    pytest.param({"w.pub": 90, "w.list": 200}, False, {"w.list": 200}, True, 0,
                 id="mixed-tighten-pure-remove"),
    pytest.param({"w.pub": 100, "w.list": 200, "new": 300}, True,
                 {"w.pub": 100, "w.list": 200, "new": 290}, False, 0,
                 id="pure-add-mixed-tighten"),
    pytest.param({"w.pub": 90, "w.list": 200}, False, {"w.list": 200}, False, 1,
                 id="mixed-remove"),
    pytest.param({"w.pub": 100, "w.list": 200, "new": 300}, False,
                 {"w.pub": 100, "w.list": 200, "new": 290}, False, 1,
                 id="mixed-add"),
    pytest.param({"w.pub": 110, "w.list": 200}, False, {"w.list": 200}, True, 1,
                 id="mixed-raise-before-pure-remove"),
    pytest.param({"w.list": 200}, False, {"w.pub": 100, "w.list": 200}, True, 1,
                 id="mixed-remove-before-pure-restore"),
])
def test_workload_steps_compose_by_their_own_purity(
        tmp_path: Path, event: str, first: dict, pure_first: bool,
        last: dict, pure_last: bool, expected: int) -> None:
    repo, base = fixtures._seeded(tmp_path)
    commits = [base]
    for index, (workloads, pure) in enumerate(((first, pure_first), (last, pure_last))):
        files = {fixtures.BUDGET: json.dumps(fixtures._budget(workloads))}
        if not pure:
            files[fixtures.CODE] = f"print({index + 2})\n"
        commits.append(fixtures._commit(repo, "workload step", **files))
    windows = [(base, commits[-1])]
    if expected == 0:
        windows = [*zip(commits, commits[1:]), *windows]
    for before, head in windows:
        result = fixtures._run(repo, "--event", event, "--head", head,
                               "--base" if event == "pull_request" else "--before", before)
        assert result.returncode == expected, result.stdout + result.stderr
