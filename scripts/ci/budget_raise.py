#!/usr/bin/env python3
"""Refuse an undeclared raise of the speed budget (.github/speed-budget.json).

A workload count or the tolerance may only fall; schema_version may not
change. A raise carries exactly one line

    Budget-Raise: .github/speed-budget.json <key> <from> -> <to>

on a commit touching nothing but the budget — the route is its own act.
Toolchain transitions need exact declarations in budget-only steps;
workload adds/removes also require budget-only steps;
mixed workload/tolerance steps may only decrease. Merge steps use combined
diffs, excluding inherited parent work. An absent-at-base budget seeds the
whole window. The window is merge-base(base, head)..head on a pull_request,
before..head (ancestor required) on a push; other events and an all-zero
before skip with a message. A squash merge can drop the raise commit's
lines from the window history; a rebase merge keeps them.
"""
from __future__ import annotations

import argparse
import json
import importlib.util
import os
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

DOCUMENT = ".github/speed-budget.json"
REGULAR_FILE = "100644 blob"
_DECLARATION_PREFIX = "Budget-Raise: "
_VALUE_TOKEN = r'"(?:[^"\\]|\\.)*"|\S+'
_DECLARATION_LINE = re.compile(
    r"^(?P<document>\S+) +(?P<key>.+?) +"
    rf"(?P<was>{_VALUE_TOKEN}) -> (?P<to>{_VALUE_TOKEN})$")
_SPEC = importlib.util.spec_from_file_location(
    "budget_chain", Path(__file__).with_name("budget_chain.py"))
assert _SPEC is not None and _SPEC.loader is not None
_CHAIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CHAIN)
_ABSENT = _CHAIN.ABSENT
_key_path = _CHAIN.key_path
_is_number = _CHAIN.is_number
_show = _CHAIN.show
_under = _CHAIN.under
_finding = _CHAIN.finding
_consume_declaration = _CHAIN.consume_declaration
_declaration_mismatch = _CHAIN.declaration_mismatch
_ZERO = "0" * 40
_WALK_EVENTS = ("pull_request", "push")


def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    # Every caller inspects the returncode.
    return subprocess.run(  # pylint: disable=subprocess-run-check
        ["git", *args], cwd=str(cwd), capture_output=True, text=True)


def require_full_history(cwd: Path) -> None:
    """A shallow history disqualifies the whole check."""
    result = _git(cwd, ["rev-parse", "--is-shallow-repository"])
    if result.returncode != 0:
        raise ValueError(
            "cannot tell whether the repository is shallow: "
            f"{result.stderr.strip()}")
    if result.stdout.strip() == "true":
        raise ValueError(
            "the repository is shallow; a shallow history can make git "
            "merge-base return a wrong base without erroring, so the check "
            "refuses to run — fetch full history first (git fetch "
            "--unshallow)")


def resolve_commit(cwd: Path, rev: str) -> str:
    result = _git(
        cwd, ["rev-parse", "--verify", "--quiet", "--end-of-options",
              f"{rev}^{{commit}}"])
    if result.returncode != 0:
        raise ValueError(f"unknown revision: {rev}")
    return result.stdout.strip()


def merge_base(cwd: Path, base: str, head: str) -> str:
    result = _git(cwd, ["merge-base", base, head])
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise ValueError(
            f"no merge base between {base} and {head} (unrelated histories "
            "or a shallow clone)")
    return sha


def _is_ancestor(cwd: Path, maybe: str, descendant: str) -> bool:
    """Whether maybe is an ancestor of descendant (the push window's
    membership rule)."""
    result = _git(cwd, ["merge-base", "--is-ancestor", maybe, descendant])
    if result.returncode not in (0, 1):
        raise ValueError(
            f"cannot tell whether {maybe} is an ancestor of "
            f"{descendant}: {result.stderr.strip()}")
    return result.returncode == 0


def entry_kind(cwd: Path, commit: str, path: str) -> str | None:
    """The tree entry's ``<mode> <type>`` at path, or None when absent."""
    listing = _git(
        cwd, ["--literal-pathspecs", "ls-tree", "-z", commit, "--", path])
    if listing.returncode != 0:
        raise ValueError(
            f"cannot list {path} at {commit}: {listing.stderr.strip()}")
    for record in listing.stdout.split("\0"):
        tab = record.find("\t")
        if tab == -1 or record[tab + 1:] != path:
            continue
        mode, kind, _object = record[:tab].split(" ", 2)
        return f"{mode} {kind}"
    return None


