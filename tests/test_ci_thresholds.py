"""Tests for the CI coverage thresholds store (scripts/ci/thresholds.py).

The loader is the one place the coverage gate reads its floor from, so a
document it accepts is a document CI acts on: these cases pin the
document shape (schema_version, the closed python domain, floor below
measured and exactly the calibration gap below it, unknown fields and
languages refused, strict JSON) and the canonical byte layout write()
publishes, including that the committed file is itself canonical.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts/ci/thresholds.py"
_THRESHOLDS = _REPO / ".github/ci-thresholds.json"
# The committed seed: python coverage measured 95.9, its floor one
# calibration gap below.
MEASURED = Decimal("95.9")
FLOOR = Decimal("94.4")


@pytest.fixture(name="thresholds")
def _thresholds() -> ModuleType:
    assert _SCRIPT.is_file(), "The coverage gate needs its thresholds module"
    spec = importlib.util.spec_from_file_location("ci_thresholds", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_SCRIPT), *args],
                          check=False, capture_output=True, text=True, timeout=20)


def _mutated(tmp_path: Path, mutate: Callable[[dict], None]) -> Path:
    """The seed document, mutated, written non-canonically to tmp_path."""
    data: dict = {"schema_version": 1,
                  "coverage": {"python": {"measured": 95.9, "floor": 94.4}}}
    mutate(data)
    target = tmp_path / "ci-thresholds.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    return target


def test_committed_document_is_canonical(
        thresholds: ModuleType, tmp_path: Path) -> None:
    # The ratchet republishes the file with the same renderer, so the
    # committed bytes must equal write()'s output for the same content;
    # anything else shows up as a spurious diff on every raise.
    target = tmp_path / "canonical.json"
    thresholds.write(target, thresholds.load(_THRESHOLDS))
    assert target.read_bytes() == _THRESHOLDS.read_bytes()


def test_committed_seed_records_python(thresholds: ModuleType) -> None:
    measured, floor = thresholds.coverage(thresholds.load(_THRESHOLDS), "python")
    assert (measured, floor) == (MEASURED, FLOOR)


def test_valid_document_round_trips_unchanged(
        thresholds: ModuleType, tmp_path: Path) -> None:
    data = {"schema_version": 1,
            "coverage": {"python": {"measured": 90.0, "floor": 88.5}}}
    normalised = thresholds.normalise(data)
    target = tmp_path / "ci-thresholds.json"
    thresholds.write(target, data)
    assert thresholds.load(target) == normalised
    measured, floor = thresholds.coverage(thresholds.load(target), "python")
    assert (measured, floor) == (Decimal("90.0"), Decimal("88.5"))


@pytest.mark.parametrize("measured,floor", [(90.0, 90.0), (88.0, 90.0)])
def test_floor_at_or_above_measured_refuses(
        thresholds: ModuleType, tmp_path: Path, measured: float, floor: float) -> None:
    target = _mutated(tmp_path, lambda data: data["coverage"]["python"].update(
        measured=measured, floor=floor))
    with pytest.raises(ValueError, match="floor must be below measured"):
        thresholds.load(target)


@pytest.mark.parametrize("measured,floor", [(93.0, 90.0), (91.4, 90.0)])
def test_wrong_calibration_gap_refuses(
        thresholds: ModuleType, tmp_path: Path, measured: float, floor: float) -> None:
    target = _mutated(tmp_path, lambda data: data["coverage"]["python"].update(
        measured=measured, floor=floor))
    with pytest.raises(ValueError, match="calibration gap must be 1.5"):
        thresholds.load(target)


def test_unknown_top_level_field_refuses(
        thresholds: ModuleType, tmp_path: Path) -> None:
    target = _mutated(tmp_path, lambda data: data.update(surprise={}))
    with pytest.raises(ValueError, match="unknown thresholds field: surprise"):
        thresholds.load(target)


def test_unknown_coverage_language_refuses(
        thresholds: ModuleType, tmp_path: Path) -> None:
    target = _mutated(
        tmp_path,
        lambda data: data["coverage"].update(ruby={"measured": 50.0, "floor": 48.5}))
    with pytest.raises(ValueError, match="unknown coverage language: ruby"):
        thresholds.load(target)


def test_missing_measured_refuses(thresholds: ModuleType, tmp_path: Path) -> None:
    target = _mutated(
        tmp_path, lambda data: data["coverage"].update(python={"floor": 94.4}))
    with pytest.raises(ValueError, match="coverage.python is missing measured"):
        thresholds.load(target)


def test_unknown_field_in_a_record_refuses(
        thresholds: ModuleType, tmp_path: Path) -> None:
    target = _mutated(tmp_path, lambda data: data["coverage"]["python"].update(
        pinned=True))
    with pytest.raises(ValueError, match="coverage.python has an unknown field"):
        thresholds.load(target)


@pytest.mark.parametrize("schema", [2, "1", None, True])
def test_schema_version_refuses(
        thresholds: ModuleType, tmp_path: Path, schema: object) -> None:
    target = _mutated(tmp_path, lambda data: data.update(schema_version=schema))
    with pytest.raises(ValueError, match="schema_version"):
        thresholds.load(target)


def test_missing_schema_version_refuses(
        thresholds: ModuleType, tmp_path: Path) -> None:
    target = _mutated(tmp_path, lambda data: data.pop("schema_version"))
    with pytest.raises(ValueError, match="missing thresholds field: schema_version"):
        thresholds.load(target)


def test_nan_literal_refuses(thresholds: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "ci-thresholds.json"
    target.write_text('{"schema_version": 1, "coverage": {"python": '
                      '{"measured": NaN, "floor": 94.4}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="NaN"):
        thresholds.load(target)


def test_a_document_that_is_not_an_object_refuses(
        thresholds: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "ci-thresholds.json"
    target.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        thresholds.load(target)
    result = _run("--check", "--thresholds", str(target))
    assert result.returncode == 1
    assert result.stderr != ""


def test_coverage_returns_none_for_an_absent_language(
        thresholds: ModuleType) -> None:
    assert thresholds.coverage({"schema_version": 1}, "python") == (None, None)


def test_coverage_refuses_an_unknown_language(thresholds: ModuleType) -> None:
    with pytest.raises(ValueError, match="unknown coverage language"):
        thresholds.coverage({"schema_version": 1}, "ruby")


def test_check_exits_zero_on_the_committed_file() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "thresholds valid\n"


def test_coverage_floor_prints_the_committed_floor() -> None:
    result = _run("--coverage-floor", "python")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "94.4\n"


def test_coverage_measured_prints_the_committed_measured() -> None:
    result = _run("--coverage-measured", "python")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "95.9\n"


def test_coverage_floor_prints_nothing_for_an_absent_language(
        tmp_path: Path) -> None:
    target = _mutated(tmp_path, lambda data: data["coverage"].pop("python"))
    result = _run("--coverage-floor", "python", "--thresholds", str(target))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_corrupt_file_fails_closed_in_every_cli_mode(tmp_path: Path) -> None:
    target = tmp_path / "corrupt.json"
    target.write_text("{", encoding="utf-8")
    for mode in (["--check"],
                 ["--coverage-floor", "python"],
                 ["--coverage-measured", "python"]):
        result = _run(*mode, "--thresholds", str(target))
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stderr != ""


def test_a_missing_file_fails_closed(tmp_path: Path) -> None:
    result = _run("--check", "--thresholds", str(tmp_path / "absent.json"))
    assert result.returncode == 1
    assert "cannot read thresholds" in result.stderr
