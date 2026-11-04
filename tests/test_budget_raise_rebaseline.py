"""A toolchain mismatch is exempt only after a declared, reviewed rebaseline."""
# pylint: disable=protected-access  # Share the real Git fixture and CLI boundary.
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

import test_budget_raise as f
import test_speed_gate as g
from test_budget_raise_matrix import _event


@pytest.fixture(name="speed")
def _speed() -> ModuleType:
    return g._load("ci_speed", g._SCRIPT)


@pytest.fixture(name="policy")
def _policy() -> ModuleType:
    return g._load("ci_speed_budget", g._POLICY)


def _line(field: str, before: object, after: object) -> str:
    return f._decl(f"toolchain.{field}", json.dumps(before), json.dumps(after))


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("field", ["python", "valgrind", "image", "lock_sha256"])
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
    field = "compiler" if shape == "add" else "image"
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
        {"w.pub": 100, "w.list": 200}, chain={**f.CHAIN_A, "image": "new"}))})
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
        line = _line("image", f.CHAIN_A["image"], "new")
        document = f._budget({"w.pub": 100, "w.list": 200},
                             chain={**f.CHAIN_A, "image": "new"})
    f._commit(repo, "raise\n\n" + line + " trailing junk", **{f.BUDGET: json.dumps(document)})
    result = _event(repo, base, "pull_request")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "does not parse" in result.stdout


def test_one_toolchain_line_does_not_declare_another_field(tmp_path: Path) -> None:
    repo, base = f._seeded(tmp_path)
    document = f._budget({"w.pub": 100, "w.list": 200},
                         chain={**f.CHAIN_A, "image": "new", "lock_sha256": "cd" * 32})
    f._commit(repo, "rebaseline\n\n" + _line("image", f.CHAIN_A["image"], "new"),
              **{f.BUDGET: json.dumps(document)})
    result = _event(repo, base, "pull_request")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "toolchain.lock_sha256" in result.stdout


_NAMES = ("service.publish", "service.list", "service.versions", "cli.transfer", "cli.text")


def _rebaseline(repo: Path, tmp_path: Path, counts: dict,
                chain: dict) -> subprocess.CompletedProcess:
    measurement = tmp_path / "measurement.json"
    measurement.write_text(json.dumps({"counts": counts, "toolchain": chain}), encoding="utf-8")
    return subprocess.run([sys.executable, str(f._REPO / "scripts/ci/speed.py"),
                           "rebaseline", "--measurements", str(measurement),
                           "--budget", str(repo / f.BUDGET)], capture_output=True,
                          text=True, check=False, timeout=20)


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("keep_toolchain_lines", [True, False])
@pytest.mark.parametrize("old_image", [pytest.param(f.CHAIN_A["image"], id="string_image"),
                                       pytest.param(None, id="null_image")])
