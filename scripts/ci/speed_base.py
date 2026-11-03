#!/usr/bin/env python3
"""Print the commit a toolchain-mismatched speed run measures beside its head.

pull_request: the merge base; push: `before`; else the head's first parent.
Whatever it cannot resolve exits 1.
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
    event, head = env.get("DOCS_HUB_EVENT_NAME"), env.get("DOCS_HUB_HEAD_SHA")
    if not event:
        raise ValueError("no event name (DOCS_HUB_EVENT_NAME): the run is not wired")
    if not head:
        raise ValueError(f"a {event} run needs the head revision (DOCS_HUB_HEAD_SHA)")
    budget_raise.require_full_history(cwd)
    head_sha = budget_raise.resolve_commit(cwd, head)
    if event == "pull_request":
        base = env.get("DOCS_HUB_BASE_SHA")
        if not base:
            raise ValueError("a pull_request run needs the base revision (DOCS_HUB_BASE_SHA)")
        return budget_raise.merge_base(cwd, budget_raise.resolve_commit(cwd, base), head_sha)
    if event == "push":
        before = env.get("DOCS_HUB_BEFORE_SHA") or ""
        if not _FULL_SHA.fullmatch(before) or before == "0" * 40:
            raise ValueError(f"a push run measures the commit it came from, and {before!r} "
                             "is not a full non-zero SHA (DOCS_HUB_BEFORE_SHA)")
        before_sha = budget_raise.resolve_commit(cwd, before)
        if not budget_raise.is_ancestor(cwd, before_sha, head_sha):
            raise ValueError(f"{before} is not an ancestor of {head}")
        return before_sha
    try:
        return budget_raise.resolve_commit(cwd, f"{head_sha}^1")
    except ValueError:
        raise ValueError(f"{head} has no first parent to measure") from None


def main() -> int:
    try:
        print(base_commit(Path.cwd(), os.environ))
    except ValueError as error:
        print(f"speed-base: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
