#!/usr/bin/env python3
"""Raise the recorded CI coverage floors from a fresh measurement."""
from __future__ import annotations

import argparse
import importlib.util
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _thresholds():
    """The sibling store module, loaded by path: scripts/ci has no package."""
    path = Path(__file__).resolve().with_name("thresholds.py")
    assert path.is_file(), "The ratchet needs its thresholds store module"
    spec = importlib.util.spec_from_file_location("thresholds", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


thresholds = _thresholds()


def update(data: dict, measured: Decimal,
           language: str = "python") -> dict | None:
    """The raised document, or None when no raise is justified: a raise
    needs the language unrecorded (seed) or measured strictly above the
    recorded measurement.
    """
    recorded, _floor = thresholds.coverage(data, language)
    if recorded is not None and measured <= recorded:
        return None
    updated = thresholds.normalise(data)
    record = {"measured": measured,
              "floor": measured - thresholds.CALIBRATION_GAP}
    updated.setdefault("coverage", {})[language] = record
    return updated


def _measured(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measured", type=_measured, required=True,
                        help="freshly measured coverage percentage")
    parser.add_argument("--language", choices=thresholds.COVERAGE_LANGUAGES,
                        default="python")
    parser.add_argument("--thresholds", type=Path, default=thresholds.THRESHOLDS)
    parser.add_argument("--base", help="SHA the measurement was taken at")
    parser.add_argument("--tip", help="SHA now at the branch head")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if (args.base is not None and args.tip is not None
            and args.base != args.tip):
        print("master moved while this run measured; the next push raises instead")
        return 0
    try:
        data = thresholds.load(args.thresholds)
        recorded, floor = thresholds.coverage(data, args.language)
        updated = update(data, args.measured, args.language)
        if updated is None:
            print(f"no raise justified above the recorded {floor:.1f} floor")
            return 0
        thresholds.write(args.thresholds, updated)
        new_floor = updated["coverage"][args.language]["floor"]
        if recorded is None:
            print(f"seeded the {args.language} coverage floor "
                  f"{new_floor:.1f} (measured {args.measured:.1f})")
        else:
            print(f"raised the {args.language} coverage floor "
                  f"{floor:.1f} -> {new_floor:.1f} "
                  f"(measured {args.measured:.1f})")
    except (OSError, ValueError, InvalidOperation) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