def test_the_printed_declarations_are_the_commit_body_the_gate_accepts(
        tmp_path: Path, event: str, keep_toolchain_lines: bool, old_image: str | None) -> None:
    repo = f._repo(tmp_path)
    base = f._commit(repo, "seed", **{f.CODE: "print(1)\n", f.BUDGET: json.dumps(
        f._budget(dict(zip(_NAMES, (100, 400, 100, 100, 100))),
                  chain={**f.CHAIN_A, "image": old_image}))})
    chain = {**f.CHAIN_A, "image": 'new "tool" -> image', "lock_sha256": "cd" * 32}
    generated = _rebaseline(repo, tmp_path, dict(zip(_NAMES, (300, 200, 100, 250, 100))), chain)
    assert generated.returncode == 0, generated.stdout + generated.stderr
    lines = generated.stdout.splitlines()
    assert len(lines) == 4 and all(line.startswith("Budget-Raise: ") for line in lines)
    body = generated.stdout if keep_toolchain_lines else "".join(
        line + "\n" for line in lines if "toolchain." not in line)
    f._git(repo, "add", f.BUDGET)
    f._git(repo, "commit", "-m", "reviewed rebaseline\n\n" + body)
    result = _event(repo, base, event)
    assert result.returncode == int(not keep_toolchain_lines), result.stdout + result.stderr


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_replacing_a_rebaseline_after_a_second_lock_update(
        tmp_path: Path, event: str) -> None:
    repo = f._repo(tmp_path)
    locks = ("base lock\n", "first lock\n", "second lock\n")
    chains = [{**f.CHAIN_A, "lock_sha256": hashlib.sha256(lock.encode()).hexdigest()}
              for lock in locks]
    counts = dict.fromkeys(_NAMES, 100)
    base = f._commit(repo, "seed", **{f.BUDGET: json.dumps(f._budget(counts, chain=chains[0])),
                                      "requirements-speed.txt": locks[0]})
    f._commit(repo, "first lock update", **{"requirements-speed.txt": locks[1]})
    first = _rebaseline(repo, tmp_path, {**counts, "service.publish": 120}, chains[1])
    assert first.returncode == 0, first.stdout + first.stderr
    assert f._decl('workloads["service.publish"]', 100, 120) in first.stdout.splitlines()
    f._git(repo, "add", f.BUDGET)
    f._git(repo, "commit", "-m", "first rebaseline\n\n" + first.stdout)
    first_sha = f._git(repo, "rev-parse", "HEAD")
    first_gate = _event(repo, base, event)
    assert first_gate.returncode == 0, first_gate.stdout + first_gate.stderr

    branch = f._git(repo, "branch", "--show-current")
    f._git(repo, "checkout", "-b", "side", base)
    side = f._commit(repo, "master code change", **{"other.py": "pass\n"})
    f._git(repo, "checkout", branch)
    f._git(repo, "merge", "--no-ff", "side", "-m", "merge master")
    f._commit(repo, "second lock update", **{"requirements-speed.txt": locks[2]})
    f._git(repo, "rebase", "--rebase-merges", "--onto", f"{first_sha}^", first_sha)
    assert side in f._git(repo, "rev-list", "HEAD").splitlines()
    second = _rebaseline(repo, tmp_path, {**counts, "service.publish": 140}, chains[2])
    assert second.returncode == 0, second.stdout + second.stderr
    f._git(repo, "add", f.BUDGET)
    f._git(repo, "commit", "-m", "second rebaseline\n\n" + second.stdout)
    result = _event(repo, base, event)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f._decl('workloads["service.publish"]', 100, 140) in second.stdout.splitlines()
    assert f._git(repo, "log", "--first-parent", "--format=%s", f"{base}..HEAD").splitlines() == [
        "second rebaseline", "second lock update", "merge master", "first lock update"]
    check = subprocess.run([sys.executable, str(f._REPO / "scripts/ci/speed.py"),
                            "check", "--refuse-mismatch", "--measurements",
                            str(tmp_path / "measurement.json"), "--budget", str(repo / f.BUDGET)],
                           capture_output=True, text=True, check=False, timeout=20)
    assert check.returncode == 0, check.stdout + check.stderr


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_an_added_workload_needs_no_printed_declaration(
        tmp_path: Path, event: str) -> None:
    repo = f._repo(tmp_path)
    base = f._commit(repo, "seed", **{f.CODE: "print(1)\n", f.BUDGET: json.dumps(
        f._budget(dict.fromkeys(_NAMES[:-1], 100)))})
    generated = _rebaseline(repo, tmp_path, dict.fromkeys(_NAMES, 100), f.CHAIN_A)
    assert generated.returncode == 0 and generated.stdout == "", generated.stdout + generated.stderr
    f._git(repo, "add", f.BUDGET)
    f._git(repo, "commit", "-m", "reviewed rebaseline")
    result = _event(repo, base, event)
    assert result.returncode == 0, result.stdout + result.stderr


