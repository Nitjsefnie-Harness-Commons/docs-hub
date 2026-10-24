"""Tests for the coverage-floor ratchet (scripts/ci/ratchet.py).

The ratchet is the only writer of the thresholds document outside a
hand edit, so these cases pin the raise decision (never lowers, the
strictly-above boundary in both directions, a same-value rerun), the
staleness refusal, the CLI's write and message behaviour and its
fail-closed error path.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts/ci/ratchet.py"
# The in-test seed, never the committed file: the ratchet rewrites that.
SEED = {"schema_version": 1,
        "coverage": {"python": {"measured": 95.9, "floor": 94.4}}}
MEASURED = Decimal("95.9")


@pytest.fixture(name="ratchet")
def _ratchet() -> ModuleType:
    assert _SCRIPT.is_file(), "The push workflow needs its ratchet script"
    spec = importlib.util.spec_from_file_location("ci_ratchet", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_SCRIPT), *args],
                          check=False, capture_output=True, text=True, timeout=20)


def _seed(tmp_path: Path) -> Path:
    target = tmp_path / "ci-thresholds.json"
    target.write_text(json.dumps(SEED), encoding="utf-8")
    return target


def _seed_data(ratchet: ModuleType, tmp_path: Path) -> dict:
    return ratchet.thresholds.load(_seed(tmp_path))


def test_a_lower_measurement_never_lowers(ratchet: ModuleType,
                                          tmp_path: Path) -> None:
    data = _seed_data(ratchet, tmp_path)
    assert ratchet.update(data, MEASURED - Decimal("0.1")) is None
    target = _seed(tmp_path)
    before = target.read_bytes()
    result = _run("--measured", "50", "--thresholds", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "no raise justified above the recorded 94.4 floor\n"
    assert target.read_bytes() == before


@pytest.mark.parametrize("measured,expected", [
    (Decimal("95.8"), None),  # Below the record: never lowers.
    (Decimal("95.9"), None),  # Equal to the record: a rerun is a no-op.
    (Decimal("96.0"), {"measured": Decimal("96.0"),
                       "floor": Decimal("94.5")}),
])
def test_the_raise_boundary_holds_in_both_directions(
        ratchet: ModuleType, tmp_path: Path, measured: Decimal,
        expected: dict | None) -> None:
    data = _seed_data(ratchet, tmp_path)
    assert ratchet.update(data, measured) == (
        None if expected is None else
        {"schema_version": 1, "coverage": {"python": expected}})


def test_a_boundary_measurement_writes_nothing(tmp_path: Path) -> None:
    target = _seed(tmp_path)
    before = target.read_bytes()
    result = _run("--measured", "95.9", "--thresholds", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "no raise justified above the recorded 94.4 floor\n"
    assert target.read_bytes() == before


def test_a_raise_rewrites_and_reports(ratchet: ModuleType,
                                      tmp_path: Path) -> None:
    target = _seed(tmp_path)
    result = _run("--measured", "99.0", "--thresholds", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("raised the python coverage floor 94.4 -> 97.5 "
                             "(measured 99.0)\n")
    measured, floor = ratchet.thresholds.coverage(
        ratchet.thresholds.load(target), "python")
    assert (measured, floor) == (Decimal("99.0"), Decimal("97.5"))


def test_a_raise_then_a_same_value_run_does_not_raise_again(
        ratchet: ModuleType, tmp_path: Path) -> None:
    data = _seed_data(ratchet, tmp_path)
    raised = ratchet.update(data, Decimal("96.0"))
    assert raised is not None
    assert ratchet.update(raised, Decimal("96.0")) is None


def test_seeding_an_absent_language_leaves_other_families_intact(
        ratchet: ModuleType, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The domain is closed at one language today; widen the private copy so
    # the test can hold a second family the seed must not disturb.
    monkeypatch.setattr(ratchet.thresholds, "COVERAGE_LANGUAGES",
                        ("python", "ruby"))
    data = {"schema_version": 1,
            "coverage": {"python": {"measured": Decimal("95.9"),
                                    "floor": Decimal("94.4")}}}
    updated = ratchet.update(data, Decimal("88.0"), "ruby")
    assert updated is not None
    assert updated["coverage"]["python"] == {"measured": Decimal("95.9"),
                                             "floor": Decimal("94.4")}
    written = tmp_path / "seeded.json"
    ratchet.thresholds.write(written, updated)
    measured, floor = ratchet.thresholds.coverage(
        ratchet.thresholds.load(written), "python")
    assert (measured, floor) == (Decimal("95.9"), Decimal("94.4"))
    measured, floor = ratchet.thresholds.coverage(
        ratchet.thresholds.load(written), "ruby")
    assert (measured, floor) == (Decimal("88.0"), Decimal("86.5"))


def test_a_seed_writes_and_reports(ratchet: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "ci-thresholds.json"
    target.write_text('{"schema_version": 1}\n', encoding="utf-8")
    result = _run("--measured", "96.5", "--thresholds", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "seeded the python coverage floor 95.0 (measured 96.5)\n"
    measured, floor = ratchet.thresholds.coverage(
        ratchet.thresholds.load(target), "python")
    assert (measured, floor) == (Decimal("96.5"), Decimal("95.0"))


def test_a_stale_run_writes_nothing(tmp_path: Path) -> None:
    # 99.9 is above the seed's record, so this raise is refused by the
    # staleness check alone, not the raise decision.
    target = _seed(tmp_path)
    before = target.read_bytes()
    result = _run("--measured", "99.9", "--base", "aaa", "--tip", "bbb",
                  "--thresholds", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("master moved while this run measured; "
                             "the next push raises instead\n")
    assert target.read_bytes() == before


def test_a_one_sided_staleness_check_still_raises(ratchet: ModuleType,
                                                  tmp_path: Path) -> None:
    target = _seed(tmp_path)
    result = _run("--measured", "99.9", "--base", "aaa",
                  "--thresholds", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("raised the python coverage floor 94.4 -> 98.4 "
                             "(measured 99.9)\n")
    measured, floor = ratchet.thresholds.coverage(
        ratchet.thresholds.load(target), "python")
    assert (measured, floor) == (Decimal("99.9"), Decimal("98.4"))


def test_a_corrupt_thresholds_file_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "corrupt.json"
    target.write_text("{", encoding="utf-8")
    result = _run("--measured", "99.0", "--thresholds", str(target))
    assert result.returncode == 1
    assert result.stderr != ""


def test_a_nan_measurement_fails_closed_and_writes_nothing(
        tmp_path: Path) -> None:
    # NaN parses as a Decimal and only detonates at the raise comparison;
    # the CLI must refuse it like any other unreadable input: one stderr
    # line, never a traceback, and the recorded file untouched.
    target = _seed(tmp_path)
    before = target.read_bytes()
    result = _run("--measured", "NaN", "--thresholds", str(target))
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert result.stderr != ""
    assert result.stdout == ""
    assert target.read_bytes() == before
