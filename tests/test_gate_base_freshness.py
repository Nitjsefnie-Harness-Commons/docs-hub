"""Execute the freshness guard against tracked workflows and local origins."""
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts/ci/gate_base_freshness.py"


@pytest.fixture(name="gate")
def _gate() -> ModuleType:
    assert _SCRIPT.is_file(), "The base-freshness workflow needs its executable guard"
    spec = importlib.util.spec_from_file_location("gate_base_freshness", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=15).stdout.strip()


def _commit_file(root: Path, path: str, text: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(root, "add", path)
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", f"Update {path}")
    return _git(root, "rev-parse", "HEAD")


def _init(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-qb", "master")
    _git(root, "config", "user.name", "CI test")
    _git(root, "config", "user.email", "ci@example.invalid")


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_SCRIPT), "--root", str(root), *args],
                          check=False, capture_output=True, text=True, timeout=20)


def _needs(root: Path) -> set[str]:
    workflow: dict[str, Any] = yaml.safe_load(
        (root / ".github/workflows/ci-gate.yml").read_text(encoding="utf-8"))
    return set(workflow["jobs"]["aggregate"]["needs"])


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    _init(origin)
    _commit_file(origin, ".github/workflows/ci-gate.yml", """jobs:
  lint:
    uses: ./.github/workflows/tests.yml
  aggregate:
    needs: [lint]
    steps:
      - run: echo aggregate-only.txt
""")
    _commit_file(origin, ".github/workflows/tests.yml", """jobs:
  check:
    steps:
      - run: python3 checker.py inputs/ .config . ${{ env.IGNORED }}
""")
    for path in ("checker.py", ".config", "inputs/data.txt", "README.md",
                 "aggregate-only.txt", "expression-only.txt"):
        _commit_file(origin, path, "initial\n")
    head = tmp_path / "head"
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(origin), str(head))
    _git(head, "config", "user.name", "CI test")
    _git(head, "config", "user.email", "ci@example.invalid")
    return origin, head


def test_guard_is_executable() -> None:
    result = _run(_REPO, "--print-paths")
    assert result.returncode == 0, result.stdout + result.stderr


def test_print_paths_covers_this_repos_named_gate_inputs(gate: ModuleType) -> None:
    result = _run(_REPO, "--print-paths")
    assert result.returncode == 0, result.stdout + result.stderr
    paths = set(result.stdout.splitlines())
    workflows = set(_git(_REPO, "ls-files", ".github/workflows").splitlines())
    assert workflows <= paths
    assert {".pylintrc", "pyrightconfig.json", "requirements.txt", "requirements-dev.txt",
            "requirements-test.txt", "requirements-audit.txt", "requirements-zizmor.txt",
            "requirements-build.txt"} <= paths
    assert paths.intersection({"package.json", "package-lock.json"})
    assert not any("tarball" in path or path.endswith(".tar.gz") for path in paths)
    assert gate.required_jobs(gate.read_workflows(_REPO)) == _needs(_REPO)


def test_removing_folded_job_shrinks_derivation(tmp_path: Path, gate: ModuleType) -> None:
    copy = tmp_path / "copy"
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(_REPO), str(copy))
    _git(copy, "config", "user.name", "CI test")
    _git(copy, "config", "user.email", "ci@example.invalid")
    before = set(gate.gate_paths(copy))
    path = ".github/workflows/ci-gate.yml"
    text = (copy / path).read_text(encoding="utf-8")
    _commit_file(copy, path, text.replace("tests, lint, types,", "tests, types,", 1))
    after = set(gate.gate_paths(copy))
    assert after < before
    assert ".pylintrc" in before - after
    assert gate.required_jobs(gate.read_workflows(copy)) == _needs(copy)


def test_unknown_folded_job_refuses(tmp_path: Path, gate: ModuleType) -> None:
    _, head = _fixture_repo(tmp_path)
    _commit_file(head, ".github/workflows/ci-gate.yml", """jobs:
  aggregate:
    needs: [missing]
""")
    with pytest.raises(gate.GateError, match="missing"):
        gate.gate_paths(head)
    result = _run(head, "--print-paths")
    assert result.returncode == 1
    assert "missing" in result.stderr