def _declared(policy: ModuleType, old: dict | None, **new: object) -> list[str]:
    return policy.declarations(old, g._budget(**new))


def test_declarations_name_a_raised_count_and_skip_the_rest(policy: ModuleType) -> None:
    old = g._budget({"service.publish": 1000, "service.list": 2000, "cli.text": 500})
    new = {"service.publish": 1500, "service.list": 1000, "cli.text": 500, "cli.transfer": 70}
    assert _declared(policy, old, workloads=new) == [
        f._decl('workloads["service.publish"]', "1000", "1500")]


@pytest.mark.parametrize("was, now, rendered", [
    ("old", "new", ('"old"', '"new"')),
    (None, "new", ("null", '"new"')),
    ("old", None, ('"old"', "null")),
    ("a -> b", 'say "x"', ('"a -> b"', '"say \\"x\\""')),
])
def test_declarations_render_each_changed_toolchain_leaf_as_json(
        policy: ModuleType, was: object, now: object, rendered: tuple[str, str]) -> None:
    old = g._budget(toolchain={**g.TOOLCHAIN, "image": was})
    assert _declared(policy, old, toolchain={**g.TOOLCHAIN, "image": now}) == [
        f._decl("toolchain.image", *rendered)]


def test_declarations_tell_an_absent_leaf_from_a_null_one(policy: ModuleType) -> None:
    short = {key: value for key, value in g.TOOLCHAIN.items() if key != "image"}
    assert _declared(policy, g._budget(toolchain=short), toolchain=g.TOOLCHAIN) == [
        f._decl("toolchain.image", "absent", json.dumps(g.TOOLCHAIN["image"]))]
    assert _declared(policy, g._budget(), toolchain=short) == [
        f._decl("toolchain.image", json.dumps(g.TOOLCHAIN["image"]), "absent")]
    assert _declared(policy, g._budget(toolchain={**g.TOOLCHAIN, "image": None}),
                     toolchain=short) == [f._decl("toolchain.image", "null", "absent")]


def test_declarations_list_toolchain_leaves_then_counts_and_nothing_else(
        policy: ModuleType) -> None:
    assert _declared(policy, g._budget(), tolerance_pct=99, toolchain=g.MEASURED_CHAIN,
                     workloads={"service.publish": 1001, "service.list": 2000}) == [
        f._decl("toolchain.lock_sha256", json.dumps(g.TOOLCHAIN["lock_sha256"]),
                json.dumps(g.MEASURED_CHAIN["lock_sha256"])),
        f._decl('workloads["service.publish"]', "1000", "1001")]
    assert _declared(policy, g._budget()) == []


def test_declarations_for_a_seed_are_empty(policy: ModuleType) -> None:
    assert _declared(policy, None, toolchain=g.MEASURED_CHAIN) == []


def test_rebaseline_prints_the_declarations_alone_on_stdout_and_its_status_on_stderr(
        speed: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    budget, measurement = tmp_path / "budget.json", tmp_path / "m.json"
    counts = dict.fromkeys(speed.WORKLOADS, 1000)
    budget.write_text(json.dumps(g._budget(counts)), encoding="utf-8")
    measurement.write_text(json.dumps(g._measurement(
        {**counts, "service.list": 1200, "cli.text": 900}, g.MEASURED_CHAIN)), encoding="utf-8")
    argv = ["rebaseline", "--measurements", str(measurement), "--budget", str(budget)]
    assert speed.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "\n".join([
        f._decl("toolchain.lock_sha256", json.dumps(g.TOOLCHAIN["lock_sha256"]),
                json.dumps(g.MEASURED_CHAIN["lock_sha256"])),
        f._decl('workloads["service.list"]', "1000", "1200")]) + "\n"
    assert captured.err == f"rebaselined {budget} from {measurement}\n"
    budget.unlink()
    assert speed.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("rebaselined ")
