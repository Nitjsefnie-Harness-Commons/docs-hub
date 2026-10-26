"""The composition matrix, exercised through real Git histories."""
# pylint: disable=protected-access  # Reuse the existing real-Git fixture helpers.
import json
from pathlib import Path

import pytest

import test_budget_raise as f


def _event(repo: Path, base: str, event: str):
    return f._run(repo, "--event", event, "--head", "HEAD",
                  "--base" if event == "pull_request" else "--before", base)


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("empty_declaration", [False, True], ids=["pass-d", "fail-empty-line"])
def test_ordinary_merge_composes_parent_work(
        tmp_path: Path, event: str, empty_declaration: bool) -> None:
    repo, base = f._seeded(tmp_path)
    f._git(repo, "checkout", "-b", "feature")
    f._commit(repo, "separate code", **{f.CODE: "print(2)\n"})
    tip = f._commit(repo, f._declared_raise_message('workloads["w.pub"]', 100, 300),
                    **{f.BUDGET: json.dumps(f._budget({"w.pub": 300, "w.list": 200}))})
    f._git(repo, "checkout", "main")
    base = f._commit(repo, "advance main", **{"other.py": "pass\n"})
    message = (f._declared_raise_message('workloads["w.pub"]', 100, 300)
               if empty_declaration else "ordinary merge")
    f._git(repo, "merge", "--no-ff", tip, "-m", message)
    assert not f._git(repo, "diff-tree", "--cc", "--no-commit-id", "HEAD")
    result = _event(repo, base, event)
    assert result.returncode == int(empty_declaration), result.stdout + result.stderr
    if empty_declaration:
        assert "does not match a raise in this diff" in result.stdout


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("message", ["seed alongside code", "seed\n\nBudget-Raise: malformed"])
def test_seed_exempts_the_whole_window(tmp_path: Path, event: str, message: str) -> None:
    repo = f._repo(tmp_path)
    base = f._commit(repo, "no budget", **{f.CODE: "print(1)\n"})
    f._commit(repo, message, **{f.CODE: "print(2)\n",
                              f.BUDGET: json.dumps(f._budget({"w.pub": 300}))})
    f._commit(repo, "another mixed step", **{f.CODE: "print(3)\n",
                                            f.BUDGET: json.dumps(f._budget({"w.pub": 400}))})
    result = _event(repo, base, event)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("declaration", [False, True], ids=["fail-d-prime", "fail-d-line"])
def test_merge_contributed_code_and_budget_are_impure(
        tmp_path: Path, event: str, declaration: bool) -> None:
    repo, base = f._seeded(tmp_path)
    f._git(repo, "checkout", "-b", "feature")
    tip = f._commit(repo, "feature", **{"feature.py": "pass\n"})
    f._git(repo, "checkout", "main")
    f._commit(repo, "main", **{"main.py": "pass\n"})
    f._git(repo, "merge", "--no-ff", "--no-commit", tip)
    message = (f._declared_raise_message('workloads["w.pub"]', 100, 300)
               if declaration else "hand-authored merge changes")
    f._commit(repo, message, **{f.CODE: "print(99)\n",
                              f.BUDGET: json.dumps(f._budget({"w.pub": 300, "w.list": 200}))})
    combined = f._git(repo, "diff-tree", "--cc", "--raw", "-r", "HEAD")
    assert f.BUDGET in combined and f.CODE in combined
    result = _event(repo, base, event)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "raised; it may only fall" in result.stdout
    if declaration:
        assert "carries declaration lines but also touches src/app.py" in result.stdout


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["workload", "tolerance"])
@pytest.mark.parametrize("pure, raised, declaration, expected", [
    pytest.param(False, False, False, 0, id="pass-b-mixed-tighten"),
    pytest.param(True, True, True, 0, id="pass-c-pure-declared-raise"),
    pytest.param(True, True, False, 1, id="fail-a-plain-undeclared"),
    pytest.param(False, True, False, 1, id="fail-a-mixed-undeclared"),
    pytest.param(False, True, True, 1, id="fail-b-impure-declaration"),
])
def test_numeric_step_contract(tmp_path: Path, event: str, field: str,
                               pure: bool, raised: bool, declaration: bool,
                               expected: int) -> None:
    repo, base = f._seeded(tmp_path)
    workloads = {"w.pub": 100, "w.list": 200}
    if field == "workload":
        key, old, new = 'workloads["w.pub"]', 100, 300 if raised else 90
        workloads["w.pub"] = new
        document = f._budget(workloads)
    else:
        key, old, new = "tolerance_pct", 1, 5 if raised else 0.5
        document = f._budget(workloads, tolerance=new)
    message = f._declared_raise_message(key, old, new) if declaration else "numeric step"
    files = {f.BUDGET: json.dumps(document)}
    if not pure:
        files[f.CODE] = "print(2)\n"
    f._commit(repo, message, **files)
    result = _event(repo, base, event)
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_a_pure_line_cannot_hide_a_mixed_tolerance_raise(tmp_path: Path, event: str) -> None:
    repo, base = f._seeded(tmp_path)
    f._commit(repo, "mixed tolerance raise", **{f.CODE: "print(2)\n",
              f.BUDGET: json.dumps(f._budget({"w.pub": 100, "w.list": 200}, tolerance=5))})
    f._commit(repo, f._declared_raise_message("tolerance_pct", 1, 5), **{
        f.BUDGET: json.dumps(f._budget({"w.pub": 90, "w.list": 200}, tolerance=5))})
    result = _event(repo, base, event)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "without a budget-only commit" in result.stdout
