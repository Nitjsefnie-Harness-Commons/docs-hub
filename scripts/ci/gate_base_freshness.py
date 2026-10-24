#!/usr/bin/env python3
"""Require this head to carry master's commits touching named CI gate inputs.

Adapted from claim's tests/gate_base_freshness.py: its fail-closed stdlib block
parser, tracked-path resolution, anonymous fetch and commit comparison form the
starting point. Here ci-gate.yml's aggregate.needs defines the required jobs;
local reusable workflows contribute every job they execute. Definitions sharing
a required job name across workflows are unioned. Only ci-gate.yml's own
aggregate is excluded from the walk. Every workflow file is watched.

Reach limits: `.` resolves to nothing; implicit tool configuration, expression-
computed paths and files read from inside scripts are invisible unless a run:
step names them. This derives named inputs, rather than every file a gate reads.
Only the explicit block workflow layout below is modeled; other shapes refuse.

    scripts/ci/gate_base_freshness.py [--root DIR] [--print-paths]
"""
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, TypedDict

BASE_BRANCH = "master"
PATHSPECS_MAX_BYTES = 65536
WORKFLOW_DIR = ".github/workflows"
GATE_WORKFLOW = f"{WORKFLOW_DIR}/ci-gate.yml"
MAPPING = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(?P<value>.*))?$")
SEQUENCE = re.compile(r"^- (?P<rest>.+)$")
BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")
JOB_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
CANDIDATE = re.compile(r"[A-Za-z0-9._/-]+")


class GateError(Exception):
    """The run could not establish freshness."""


class WorkflowError(GateError):
    """A workflow uses a shape this reader does not model."""


class Step(TypedDict):
    run: str | None
    uses: str | None


@dataclass
class Job:
    steps: list[Step]
    uses: str | None = None
    needs: set[str] | None = None


def git(root: Path, *arguments: str, what: str) -> str:
    command = ("git", "-C", str(root), *arguments)
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode:
        raise GateError(f"cannot {what}: git exited {done.returncode}: "
                        f"{done.stderr.strip() or 'no output'}")
    return done.stdout


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def skippable(line: str) -> bool:
    return not line.strip() or line.lstrip().startswith("#")


def block_end(lines: list[str], start: int, indent: int) -> int:
    index = start + 1
    while index < len(lines):
        if not skippable(lines[index]) and indent_of(lines[index]) <= indent:
            break
        index += 1
    return index


def block_scalar(lines: list[str], start: int, key_indent: int, limit: int) -> tuple[str, int]:
    body = []
    index = start + 1
    while index < limit:
        line = lines[index]
        if line.strip() and indent_of(line) <= key_indent:
            break
        body.append(line)
        index += 1
    leads = [indent_of(line) for line in body if line.strip()]
    lead = min(leads) if leads else key_indent + 2
    return "\n".join(line[lead:] if len(line) > lead else "" for line in body), index


def needs_list(lines: list[str], start: int, end: int, value: str,
               workflow: str) -> set[str]:
    pieces = [line.split("#", 1)[0].strip() for line in lines[start + 1:end]
              if not skippable(line)]
    value = value.split("#", 1)[0].strip()
    if value.startswith("["):
        text = " ".join([value, *pieces])
        if not text.endswith("]"):
            raise WorkflowError(f"{workflow}: unmodeled needs list {text!r}")
        names = text[1:-1].split(",")
        if names[-1].strip() == "":
            names.pop()
    elif not value:
        if any(not piece.startswith("- ") for piece in pieces):
            raise WorkflowError(f"{workflow}: expected block needs list")
        names = [piece[2:] for piece in pieces]
    else:
        names = [value] if not pieces else []
    names = [name.strip() for name in names]
    if not names or any(JOB_NAME.fullmatch(name) is None for name in names):
        raise WorkflowError(f"{workflow}: needs must name explicit jobs")
    if len(set(names)) != len(names):
        raise WorkflowError(f"{workflow}: duplicate needs entry")
    return set(names)


def workflow_jobs(text: str, workflow: str) -> dict[str, Job]:
    lines = text.splitlines()
    tops = [index for index, line in enumerate(lines)
            if not skippable(line) and indent_of(line) == 0
            and (header := MAPPING.match(line)) is not None and header["key"] == "jobs"]
    if len(tops) != 1:
        raise WorkflowError(f"{workflow}: expected exactly one top-level jobs mapping")
    top = tops[0]
    header = MAPPING.match(lines[top])
    if header is None or header["value"] is not None:
        raise WorkflowError(f"{workflow}: jobs uses a flow mapping; expected block form")
    jobs: dict[str, Job] = {}
    index = top + 1
    while index < len(lines):
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) < 2:
            break
        match = MAPPING.match(line.strip())
        if indent_of(line) != 2 or not match or match["value"] is not None:
            raise WorkflowError(f"{workflow}: expected a block job entry, found {line!r}")
        name = match["key"]
        if name in jobs:
            raise WorkflowError(f"{workflow}: duplicate job {name!r}")
        stop = block_end(lines, index, 2)
        jobs[name] = job_fields(lines, name, index + 1, stop, workflow)
        index = stop
    return jobs


