"""The speed job's counting image: its pin, how the job reads it, its record."""
# pylint: disable=protected-access  # Shares the sibling suites' loaders and runners.
from __future__ import annotations

import shlex
from pathlib import Path
from types import ModuleType

import pytest
import yaml

import test_release_chain as rc
import test_speed_gate as f

_IMAGE = "python:3.15.1-slim-trixie@sha256:" + "5" * 64


def _speed_jobs() -> dict:
    return rc._workflow("speed.yml")["jobs"]


@pytest.fixture(name="policy")
def _policy() -> ModuleType:
    return f._load("ci_speed_budget", f._POLICY)


def _valgrind(tmp_path: Path) -> str:
    stub = tmp_path / "valgrind"
    stub.write_text("#!/bin/sh\necho valgrind-9.9.9\n", encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)


@pytest.mark.parametrize("image", [_IMAGE, None])
def test_the_toolchain_image_is_the_speed_jobs_image_variable_alone(
        policy: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        image: str | None) -> None:
    monkeypatch.setenv("ImageOS", "ubuntu24")
    monkeypatch.setenv("ImageVersion", "20261023.334.3")
    if image is None:
        monkeypatch.delenv("DOCS_HUB_SPEED_IMAGE", raising=False)
    else:
        monkeypatch.setenv("DOCS_HUB_SPEED_IMAGE", image)
    lock = tmp_path / "requirements-speed.txt"
    lock.write_text("lock\n", encoding="utf-8")
    chain = policy.toolchain(lock, _valgrind(tmp_path))
    assert set(chain) == set(policy.TOOLCHAIN_FIELDS)
    assert chain["image"] == image
    assert chain["valgrind"] == "valgrind-9.9.9"


@pytest.mark.parametrize("pin, ok", [
    (_IMAGE, True), ("python:3.15.1-slim-trixie", False),
    ("postgres:17.1@sha256:" + "7" * 64, False), ("", False),
])
def test_the_images_job_resolves_only_a_digest_pinned_python_image(
        tmp_path: Path, pin: str, ok: bool) -> None:
    directory = tmp_path / "docker" / "speed"
    directory.mkdir(parents=True)
    (directory / "Dockerfile").write_text(f"FROM {pin}\r\n", encoding="utf-8")
    postgres = tmp_path / "docker" / "postgres" / "17"
    postgres.mkdir(parents=True)
    (postgres / "Dockerfile").write_text("FROM postgres:17.1@sha256:" + "7" * 64 + "\n",
                                         encoding="utf-8")
    reading = next(step for step in _speed_jobs()["images"]["steps"] if step.get("id") == "pins")
    output = tmp_path / "output"
    result = rc._run_script(tmp_path, reading["run"], {"GITHUB_OUTPUT": str(output)})
    if not ok:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "docker/speed/Dockerfile" in result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"speed={_IMAGE}" in output.read_text(encoding="utf-8").splitlines()


def test_the_speed_job_runs_in_and_records_the_images_jobs_image() -> None:
    images, speed = _speed_jobs()["images"], _speed_jobs()["speed"]
    assert "speed" in images["outputs"]
    assert "images" in rc._needs(speed)
    expression = "${{ needs.images.outputs.speed }}"
    assert speed["container"]["image"] == expression
    assert speed["env"]["DOCS_HUB_SPEED_IMAGE"] == expression


def test_the_speed_dockerfile_holds_one_from_line_of_a_python_image() -> None:
    lines = (rc._REPO / "docker/speed/Dockerfile").read_text(encoding="utf-8").splitlines()
    froms = [line for line in lines if line.startswith("FROM ")]
    assert len(froms) == 1 and froms[0].startswith("FROM python:3.")


def _install_args(job: dict) -> list[str]:
    for step in job["steps"]:
        for line in step.get("run", "").replace("\\\n", " ").splitlines():
            words = shlex.split(line)
            if "apt-get" in words and "install" in words:
                return words
    raise AssertionError("no apt-get install in the speed job")


def test_git_is_installed_before_the_checkout_that_needs_it() -> None:
    steps = _speed_jobs()["speed"]["steps"]
    install = next(i for i, step in enumerate(steps) if "apt-get" in step.get("run", ""))
    assert steps[0].get("uses", "").partition("@")[0] == "actions/checkout"
    checkout = max(i for i, step in enumerate(steps)
                   if step.get("uses", "").partition("@")[0] == "actions/checkout")
    assert 0 < install < checkout
    assert "git" in _install_args(_speed_jobs()["speed"])


def test_the_counted_packages_install_at_exact_versions() -> None:
    words = _install_args(_speed_jobs()["speed"])
    for package in ("valgrind", "libc6-dbg"):
        assert any(word.startswith(f"{package}=") for word in words), (package, words)
    assert "--no-install-recommends" in words


def test_dependabot_updates_the_speed_image_within_its_minor() -> None:
    updates = yaml.safe_load(
        (rc._REPO / ".github/dependabot.yml").read_text(encoding="utf-8"))["updates"]
    entries = [entry for entry in updates if "/docker/speed" in entry.get("directories", [])]
    assert [entry["package-ecosystem"] for entry in entries] == ["docker"]
    # Every ignore in the entry applies to each of its directories, the speed image's too.
    assert sorted(entries[0]["ignore"], key=lambda condition: condition["dependency-name"]) == [
        {"dependency-name": "postgres", "update-types": ["version-update:semver-major"]},
        {"dependency-name": "python", "update-types": [
            "version-update:semver-major", "version-update:semver-minor"]}]
