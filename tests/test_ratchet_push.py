"""Structure pins over ratchet-push.yml across the two workflows.

The run-id fallback is pinned behaviourally: the listing step's script
runs under bash -e against a stub `gh` recording the artifacts
endpoint it was called with (as in tests/test_tag_waiter.py).
"""
import json
import os
import shlex
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent

_GIT_IDENTITY = {"GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example.com",
                 "GIT_COMMITTER_NAME": "seed",
                 "GIT_COMMITTER_EMAIL": "seed@example.com"}


def _workflow() -> dict[str, Any]:
    path = _REPO / ".github/workflows/ratchet-push.yml"
    assert path.is_file(), "The push workflow needs its workflow file"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML's YAML 1.1 loader reads unquoted `on` as True.
    return workflow.get("on", workflow.get(True, {}))


def _push_job() -> dict[str, Any]:
    jobs = _workflow()["jobs"]
    assert set(jobs) == {"push"}
    return jobs["push"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _push_job()["steps"] if step.get("name") == name)


def _conjuncts(expression: str) -> set[str]:
    # A disjunction would widen the admission set.
    body = expression.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()
    assert "||" not in body
    return {part.strip() for part in body.split("&&")}


def test_the_trigger_keys_on_the_gate_workflow_itself() -> None:
    gate = yaml.safe_load((_REPO / ".github/workflows/ci-gate.yml").read_text(encoding="utf-8"))
    run = _triggers(_workflow())["workflow_run"]
    assert run["workflows"] == [gate["name"]]
    assert run["types"] == ["completed"]


def test_the_push_job_admits_only_green_master_pushes() -> None:
    # Green-only lives here: the job reads nothing of the run's conclusion.
    assert _conjuncts(_push_job()["if"]) == {
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'master'",
        "github.event.workflow_run.conclusion == 'success'",
    }


def test_the_push_job_runs_in_the_master_push_environment() -> None:
    job = _push_job()
    assert job["environment"] == "master-push"
    assert _workflow()["permissions"] == {}
    assert job["permissions"] == {"actions": "read"}


def test_the_push_never_cancels_a_run_mid_push() -> None:
    concurrency = _workflow()["concurrency"]
    # Keyed on the triggering run: a pull request cannot supersede a
    # pending master tighten.
    for field in ("event", "head_branch"):
        assert f"github.event.workflow_run.{field}" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_the_listing_step_sets_only_the_prefixed_run_id_variable() -> None:
    # Workflow-set variables carry the DOCS_HUB_ prefix. The old name is never
    # set; the script below still reads it as a fallback so anything still
    # exporting it keeps working.
    env = _step("List the triggering run's artifacts")["env"]
    assert "DOCS_HUB_RUN_ID" in env
    assert "RUN_ID" not in env


def _bash() -> str:
    if os.name != "nt":
        return "bash"
    # The first bash on PATH under Windows is the WSL launcher, which
    # cannot run Windows-path arguments.
    git = shutil.which("git")
    assert git, "Git for Windows supplies the bash that runs workflow scripts"
    return next(str(candidate) for ancestor in Path(git).parents
                for candidate in (ancestor / "usr" / "bin" / "bash.exe",
                                  ancestor / "bin" / "bash.exe")
                if candidate.is_file())