def job_fields(lines: list[str], name: str, start: int, end: int, workflow: str) -> Job:
    job = Job([])
    seen: set[str] = set()
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        match = MAPPING.match(line.strip())
        if indent_of(line) != 4 or not match:
            raise WorkflowError(f"{workflow}: expected a key in job {name!r}, found {line!r}")
        key, value = match["key"], (match["value"] or "").strip()
        if key in seen:
            raise WorkflowError(f"{workflow}: duplicate {key!r} in job {name!r}")
        seen.add(key)
        stop = block_end(lines, index, 4)
        if key == "steps":
            if value:
                raise WorkflowError(f"{workflow}: steps must use block form in job {name!r}")
            job.steps = step_entries(lines, name, index + 1, stop, workflow)
        elif key == "uses":
            if not value or stop != index + 1 and any(
                    not skippable(line) for line in lines[index + 1:stop]):
                raise WorkflowError(f"{workflow}: expected a scalar workflow reference")
            job.uses = value.split()[0]
        elif key == "needs":
            job.needs = needs_list(lines, index, stop, value, workflow)
        index = stop
    if job.uses and "steps" in seen:
        raise WorkflowError(f"{workflow}: job {name!r} has both uses and steps")
    return job


def step_entries(lines: list[str], name: str, start: int, end: int,
                 workflow: str) -> list[Step]:
    steps: list[Step] = []
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        entry = SEQUENCE.match(line.strip()) if indent_of(line) == 6 else None
        if entry is None:
            raise WorkflowError(f"{workflow}: expected a step in job {name!r}, found {line!r}")
        stop = block_end(lines, index, 6)
        steps.append(step_fields(lines, index, stop, workflow, entry["rest"]))
        index = stop
    return steps


def step_fields(lines: list[str], start: int, end: int, workflow: str, dash: str) -> Step:
    step: Step = {"run": None, "uses": None}
    seen: set[str] = set()
    index = start
    while index < end:
        if skippable(lines[index]):
            index += 1
            continue
        text = dash if index == start else lines[index].strip()
        match = MAPPING.match(text)
        if not match or index != start and indent_of(lines[index]) != 8:
            raise WorkflowError(f"{workflow}: expected a step key, found {text!r}")
        key, value = match["key"], (match["value"] or "").strip()
        if key in seen:
            raise WorkflowError(f"{workflow}: duplicate step key {key!r}")
        seen.add(key)
        if key == "run":
            if BLOCK_SCALAR.fullmatch(value):
                step["run"], index = block_scalar(lines, index, 8, end)
                continue
            if not value or value.startswith(("{", "[", "&", "*", "!", "'", '"')):
                raise WorkflowError(f"{workflow}: unmodeled run scalar {value!r}")
            step["run"] = value
        elif key == "uses":
            if not value or value.startswith(("'", '"', "|", ">", "{", "[", "&", "*", "!")):
                raise WorkflowError(f"{workflow}: unmodeled action reference {value!r}")
            step["uses"] = value.split()[0]
        elif not value or BLOCK_SCALAR.fullmatch(value):
            index = min(end, block_end(lines, index, 8))
            continue
        index += 1
    return step


def tracked_files(root: Path) -> tuple[str, ...]:
    command = ("git", "-C", str(root), "ls-tree", "-z", "-r", "--name-only",
               "--full-tree", "HEAD")
    done = subprocess.run(command, capture_output=True, check=False)
    if done.returncode:
        raise GateError("cannot list the tracked tree: "
                        + done.stderr.decode("utf-8", errors="replace").strip())
    try:
        return tuple(raw.decode("utf-8") for raw in done.stdout.split(b"\0") if raw)
    except UnicodeDecodeError as refusal:
        raise GateError("a tracked path is not valid UTF-8") from refusal


def read_workflows(root: Path) -> dict[str, dict[str, Job]]:
    files = tracked_files(root)
    names = sorted(path for path in files if path.startswith(WORKFLOW_DIR + "/")
                   and path.endswith((".yml", ".yaml")))
    return {name: workflow_jobs(git(root, "cat-file", "blob", f"HEAD:{name}",
                                    what=f"read {name}"), name) for name in names}


def required_jobs(parsed: dict[str, dict[str, Job]]) -> set[str]:
    aggregate = parsed.get(GATE_WORKFLOW, {}).get("aggregate")
    if aggregate is None or not aggregate.needs or "aggregate" in aggregate.needs:
        raise GateError(f"{GATE_WORKFLOW}: aggregate must need explicit folded jobs")
    return aggregate.needs


def candidates(text: str) -> list[str]:
    return CANDIDATE.findall(EXPRESSION.sub(" ", text))


