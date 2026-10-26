"""Tests for the budget-raise gate (scripts/ci/budget_raise.py).

Every case drives a real git repository and the real script, so the git
call sites the gate makes are the ones the fixtures exercise.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts/ci/budget_raise.py"
BUDGET = ".github/speed-budget.json"
ZERO = "0" * 40
CODE = "src/app.py"

CHAIN_A = {"python": "Python 3.15.1", "valgrind": "valgrind-3.22.0",
           "runner_image": "ubuntu24 20261023", "lock_sha256": "ab" * 32}
CHAIN_B = {**CHAIN_A, "lock_sha256": "cd" * 32}


def _budget(workloads: dict, tolerance: float = 1.0, chain: dict | None = None,
            schema: int = 1) -> dict:
    return {"schema_version": schema, "counter": "cachegrind-ir",
            "tolerance_pct": tolerance, "toolchain": dict(chain or CHAIN_A),
            "workloads": workloads}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True)
    return done.stdout.strip()


def _commit(repo: Path, message: str, **files: str | None) -> str:
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "gate@example.com")
    _git(repo, "config", "user.name", "gate")
    return repo


def _seeded(tmp_path: Path, workloads: dict | None = None) -> tuple[Path, str]:
    """A repo whose base commit carries a budget over w1=100, w2=200."""
    repo = _repo(tmp_path)
    budget = json.dumps(_budget(workloads or {"w.pub": 100, "w.list": 200}))
    base = _commit(repo, "seed", **{CODE: "print(1)\n"}, **{BUDGET: budget})
    return repo, base


def _decl(key: str, was: object, to: object) -> str:
    return f"Budget-Raise: {BUDGET} {key} {was} -> {to}"


def _declared_raise_message(key: str, was: object, to: object) -> str:
    return f"raise\n\n{_decl(key, was, to)}\n"


def _toolchain_message(before: dict, after: dict) -> str:
    lines = []
    for field in sorted(set(before) | set(after)):
        if field in before and field in after and before[field] == after[field]:
            continue
        old = json.dumps(before[field]) if field in before else "absent"
        new = json.dumps(after[field]) if field in after else "absent"
        lines.append(_decl(f"toolchain.{field}", old, new))
    return "rebaseline\n\n" + "\n".join(lines)


def _run(repo: Path, *flags: str, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = {**os.environ, "DOCS_HUB_REF_NAME": "main", "DOCS_HUB_DEFAULT_BRANCH": "main"}
    if env:
        merged.update(env)
    return subprocess.run([sys.executable, str(_SCRIPT), *flags], cwd=repo,
                          capture_output=True, text=True, env=merged,
                          check=False)


def _pull(repo: Path, base: str, head: str) -> subprocess.CompletedProcess:
    return _run(repo, "--base", base, "--head", head, "--event",
                "pull_request")


def test_a_declared_raise_on_a_budget_only_commit_passes(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 300),
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "not relaxed" in done.stdout


def test_a_raise_without_a_declaration_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "raise without saying so",
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert 'workloads["w.pub"]' in done.stdout
    assert "raised; it may only fall" in done.stdout


def test_a_declaration_on_an_impure_commit_is_not_consumable(
        tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 300),
                   **{CODE: "print(2)\n"},
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "carries declaration lines but also touches src/app.py" in done.stdout
    assert "raised; it may only fall" in done.stdout


def test_a_duplicate_declaration_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    line = _decl('workloads["w.pub"]', 100, 300)
    message = f"raise\n\n{line}\n{line}\n"
    head = _commit(repo, message,
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "does not match a raise in this diff" in done.stdout


@pytest.mark.parametrize("line", [
    'Budget-Raise: .github/speed-budget.json workloads["w.pub"] 100 ->',
    "Budget-Raise: .github/speed-budget.json",
    f"Budget-Raise: {BUDGET} tolerance_pct 1.0 -> NaN",
])
def test_a_malformed_declaration_line_fails(tmp_path: Path, line: str) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, f"raise\n\n{line}\n",
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200},
                                                 tolerance=5.0))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "does not parse" in done.stdout


def test_a_declaration_whose_value_mismatches_the_diff_fails(
        tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 250),
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "declaration 100 -> 250 does not match" in done.stdout


def test_a_declared_tolerance_raise_passes(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message("tolerance_pct", "1.0", "5.0"),
                   **{BUDGET: json.dumps(_budget({"w.pub": 100, "w.list": 200},
                                                 tolerance=5.0))})
    done = _pull(repo, base, head)
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_undeclared_toolchain_change_on_a_budget_only_commit_fails(
        tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "relock",
                   **{BUDGET: json.dumps(_budget({"w.pub": 100, "w.list": 200},
                                                 chain=CHAIN_B))})
    done = _pull(repo, base, head)
    assert done.returncode == 1, done.stdout + done.stderr


def test_a_toolchain_change_on_a_mixed_commit_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "relock while touching code", **{CODE: "print(2)\n"},
                   **{BUDGET: json.dumps(_budget({"w.pub": 100, "w.list": 200},
                                                 chain=CHAIN_B))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "toolchain.lock_sha256" in done.stdout
    assert "without a budget-only commit" in done.stdout


def test_a_budget_absent_at_base_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "seed without a budget", **{CODE: "print(1)\n"})
    head = _commit(repo, "seed the budget",
                   **{BUDGET: json.dumps(_budget({"w.pub": 900, "w.list": 900}))})
    done = _pull(repo, base, head)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule"])
def test_an_event_without_a_window_skips(tmp_path: Path, event: str) -> None:
    repo, base = _seeded(tmp_path)
    done = _run(repo, "--base", base, "--head", "HEAD", "--event", event)
    assert done.returncode == 0
    assert "skipping" in done.stdout


def test_an_all_zero_before_skips(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    done = _run(repo, "--base", base, "--head", "HEAD", "--event", "push",
                "--before", ZERO)
    assert done.returncode == 0
    assert "skipping" in done.stdout


def test_a_non_ancestor_before_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    _git(repo, "checkout", "-b", "side", base)
    other = _commit(repo, "unrelated branch work", **{CODE: "print(9)\n"})
    _git(repo, "checkout", "main")
    head = _commit(repo, "main work", **{CODE: "print(3)\n"})
    done = _run(repo, "--base", base, "--head", head, "--event", "push",
                "--before", other)
    assert done.returncode == 2
    assert "not an ancestor" in done.stderr


def test_added_and_removed_workloads_on_a_budget_only_commit_pass(
        tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "reshape the workloads",
                   **{BUDGET: json.dumps(_budget({"w.pub": 100, "w3": 300}))})
    done = _pull(repo, base, head)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_push_window_judges_before_head(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 300),
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _run(repo, "--base", base, "--head", head, "--event", "push",
                "--before", base)
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_unknown_revision_is_a_usage_error(tmp_path: Path) -> None:
    repo, _ = _seeded(tmp_path)
    done = _pull(repo, "nosuchrev", "HEAD")
    assert done.returncode == 2
    assert "unknown revision" in done.stderr


def test_a_shallow_clone_refuses(tmp_path: Path) -> None:
    repo, _ = _seeded(tmp_path)
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--depth", "1", f"file://{repo}", str(shallow))
    tip = _git(repo, "rev-parse", "HEAD")
    done = _pull(shallow, tip, tip)
    assert done.returncode == 2
    assert "shallow" in done.stderr


def test_a_deleted_budget_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "delete the budget", **{BUDGET: None})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "was deleted" in done.stdout


def test_a_schema_version_change_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "bump the schema",
                   **{BUDGET: json.dumps(_budget({"w.pub": 100, "w.list": 200},
                                                 schema=2))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "schema_version changed" in done.stdout


def test_a_declaration_naming_another_document_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    message = "tighten\n\nBudget-Raise: .github/other.json k 1 -> 2\n"
    head = _commit(repo, message,
                   **{BUDGET: json.dumps(_budget({"w.pub": 100, "w.list": 150}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "not declarable" in done.stdout


def test_a_declaration_for_a_key_that_did_not_rise_is_a_leftover(
        tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 300),
                   **{BUDGET: json.dumps(_budget({"w.pub": 50, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "does not match a raise in this diff" in done.stdout


def test_a_lowered_count_needs_no_declaration(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "tighten",
                   **{BUDGET: json.dumps(_budget({"w.pub": 50, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_pure_declaration_cannot_authorize_a_mixed_workload_raise(
        tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    _commit(repo, "raise while touching code", **{CODE: "print(2)\n"},
            **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 300),
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 150}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "without a budget-only commit" in done.stdout


@pytest.mark.parametrize("literal", ["true", "Infinity", "NaN"])
def test_a_non_finite_or_boolean_budget_value_fails(tmp_path: Path,
                                                    literal: str) -> None:
    repo, base = _seeded(tmp_path)
    poisoned = json.dumps(_budget({"w.pub": 100, "w.list": 200})).replace(
        '"w.pub": 100', f'"w.pub": {literal}')
    head = _commit(repo, "poison one count", **{BUDGET: poisoned})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert "not a finite number" in done.stdout


def test_a_same_equality_boolean_budget_value_fails(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path, {"w.pub": 1, "w.list": 200})
    head = _commit(repo, "replace one with true",
                   **{BUDGET: json.dumps(_budget({"w.pub": True, "w.list": 200}))})
    done = _pull(repo, base, head)
    assert done.returncode == 1
    assert 'workloads["w.pub"]' in done.stdout
    assert "not a finite number" in done.stdout


def test_the_environment_carries_the_revisions(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 300),
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _run(repo, env={"DOCS_HUB_BASE_SHA": base,
                           "DOCS_HUB_HEAD_SHA": head,
                           "DOCS_HUB_EVENT_NAME": "pull_request"})
    assert done.returncode == 0, done.stdout + done.stderr


def test_flags_overrule_the_environment(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, "raise without saying so",
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _run(repo, "--base", base, "--head", head, "--event",
                "pull_request",
                env={"DOCS_HUB_EVENT_NAME": "workflow_dispatch"})
    assert done.returncode == 1
    assert "raised; it may only fall" in done.stdout


def test_a_push_run_with_an_empty_base_judges_the_window(tmp_path: Path) -> None:
    repo, base = _seeded(tmp_path)
    head = _commit(repo, _declared_raise_message('workloads["w.pub"]', 100, 300),
                   **{BUDGET: json.dumps(_budget({"w.pub": 300, "w.list": 200}))})
    done = _run(repo, "--base", "", "--head", head, "--event", "push",
                "--before", base)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"not relaxed against base {base}" in done.stdout


def test_a_push_run_without_a_before_refuses(tmp_path: Path) -> None:
    repo, _ = _seeded(tmp_path)
    done = _run(repo, "--head", "HEAD", "--event", "push")
    assert done.returncode == 2
    assert "needs the commit it came from" in done.stderr


def test_an_unlabelled_run_with_no_inputs_refuses(tmp_path: Path) -> None:
    repo, _ = _seeded(tmp_path)
    done = _run(repo, env={"DOCS_HUB_BASE_SHA": "", "DOCS_HUB_HEAD_SHA": "",
                           "DOCS_HUB_BEFORE_SHA": "", "DOCS_HUB_EVENT_NAME": ""})
    assert done.returncode == 2
    assert "not wired" in done.stderr


def test_a_dispatch_run_with_a_head_but_no_base_skips(tmp_path: Path) -> None:
    repo, _ = _seeded(tmp_path)
    done = _run(repo, "--event", "workflow_dispatch", "--head", "HEAD",
                env={"DOCS_HUB_BASE_SHA": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout


@pytest.mark.parametrize("declaration", [False, True],
                         ids=["mixed-toolchain", "impure-declaration"])
def test_a_synthetic_merge_cannot_sanction_mixed_work(
        tmp_path: Path, declaration: bool) -> None:
    repo, base = _seeded(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    tip = _commit(repo, "mixed branch work", **{CODE: "print(2)\n"},
                  **{BUDGET: json.dumps(_budget(
                      {"w.pub": 300 if declaration else 100, "w.list": 200},
                      chain=CHAIN_A if declaration else CHAIN_B))})
    _git(repo, "checkout", "main")
    base = _commit(repo, "advance base", **{"other.py": "print(3)\n"})
    message = (_declared_raise_message('workloads["w.pub"]', 100, 300)
               if declaration else "synthetic merge")
    _git(repo, "merge", "--no-ff", tip, "-m", message)
    done = _pull(repo, base, "HEAD")
    assert done.returncode == 1, done.stdout + done.stderr
    if declaration:
        assert "does not match a raise in this diff" in done.stdout
        assert "raised; it may only fall" in done.stdout
    else:
        assert "toolchain.lock_sha256" in done.stdout
        assert "without a budget-only commit" in done.stdout


@pytest.mark.parametrize("pure_steps", [(True, False), (False, True), (True, True)],
                         ids=["pure-then-mixed", "mixed-then-pure", "pure-chain"])
def test_toolchain_authorization_follows_the_exact_transitions(
        tmp_path: Path, pure_steps: tuple[bool, bool]) -> None:
    repo, base = _seeded(tmp_path)
    previous = CHAIN_A
    for index, pure in enumerate(pure_steps, start=1):
        chain = {**CHAIN_A, "lock_sha256": ("cd" if index == 1 else "ef") * 32}
        files = {BUDGET: json.dumps(_budget({"w.pub": 100, "w.list": 200},
                                            chain=chain))}
        if not pure:
            files[CODE] = f"print({index + 1})\n"
        _commit(repo, _toolchain_message(previous, chain) if pure else "mixed relock", **files)
        previous = chain
    done = _pull(repo, base, "HEAD")
    assert done.returncode == (0 if all(pure_steps) else 1), done.stdout + done.stderr
    if not all(pure_steps):
        assert "toolchain.lock_sha256" in done.stdout
        assert "without a budget-only commit" in done.stdout
