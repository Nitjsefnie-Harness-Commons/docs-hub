"""A toolchain-mismatched head judged against a base measured in the same job."""
# pylint: disable=protected-access  # test_speed_gate.py's loader
from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest

import test_speed_gate as f

# The head left the budget's toolchain; the base shares the head's.
HEAD_CHAIN = {**f.TOOLCHAIN, "valgrind": "valgrind-3.25.0"}
BASE_SHA = "1f" * 20


@pytest.fixture(name="speed")
def _speed() -> ModuleType:
    return f._load("ci_speed", f._SCRIPT)


@pytest.fixture(name="paired")
def _paired(speed: ModuleType) -> ModuleType:
    return speed.speed_paired


def _head(counts: dict[str, int]) -> dict:
    return f._measurement(counts, HEAD_CHAIN)


def _base(counts: dict[str, int], chain: dict | None = None) -> dict:
    return {**f._measurement(counts, chain or HEAD_CHAIN), "base_sha": BASE_SHA}


def test_a_head_within_tolerance_of_base_passes_over_a_stale_budget(
        paired: ModuleType) -> None:
    # 2500 is past the budget's 1300 ceiling and inside the base's 2600.
    result = paired.paired_check(f._budget(),
                                 _head({"service.publish": 2500, "service.list": 2000}),
                                 _base({"service.publish": 2000, "service.list": 2000}))
    assert f._rows(result) == {"service.publish": "ok", "service.list": "ok"}
    assert result["failed"] is False


def test_a_head_over_base_fails_though_under_its_budget(paired: ModuleType) -> None:
    # 1400 is inside the budget's 2600 ceiling and past the base's 1300.
    result = paired.paired_check(f._budget({"service.publish": 2000, "service.list": 2000}),
                                 _head({"service.publish": 1400, "service.list": 2000}),
                                 _base({"service.publish": 1000, "service.list": 2000}))
    assert f._rows(result) == {"service.publish": "FAIL", "service.list": "ok"}
    assert result["failed"] is True


@pytest.mark.parametrize("count,verdict", [(1299, "ok"), (1300, "ok"), (1301, "FAIL")])
def test_paired_check_bounds_at_base_plus_tolerance(paired: ModuleType, count: int,
                                                    verdict: str) -> None:
    result = paired.paired_check(f._budget({"service.publish": 5000, "service.list": 2000}),
                                 _head({"service.publish": count, "service.list": 2000}),
                                 _base({"service.publish": 1000, "service.list": 2000}))
    assert f._rows(result)["service.publish"] == verdict
    assert result["failed"] is (verdict == "FAIL")


def test_a_row_carries_the_head_count_the_base_count_and_their_delta(
        paired: ModuleType) -> None:
    head = _head({"service.publish": 1100, "service.list": 2000})
    result = paired.paired_check(f._budget(), head,
                                 _base({"service.publish": 1050, "service.list": 2000}))
    assert result["rows"][1] == {"name": "service.publish", "count": 1100, "budget": 1050,
                                 "delta": 50, "verdict": "ok"}
    assert result["base_sha"] == BASE_SHA
    assert result["toolchain_diff"] == {"valgrind": ("valgrind-3.24.0", "valgrind-3.25.0")}


@pytest.mark.parametrize("head_extra,base_extra", [(True, False), (False, True), (True, True)])
def test_a_workload_absent_from_the_budget_is_unknown(paired: ModuleType, head_extra: bool,
                                                      base_extra: bool) -> None:
    counts = {"service.publish": 1000, "service.list": 2000}
    result = paired.paired_check(
        f._budget(), _head({**counts, **({"cli.text": 700} if head_extra else {})}),
        _base({**counts, **({"cli.text": 700} if base_extra else {})}))
    assert f._rows(result)["cli.text"] == "unknown"
    assert result["failed"] is True


@pytest.mark.parametrize("side", ["head", "base"])
def test_a_budgeted_workload_missing_from_one_side_is_missing(paired: ModuleType,
                                                              side: str) -> None:
    full, short = ({"service.publish": 1000, "service.list": 2000}, {"service.list": 2000})
    result = paired.paired_check(f._budget(), _head(short if side == "head" else full),
                                 _base(short if side == "base" else full))
    assert f._rows(result) == {"service.publish": "missing", "service.list": "ok"}
    assert result["failed"] is True