def resolve(candidate: str, files: Sequence[str]) -> tuple[str, ...]:
    """Resolve an exact tracked file or directory prefix; `.` names nothing."""
    candidate = candidate[2:] if candidate.startswith("./") else candidate
    candidate = candidate.rstrip("/")
    if not candidate:
        return ()
    if candidate in files:
        return (candidate,)
    return tuple(path for path in files if path.startswith(candidate + "/"))


def folded_steps(parsed: dict[str, dict[str, Job]]) -> list[Step]:
    pending = [(workflow, name) for name in sorted(required_jobs(parsed))
               for workflow, jobs in parsed.items() if name in jobs]
    for name in required_jobs(parsed):
        if not any(name in jobs for jobs in parsed.values()):
            raise GateError(f"no workflow under {WORKFLOW_DIR}/ defines required job `{name}`")
    seen: set[tuple[str, str]] = set()
    steps: list[Step] = []
    while pending:
        workflow, name = pending.pop()
        if (workflow, name) == (GATE_WORKFLOW, "aggregate") or (workflow, name) in seen:
            continue
        seen.add((workflow, name))
        job = parsed[workflow][name]
        steps.extend(job.steps)
        if job.uses:
            target = job.uses.removeprefix("./")
            if not job.uses.startswith("./") or target not in parsed or not parsed[target]:
                raise GateError(f"{workflow}: cannot model reusable workflow {job.uses!r}")
            pending.extend((target, child) for child in parsed[target])
    return steps


def gate_paths(root: Path) -> list[str]:
    files = tracked_files(root)
    parsed = read_workflows(root)
    derived = set(resolve(WORKFLOW_DIR, files))
    for step in folded_steps(parsed):
        uses = step["uses"] or ""
        if uses.startswith("./"):
            derived.update(files if uses == "./" else resolve(uses, files))
        for candidate in candidates(step["run"] or ""):
            derived.update(resolve(candidate, files))
    if not derived:
        raise GateError("the derived path set is empty, so there is nothing to compare")
    return sorted(derived)


def fetch_base(root: Path) -> None:
    """Fetch fresh master anonymously, deepening a shallow checkout first."""
    arguments = ["fetch", "--no-tags", "--quiet"]
    if git(root, "rev-parse", "--is-shallow-repository",
           what="ask whether the checkout is shallow").strip() == "true":
        arguments.append("--unshallow")
    arguments += ["origin", f"+refs/heads/{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}"]
    git(root, *arguments, what=f"fetch origin/{BASE_BRANCH}")


def stale_commits(root: Path, head: str, base: str,
                  paths: Sequence[str]) -> list[tuple[str, str, list[str]]]:
    if not paths:
        raise GateError("the derived path set is empty, so there is nothing to compare")
    sized = sum(len(path.encode("utf-8")) + 1 for path in paths)
    if sized > PATHSPECS_MAX_BYTES:
        raise GateError(f"the derived path set is {sized} B, past {PATHSPECS_MAX_BYTES} B "
                        "allowed in an argument list")
    listing = git(root, "log", f"{head}..{base}", "--name-only", "--format=%x00%H%x09%s",
                  "--", *paths, what=f"list what {BASE_BRANCH} holds that this head lacks")
    stale = []
    for chunk in listing.split("\0"):
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        sha, _, subject = header.partition("\t")
        stale.append((sha.strip(), subject.strip(),
                      [line for line in body.splitlines() if line.strip()]))
    return stale


def check(root: Path) -> int:
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}",
               what="resolve the checked-out head").strip()
    fetch_base(root)
    base = git(root, "rev-parse", "--verify", f"refs/remotes/origin/{BASE_BRANCH}^{{commit}}",
               what=f"resolve origin/{BASE_BRANCH}").strip()
    paths = gate_paths(root)
    stale = stale_commits(root, head, base, paths)
    if not stale:
        print(f"This head carries every commit on {BASE_BRANCH} that touches "
              f"the {len(paths)} file(s) a required check reads BY NAME.")
        print("  The whole-tree `.` spelling resolves to nothing; see the module docstring.")
        return 0
    print(f"{BASE_BRANCH} holds {len(stale)} commit{'s' if len(stale) != 1 else ''} "
          "this head does not:")
    for sha, subject, touched in stale:
        print(f"  {sha} {subject}")
        if not touched:
            print("    (a merge commit, which git names no file for; the "
                  "commits it brought in are listed as themselves)")
        for path in touched:
            print(f"    {path}")
    print(f"Rebase onto {BASE_BRANCH} and push again, so this run's checks read "
          f"the files {BASE_BRANCH} reads.")
    return 1


def main(argv: Sequence[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    print_paths = False
    rest = list(argv[1:])
    while rest:
        argument = rest.pop(0)
        if argument == "--print-paths":
            print_paths = True
        elif argument == "--root" and rest:
            root = Path(rest.pop(0))
        else:
            print("usage: gate_base_freshness.py [--root DIR] [--print-paths]", file=sys.stderr)
            return 2
    try:
        if print_paths:
            print("\n".join(gate_paths(root)))
            return 0
        return check(root)
    except (GateError, OSError, UnicodeError) as refusal:
        print(f"head freshness: {refusal}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
