"""A test marked portable never reaches the database fixtures in conftest.py."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.portable

_TESTS = Path(__file__).resolve().parent
_PORTABLE = "import pytest\npytestmark = pytest.mark.portable\n"


def _run_probe(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """Run a probe under the real conftest; any database use fails it."""
    (tmp_path / "test_probe.py").write_text(source, encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PG", "DOCS_HUB_"))}
    env |= {"PATH": str(tmp_path), "PGHOST": str(tmp_path), "PYTHONPATH": str(_TESTS)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False)


def test_a_portable_test_runs_without_the_database(tmp_path: Path) -> None:
    done = _run_probe(tmp_path, _PORTABLE + "def test_ok():\n    pass\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 passed" in done.stdout


@pytest.mark.parametrize("source", [
    "def test_db(_test_env):\n    pass\n",
    "@pytest.fixture\ndef uses_db(_test_env):\n    pass\n"
    "def test_db(uses_db):\n    pass\n",
], ids=["direct", "through-a-fixture"])
def test_a_portable_test_that_requests_the_database_is_refused(
        tmp_path: Path, source: str) -> None:
    done = _run_probe(tmp_path, _PORTABLE + source)
    assert done.returncode == pytest.ExitCode.USAGE_ERROR, done.stdout + done.stderr
    assert "test_probe.py::test_db" in done.stderr


def test_an_unmarked_test_still_gets_the_database(tmp_path: Path) -> None:
    # The live oracle for the cases above: without the marker, the probe's
    # stripped PATH makes the database setup fail on dropdb.
    done = _run_probe(tmp_path, "def test_db():\n    pass\n")
    assert done.returncode == pytest.ExitCode.TESTS_FAILED, done.stdout + done.stderr
    assert "dropdb" in done.stdout
