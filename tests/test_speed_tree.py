"""speed.py measure --tree: another tree's code, this checkout's harness."""
# pylint: disable=protected-access  # test_speed_gate.py's loader
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import test_speed_gate as f

# Each second-tree module logs its limb and whether the stub launched it.
_TREE_LOG = ('import os\n'
             'with open(os.environ["TREE_LOG"], "a", encoding="utf-8") as log:\n'
             '    log.write("{limb} " + os.environ.get("STUB_RUN", "native") + "\\n")\n')
_TREE_APP = '''

async def app(scope, receive, send):
    if scope["type"] != "http":
        return  # uvicorn's lifespan "auto" carries on without one
    message = await receive()
    while message.get("more_body"):
        message = await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})
'''
# The count file valgrind would write ($$ survives exec), then exec.
_COUNTING_STUB = '''#!/bin/sh
export STUB_RUN=counted
while [ "$#" -gt 0 ]; do
    case "$1" in
        --cachegrind-out-file=*) out="${1#*=}"; printf 'summary: 5\\n' > "${out%.%p}.$$" ;;
        --*) ;;
        *) exec "$@" ;;
    esac
    shift
done
'''


def test_measure_takes_schema_server_cli_and_lock_from_the_tree(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    speed = f._load("ci_speed", f._SCRIPT)
    tree = tmp_path / "tree"
    for module, limb, code in (("backend/app.py", "server", _TREE_APP),
                               ("docs_hub_cli/cli.py", "cli", "")):
        (tree / module).parent.mkdir(parents=True)
        # A regular package: a namespace one would lose to an installed copy.
        (tree / module).with_name("__init__.py").write_text("", encoding="utf-8")
        (tree / module).write_text(_TREE_LOG.format(limb=limb) + code, encoding="utf-8")
    schema = "-- the second tree's schema\n"
    (tree / "backend/schema.sql").write_text(schema, encoding="utf-8")
    (tree / "requirements-speed.txt").write_bytes(b"the second tree's lock\n")
    stub, log = tmp_path / "valgrind-stub", tmp_path / "tree.log"
    stub.write_text(_COUNTING_STUB, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("TREE_LOG", str(log))
    # This checkout's backend would fail at startup: no Postgres socket here.
    monkeypatch.setenv("PGHOST", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    loaded: list[str] = []
    run = subprocess.run

    def database(command: list[str], **kwargs: object) -> object:
        if command[0] not in ("dropdb", "createdb", "psql"):
            return run(command, check=bool(kwargs.pop("check", False)), **kwargs)
        if command[0] == "psql":
            loaded.append(Path(command[command.index("-f") + 1]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(speed.subprocess, "run", database)
    out = tmp_path / "measurement.json"
    assert speed.main(["measure", "--database", "docs_speed_tree", "--out", str(out),
                       "--valgrind", str(stub), "--tree", str(tree)]) == 0
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == [schema] * (len(speed.WORKLOADS) + 1)
    assert artifact["toolchain"]["lock_sha256"] == hashlib.sha256(
        b"the second tree's lock\n").hexdigest()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines.count("server counted") == len(speed.WORKLOADS)
    assert lines.count("server native") == 1
    assert set(lines) == {"server counted", "server native", "cli counted", "cli native"}
