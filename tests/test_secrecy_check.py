"""Exercise auth database name selection without credentials or live databases."""
from pathlib import Path
import subprocess

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts/secrecy-check.sh"


@pytest.mark.parametrize("settings,detected,ignored", [
    ("DOCS_HUB_DATABASE_URL_AUTH=postgresql:///preferred_auth_fixture\n",
     "preferred_auth_fixture", "legacy_auth_fixture"),
    ("DATABASE_URL_AUTH=postgresql:///legacy_auth_fixture\n",
     "legacy_auth_fixture", "preferred_auth_fixture"),
    ("DATABASE_URL_AUTH=postgresql:///legacy_auth_fixture\n"
     "DOCS_HUB_DATABASE_URL_AUTH=postgresql:///preferred_auth_fixture\n",
     "preferred_auth_fixture", "legacy_auth_fixture"),
    ("DOCS_HUB_DATABASE_URL_AUTH=\nDATABASE_URL_AUTH=postgresql:///legacy_auth_fixture\n",
     "legacy_auth_fixture", "preferred_auth_fixture"),
    (" export DOCS_HUB_DATABASE_URL_AUTH = "
     "'postgresql:///preferred_auth_fixture?sslmode=require'\n",
     "preferred_auth_fixture", "legacy_auth_fixture"),
])
def test_auth_database_name_prefers_prefix_and_accepts_legacy(tmp_path: Path,
                                                              settings: str,
                                                              detected: str, ignored: str):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".env").write_text(settings, encoding="utf-8")
    candidate = tmp_path / "candidate"
    candidate.write_text(detected, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "candidate"], check=True)
    result = subprocess.run(["bash", str(_SCRIPT), "--tree"], cwd=tmp_path,
                            check=False, capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "the auth-DB name" in result.stderr
    assert "candidate" in result.stderr
    assert detected not in result.stderr
    candidate.write_text(ignored, encoding="utf-8")
    result = subprocess.run(["bash", str(_SCRIPT), "--tree"], cwd=tmp_path,
                            check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