def test_a_base_on_another_lock_is_judged(paired: ModuleType) -> None:
    result = paired.paired_check(f._budget(),
                                 _head({"service.publish": 1400, "service.list": 2000}),
                                 _base({"service.publish": 1000, "service.list": 2000},
                                       {**HEAD_CHAIN, "lock_sha256": "ef" * 32}))
    assert f._rows(result)["service.publish"] == "FAIL"


@pytest.mark.parametrize("field", ["python", "valgrind", "image"])
def test_a_base_off_the_heads_toolchain_is_refused(speed: ModuleType, paired: ModuleType,
                                                   field: str) -> None:
    counts = {"service.publish": 1000, "service.list": 2000}
    with pytest.raises(speed.speed_budget.BudgetError, match=field):
        paired.paired_check(f._budget(), _head(counts),
                            _base(counts, {**HEAD_CHAIN, field: "elsewhere"}))


def _files(tmp_path: Path, head: dict, base: dict | None = None) -> list[str]:
    budget, measurement = tmp_path / "budget.json", tmp_path / "m.json"
    budget.write_text(json.dumps(f._budget()), encoding="utf-8")
    measurement.write_text(json.dumps(head), encoding="utf-8")
    argv = ["--measurements", str(measurement), "--budget", str(budget)]
    if base is not None:
        (tmp_path / "base.json").write_text(json.dumps(base), encoding="utf-8")
    return argv


@pytest.mark.parametrize("count,code", [(1400, 1), (1300, 0)])
def test_check_judges_a_mismatched_head_against_the_base_file(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        count: int, code: int) -> None:
    # Both counts are over the budget's 1300 but neither is compared to it.
    argv = _files(tmp_path, _head({"service.publish": count, "service.list": 2000}),
                  _base({"service.publish": 1000, "service.list": 2000}))
    assert speed.main(["check", *argv, "--base-measurements",
                       str(tmp_path / "base.json")]) == code
    out = capsys.readouterr().out
    assert "| workload | count | base | delta |" in out
    assert f"counts judged against base commit {BASE_SHA}" in out
    assert "without a base measurement" not in out


def test_check_on_a_matching_toolchain_ignores_the_base_file(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The base would pass 1301; the budget fails it. The base path is unreadable.
    argv = _files(tmp_path, f._measurement({"service.publish": 1301, "service.list": 2000}))
    assert speed.main(["check", *argv, "--base-measurements",
                       str(tmp_path / "absent.json")]) == 1
    out = capsys.readouterr().out
    assert "base measurements ignored: the toolchain matches the budget" in out
    assert "| workload | count | budget | delta |" in out


def test_check_refuses_an_unreadable_base_file_on_a_mismatch(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = _files(tmp_path, _head({"service.publish": 1000, "service.list": 2000}))
    assert speed.main(["check", *argv, "--base-measurements",
                       str(tmp_path / "absent.json")]) == 1
    assert "absent.json" in capsys.readouterr().err


@pytest.mark.parametrize("chain,answer", [(HEAD_CHAIN, "true"), (f.TOOLCHAIN, "false")])
def test_mismatch_prints_whether_the_toolchain_left_the_budget(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        chain: dict, answer: str) -> None:
    argv = _files(tmp_path, f._measurement({"service.publish": 1000}, chain))
    assert speed.main(["mismatch", *argv]) == 0
    assert capsys.readouterr().out == f"{answer}\n"


@pytest.mark.parametrize("unreadable", ["--measurements", "--budget"])
def test_mismatch_exits_one_on_an_unreadable_input(
        speed: ModuleType, tmp_path: Path, unreadable: str) -> None:
    argv = _files(tmp_path, f._measurement({"service.publish": 1000}))
    argv[argv.index(unreadable) + 1] = str(tmp_path / "absent.json")
    assert speed.main(["mismatch", *argv]) == 1
