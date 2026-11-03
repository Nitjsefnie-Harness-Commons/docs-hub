"""The paired check's edges: when its note prints, what a mismatch is, a base's commit."""
# pylint: disable=protected-access  # test_speed_gate.py's loader and fixtures
from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

import test_speed_gate as f
import test_speed_paired as p

_NOTE = "base measurements ignored"


@pytest.fixture(name="speed")
def _speed() -> ModuleType:
    return f._load("ci_speed", f._SCRIPT)


@pytest.mark.parametrize("chain", [f.TOOLCHAIN, p.HEAD_CHAIN])
def test_check_without_base_measurements_prints_no_ignored_note(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        chain: dict) -> None:
    argv = p._files(tmp_path, f._measurement({"service.publish": 1000,
                                              "service.list": 2000}, chain))
    speed.main(["check", *argv])
    assert _NOTE not in capsys.readouterr().out


def test_mismatch_answers_true_on_a_lock_only_difference(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert set(f.TOOLCHAIN.items()) - set(f.MEASURED_CHAIN.items()) == {
        ("lock_sha256", f.TOOLCHAIN["lock_sha256"])}
    argv = p._files(tmp_path, f._measurement({"service.publish": 1000}, f.MEASURED_CHAIN))
    assert speed.main(["mismatch", *argv]) == 0
    assert capsys.readouterr().out == "true\n"


@pytest.mark.parametrize("base_sha", ["absent", None, "", 123, True])
def test_a_base_measurement_naming_no_commit_is_refused(
        speed: ModuleType, base_sha: object) -> None:
    counts = {"service.publish": 1000, "service.list": 2000}
    base = p._base(counts)
    if base_sha == "absent":
        del base["base_sha"]
    else:
        base["base_sha"] = base_sha
    with pytest.raises(speed.speed_budget.BudgetError, match="base_sha"):
        speed.speed_paired.paired_check(f._budget(), p._head(counts), base)


def test_check_exits_one_on_a_base_file_naming_no_commit(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    counts = {"service.publish": 1000, "service.list": 2000}
    argv = p._files(tmp_path, p._head(counts), {**p._base(counts), "base_sha": None})
    assert speed.main(["check", *argv, "--base-measurements",
                       str(tmp_path / "base.json")]) == 1
    captured = capsys.readouterr()
    assert "base_sha" in captured.err and "base commit None" not in captured.out