def _run_list_step(tmp_path: Path,
                   workflow_env: dict[str, str],
                   artifacts: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    step = _step("List the triggering run's artifacts")
    script = tmp_path / "step.sh"
    # LF: bash rejects the CRLF write_text emits on Windows.
    script.write_text(step["run"], encoding="utf-8", newline="\n")
    if not {"REPO", "DOCS_HUB_REPO"} & workflow_env.keys():
        workflow_env = {"DOCS_HUB_REPO": "example/repository", **workflow_env}
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    repos/*/artifacts) printf '%s\\n' "$arg" >> "$GH_LOG" ;;
  esac
done
cat "${GH_ARTIFACTS:-/dev/null}"
""", encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key not in {"REPO", "DOCS_HUB_REPO", "RUN_ID", "DOCS_HUB_RUN_ID"}}
    if artifacts is not None:
        (tmp_path / "artifacts.txt").write_text(
            "".join(f"{name}\n" for name in artifacts), encoding="utf-8")
        env["GH_ARTIFACTS"] = str(tmp_path / "artifacts.txt")
    return subprocess.run(
        [_bash(), "-e", str(script)], check=False, capture_output=True,
        text=True, timeout=5,
        env={**env, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
             **workflow_env,
             "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
             "GH_LOG": str(tmp_path / "gh-calls.log")},
    )


@pytest.mark.parametrize("artifacts, staged_speed", [
    (["speed-measurement"], "true"),
    (["test-ratchet-x-1_2", "speed-measurement"], "true"),
    (["test-ratchet-x-1_2"], "false"),
    ([], "false"),
])
def test_the_listing_reports_the_speed_arm(
        tmp_path: Path, artifacts: list[str], staged_speed: str) -> None:
    result = _run_list_step(tmp_path, {"DOCS_HUB_RUN_ID": "3" * 10}, artifacts)
    assert result.returncode == 0, result.stdout + result.stderr
    output = dict(line.split("=", 1) for line in
                  (tmp_path / "output.txt").read_text(encoding="utf-8").splitlines())
    assert output == {"staged_speed": staged_speed}
    log = (tmp_path / "gh-calls.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1


@pytest.mark.parametrize("workflow_env, expected_run_id", [
    ({"DOCS_HUB_RUN_ID": "1" * 10}, "1" * 10),
    ({"RUN_ID": "2" * 10}, "2" * 10),
    ({"DOCS_HUB_RUN_ID": "1" * 10, "RUN_ID": "2" * 10}, "1" * 10),
])
def test_the_run_id_reaches_the_artifacts_url(
        tmp_path: Path, workflow_env: dict[str, str],
        expected_run_id: str) -> None:
    result = _run_list_step(tmp_path, workflow_env)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / "gh-calls.log").read_text(encoding="utf-8").splitlines()
    assert log == [f"repos/example/repository/actions/runs/{expected_run_id}/artifacts"]


def test_the_deploy_key_is_prefixed_and_falls_back_to_the_old_secret() -> None:
    # GitHub's || is falsy-on-empty: both secrets set -> the prefixed name
    # wins; the old one alone still authenticates, never as its own variable.
    env = _step("Push the tighten commit to master")["env"]
    # The secret cannot be run locally; check the names and their order.
    expression = env["DOCS_HUB_MASTER_PUSH_DEPLOY_KEY"]
    assert "secrets.DOCS_HUB_MASTER_PUSH_DEPLOY_KEY" in expression
    assert "secrets.MASTER_PUSH_DEPLOY_KEY" in expression
    assert (expression.index("secrets.DOCS_HUB_MASTER_PUSH_DEPLOY_KEY")
            < expression.index("secrets.MASTER_PUSH_DEPLOY_KEY"))
    assert "MASTER_PUSH_DEPLOY_KEY" not in env


def test_the_deploy_key_leaves_the_environment_before_fetched_code_runs() -> None:
    lines = [line.strip() for line in
             _step("Push the tighten commit to master")["run"].splitlines()]
    loaded = next(i for i, line in enumerate(lines) if line.startswith("ssh-add"))
    unset = lines.index("unset DOCS_HUB_MASTER_PUSH_DEPLOY_KEY")
    python = next(i for i, line in enumerate(lines) if "python3" in line)
    assert loaded < unset < python
    assert not any("MASTER_PUSH_DEPLOY_KEY" in line for line in lines[unset + 1:])


def test_the_speed_download_runs_only_for_the_speed_arm() -> None:
    download = _step("Download the speed measurement artifact")
    assert frozenset(_conjuncts(download["if"])) == frozenset(
        {"steps.list.outputs.staged_speed == 'true'"})
    # The push step holds the deploy key: it runs for the speed arm alone.
    push = _step("Push the tighten commit to master")
    assert frozenset(_conjuncts(push["if"])) == frozenset(
        {"steps.list.outputs.staged_speed == 'true'"})


_SPEED_BUDGET = {"schema_version": 1, "counter": "cachegrind-ir",
                 "tolerance_pct": 0.5, "toolchain": {
                     "python": "fixture", "valgrind": "fixture",
                     "image": None, "lock_sha256": "ab" * 32},
                 "workloads": {"fixture": 2_000_000}}


def _seed_remote(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin" / "example" / "repository.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    for rel in ("scripts/ci/speed.py", "scripts/ci/speed_budget.py",
                "scripts/ci/speed_paired.py", "scripts/ci/budget_chain.py"):
        target = seed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REPO / rel, target)
    (seed / ".github").mkdir()
    (seed / ".github/speed-budget.json").write_text(json.dumps(_SPEED_BUDGET), encoding="utf-8")
    identity = _GIT_IDENTITY
    for command in (["git", "init", "-q", str(seed)],
                    ["git", "-C", str(seed), "add", "."],
                    ["git", "-C", str(seed), "commit", "-m", "seed"],
                    ["git", "clone", "-q", "--bare", str(seed), str(origin)]):
        subprocess.run(command, check=True, capture_output=True, env={**os.environ, **identity})
    return origin, subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "refs/heads/master"],
        check=True, capture_output=True, text=True).stdout.strip()


def _measurement(tmp_path: Path, base: str) -> None:
    budget = _SPEED_BUDGET
    counts = {name: value - 1_000_000 for name, value in budget["workloads"].items()}
    data = {"schema_version": 1, "base_sha": base, "toolchain": budget["toolchain"],
            "counts": counts, "wall_seconds": {}}
    path = tmp_path / "speed-data" / "measurement.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _run_push_step(tmp_path: Path, repo_env: dict[str, str] | None = None
                   ) -> subprocess.CompletedProcess[str]:
    step = _step("Push the tighten commit to master")
    if repo_env is None:
        repo_env = {"DOCS_HUB_REPO": "example/repository"}
    script = tmp_path / "push-step.sh"
    script.write_text(step["run"], encoding="utf-8", newline="\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent = bin_dir / "ssh-agent"
    agent.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'SSH_AUTH_SOCK=%s; export SSH_AUTH_SOCK;"
        "\\nSSH_AGENT_PID=%s; export SSH_AGENT_PID;\\n' \"$PWD/agent.sock\" \"$$\"\n",
        encoding="utf-8", newline="\n")
    agent.chmod(0o755)
    adder = bin_dir / "ssh-add"
    adder.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
    adder.chmod(0o755)
    python = bin_dir / "python3"
    python.write_text('#!/bin/sh\n'
                      'if [ "${DOCS_HUB_MASTER_PUSH_DEPLOY_KEY+x}" ]; then exit 91; fi\n'
                      f'exec {shlex.quote(sys.executable)} "$@"\n',
                      encoding="utf-8", newline="\n")
    python.chmod(0o755)
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "json.py").write_text('raise RuntimeError("poisoned json")\n', encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[url "{tmp_path / "origin"}/"]\n\tinsteadOf = git@github.com:\n',
        encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in {"DOCS_HUB_MASTER_PUSH_DEPLOY_KEY", "REPO", "DOCS_HUB_REPO", "GIT_DIR",
                          "GIT_WORK_TREE", "GIT_INDEX_FILE", "XDG_CONFIG_HOME",
                          "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"}}
    return subprocess.run(
        [_bash(), "-e", str(script)], cwd=tmp_path, check=False,
        capture_output=True, text=True, timeout=120,
        env={**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "HOME": str(home), "RUNNER_TEMP": str(tmp_path),
             **repo_env,
             "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
             "DOCS_HUB_MASTER_PUSH_DEPLOY_KEY": "test-key",
             "PYTHONPATH": str(poison)})


def _git(origin: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(origin), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_the_push_step_lands_its_commit(tmp_path: Path) -> None:
    origin, seed = _seed_remote(tmp_path)
    _measurement(tmp_path, seed)
    result = _run_push_step(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(origin, "log", "--format=%s", "refs/heads/master").splitlines() == [
        "chore(ratchet): Tighten the speed budget (automated)", "seed"]
    assert _git(origin, "diff-tree", "--no-commit-id", "--name-only", "-r",
                "refs/heads/master").splitlines() == [".github/speed-budget.json"]
    assert _git(origin, "rev-parse", "refs/heads/master") == subprocess.run(
        ["git", "-C", str(tmp_path / "raise-repo"), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()


def test_a_stale_measurement_drops_with_a_notice(tmp_path: Path) -> None:
    origin, _seed = _seed_remote(tmp_path)
    _measurement(tmp_path, "b" * 40)
    result = _run_push_step(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(origin, "log", "--format=%s", "refs/heads/master") == "seed"
    assert "master moved while this run measured" in (
        tmp_path / "summary").read_text(encoding="utf-8")


def test_a_rejected_push_while_master_stood_still_fails_the_step(tmp_path: Path) -> None:
    origin, seed = _seed_remote(tmp_path)
    _measurement(tmp_path, seed)
    (origin / "hooks" / "pre-receive").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (origin / "hooks" / "pre-receive").chmod(0o755)
    result = _run_push_step(tmp_path)
    assert result.returncode == 1
    assert "rejected while master stood still" in result.stderr
    assert _git(origin, "log", "--format=%s", "refs/heads/master") == "seed"


def test_a_rejected_push_while_master_moved_drops_with_a_notice(tmp_path: Path) -> None:
    origin, seed = _seed_remote(tmp_path)
    _measurement(tmp_path, seed)
    moved = subprocess.run(
        ["git", "-C", str(origin), "commit-tree", "refs/heads/master^{tree}",
         "-m", "moved"], check=True, capture_output=True, text=True,
        env={**os.environ, **_GIT_IDENTITY}).stdout.strip()
    (origin / "hooks" / "pre-receive").write_text(
        f"#!/bin/sh\n"
        f"env -u GIT_QUARANTINE_PATH -u GIT_OBJECT_DIRECTORY"
        f" -u GIT_ALTERNATE_OBJECT_DIRECTORIES"
        f" git update-ref refs/heads/master {moved}\nexit 1\n",
        encoding="utf-8")
    (origin / "hooks" / "pre-receive").chmod(0o755)
    result = _run_push_step(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "master moved before the tighten landed" in (
        tmp_path / "summary").read_text(encoding="utf-8")
    assert _git(origin, "rev-parse", "refs/heads/master") == moved
