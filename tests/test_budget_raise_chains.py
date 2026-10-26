"""Window endpoints and every intermediate value are separate obligations."""
# pylint: disable=protected-access  # Share the real Git histories and CLI driver.
import json
from pathlib import Path

import pytest

import test_budget_raise as f
from test_budget_raise_matrix import _event

_MISSING = object()


def _document(field: str, value: object) -> str:
    document = f._budget({"w.pub": 100, "w.list": 200})
    target, key = (document["workloads"], "w.pub") if field == "workload" \
        else (document, "tolerance_pct")
    if value is _MISSING:
        del target[key]
    else:
        target[key] = value
    return json.dumps(document)


def _endpoints(field: str) -> tuple:
    return ('workloads["w.pub"]', 100, 300) if field == "workload" \
        else ("tolerance_pct", 1, 5)


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["workload", "tolerance"])
@pytest.mark.parametrize("intermediate", [None, _MISSING], ids=["null", "missing"])
@pytest.mark.parametrize("declaration", ["none", "exact", "duplicate", "mismatch"])
def test_removal_readdition_requires_one_exact_net_declaration(
        tmp_path: Path, event: str, field: str, intermediate: object,
        declaration: str) -> None:
    repo, base = f._seeded(tmp_path)
    key, old, new = _endpoints(field)
    f._commit(repo, "remove", **{f.BUDGET: _document(field, intermediate)})
    lines = [] if declaration == "none" else [f._decl(key, old, new)]
    if declaration == "duplicate":
        lines *= 2
    if declaration == "mismatch":
        lines = [f._decl(key, old + 1, new)]
    f._commit(repo, "re-add\n\n" + "\n".join(lines),
              **{f.BUDGET: _document(field, new)})
    result = _event(repo, base, event)
    assert result.returncode == (0 if declaration == "exact" else 1), result.stdout + result.stderr


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["workload", "tolerance"])
def test_net_declaration_may_be_on_the_pure_removal(
        tmp_path: Path, event: str, field: str) -> None:
    repo, base = f._seeded(tmp_path)
    key, old, new = _endpoints(field)
    f._commit(repo, f._declared_raise_message(key, old, new),
              **{f.BUDGET: _document(field, None)})
    f._commit(repo, "re-add", **{f.BUDGET: _document(field, new)})
    result = _event(repo, base, event)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["workload", "tolerance"])
@pytest.mark.parametrize("junk", [False, True, "100", float("inf"), float("-inf"),
                                  float("nan"), [], {}, {"nested": 100}],
                         ids=["false", "true", "string", "inf", "negative-inf", "nan",
                              "list", "empty-object", "nested-object"])
@pytest.mark.parametrize("raise_net", [False, True], ids=["net-tighten", "declared-net-raise"])
def test_junk_intermediates_cannot_launder_numeric_endpoints(
        tmp_path: Path, event: str, field: str, junk: object, raise_net: bool) -> None:
    repo, base = f._seeded(tmp_path)
    key, old, raised = _endpoints(field)
    f._commit(repo, "junk intermediate", **{f.BUDGET: _document(field, junk)})
    message = f._declared_raise_message(key, old, raised) if raise_net else "tighten"
    f._commit(repo, message, **{f.BUDGET: _document(field, raised if raise_net else old / 2)})
    result = _event(repo, base, event)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not a finite number in value chain" in result.stdout


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["workload", "tolerance"])
@pytest.mark.parametrize("mixed_step", ["remove", "re-add"])
def test_a_net_line_cannot_authorize_a_mixed_add_or_removal(
        tmp_path: Path, event: str, field: str, mixed_step: str) -> None:
    repo, base = f._seeded(tmp_path)
    key, old, new = _endpoints(field)
    for step, value in [("remove", None), ("re-add", new)]:
        files = {f.BUDGET: _document(field, value)}
        if step == mixed_step:
            files[f.CODE] = "print(2)\n"
        message = f._declared_raise_message(key, old, new) if step != mixed_step else step
        f._commit(repo, message, **files)
    result = _event(repo, base, event)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "without a budget-only commit" in result.stdout


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["workload", "tolerance"])
@pytest.mark.parametrize("pure", [False, True])
def test_null_removal_and_readdition_obey_step_purity(
        tmp_path: Path, event: str, field: str, pure: bool) -> None:
    repo, base = f._seeded(tmp_path)
    files = {f.BUDGET: _document(field, None)}
    if not pure:
        files[f.CODE] = "print(2)\n"
    removed = f._commit(repo, "remove", **files)
    result = _event(repo, base, event)
    assert result.returncode == int(not pure), result.stdout + result.stderr
    files = {f.BUDGET: _document(field, _endpoints(field)[1])}
    if not pure:
        files[f.CODE] = "print(3)\n"
    f._commit(repo, "re-add", **files)
    result = _event(repo, removed, event)
    assert result.returncode == int(not pure), result.stdout + result.stderr


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_empty_merge_declaration_cannot_authorize_a_net_raise(tmp_path: Path, event: str) -> None:
    repo, base = f._seeded(tmp_path)
    f._git(repo, "checkout", "-b", "feature")
    f._commit(repo, "remove", **{f.BUDGET: _document("workload", None)})
    tip = f._commit(repo, "re-add", **{f.BUDGET: _document("workload", 300)})
    f._git(repo, "checkout", "main")
    f._commit(repo, "code", **{f.CODE: "print(2)\n"})
    f._git(repo, "merge", "--no-ff", tip, "-m",
           f._declared_raise_message('workloads["w.pub"]', 100, 300))
    result = _event(repo, base, event)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "does not match a raise in this diff" in result.stdout
