"""Tests for the instruction-count speed gate (scripts/ci/speed.py)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts/ci/speed.py"
_POLICY = _REPO / "scripts/ci/speed_budget.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="speed")
def _speed() -> ModuleType:
    return _load("ci_speed", _SCRIPT)


@pytest.fixture(name="policy")
def _policy() -> ModuleType:
    return _load("ci_speed_budget", _POLICY)


TOOLCHAIN = {"python": "3.15.1 (main, Oct 21 2026, 14:53:25) [GCC 13.3.0]",
             "valgrind": "valgrind-3.24.0",
             "runner_image": "ubuntu24 20261023.334.3",
             "lock_sha256": "ab" * 32}


def _measurement(counts: dict[str, int], chain: dict | None = None) -> dict:
    return {"toolchain": dict(chain if chain is not None else TOOLCHAIN),
            "counts": counts}


def _budget(counts: dict[str, int] | None = None, **fields: object) -> dict:
    document = {"schema_version": 1, "counter": "cachegrind-ir",
                "tolerance_pct": 30, "toolchain": dict(TOOLCHAIN),
                "workloads": counts or {"service.publish": 1000,
                                        "service.list": 2000}}
    return {**document, **fields}


def _rows(result: dict) -> dict[str, str]:
    return {row["name"]: row["verdict"] for row in result["rows"]}


def test_budget_refuses_malformed_json(policy: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(policy.BudgetError, match="budget.json"):
        policy.load(path)


def test_budget_refuses_an_unknown_field(policy: ModuleType) -> None:
    with pytest.raises(policy.BudgetError, match="unknown budget field"):
        policy.validate(_budget(extra=1))


@pytest.mark.parametrize("field", ["schema_version", "counter", "tolerance_pct",
                                   "toolchain", "workloads"])
def test_budget_refuses_a_missing_field(policy: ModuleType, field: str) -> None:
    document = _budget()
    del document[field]
    with pytest.raises(policy.BudgetError, match="missing budget field"):
        policy.validate(document)


@pytest.mark.parametrize("count", ["900", 900.0, True])
def test_budget_refuses_a_non_integer_count(policy: ModuleType,
                                            count: object) -> None:
    with pytest.raises(policy.BudgetError, match="service.publish"):
        policy.validate(_budget({"service.publish": count}))


@pytest.mark.parametrize("count", [0, -1])
def test_budget_refuses_a_non_positive_count(policy: ModuleType,
                                             count: int) -> None:
    with pytest.raises(policy.BudgetError, match="service.publish"):
        policy.validate(_budget({"service.publish": count}))


def test_budget_refuses_a_tolerance_that_is_not_a_finite_number(
        policy: ModuleType) -> None:
    for literal in ("Infinity", "NaN", "-Infinity"):
        with pytest.raises(policy.BudgetError, match="tolerance_pct"):
            policy.validate(json.loads(
                json.dumps(_budget()).replace('"tolerance_pct": 30',
                                              f'"tolerance_pct": {literal}')))


def test_budget_refuses_an_unknown_counter(policy: ModuleType) -> None:
    with pytest.raises(policy.BudgetError, match="counter"):
        policy.validate(_budget(counter="wall-clock"))


def test_budget_refuses_an_unknown_toolchain_field(policy: ModuleType) -> None:
    with pytest.raises(policy.BudgetError, match="compiler"):
        policy.validate(_budget(toolchain={**TOOLCHAIN, "compiler": "gcc 13"}))


def test_check_passes_a_count_within_tolerance(policy: ModuleType) -> None:
    result = policy.check(_budget(),
                          _measurement({"service.publish": 1299,
                                        "service.list": 2000}))
    assert result["failed"] is False
    rows = {row["name"]: row for row in result["rows"]}
    assert rows["service.publish"]["verdict"] == "ok"
    assert rows["service.publish"]["delta"] == 299


@pytest.mark.parametrize("count,verdict", [(1299, "ok"), (1300, "ok"),
                                           (1301, "FAIL")])
def test_check_bounds_at_budget_plus_tolerance(policy: ModuleType, count: int,
                                               verdict: str) -> None:
    result = policy.check(_budget({"service.publish": 1000,
                                   "service.list": 2000}),
                          _measurement({"service.publish": count,
                                        "service.list": 2000}))
    assert _rows(result)["service.publish"] == verdict
    assert result["failed"] is (verdict == "FAIL")


def test_check_fails_a_declared_workload_missing_from_the_measurement(
        policy: ModuleType) -> None:
    result = policy.check(_budget(), _measurement({"service.list": 2000}))
    assert _rows(result)["service.publish"] == "missing"
    assert result["failed"] is True


def test_check_fails_a_measured_workload_absent_from_the_budget(
        policy: ModuleType) -> None:
    result = policy.check(_budget(),
                          _measurement({"service.publish": 500,
                                        "service.list": 2000,
                                        "cli.text": 700}))
    assert _rows(result)["cli.text"] == "unknown"
    assert result["failed"] is True


def test_check_on_a_toolchain_mismatch_compares_nothing(
        policy: ModuleType) -> None:
    measured = {**TOOLCHAIN, "valgrind": "valgrind-3.22.0"}
    result = policy.check(_budget(),
                          _measurement({"service.publish": 900000,
                                        "service.list": 2000}, measured))
    assert result["failed"] is False
    assert result["toolchain_diff"] == {"valgrind": ("valgrind-3.24.0",
                                                     "valgrind-3.22.0")}
    assert set(_rows(result).values()) == {"not compared"}


def test_a_null_toolchain_field_differs(policy: ModuleType) -> None:
    measured = {**TOOLCHAIN, "runner_image": None}
    result = policy.check(_budget(), _measurement(
        {"service.publish": 900000, "service.list": 2000}, measured))
    assert set(_rows(result).values()) == {"not compared"}
    assert result["failed"] is False


def test_check_exits_zero_on_a_toolchain_mismatch(speed: ModuleType,
                                                  tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    budget, measurement = tmp_path / "budget.json", tmp_path / "m.json"
    budget.write_text(json.dumps(_budget()), encoding="utf-8")
    measurement.write_text(json.dumps(_measurement(
        {"service.publish": 900000, "service.list": 900000},
        {**TOOLCHAIN, "valgrind": "valgrind-3.22.0"})), encoding="utf-8")
    assert speed.main(["check", "--measurements", str(measurement),
                       "--budget", str(budget)]) == 0
    assert "a rebaseline commit is required" in capsys.readouterr().out


def test_check_fails_a_count_past_the_budget_with_exit_one(
        speed: ModuleType, tmp_path: Path) -> None:
    budget, measurement = tmp_path / "budget.json", tmp_path / "m.json"
    budget.write_text(json.dumps(_budget()), encoding="utf-8")
    measurement.write_text(json.dumps(_measurement(
        {"service.publish": 1301, "service.list": 2000})), encoding="utf-8")
    assert speed.main(["check", "--measurements", str(measurement),
                       "--budget", str(budget)]) == 1


def test_tighten_lowers_only_the_workloads_that_fell(policy: ModuleType) -> None:
    result = policy.tighten(_budget(), _measurement({"service.publish": 900,
                                                     "service.list": 2500}))
    assert result == {"service.publish": 900, "service.list": 2000}


def test_tighten_never_raises(policy: ModuleType) -> None:
    # service.publish measured over budget (within tolerance): tighten must
    # leave it at 1000 while following service.list down to 1900.
    result = policy.tighten(_budget(), _measurement({"service.publish": 1100,
                                                     "service.list": 1900}))
    assert result == {"service.publish": 1000, "service.list": 1900}


def test_tighten_writes_nothing_when_nothing_fell(policy: ModuleType) -> None:
    assert policy.tighten(_budget(), _measurement(
        {"service.publish": 1000, "service.list": 2000})) is None


def test_tighten_is_a_noop_on_a_toolchain_mismatch(policy: ModuleType) -> None:
    measured = {**TOOLCHAIN, "lock_sha256": "cd" * 32}
    assert policy.tighten(_budget(), _measurement(
        {"service.publish": 900, "service.list": 2000}, measured)) is None


def test_tighten_is_a_noop_after_a_failure(policy: ModuleType) -> None:
    assert policy.tighten(_budget(), _measurement(
        {"service.publish": 1400, "service.list": 900})) is None


def test_tighten_writes_the_budget_only_when_something_fell(
        speed: ModuleType, tmp_path: Path) -> None:
    budget, measurement = tmp_path / "budget.json", tmp_path / "m.json"
    budget.write_text(json.dumps(_budget()), encoding="utf-8")
    measurement.write_text(json.dumps(_measurement(
        {"service.publish": 900, "service.list": 2500})), encoding="utf-8")
    assert speed.main(["tighten", "--measurements", str(measurement),
                       "--budget", str(budget)]) == 0
    document = json.loads(budget.read_text(encoding="utf-8"))
    assert document["workloads"] == {"service.publish": 900,
                                     "service.list": 2000}
    assert document["toolchain"] == TOOLCHAIN
    assert document["tolerance_pct"] == 30
    before = budget.read_bytes()
    measurement.write_text(json.dumps(_measurement(
        {"service.publish": 1000, "service.list": 2000})), encoding="utf-8")
    assert speed.main(["tighten", "--measurements", str(measurement),
                       "--budget", str(budget)]) == 0
    assert budget.read_bytes() == before


def test_rebaseline_rewrites_toolchain_and_workloads(policy: ModuleType) -> None:
    document = policy.rebaseline(
        _budget(), _measurement({"service.publish": 1500, "service.list": 2600}),
        ("service.publish", "service.list"))
    assert document["toolchain"] == TOOLCHAIN
    assert document["workloads"] == {"service.publish": 1500,
                                     "service.list": 2600}
    assert document["tolerance_pct"] == 30
    assert document["counter"] == "cachegrind-ir"
    assert document["schema_version"] == 1


def test_rebaseline_refuses_a_measurement_that_misses_a_workload(
        policy: ModuleType) -> None:
    with pytest.raises(policy.BudgetError, match="service.list"):
        policy.rebaseline(_budget(), _measurement({"service.publish": 1500}),
                          ("service.publish", "service.list"))


def test_rebaseline_without_a_budget_seeds_the_default_tolerance(
        policy: ModuleType) -> None:
    document = policy.rebaseline(
        None, _measurement({"service.publish": 1500, "service.list": 2600}),
        ("service.publish", "service.list"))
    assert document["tolerance_pct"] == policy.DEFAULT_TOLERANCE_PCT


def test_measure_writes_counts_toolchain_wall_and_base_sha(
        speed: ModuleType, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path) -> None:
    counts = dict.fromkeys(speed.WORKLOADS, 111)
    monkeypatch.setattr(speed, "reset", lambda tree, database: None)
    monkeypatch.setattr(speed, "counted",
                        lambda database, name, scratch: 111)
    monkeypatch.setattr(speed, "wall_times",
                        lambda database: dict.fromkeys(speed.WORKLOADS, 0.5))
    monkeypatch.setattr(speed.speed_budget, "toolchain", lambda: dict(TOOLCHAIN))
    out = tmp_path / "measurements.json"
    assert speed.main(["measure", "--database", "docs_speed_test",
                       "--out", str(out), "--base-sha", "abc123"]) == 0
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["counts"] == counts
    assert artifact["toolchain"] == TOOLCHAIN
    assert artifact["wall_seconds"] == dict.fromkeys(speed.WORKLOADS, 0.5)
    assert artifact["base_sha"] == "abc123"


def test_workload_prints_the_counts_as_json(speed: ModuleType,
                                            monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str],
                                            tmp_path: Path) -> None:
    counts = dict.fromkeys(speed.WORKLOADS, 111)
    monkeypatch.setattr(speed, "workload", lambda database, names=None: counts)
    assert speed.main(["workload", "--database", "docs_speed_test"]) == 0
    assert json.loads(capsys.readouterr().out) == counts


def test_workload_refuses_a_server_that_exits_at_startup(tmp_path: Path) -> None:
    done = subprocess.run([sys.executable, str(_SCRIPT), "workload", "--database", "x"],
                          cwd=tmp_path, capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "the server exited" in done.stderr


def test_workflow_builds_each_side_from_its_own_tree() -> None:
    with (_REPO / ".github/workflows/speed.yml").open(encoding="utf-8") as stream:
        job = yaml.safe_load(stream)["jobs"]["speed"]
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "git describe --tags --abbrev=0 --match 'v[0-9]*' HEAD" in script
    assert '-r "$RUNNER_TEMP/base/requirements.txt"' in script
    assert '"$RUNNER_TEMP/venv-head/bin/python" -m pip install -r requirements.txt' in script
    assert "python scripts/ci/speed.py" in script
