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


def _refusal(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("toolchain: the measured")]


@pytest.mark.parametrize("paired", [True, False])
@pytest.mark.parametrize("count", [1000, 1400])
def test_refuse_mismatch_fails_a_mismatched_head_whatever_its_counts_say(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        paired: bool, count: int) -> None:
    # 1000 passes both ways without the flag (base-less compares nothing; paired
    # is within base); 1400 is the paired path's own failure, still one verdict.
    argv = _files(tmp_path, _head({"service.publish": count, "service.list": 2000}),
                  _base({"service.publish": 1000, "service.list": 2000}))
    without = [*argv, *(["--base-measurements", str(tmp_path / "base.json")] if paired else [])]
    expected = 1 if paired and count == 1400 else 0
    assert speed.main(["check", *without]) == expected
    capsys.readouterr()
    assert speed.main(["check", *without, "--refuse-mismatch"]) == 1
    assert _refusal(capsys.readouterr().out) == [
        "toolchain: the measured toolchain differs from the budget's on valgrind; the pull "
        "request needs its declared rebaseline commit (CONTRIBUTING.md, speed budget)"]


def test_refuse_mismatch_names_every_differing_field_and_keeps_the_report(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    chain = {**HEAD_CHAIN, "python": "3.15.2", "lock_sha256": "ef" * 32}
    head = {**f._measurement({"service.publish": 1000, "service.list": 2000}, chain),
            "wall_seconds": {"service.publish": 0.5}}
    argv = _files(tmp_path, head)
    summary = tmp_path / "summary.md"
    assert speed.main(["check", *argv, "--summary", str(summary), "--refuse-mismatch"]) == 1
    out = capsys.readouterr().out
    assert summary.read_text(encoding="utf-8") == out
    lines = out.splitlines()
    (refusal,) = _refusal(out)
    assert "differs from the budget's on python, valgrind, lock_sha256;" in refusal
    # After the toolchain block, before the wall-time block; the tables stay.
    assert lines.index("| lock_sha256 | " + "ab" * 32 + " | " + "ef" * 32 + " |") \
        < lines.index(refusal) < lines.index("### Wall time (non-gating)")
    assert "| workload | count | budget | delta |" in out
    assert "toolchain: MISMATCH — counts not compared without a base measurement" in lines


def test_refuse_mismatch_fails_a_lock_only_difference_the_paired_check_tolerates(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    chain = {**f.TOOLCHAIN, "lock_sha256": "ef" * 32}
    counts = {"service.publish": 1000, "service.list": 2000}
    argv = _files(tmp_path, f._measurement(counts, chain),
                  {**f._measurement(counts, chain), "base_sha": BASE_SHA})
    paired = [*argv, "--base-measurements", str(tmp_path / "base.json")]
    assert speed.main(["check", *paired]) == 0
    capsys.readouterr()
    assert speed.main(["check", *paired, "--refuse-mismatch"]) == 1
    assert "differs from the budget's on lock_sha256;" in capsys.readouterr().out


@pytest.mark.parametrize("count,code", [(1300, 0), (1301, 1)])
def test_refuse_mismatch_changes_nothing_on_a_matching_toolchain(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        count: int, code: int) -> None:
    argv = _files(tmp_path, f._measurement({"service.publish": count, "service.list": 2000}))
    assert speed.main(["check", *argv]) == code
    plain = capsys.readouterr().out
    assert speed.main(["check", *argv, "--refuse-mismatch"]) == code
    assert capsys.readouterr().out == plain
    assert not _refusal(plain)
