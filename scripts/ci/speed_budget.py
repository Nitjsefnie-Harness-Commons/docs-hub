#!/usr/bin/env python3
"""The speed gate's budget: schema, toolchain identity, check/tighten/rebaseline.

`tighten` only follows a count down; `rebaseline` is the manual path and
may raise one; the gate semantics are on `check`.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ARTIFACT = Path(__file__).resolve().parents[2] / ".github" / "speed-budget.json"
LOCK = Path(__file__).resolve().parents[2] / "requirements-speed.txt"
COUNTER = "cachegrind-ir"
SCHEMA_VERSION = 1
# A first seed has no committed tolerance to keep, so rebaseline takes this.
DEFAULT_TOLERANCE_PCT = 30
TOOLCHAIN_FIELDS = ("python", "valgrind", "runner_image", "lock_sha256")


class BudgetError(ValueError):
    """The budget or a measurement is not in the shape the gate reads."""


def validate(document: object) -> None:
    """Refuse every budget shape the gate cannot judge."""
    if not isinstance(document, dict):
        raise BudgetError("the budget is not a JSON object")
    fields = {"schema_version", "counter", "tolerance_pct", "toolchain",
              "workloads"}
    unknown = sorted(set(document) - fields)
    if unknown:
        raise BudgetError(
            f"unknown budget field(s): {', '.join(map(str, unknown))}")
    missing = sorted(fields - set(document))
    if missing:
        raise BudgetError(f"missing budget field(s): {', '.join(missing)}")
    if document["schema_version"] != SCHEMA_VERSION:
        raise BudgetError(f"schema_version must be {SCHEMA_VERSION}")
    if document["counter"] != COUNTER:
        raise BudgetError(f'counter must be "{COUNTER}"')
    tolerance = document["tolerance_pct"]
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) \
            or not math.isfinite(tolerance) or tolerance < 0:
        raise BudgetError("tolerance_pct must be a finite number >= 0")
    chain = document["toolchain"]
    if not isinstance(chain, dict):
        raise BudgetError("toolchain must be an object")
    unknown = sorted(set(chain) - set(TOOLCHAIN_FIELDS))
    if unknown:
        raise BudgetError(
            f"unknown toolchain field(s): {', '.join(map(str, unknown))}")
    missing = sorted(set(TOOLCHAIN_FIELDS) - set(chain))
    if missing:
        raise BudgetError(f"missing toolchain field(s): {', '.join(missing)}")
    workloads = document["workloads"]
    if not isinstance(workloads, dict) or not workloads:
        raise BudgetError("workloads must be a non-empty object")
    for name, count in workloads.items():
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise BudgetError(
                f"workload {name}: count must be a positive integer, "
                f"got {count!r}")


def load(path: Path) -> dict:
    """Read and validate the budget document at `path`."""
    return _load_json(path, validate)


def load_measurement(path: Path) -> dict:
    """Read one measurement file; extra keys ride along for the report."""
    return _load_json(path, _validate_measurement)


def _validate_measurement(measurement: object) -> None:
    counts = measurement.get("counts")
    if not isinstance(counts, dict) or not all(
            isinstance(name, str) and isinstance(count, int)
            and not isinstance(count, bool) and count >= 0
            for name, count in counts.items()):
        raise BudgetError(
            "counts must map workload names to non-negative integers")
    if not isinstance(measurement.get("toolchain"), dict):
        raise BudgetError("toolchain must be an object")


def _load_json(path: Path, check: Callable[[object], None]) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BudgetError(f"{path}: {error}") from error
    check(document)
    return document


def toolchain() -> dict[str, str | None]:
    """What counts depend on but no code change controls."""
    version = subprocess.run(["valgrind", "--version"], check=True,
                             capture_output=True, text=True).stdout.strip()
    image = " ".join(part for part in (os.environ.get("ImageOS"),
                                       os.environ.get("ImageVersion")) if part)
    return {"python": sys.version.splitlines()[0],
            "valgrind": version or None,
            "runner_image": image or None,
            "lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest()}


def toolchain_diff(recorded: object, measured: object) -> dict[str, tuple]:
    """Every field the two toolchains differ on; a null differs too."""
    recorded = recorded or {}
    measured = measured or {}
    return {field: (recorded.get(field), measured.get(field))
            for field in TOOLCHAIN_FIELDS
            if recorded.get(field) != measured.get(field)}


def check(document: dict, measurement: dict) -> dict:
    """Compare one measurement against the budget; the caller renders, exits.

    `failed` is the gate: a toolchain difference compares nothing, a
    workload missing on either side fails, and so does one over budget."""
    diff = toolchain_diff(document.get("toolchain"),
                          measurement.get("toolchain"))
    counts = measurement.get("counts") or {}
    budgets = document["workloads"]
    tolerance = document["tolerance_pct"]
    rows, failed = [], False
    for name in sorted(set(budgets) | set(counts)):
        count, budget = counts.get(name), budgets.get(name)
        delta = (count - budget
                 if count is not None and budget is not None else None)
        if diff:
            verdict = "not compared"
        elif name not in budgets:
            failed = True
            verdict = "unknown"
        elif count is None:
            failed = True
            verdict = "missing"
        else:
            over = count > budget * (1 + tolerance / 100)
            failed = failed or over
            verdict = "FAIL" if over else "ok"
        rows.append({"name": name, "count": count, "budget": budget,
                     "delta": delta, "verdict": verdict})
    return {"toolchain_diff": diff, "rows": rows, "failed": failed}


def tighten(document: dict, measurement: dict) -> dict[str, int] | None:
    """Each workload the measurement found cheaper, lowered; None writes
    nothing."""
    if toolchain_diff(document.get("toolchain"),
                      measurement.get("toolchain")):
        return None
    if check(document, measurement)["failed"]:
        return None
    counts = measurement["counts"]
    lowered = {name: counts[name] for name in document["workloads"]
               if counts[name] < document["workloads"][name]}
    return {**document["workloads"], **lowered} if lowered else None


def rebaseline(document: dict | None, measurement: dict,
               names: tuple[str, ...]) -> dict:
    """Carry the whole budget from one measurement; a reviewed commit lands
    it. Unlike `tighten` this may raise a recorded count; `names` is the
    set the runner measures."""
    counts = measurement.get("counts") or {}
    if missing := [name for name in names if name not in counts]:
        raise BudgetError(f"the measurement lacks {', '.join(missing)}")
    if unknown := [name for name in counts if name not in names]:
        raise BudgetError(f"the measurement names {', '.join(unknown)}, "
                          "which the runner does not measure")
    return {"schema_version": SCHEMA_VERSION, "counter": COUNTER,
            "tolerance_pct": (document or {}).get(
                "tolerance_pct", DEFAULT_TOLERANCE_PCT),
            "toolchain": measurement["toolchain"],
            "workloads": {name: counts[name] for name in names}}
