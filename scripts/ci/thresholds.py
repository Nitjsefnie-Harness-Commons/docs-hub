#!/usr/bin/env python3
"""Store and reader for the CI coverage thresholds document.

.github/ci-thresholds.json is committed data: per language, the coverage
the raising run measured and the floor the next runs must clear, one
calibration gap below measured. load() validates a document in full;
write() republishes canonical bytes atomically; the CLI prints one value
or validates, exiting nonzero on any refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

# The committed document this module is the store for, named from the
# module's own location so a checkout anywhere answers the same path.
THRESHOLDS = Path(__file__).resolve().parents[2] / ".github" / "ci-thresholds.json"
# The fixed distance between a recorded measured value and its floor: a
# yardstick, not a tunable — both values move together in one publish.
CALIBRATION_GAP = Decimal("1.5")
SCHEMA_VERSION = 1
# Every coverage language the store gates. A closed domain: a language
# outside it would sit ungated forever, so unknown names refuse.
COVERAGE_LANGUAGES = ("python",)
_TOP_LEVEL_FIELDS = ("schema_version", "coverage")
_COVERAGE_FIELDS = ("measured", "floor")


def _reject_constant(token: str) -> None:
    raise ValueError(f"thresholds cannot parse the JSON constant {token}")


def _number(value: object, name: str) -> Decimal:
    """One JSON number as a Decimal; a JSON true is not a number.

    A finite Decimal passes through as itself: normalise() reads its own
    output when write() republishes an already-normalised document.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} must be a JSON number")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a JSON number")
    return Decimal(str(value))


def normalise(data: dict) -> dict:
    """Return the canonical document, or refuse an unknown or bad value.

    schema_version is required; coverage and each language record are
    optional, but a present record is validated in full and carries
    exactly measured and floor. Unknown top-level fields and unknown
    languages refuse, so only the schema's own shapes survive.
    """
    if not isinstance(data, dict):
        raise ValueError("thresholds must be a JSON object")
    unknown = sorted(set(data) - set(_TOP_LEVEL_FIELDS))
    if unknown:
        raise ValueError(f"unknown thresholds field: {unknown[0]}")
    if "schema_version" not in data:
        raise ValueError("missing thresholds field: schema_version")
    schema = data["schema_version"]
    if schema.__class__ is not int:
        raise ValueError("schema_version must be the integer 1")
    if schema != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {schema}")

    normalised: dict = {"schema_version": SCHEMA_VERSION}
    if "coverage" in data:
        coverage_data = data["coverage"]
        if not isinstance(coverage_data, dict):
            raise ValueError("coverage must be a JSON object")
        languages = sorted(set(coverage_data) - set(COVERAGE_LANGUAGES))
        if languages:
            raise ValueError(f"unknown coverage language: {languages[0]}")
        normalised["coverage"] = {
            language: _coverage_record(language, coverage_data[language])
            for language in COVERAGE_LANGUAGES if language in coverage_data}
    return normalised


def _coverage_record(language: str, record: object) -> dict:
    """One language's record: exactly measured and floor, gap below."""
    prefix = f"coverage.{language}"
    if not isinstance(record, dict):
        raise ValueError(f"{prefix} must be a JSON object")
    missing = [field for field in _COVERAGE_FIELDS if field not in record]
    if missing:
        raise ValueError(f"{prefix} is missing {missing[0]}")
    extra = sorted(set(record) - set(_COVERAGE_FIELDS))
    if extra:
        raise ValueError(f"{prefix} has an unknown field: {extra[0]}")
    measured = _number(record["measured"], f"{prefix}.measured")
    floor = _number(record["floor"], f"{prefix}.floor")
    if floor >= measured:
        raise ValueError(f"{prefix}.floor must be below measured")
    if measured - floor != CALIBRATION_GAP:
        raise ValueError(f"{prefix} calibration gap must be 1.5")
    return {"measured": measured, "floor": floor}


def _decode(raw: bytes) -> dict:
    """Strict JSON: NaN and Infinity literals refuse."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"thresholds is not valid UTF-8: {error}") from None
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        raise ValueError(f"cannot parse thresholds: {error}") from None


def load(path: str | Path = THRESHOLDS) -> dict:
    """Read and validate a thresholds document."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read thresholds: {error}") from None
    return normalise(_decode(raw))


def coverage(data, language) -> tuple[Decimal | None, Decimal | None]:
    """One language's (measured, floor), or (None, None) when absent."""
    if language not in COVERAGE_LANGUAGES:
        raise ValueError(f"unknown coverage language: {language}")
    record = normalise(data).get("coverage", {}).get(language)
    if record is None:
        return None, None
    return record["measured"], record["floor"]


def _render(data: dict) -> bytes:
    normalised = normalise(data)
    text = json.dumps(normalised, ensure_ascii=True, indent=2, sort_keys=True,
                      allow_nan=False, default=float)
    encoded = (text + "\n").encode("utf-8")
    if normalise(_decode(encoded)) != normalised:
        raise ValueError("serialized thresholds failed validation")
    return encoded


def write(path, data) -> None:
    """Validate and atomically replace ``path`` with canonical JSON bytes."""
    target = Path(path)
    payload = _render(data)
    mode = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        # A new destination keeps mkstemp's restrictive default mode.
        pass
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    temporary_path = Path(temporary)
    open_fd = fd
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            open_fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, target)
        replaced = True
    finally:
        if open_fd is not None:
            try:
                os.close(open_fd)
            except OSError:
                # Preserve the primary failure when redundant close fails.
                pass
        if not replaced:
            try:
                os.unlink(temporary_path)
            except OSError:
                # Cleanup must not hide the publication failure that prompted it.
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true",
                       help="validate the threshold document")
    modes.add_argument("--coverage-floor", choices=COVERAGE_LANGUAGES,
                       help="print one language floor")
    modes.add_argument("--coverage-measured", choices=COVERAGE_LANGUAGES,
                       help="print one language measured value")
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        data = load(args.thresholds)
        if args.coverage_floor:
            _measured, floor = coverage(data, args.coverage_floor)
            if floor is not None:
                print(f"{floor:.1f}")
        elif args.coverage_measured:
            measured, _floor = coverage(data, args.coverage_measured)
            if measured is not None:
                print(f"{measured:.1f}")
        else:
            print("thresholds valid")
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
