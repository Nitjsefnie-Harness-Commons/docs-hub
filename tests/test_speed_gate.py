"""Tests for the release-to-head speed gate (scripts/ci/speed.py)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts/ci/speed.py"


@pytest.fixture(name="speed")
def _speed() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_speed", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _samples(speed: ModuleType, seconds: list[float]) -> dict[str, list[float]]:
    return {name: list(seconds) for name in speed.WORKLOADS}


def test_compare_reads_the_median_block_ratio(speed: ModuleType) -> None:
    # One spoiled block (9.0) cannot decide the verdict.
    rows, failed = speed.compare(_samples(speed, [1.0, 1.0, 1.0]),
                                 _samples(speed, [1.2, 9.0, 1.1]), 0.30)
    assert failed == []
    assert all("+20.0%" in row and row.endswith("| ok |") for row in rows)


def test_compare_fails_only_the_workload_past_the_threshold(speed: ModuleType) -> None:
    head = _samples(speed, [1.0])
    head["cli.text"] = [1.31]
    rows, failed = speed.compare(_samples(speed, [1.0]), head, 0.30)
    assert failed == ["cli.text"]
    assert "| cli.text | 1.000 | 1.310 | +31.0% | FAIL |" in rows


@pytest.mark.parametrize("head_blocks", [[], [1.0], [1.0, 1.0, 1.0]])
def test_compare_fails_a_workload_without_paired_blocks(speed: ModuleType,
                                                        head_blocks: list[float]) -> None:
    head = _samples(speed, [1.0, 1.0])
    head["service.list"] = head_blocks
    rows, failed = speed.compare(_samples(speed, [1.0, 1.0]), head, 0.30)
    assert failed == ["service.list"]
    assert "| service.list | - | - | - | missing blocks |" in rows


def test_gate_interleaves_sides_each_on_its_own_tree_and_database(
        speed: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: list[str], cwd: Path, **_kwargs: object) -> SimpleNamespace:
        calls.append((str(cwd), command[0], command[-1]))
        slow = 2.0 if command[-1] == "docs_speed_head" else 1.0
        return SimpleNamespace(stdout=json.dumps(dict.fromkeys(speed.WORKLOADS, slow)))

    monkeypatch.setattr(speed, "reset", lambda tree, database: calls.append((database,)))
    monkeypatch.setattr(speed.subprocess, "run", run)
    summary = tmp_path / "summary.md"
    code = speed.main(["--base", "rel", "--base-python", "py-base", "--head", "head",
                       "--head-python", "py-head", "--blocks", "2", "--label", "v1",
                       "--summary", str(summary)])
    assert code == 1
    order = [call[-1] for call in calls if len(call) == 3]
    # A discarded warm-up, then two measured release-head-head-release blocks.
    assert order == ["docs_speed_base", "docs_speed_head",
                     "docs_speed_base", "docs_speed_head", "docs_speed_head", "docs_speed_base",
                     "docs_speed_base", "docs_speed_head", "docs_speed_head", "docs_speed_base"]
    assert ("head", "py-head", "docs_speed_head") in calls
    assert ("rel", "py-base", "docs_speed_base") in calls
    assert "| service.publish | 1.000 | 2.000 | +100.0% | FAIL |" in summary.read_text()


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