def read_document(cwd: Path, commit: str, path: str) -> dict | None:
    """The parsed document at commit, or None when absent there."""
    kind = entry_kind(cwd, commit, path)
    if kind is None:
        return None
    if kind != REGULAR_FILE:
        raise ValueError(f"{path} at {commit} is {kind}, not a regular file")
    blob = _git(cwd, ["show", f"{commit}:{path}"])
    if blob.returncode != 0:
        raise ValueError(
            f"cannot read {path} at {commit}: {blob.stderr.strip()}")
    try:
        return json.loads(blob.stdout, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{path} at {commit} is not valid JSON: {error}") from None


def leaves(value: object, parts: list[str] | None = None,
           out: dict | None = None) -> dict:
    """Leaf values keyed by their rendered path; an empty or non-object
    value anywhere is a leaf."""
    parts, out = parts or [], out if out is not None else {}
    if not isinstance(value, dict) or not value:
        out[_key_path(parts)] = value
        return out
    for key, child in value.items():
        leaves(child, [*parts, key], out)
    return out


def _declaration_parse_error(line: str) -> str:
    return (f"Budget-Raise declaration {line!r} does not parse — the "
            "format is 'Budget-Raise: <document> <key> <from> -> <to>'")


def _parse_declarations(lines: list[str]) -> tuple[list, list]:
    """Parse raw lines to (document, key, was, to); malformed is a finding."""
    declarations, errors = [], []
    for line in lines:
        match = _DECLARATION_LINE.match(line[len(_DECLARATION_PREFIX):])
        if match is None:
            errors.append(_declaration_parse_error(line))
            continue
        try:
            was, to = (_CHAIN.declaration_value(match.group("key"), match.group(field))
                       for field in ("was", "to"))
        except (InvalidOperation, ValueError):
            errors.append(_declaration_parse_error(line))
            continue
        declarations.append(
            (match.group("document"), match.group("key"), was, to))
    return declarations, errors


def _commits_with_declaration_lines(cwd: Path, base: str,
                                    head: str) -> list[tuple[str, list[str]]]:
    """(sha, raw declaration lines) for each window commit carrying any."""
    result = _git(cwd, ["log", "--format=%x00%H%n%B", f"{base}..{head}"])
    if result.returncode != 0:
        raise ValueError(
            f"cannot walk {base}..{head} for declarations: "
            f"{result.stderr.strip()}")
    found = []
    for record in result.stdout.split("\0"):
        if not record:
            continue
        sha, _newline, body = record.partition("\n")
        lines = [line.strip() for line in body.splitlines()
                 if line.strip().startswith(_DECLARATION_PREFIX)]
        if lines:
            found.append((sha, lines))
    return found


def _commit_paths(cwd: Path, base: str, head: str) -> dict[str, list[str]]:
    """Ordinary steps and merge contributions, never inherited parent work."""
    result = _git(cwd, ["rev-list", "--topo-order", f"{base}..{head}"])
    if result.returncode != 0:
        raise ValueError(f"cannot walk {base}..{head}: {result.stderr.strip()}")
    paths_by_commit = {}
    for sha in result.stdout.splitlines():
        diff = _git(cwd, ["diff-tree", "--cc", "--raw", "-r", "-z",
                          "--no-commit-id", "--no-renames", sha])
        if diff.returncode != 0:
            raise ValueError(f"cannot diff {sha}: {diff.stderr.strip()}")
        records = diff.stdout.split("\0")[:-1]
        if len(records) % 2 or any(not item.startswith(":") for item in records[::2]):
            raise ValueError(f"unrecognized raw diff for {sha}")
        paths_by_commit[sha] = records[1::2]
    return paths_by_commit


def _step_values(cwd: Path, sha: str, chains=None) -> tuple[dict, dict]:
    parents = _git(cwd, ["rev-parse", f"{sha}^@"])
    if parents.returncode != 0:
        raise ValueError(f"cannot read parents of {sha}: {parents.stderr.strip()}")
    parent_documents = [read_document(cwd, parent, DOCUMENT)
                        for parent in parents.stdout.splitlines()]
    document = read_document(cwd, sha, DOCUMENT)
    if chains is not None:
        chains.record(sha, parent_documents, document)
    before = [_CHAIN.normalized(leaves(parent or {})) for parent in parent_documents] or [{}]
    after = _CHAIN.normalized(leaves(document or {}))
    changed = {key for key in set(after).union(*before)
               if all(not _CHAIN.same(parent.get(key, _ABSENT), after.get(key, _ABSENT))
                      for parent in before)}
    return ({key: [parent.get(key, _ABSENT) for parent in before] for key in changed},
            {key: after[key] for key in changed if key in after})


def _collect_declarations(cwd: Path, base: str, head: str,
                          paths_by_commit: dict[str, list[str]]) -> tuple[dict, list]:
    """Declaration pools keyed by their contributing commit."""
    pools, findings = {}, []
    for sha, lines in _commits_with_declaration_lines(cwd, base, head):
        outside = sorted(set(paths_by_commit.get(sha, [])) - {DOCUMENT})
        if outside:
            findings.append(
                f"Budget-Raise: commit {sha[:12]} carries declaration lines "
                f"but also touches {', '.join(outside)} — a declared raise "
                f"lands as its own commit touching only {DOCUMENT}")
            continue
        parsed, errors = _parse_declarations(lines)
        findings.extend(errors)
        for document, key, was, to in parsed:
            if document != DOCUMENT:
                findings.append(
                    f"{document}: {key}: declaration {was} -> {to} names a "
                    "document whose values are not declarable — only "
                    f"{DOCUMENT} raises are")
        pools[sha] = [declaration for declaration in parsed if declaration[0] == DOCUMENT]
    return pools, findings


def _sanctioned(cwd: Path, commits: dict[str, list[str]],
                declarations: dict | None = None, chains=None) -> tuple[dict, list]:
    """Judge contributed steps and compose unique legal endpoint pairs."""
    accepted: dict[str, list[tuple]] = {}
    findings = []
    for sha in reversed(commits):
        pure = not set(commits[sha]) - {DOCUMENT}
        pool = (declarations or {}).get(sha, [])
        was, now = _step_values(cwd, sha, chains) if DOCUMENT in commits[sha] else ({}, {})
        for key, values in was.items():
            old, new = values[0], now.get(key, _ABSENT)
            numeric = _under(key, "workloads") or key == "tolerance_pct"
            raised = [value for value in values if numeric and _is_number(value)
                      and _is_number(new) and new > value]
            allowed = pure or (numeric and _is_number(new) and all(
                _is_number(value) and new < value for value in values))
            if not allowed:
                if numeric:
                    findings.append(_finding(
                        DOCUMENT, key, old, new,
                        f"commit {sha[:12]} changed without a budget-only commit"))
                    if raised:
                        findings.append(_finding(
                            DOCUMENT, key, old, new, "raised; it may only fall"))
                continue
            if _under(key, "toolchain") and not any(
                    _consume_declaration(pool, key, value, new) for value in values):
                findings.append(_finding(DOCUMENT, key, old, new, "undeclared toolchain change"))
                continue
            if raised and not any(_consume_declaration(pool, key, value, new) for value in raised):
                findings.append(_finding(DOCUMENT, key, old, new, "raised; it may only fall"))
            transitions = accepted.setdefault(key, [])
            for origin in values:
                for pair in [(first, new) for first, last in transitions
                             if last == origin] + [(origin, new)]:
                    if pair not in transitions:
                        transitions.append(pair)
    return accepted, findings


def _relaxations(base: object, head: object,
                 sanctioned: dict[str, list[tuple]]) -> list[str]:
    # pylint: disable=too-many-branches

    """Findings for a head document that relaxes the base's; a budget
    absent at base is a seed."""
    if base is None:
        return []
    if head is None:
        return [_finding(DOCUMENT, "(document)", "present", None,
                         "the document was deleted")]
    findings = []
    before, after = _CHAIN.normalized(leaves(base)), _CHAIN.normalized(leaves(head))
    for key, was in before.items():
        if key in after:
            continue
        if (_under(key, "toolchain") or _CHAIN.numeric_key(key)) \
                and (was, _ABSENT) in sanctioned.get(key, []):
            continue
        findings.append(_finding(DOCUMENT, key, was, None, "key removed"))
    for key, now in after.items():
        if key not in before:
            if (_under(key, "toolchain") or _CHAIN.numeric_key(key)) \
                    and (_ABSENT, now) in sanctioned.get(key, []):
                continue
            findings.append(_finding(DOCUMENT, key, None, now, "key added"))
            continue
        was = before[key]
        if _under(key, "toolchain"):
            if was != now and (was, now) not in sanctioned.get(key, []):
                findings.append(_finding(
                    DOCUMENT, key, was, now,
                    "changed without a budget-only commit"))
            continue
        if key == "counter" and was == now:
            continue
        if not _is_number(now):
            findings.append(_finding(
                DOCUMENT, key, was, now, "not a finite number"))
            continue
        if was == now:
            continue
        if key == "schema_version":
            findings.append(_finding(
                DOCUMENT, key, was, now, "schema_version changed"))
            continue
        if not _is_number(was):
            findings.append(_finding(
                DOCUMENT, key, was, now,
                "reference value is not a finite number"))
            continue
    return findings


def _judge(cwd: Path, window_base: str, head_sha: str) -> list[str]:
    """Every relaxation of the budget document across the window."""
    base = read_document(cwd, window_base, DOCUMENT)
    if base is None:
        return []
    paths_by_commit = _commit_paths(cwd, window_base, head_sha)
    pools, findings = _collect_declarations(cwd, window_base, head_sha, paths_by_commit)
    originals = {sha: list(pool) for sha, pool in pools.items()}
    chains = _CHAIN.ValueChains(base)
    sanctioned, steps = _sanctioned(cwd, paths_by_commit, pools, chains)
    findings.extend(steps)
    head = read_document(cwd, head_sha, DOCUMENT)
    eligible = {sha for sha, paths in paths_by_commit.items() if set(paths) == {DOCUMENT}}
    findings.extend(chains.finish(head, pools, originals, eligible))
    for pool in pools.values():
        for document, key, old, new in pool:
            findings.append(_declaration_mismatch(document, key, old, new))
    findings.extend(_relaxations(base, head, sanctioned))
    return findings


def run(cwd: Path, base: str | None, head: str | None, before: str | None,
        event: str | None) -> tuple[int, list[str]]:
    """(exit code, report lines): 0 clean or skipped, 1 per finding, 2
    usage."""
    if event not in _WALK_EVENTS:
        if not base and not head:
            raise ValueError("no event name and no base or head "
                             "revision: the run is not wired")
        return 0, [f"budget-raise: the {event or 'unlabelled'} run has no "
                   "window to judge — skipping"]
    if not head:
        raise ValueError(f"a {event} run needs the head revision "
                         "(DOCS_HUB_HEAD_SHA)")
    require_full_history(cwd)
    head_sha = resolve_commit(cwd, head)
    if event == "push":
        # A push judges before..head; the pull request's base is not an
        # input on this route.
        if not before:
            raise ValueError("a push run needs the commit it came from "
                             "(DOCS_HUB_BEFORE_SHA)")
        if before.strip() == _ZERO:
            return 0, ["budget-raise: the push created its branch, so no "
                       "window precedes it — skipping"]
        before_sha = resolve_commit(cwd, before)
        if not _is_ancestor(cwd, before_sha, head_sha):
            raise ValueError(
                f"{before} is not an ancestor of {head}; the push window is "
                "not the push's own commits")
        window_base = before_sha
        label = ("base", before_sha)
    else:
        if not base:
            raise ValueError("a pull_request run needs the base revision "
                             "(DOCS_HUB_BASE_SHA)")
        window_base = merge_base(cwd, resolve_commit(cwd, base), head_sha)
        label = ("merge base", window_base)
    findings = _judge(cwd, window_base, head_sha)
    if findings:
        return 1, [*findings,
                   f"budget-raise: {len(findings)} relaxation(s) against "
                   f"{label[0]} {label[1]}; the budget may only tighten"]
    return 0, [f"budget-raise: {DOCUMENT} not relaxed against {label[0]} "
               f"{label[1]} — ok"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("DOCS_HUB_BASE_SHA"),
                        help="the pull request's base revision")
    parser.add_argument("--head", default=os.environ.get("DOCS_HUB_HEAD_SHA"),
                        help="the head revision")
    parser.add_argument(
        "--before", default=os.environ.get("DOCS_HUB_BEFORE_SHA"))
    parser.add_argument(
        "--event", default=os.environ.get("DOCS_HUB_EVENT_NAME"))
    args = parser.parse_args(argv)
    try:
        code, lines = run(Path.cwd(), args.base, args.head, args.before,
                          args.event)
    except ValueError as error:
        print(f"budget-raise: {error}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
