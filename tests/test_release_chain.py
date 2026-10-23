"""Check release permissions and execute the writer scripts in isolation."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent


def _workflow(filename: str) -> dict[str, Any]:
    with (_REPO / ".github/workflows" / filename).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _action(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"]
                if step.get("uses", "").partition("@")[0] == name)


def _step(filename: str, job: str, name: str) -> dict[str, Any]:
    return next(step for step in _workflow(filename)["jobs"][job]["steps"]
                if step.get("name") == name)


def _assert_minimal_writer(job: dict[str, Any]) -> None:
    for step in job["steps"]:
        assert step.get("uses", "").partition("@")[0] != "actions/checkout"
        script = step.get("run", "")
        assert "pip install" not in script
        assert not re.search(r"\bpython(?:\d+(?:\.\d+)*)?\b", script)


def test_release_build_is_read_only_and_hands_off_artifact():
    workflow = _workflow("release.yml")
    assert workflow["permissions"] == {"contents": "read"}
    build = workflow["jobs"]["build"]
    assert build["permissions"] == {"contents": "read"}
    assert "postgres" in build["services"]
    assert set(build["env"]) >= {"PGHOST", "PGPORT", "PGUSER", "PGPASSWORD"}
    assert "services" not in workflow and "env" not in workflow
    assert _action(build, "actions/checkout")["with"]["persist-credentials"] is False
    upload = _action(build, "actions/upload-artifact")
    assert build["steps"][-1] == upload
    publish = workflow["jobs"]["publish"]
    download = _action(publish, "actions/download-artifact")
    assert upload["with"]["name"] == download["with"]["name"] == "release-dist"


def test_release_publish_has_only_the_writer_permissions():
    publish = _workflow("release.yml")["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["permissions"] == {"contents": "write"}
    assert publish["timeout-minutes"] == 10
    assert "services" not in publish and "env" not in publish
    _assert_minimal_writer(publish)


def test_tag_readiness_is_read_only_and_exports_skip():
    workflow = _workflow("tag.yml")
    assert workflow["permissions"] == {"contents": "read"}
    readiness = workflow["jobs"]["release-readiness"]
    assert readiness["permissions"] == {"contents": "read", "checks": "read"}
    assert readiness["timeout-minutes"] == 60
    assert _action(readiness, "actions/checkout")["with"]["persist-credentials"] is False
    assert set(readiness["outputs"]) == {"tag", "sha", "skip"}
    for name in ("Refuse to re-tag an existing version", "Wait for the other gates on this commit"):
        step = _step("tag.yml", "release-readiness", name)
        assert step["if"] == "steps.version.outputs.skip != 'true'"


def test_tag_writer_has_only_the_writer_permissions():
    tag = _workflow("tag.yml")["jobs"]["tag"]
    assert tag["needs"] == "release-readiness"
    assert tag["if"] == "needs.release-readiness.outputs.skip != 'true'"
    assert tag["permissions"] == {"contents": "write", "actions": "write"}
    assert tag["timeout-minutes"] == 10
    _assert_minimal_writer(tag)


def _run_script(tmp_path: Path, script: str,
                env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "step.sh"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(
        ["bash", "-e", str(path)], cwd=tmp_path, check=False,
        capture_output=True, text=True, timeout=5,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), **env},
    )


def _stub_gh(tmp_path: Path) -> Path:
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
args = sys.argv[1:]
with open(os.environ['GH_CALLS'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'api' and any(arg.endswith('/git/tags') for arg in args):
    response = json.dumps({'sha': 'tagobjsha123'})
    if '--jq' in args:
        result = subprocess.run(['jq', '-r', args[args.index('--jq') + 1]],
                                input=response, text=True, check=False)
        raise SystemExit(result.returncode)
    print(response)
elif args[0] == 'api' and any(arg.endswith('/git/refs') for arg in args):
    print('{}')
elif args[:3] != ['workflow', 'run', 'release.yml']:
    raise SystemExit('unmodelled gh call: ' + repr(args))
""", encoding="utf-8")
    gh.chmod(0o755)
    return tmp_path / "gh-calls.jsonl"


@pytest.mark.parametrize("repo,tag", [("example/one", "v1.2.3"), ("example/two", "v4.5.6rc1")])
def test_tag_writer_creates_annotated_object_then_ref(tmp_path: Path, repo: str, tag: str):
    calls_path = _stub_gh(tmp_path)
    sha = "a" * 40
    result = _run_script(tmp_path, _step("tag.yml", "tag", "Create and push the tag")["run"],
                         {"GH_CALLS": str(calls_path), "REPO": repo, "TAG": tag, "SHA": sha})
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    for call, endpoint in zip(calls, ("tags", "refs")):
        assert call[0] == "api"
        assert f"repos/{repo}/git/{endpoint}" in call
    for field in (f"tag={tag}", f"message={tag}", f"object={sha}", "type=commit"):
        assert field in calls[0]
    assert "sha=tagobjsha123" in calls[1]
    assert f"ref=refs/tags/{tag}" in calls[1]


@pytest.mark.parametrize("tag", [
    "vnot-a-version", "1.2.3", "x1.2.3", "v1.2.3!", "v1.2.3\nignored",
])
def test_tag_writer_refuses_invalid_tag_before_api_calls(tmp_path: Path, tag: str):
    calls_path = _stub_gh(tmp_path)
    calls_path.write_text("", encoding="utf-8")
    result = _run_script(tmp_path, _step("tag.yml", "tag", "Create and push the tag")["run"],
                         {"GH_CALLS": str(calls_path), "REPO": "example/repository",
                          "TAG": tag, "SHA": "a" * 40})
    assert result.returncode == 1, result.stdout + result.stderr
    assert calls_path.read_text(encoding="utf-8") == ""
    assert "invalid" in result.stderr.lower()
    assert tag in result.stderr
    assert "already exists" not in result.stderr


def test_tag_writer_dispatches_release_at_tag(tmp_path: Path):
    calls_path = _stub_gh(tmp_path)
    result = _run_script(tmp_path, _step("tag.yml", "tag", "Start the release")["run"],
                         {"GH_CALLS": str(calls_path), "REPO": "example/repository",
                          "TAG": "v1.2.3"})
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    assert calls == [["workflow", "run", "release.yml", "--repo", "example/repository",
                      "--ref", "v1.2.3"]]


@pytest.mark.parametrize("tampered", [False, True])
def test_publish_verifies_downloaded_checksums(tmp_path: Path, tampered: bool):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "fixture.whl"
    payload = b"wheel fixture"
    wheel.write_bytes(payload)
    (dist / "SHA256SUMS").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {wheel.name}\n", encoding="utf-8")
    if tampered:
        wheel.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    step = _step("release.yml", "publish", "Verify the downloaded artifact against its checksums")
    result = _run_script(tmp_path, step["run"], {})
    assert result.returncode == (1 if tampered else 0), result.stdout + result.stderr
