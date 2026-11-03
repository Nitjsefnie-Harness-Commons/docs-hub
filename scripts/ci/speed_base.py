#!/usr/bin/env python3
"""Print the commit a toolchain-mismatched speed run measures beside HEAD.

CONTRIBUTING.md's speed row gives the rule per event; the rest exits 1.
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import budget_raise  # noqa: E402  pylint: disable=wrong-import-position

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def base_commit(cwd: Path, env: Mapping[str, str]) -> str:
    event = env.get("DOCS_HUB_EVENT_NAME")
    if not event:
        raise ValueError("no event name (DOCS_HUB_EVENT_NAME): the run is not wired")
    budget_raise.require_full_history(cwd)
    head = budget_raise.resolve_commit(cwd, "HEAD")
    if event == "pull_request":
        if not env.get("DOCS_HUB_BASE_SHA"):
            raise ValueError("a pull_request run needs the base revision (DOCS_HUB_BASE_SHA)")
        base = budget_raise.resolve_commit(cwd, env["DOCS_HUB_BASE_SHA"])
        if not budget_raise.is_ancestor(cwd, base, head):
            raise ValueError(f"{base} is not an ancestor of the checked-out {head}")
        return base
    if event == "push":
        before = env.get("DOCS_HUB_BEFORE_SHA") or ""
        if not _FULL_SHA.fullmatch(before):
            raise ValueError(f"{before!r} is not a full SHA (DOCS_HUB_BEFORE_SHA)")
        if before != "0" * 40 and _contains(cwd, before, head):
            return before
    try:
        return budget_raise.resolve_commit(cwd, f"{head}^1")
    except ValueError:
        raise ValueError(f"{head} has no first parent to measure") from None


def _contains(cwd: Path, before: str, head: str) -> bool:
    try:  # A force push's old tip may never have been fetched.
        resolved = budget_raise.resolve_commit(cwd, before)
    except ValueError:
        return False
    return budget_raise.is_ancestor(cwd, resolved, head)


def main() -> int:
    try:
        print(base_commit(Path.cwd(), os.environ))
    except ValueError as error:
        print(f"speed-base: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
