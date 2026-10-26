#!/usr/bin/env python3
"""Refuse an undeclared raise of the speed budget (.github/speed-budget.json).

A workload count or the tolerance may only fall; schema_version may not
change. A raise carries exactly one line

    Budget-Raise: .github/speed-budget.json <key> <from> -> <to>

on a commit touching nothing but the budget — the route is its own act.
Toolchain changes and workload adds or removes take purity instead of a
line. The window is merge-base(base, head)..head on a pull_request,
before..head (ancestor required) on a push; other events and an all-zero
before skip with a message. A squash merge can drop the raise commit's
lines from the window history; a rebase merge keeps them.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

DOCUMENT = ".github/speed-budget.json"
REGULAR_FILE = "100644 blob"
_DECLARATION_PREFIX = "Budget-Raise: "
_DECLARATION_LINE = re.compile(
    r"^(?P<document>\S+) +(?P<key>.+) +(?P<was>\S+) -> (?P<to>\S+)$")
_ABSENT = object()
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


def _key_path(parts: list[str]) -> str:
    if not parts:
        return "(document)"
    rendered = []
    for index, part in enumerate(parts):
        if part.replace("_", "a").isalnum() and not part[0].isdigit():
            rendered.append(part if index == 0 else f".{part}")
        else:
            rendered.append(f"[{json.dumps(part)}]")
    return "".join(rendered)


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


def _is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, int):
        return True
    # A JSON NaN/Infinity literal arrives as a float; it must fail closed.
    return isinstance(value, float) and math.isfinite(value)


def _show(value: object) -> str:
    if value is None:
        return "absent"
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value)


def _under(key: str, parent: str) -> bool:
    """Whether the rendered key names a leaf under parent."""
    return (key.startswith(f"{parent}.")
            or key.startswith(f"{parent}["))


def _finding(document: str, key: str, base: object, head: object,
             why: str) -> str:
    return (f"{document}: {key}: reference {_show(base)}, "
            f"head {_show(head)} — {why}")


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
            was = Decimal(match.group("was"))
            to = Decimal(match.group("to"))
        except InvalidOperation:
            errors.append(_declaration_parse_error(line))
            continue
        # A non-finite token is a parse error, never a consumable.
        if not (was.is_finite() and to.is_finite()):
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
    """Paths against each commit's first parent, including merges."""
    result = _git(cwd, ["log", "--topo-order", "--diff-merges=first-parent",
                        "--format=%x00%H%n", "--name-only",
                        f"{base}..{head}"])
    if result.returncode != 0:
        raise ValueError(
            f"cannot walk {base}..{head} for its paths: "
            f"{result.stderr.strip()}")
    paths_by_commit: dict[str, list[str]] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        lines = [line for line in record.split("\n") if line]
        paths_by_commit[lines[0]] = lines[1:]
    return paths_by_commit


def _consume_declaration(declarations: list, key: str, was: object,
                         now: object) -> bool:
    """Pop the one declaration naming this raise exactly."""
    for index, (_document, decl_key, decl_was, decl_to) in enumerate(
            declarations):
        if decl_key == key and decl_was == was and decl_to == now:
            del declarations[index]
            return True
    return False


def _declaration_mismatch(document: str, key: str, was: object,
                          to: object) -> str:
    """The finding for a declaration the diff does not carry exactly."""
    return (f"{document}: {key}: declaration {was} -> {to} does not match "
            "a raise in this diff — a declaration must name the exact from "
            "and to the diff carries")


def _collect_declarations(cwd: Path, base: str, head: str,
                          paths_by_commit: dict[str, list[str]]) -> tuple[list, list]:
    """(pool, findings): a pure commit's lines pooled; any other line is
    a finding."""
    pool, findings = [], []
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
        pool.extend(declaration for declaration in parsed
                    if declaration[0] == DOCUMENT)
    return pool, findings


def _sanctioned(cwd: Path, commits: dict[str, list[str]]) -> tuple[dict, list]:
    """Legal transitions and impure workload steps, in history order."""
    accepted: dict[str, list[tuple]] = {}
    findings = []
    for sha in reversed(commits):
        pure = not set(commits[sha]) - {DOCUMENT}
        was = leaves(read_document(cwd, f"{sha}^", DOCUMENT) or {})
        now = leaves(read_document(cwd, sha, DOCUMENT) or {})
        for key in set(was) | set(now):
            old, new = was.get(key, _ABSENT), now.get(key, _ABSENT)
            if old == new:
                continue
            if not pure:
                if not _under(key, "workloads"):
                    continue
                if not (_is_number(old) and _is_number(new) and new < old):
                    findings.append(_finding(
                        DOCUMENT, key, was.get(key), now.get(key),
                        f"commit {sha[:12]} changed without a budget-only commit"))
                    continue
            transitions = accepted.setdefault(key, [])
            for pair in [(first, new) for first, last in transitions
                         if last == old] + [(old, new)]:
                if pair not in transitions:
                    transitions.append(pair)
    return accepted, findings


def _relaxations(base: object, head: object, declarations: list,
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
    before, after = leaves(base), leaves(head)
    for key, was in before.items():
        if key in after:
            continue
        if (_under(key, "toolchain") or _under(key, "workloads")) \
                and (was, _ABSENT) in sanctioned.get(key, []):
            continue
        findings.append(_finding(DOCUMENT, key, was, None, "key removed"))
    for key, now in after.items():
        if key not in before:
            if (_under(key, "toolchain") or _under(key, "workloads")) \
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
        if now > was:
            if _consume_declaration(declarations, key, was, now):
                continue
            findings.append(_finding(
                DOCUMENT, key, was, now, "raised; it may only fall"))
    for _document, key, was, to in declarations:
        findings.append(_declaration_mismatch(DOCUMENT, key, was, to))
    return findings


def _judge(cwd: Path, window_base: str, head_sha: str) -> list[str]:
    """Every relaxation of the budget document across the window."""
    paths_by_commit = _commit_paths(cwd, window_base, head_sha)
    pool, findings = _collect_declarations(
        cwd, window_base, head_sha, paths_by_commit)
    sanctioned, impure = _sanctioned(cwd, paths_by_commit)
    findings.extend(impure)
    findings.extend(_relaxations(
        read_document(cwd, window_base, DOCUMENT),
        read_document(cwd, head_sha, DOCUMENT),
        pool, sanctioned))
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