def test_fresh_base_is_green(tmp_path: Path, gate: ModuleType) -> None:
    _, head = _fixture_repo(tmp_path)
    result = _run(head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every commit on master" in result.stdout
    paths = set(gate.gate_paths(head))
    assert {"checker.py", ".config", "inputs/data.txt"} <= paths
    assert "README.md" not in paths
    assert "aggregate-only.txt" not in paths
    assert "expression-only.txt" not in paths


def test_workflow_change_is_stale(tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    path = ".github/workflows/tests.yml"
    sha = _commit_file(origin, path, (origin / path).read_text(encoding="utf-8") + "# changed\n")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert sha in result.stdout and path in result.stdout
    assert f"Update {path}" in result.stdout
    assert "Rebase onto master and push again" in result.stdout
    assert gate.BASE_BRANCH == "master"


def test_readme_only_change_is_green(tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    sha = _commit_file(origin, "README.md", "unrelated\n")
    result = _run(head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sha not in result.stdout
    assert "README.md" not in gate.gate_paths(head)


def test_only_gate_input_commit_is_reported(tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    unrelated = _commit_file(origin, "README.md", "unrelated\n")
    relevant = _commit_file(origin, "inputs/data.txt", "changed input\n")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert relevant in result.stdout and "inputs/data.txt" in result.stdout
    assert unrelated not in result.stdout
    assert "README.md" not in result.stdout
    assert "inputs/data.txt" in gate.gate_paths(head)


@pytest.mark.parametrize("text", [
    "jobs: {lint: {steps: []}}\n",
    "jobs:\n  lint:\n    steps: [{run: echo checker.py}]\n",
    "jobs:\n  lint:\n    steps:\n      - {run: echo checker.py}\n",
    "jobs:\n  lint:\n    steps: []\njobs:\n  lint:\n    steps: []\n",
])
def test_unmodeled_workflow_refuses(tmp_path: Path, gate: ModuleType, text: str) -> None:
    _, head = _fixture_repo(tmp_path)
    _commit_file(head, ".github/workflows/other.yml", text)
    with pytest.raises(gate.GateError):
        gate.gate_paths(head)
    result = _run(head, "--print-paths")
    assert result.returncode == 1
    assert "other.yml" in result.stderr


def test_same_job_in_multiple_workflows_is_unioned(tmp_path: Path, gate: ModuleType) -> None:
    _, head = _fixture_repo(tmp_path)
    _commit_file(head, ".github/workflows/second.yml", """jobs:
  lint:
    steps:
      - run: cat expression-only.txt
""")
    assert {"expression-only.txt", "checker.py"} <= set(gate.gate_paths(head))


@pytest.mark.parametrize("needs", ["${{ inputs.jobs }}", "{lint: true}", "[]", "[aggregate]"])
def test_unmodeled_or_empty_needs_refuses(
        tmp_path: Path, gate: ModuleType, needs: str) -> None:
    _, head = _fixture_repo(tmp_path)
    _commit_file(head, ".github/workflows/ci-gate.yml",
                 f"jobs:\n  aggregate:\n    needs: {needs}\n")
    with pytest.raises(gate.GateError):
        gate.gate_paths(head)


def test_resolution_strips_expressions_and_expands_directories(gate: ModuleType) -> None:
    files = (".config", "inputs/one", "inputs/two", "expression-only.txt")
    found = {path for token in gate.candidates(
        "cat .config ./inputs/ . ${{ 'expression-only.txt' }} missing.tar.gz")
             for path in gate.resolve(token, files)}
    assert found == {".config", "inputs/one", "inputs/two"}


def test_comparison_refuses_empty_and_oversized_pathsets(gate: ModuleType) -> None:
    with pytest.raises(gate.GateError, match="empty"):
        gate.stale_commits(_REPO, "HEAD", "HEAD", [])
    with pytest.raises(gate.GateError, match="argument list"):
        gate.stale_commits(_REPO, "HEAD", "HEAD", ["é" * 32768])


def test_shallow_checkout_fetches_full_base_history(tmp_path: Path, gate: ModuleType) -> None:
    origin, _ = _fixture_repo(tmp_path)
    carried = _git(origin, "rev-parse", "HEAD")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth=1", origin.as_uri(), str(shallow))
    relevant = _commit_file(origin, "checker.py", "new input\n")
    result = _run(shallow)
    assert result.returncode == 1, result.stdout + result.stderr
    assert relevant in result.stdout
    assert carried not in result.stdout
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "false"
    assert "checker.py" in gate.gate_paths(shallow)


def test_unreachable_origin_refuses(tmp_path: Path, gate: ModuleType) -> None:
    _, head = _fixture_repo(tmp_path)
    _git(head, "remote", "set-url", "origin", str(tmp_path / "absent"))
    with pytest.raises(gate.GateError, match="fetch origin/master"):
        gate.fetch_base(head)
    result = _run(head)
    assert result.returncode == 1
    assert "fetch origin/master" in result.stderr


def test_unicode_workflow_is_watched(tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    path = ".github/workflows/über.yml"
    text = "jobs:\n  extra:\n    steps:\n      - run: echo extra\n"
    _commit_file(head, path, text)
    _git(origin, "fetch", "-q", str(head), "master")
    _git(origin, "merge", "--ff-only", "FETCH_HEAD")
    assert path in gate.gate_paths(head)
    sha = _commit_file(origin, path, text + "# changed\n")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert sha in result.stdout


def test_block_needs_and_local_action_inputs(tmp_path: Path, gate: ModuleType) -> None:
    _, head = _fixture_repo(tmp_path)
    _commit_file(head, ".github/workflows/ci-gate.yml", """jobs:
  lint:
    steps:
      - uses: ./inputs
  aggregate:
    needs:
      - lint
""")
    assert "inputs/data.txt" in gate.gate_paths(head)
    assert gate.required_jobs(gate.read_workflows(head)) == {"lint"}


def test_merge_row_names_its_limit(tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    _git(origin, "checkout", "-qb", "side")
    side = _commit_file(origin, "checker.py", "side input\n")
    _git(origin, "checkout", "-q", "master")
    main = _commit_file(origin, "inputs/data.txt", "master input\n")
    _git(origin, "-c", "core.hooksPath=/dev/null", "merge", "-q", "--no-ff", "side", "-m",
         "Merge gate inputs")
    merge = _git(origin, "rev-parse", "HEAD")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert all(sha in result.stdout for sha in (side, main, merge))
    assert "a merge commit" in result.stdout
    assert "checker.py" in result.stdout and "inputs/data.txt" in result.stdout
    assert gate.stale_commits(head, "HEAD", "origin/master", ["checker.py", "inputs/data.txt"])


def test_called_aggregate_input_is_stale_but_root_aggregate_input_is_not(
        tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    _commit_file(origin, ".github/workflows/tests.yml", """jobs:
  aggregate:
    steps:
      - run: cat inputs/sentinel.txt
""")
    _commit_file(origin, "inputs/sentinel.txt", "initial\n")
    _git(head, "fetch", "-q", "origin")
    _git(head, "merge", "--ff-only", "origin/master")
    root_only = _commit_file(origin, "aggregate-only.txt", "root aggregate input\n")
    result = _run(head)
    assert result.returncode == 0, result.stdout + result.stderr
    called = _commit_file(origin, "inputs/sentinel.txt", "called aggregate input\n")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert called in result.stdout and "inputs/sentinel.txt" in result.stdout
    assert root_only not in result.stdout and "aggregate-only.txt" not in result.stdout
    paths = gate.gate_paths(head)
    assert "inputs/sentinel.txt" in paths
    assert "aggregate-only.txt" not in paths


@pytest.mark.parametrize("reference", [
    '"./inputs"', "'./inputs'", "|\n          ./inputs", ">-\n          ./inputs",
])
def test_unmodeled_local_action_scalar_refuses(
        tmp_path: Path, gate: ModuleType, reference: str) -> None:
    _, head = _fixture_repo(tmp_path)
    _commit_file(head, ".github/workflows/ci-gate.yml", f"""jobs:
  lint:
    steps:
      - uses: {reference}
  aggregate:
    needs: [lint]
""")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    with pytest.raises(gate.GateError):
        gate.gate_paths(head)


def test_comparison_accepts_pathset_at_byte_ceiling(
        gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = "é" * 32767 + "a"
    forwarded: list[str] = []

    def git_boundary(root: Path, *arguments: str, what: str) -> str:
        assert root == _REPO and what
        assert arguments[:5] == ("log", "HEAD..HEAD", "--name-only", "--format=%x00%H%x09%s",
                                 "--")
        forwarded.extend(arguments[5:])
        return ""

    monkeypatch.setattr(gate, "git", git_boundary)
    assert gate.stale_commits(_REPO, "HEAD", "HEAD", [candidate]) == []
    assert forwarded == [candidate]


@pytest.mark.parametrize("text", [
    "jobs:\n  check:\njobs:\n  another:\n",
    "jobs:\n  check:\n    steps:\n      - run: [cat, inputs/data.txt]\n",
    "jobs:\n  check:\n    steps:\n      - uses: {path: ./inputs}\n",
])
def test_workflow_refusal_edges(
        tmp_path: Path, gate: ModuleType, text: str) -> None:
    _, head = _fixture_repo(tmp_path)
    _commit_file(head, ".github/workflows/edge.yml", text)
    result = _run(head, "--print-paths")
    assert result.returncode == 1, result.stdout + result.stderr
    with pytest.raises(gate.GateError):
        gate.gate_paths(head)


def test_merged_checkout_of_diverged_head_is_stale(
        tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    pr_sha = _commit_file(head, "README.md", "pr-only change\n")
    base_sha = _commit_file(origin, "inputs/data.txt", "new gate input\n")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert base_sha in result.stdout and "inputs/data.txt" in result.stdout
    assert _git(head, "rev-list", "--count", f"{pr_sha}..origin/master") == "1"
    _git(head, "fetch", "-q", "origin")
    _git(head, "checkout", "-q", "--detach", "origin/master")
    _git(head, "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "-q", "-m",
         "synthetic PR merge", pr_sha)
    # A merge of base and head contains master by construction, so the
    # default spelling cannot name the base commit there; that is why the
    # workflow passes the pull request's head sha instead of relying on the
    # checked-out HEAD.
    result = _run(head, "--head", pr_sha)
    assert result.returncode == 1, result.stdout + result.stderr
    assert base_sha in result.stdout and "inputs/data.txt" in result.stdout
    assert pr_sha not in result.stdout
    assert "Rebase onto master and push again" in result.stdout
    assert gate.stale_commits(head, pr_sha, "origin/master", ["inputs/data.txt"])


def test_workflow_added_on_master_is_stale(tmp_path: Path) -> None:
    origin, head = _fixture_repo(tmp_path)
    path = ".github/workflows/new.yml"
    sha = _commit_file(origin, path,
                       "jobs:\n  extra:\n    steps:\n      - run: echo extra\n")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert sha in result.stdout and path in result.stdout


def test_input_added_on_master_is_stale(tmp_path: Path) -> None:
    origin, head = _fixture_repo(tmp_path)
    sha = _commit_file(origin, "inputs/new.txt", "new named-directory input\n")
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert sha in result.stdout and "inputs/new.txt" in result.stdout


def test_base_added_fold_and_named_input_are_stale(
        tmp_path: Path, gate: ModuleType) -> None:
    origin, head = _fixture_repo(tmp_path)
    workflow = ".github/workflows/extra.yml"
    input_sha = _commit_file(origin, "inputs/extra.txt", "extra gate input\n")
    extra = _commit_file(origin, workflow,
                         "jobs:\n  extra:\n    steps:\n      - run: cat inputs/extra.txt\n")
    gate_path = ".github/workflows/ci-gate.yml"
    fold = _commit_file(origin, gate_path,
                        (origin / gate_path).read_text(encoding="utf-8")
                        .replace("needs: [lint]", "needs: [lint, extra]"))
    result = _run(head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert extra in result.stdout and workflow in result.stdout
    assert fold in result.stdout and gate_path in result.stdout
    assert input_sha in result.stdout and "inputs/extra.txt" in result.stdout
    assert "inputs/extra.txt" in gate.gate_paths(head)
