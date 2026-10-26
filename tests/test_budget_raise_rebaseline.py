"""A toolchain mismatch is exempt only after a declared, reviewed rebaseline."""
# pylint: disable=protected-access  # Share the real Git fixture and CLI boundary.
import json
from pathlib import Path
import subprocess
import sys

import pytest

import test_budget_raise as f
from test_budget_raise_matrix import _event


def _line(field: str, before: object, after: object) -> str:
    return f._decl(f"toolchain.{field}", json.dumps(before), json.dumps(after))


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["python", "valgrind", "runner_image", "lock_sha256"])
@pytest.mark.parametrize("mode, expected", [
    ("none", 1), ("exact", 0), ("mismatch", 1), ("duplicate", 1), ("impure", 1),
])
def test_toolchain_rebaseline_requires_its_own_exact_line(
        tmp_path: Path, event: str, field: str, mode: str, expected: int) -> None:
    repo, base = f._seeded(tmp_path)
    new = "cd" * 32 if field == "lock_sha256" else 'new "tool" -> image'
    line = _line(field, f.CHAIN_A[field], new)
    if mode == "mismatch":
        line = _line(field, f.CHAIN_A[field], "wrong")
    if mode == "duplicate":
        line += "\n" + line
    message = "rebaseline" if mode == "none" else "rebaseline\n\n" + line
    files = {f.BUDGET: json.dumps(f._budget({"w.pub": 100, "w.list": 200},
                                            chain={**f.CHAIN_A, field: new}))}
    if mode == "impure":
        files[f.CODE] = "print(2)\n"
    f._commit(repo, message, **files)
    f._commit(repo, "later feature", **{f.CODE: "print(3)\n"})
    result = _event(repo, base, event)
    assert result.returncode == expected, result.stdout + result.stderr
    if mode == "none":
        assert "undeclared toolchain change" in result.stdout


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("shape", ["null", "add", "remove"])
def test_toolchain_nulls_additions_and_removals_need_declarations(
        tmp_path: Path, event: str, declared: bool, shape: str) -> None:
    repo, base = f._seeded(tmp_path)
    field = "compiler" if shape == "add" else "runner_image"
    chain = dict(f.CHAIN_A)
    before = json.dumps(chain[field]) if field in chain else "absent"
    if shape == "remove":
        del chain[field]
        after = "absent"
    else:
        chain[field] = None if shape == "null" else "new compiler"
        after = json.dumps(chain[field])
    message = "rebaseline\n\n" + f._decl(f"toolchain.{field}", before, after) \
        if declared else "no declaration"
    f._commit(repo, message, **{f.BUDGET: json.dumps(f._budget(
        {"w.pub": 100, "w.list": 200}, chain=chain))})
    result = _event(repo, base, event)
    assert result.returncode == int(not declared), result.stdout + result.stderr


def test_an_undeclared_rebaseline_cannot_exempt_a_triple_budget_measurement(tmp_path: Path) -> None:
    repo, base = f._seeded(tmp_path)
    f._commit(repo, "undeclared runner edit", **{f.BUDGET: json.dumps(f._budget(
        {"w.pub": 100, "w.list": 200}, chain={**f.CHAIN_A, "runner_image": "new"}))})
    f._commit(repo, "feature", **{f.CODE: "print(2)\n"})
    measurement = repo / "measurement.json"
    measurement.write_text(json.dumps({"counts": {"w.pub": 300, "w.list": 600},
                                       "toolchain": f.CHAIN_A}), encoding="utf-8")
    check = subprocess.run([sys.executable, str(f._REPO / "scripts/ci/speed.py"),
                            "check", "--measurements", str(measurement),
                            "--budget", str(repo / f.BUDGET)], capture_output=True,
                           text=True, check=False, timeout=20)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "not compared" in check.stdout
    result = _event(repo, base, "pull_request")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "undeclared toolchain change" in result.stdout


@pytest.mark.parametrize("kind", ["numeric", "toolchain"])
def test_trailing_declaration_junk_is_a_parse_error(tmp_path: Path, kind: str) -> None:
    repo, base = f._seeded(tmp_path)
    if kind == "numeric":
        line = f._decl('workloads["w.pub"]', 100, 300)
        document = f._budget({"w.pub": 300, "w.list": 200})
    else:
        line = _line("runner_image", f.CHAIN_A["runner_image"], "new")
        document = f._budget({"w.pub": 100, "w.list": 200},
                             chain={**f.CHAIN_A, "runner_image": "new"})
    f._commit(repo, "raise\n\n" + line + " trailing junk", **{f.BUDGET: json.dumps(document)})
    result = _event(repo, base, "pull_request")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "does not parse" in result.stdout


def test_one_toolchain_line_does_not_declare_another_field(tmp_path: Path) -> None:
    repo, base = f._seeded(tmp_path)
    document = f._budget({"w.pub": 100, "w.list": 200},
                         chain={**f.CHAIN_A, "runner_image": "new", "lock_sha256": "cd" * 32})
    f._commit(repo, "rebaseline\n\n" + _line("runner_image", f.CHAIN_A["runner_image"], "new"),
              **{f.BUDGET: json.dumps(document)})
    result = _event(repo, base, "pull_request")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "toolchain.lock_sha256" in result.stdout


@pytest.mark.parametrize("declare_toolchain", [False, True])
def test_generated_rebaseline_needs_numeric_and_toolchain_lines(
        tmp_path: Path, declare_toolchain: bool) -> None:
    names = ("service.publish", "service.list", "service.versions", "cli.transfer", "cli.text")
    repo = f._repo(tmp_path)
    base = f._commit(repo, "seed", **{f.CODE: "print(1)\n", f.BUDGET: json.dumps(
        f._budget(dict.fromkeys(names, 100)))})
    measurement = tmp_path / "measurement.json"
    measurement.write_text(json.dumps({"counts": dict.fromkeys(names, 300),
                                       "toolchain": {**f.CHAIN_A, "runner_image": "new"}}),
                           encoding="utf-8")
    generated = subprocess.run([sys.executable, str(f._REPO / "scripts/ci/speed.py"),
                                "rebaseline", "--measurements", str(measurement),
                                "--budget", str(repo / f.BUDGET)], capture_output=True,
                               text=True, check=False, timeout=20)
    assert generated.returncode == 0, generated.stdout + generated.stderr
    lines = [f._decl(f"workloads[{json.dumps(name)}]", 100, 300) for name in names]
    if declare_toolchain:
        lines.append(_line("runner_image", f.CHAIN_A["runner_image"], "new"))
    f._git(repo, "add", f.BUDGET)
    f._git(repo, "commit", "-m", "reviewed rebaseline\n\n" + "\n".join(lines))
    result = _event(repo, base, "pull_request")
    assert result.returncode == int(not declare_toolchain), result.stdout + result.stderr
