#!/usr/bin/env python3
"""Gate each workload on a deterministic instruction count, not on wall time.

Each workload runs on its own fresh server under `valgrind --tool=cachegrind
--cache-sim=no`; scripts/ci/speed_budget.py compares the counts against
`.github/speed-budget.json`. Wall time is reported, never gated: the counter
cannot see off-CPU latency (event-loop blocking, sleeps, lock waits, work
inside Postgres), for which wall time is the only signal.

    speed.py workload --database NAME [--workload NAME]
    speed.py measure --database NAME --out FILE [--base-sha SHA]
    speed.py check --measurements FILE [--budget FILE] [--summary FILE]
    speed.py tighten --measurements FILE --budget FILE
    speed.py rebaseline --measurements FILE [--budget FILE]
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import speed_budget  # noqa: E402  pylint: disable=wrong-import-position

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = ("service.publish", "service.list", "service.versions",
             "cli.transfer", "cli.text")
KEY = "speed-key"
CLIENTS = 4
USERS = ("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, "
         "config JSONB NOT NULL DEFAULT '{}'::jsonb)")
# Every counted child runs with a fixed hash seed and no bytecode cache:
# either unpinned would make the count nondeterministic.
MEASURED_ENV = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
# Valgrind is 20-50x slower than native: these counts replace the wall
# gate's (96 slugs, 3 versions, list x4, versions x8, transfer x40, text
# x10) and keep a full measure inside the job's 30 timeout-minutes.
SLUGS = 16
VERSIONS_PER_SLUG = 1
LIST_CALLS_PER_SLUG = 2
VERSION_CALLS_PER_SLUG = 2
TRANSFER_ITERATIONS = 2
TEXT_ITERATIONS = 1
# A valgrind'd server takes tens of seconds to start; native takes one.
READY_TICKS = 3000


def document(kilobytes: int, seed: int) -> bytes:
    section = ("<section><h2>Finding {n}</h2><p>The <strong>measured</strong> value moved "
               "by {n}&nbsp;% against the <b>baseline</b> run.</p><ul><li>cause {n}</li>"
               "<li>effect {seed}</li></ul><script>var n = {n};</script></section>\n")
    body, n = [], 0
    while sum(map(len, body)) < kilobytes * 1024:
        body.append(section.format(n=n, seed=seed))
        n += 1
    return (f"<!doctype html><html><head><title>Report {seed}</title><style>p{{margin:0}}"
            f"</style></head><body><h1>Report {seed}</h1>{''.join(body)}</body></html>"
            ).encode()


class Service:
    """One keep-alive connection to the server under measurement."""

    def __init__(self, port: int) -> None:
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)

    def call(self, method: str, path: str, body: bytes | None = None,
             content_type: str = "") -> bytes:
        headers = {"x-docs-key": KEY}
        if content_type:
            headers["Content-Type"] = content_type
        self.connection.request(method, path, body=body, headers=headers)
        response = self.connection.getresponse()
        payload = response.read()
        if response.status != 200:
            raise RuntimeError(f"{method} {path}: HTTP {response.status}: {payload[:200]!r}")
        return payload

    def publish(self, slug: str, html: bytes) -> None:
        boundary = "speedboundary"
        fields = {"slug": slug, "title": f"Report {slug}", "tags": "speed,report",
                  "project": "speed", "from": "speed"}
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n".encode() for name, value in fields.items()]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                     f'filename="{slug}.html"\r\nContent-Type: text/html\r\n\r\n'.encode()
                     + html + f"\r\n--{boundary}--\r\n".encode())
        self.call("POST", "/api/publish", b"".join(parts),
                  f"multipart/form-data; boundary={boundary}")


def valgrind_argv(out_prefix: str) -> list[str]:
    return ["valgrind", "--tool=cachegrind", "--cache-sim=no",
            f"--cachegrind-out-file={out_prefix}.%p"]


def cli(argv: tuple[str, ...], counted: bool, prefix: str,
        env: dict[str, str]) -> None:
    """Run one CLI call as its own subprocess, under valgrind when counted."""
    command = ([*valgrind_argv(prefix), sys.executable] if counted
               else [sys.executable])
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [*command, "-m", "docs_hub_cli.cli", *argv], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, error = child.communicate()
    if child.returncode != 0:
        raise RuntimeError(f"docs-hub {' '.join(argv)} exited "
                           f"{child.returncode}: {error[-200:].decode(errors='replace')}")
    if counted and not Path(f"{prefix}.{child.pid}").is_file():
        raise RuntimeError(f"valgrind wrote no count file for docs-hub {' '.join(argv)}")


def execute(name: str, port: int, scratch: Path, counted: bool,
            env: dict[str, str]) -> None:
    """One workload's request sequence: the state it reads, then its calls."""
    services = [Service(port) for _ in range(CLIENTS)]
    small, large = document(24, 1), document(256, 2)
    slugs = [f"speed/doc-{n}" for n in range(SLUGS)]
    (scratch / "large.html").write_bytes(large)
    prefix = str(scratch / f"{name}.cli")
    call = lambda *argv: cli(argv, counted, prefix, env)

    def clients(work: Callable[[Service, list[str]], object]) -> None:
        # Each client owns one connection and every version of its own slugs.
        with ThreadPoolExecutor(CLIENTS) as pool:
            list(pool.map(lambda client: work(services[client], slugs[client::CLIENTS]),
                          range(CLIENTS)))

    def publish_all() -> None:
        clients(lambda service, mine: [service.publish(slug, small)
                                       for _ in range(VERSIONS_PER_SLUG)
                                       for slug in mine])

    def transfer(count: int) -> None:
        for _ in range(count):
            call("publish", str(scratch / "large.html"), "--slug", "speed/large",
                 "--title", "Large", "--from", "speed")
            call("get", "speed/large", "-o", str(scratch / "out"))

    def text(count: int) -> None:
        for _ in range(count):
            call("get", "speed/large", "--text-only", "-o", str(scratch / "out"))

    if name == "service.publish":
        publish_all()
    elif name == "service.list":
        publish_all()  # state: the list has content to return
        clients(lambda service, mine: [service.call("GET", "/api/list")
                                       for _ in mine * LIST_CALLS_PER_SLUG])
    elif name == "service.versions":
        publish_all()  # state: one version exists per slug
        clients(lambda service, mine: [service.call("GET", f"/api/versions/{slug}")
                                       for slug in mine * VERSION_CALLS_PER_SLUG])
    elif name == "cli.transfer":
        transfer(TRANSFER_ITERATIONS)
    elif name == "cli.text":
        transfer(1)  # state: speed/large exists for the text-only reads
        text(TEXT_ITERATIONS)
    else:
        raise RuntimeError(f"unknown workload {name}")


def read_count(path: Path) -> int:
    """The Ir total from a cachegrind out file's trailing summary line."""
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if line.startswith("summary:"):
            return int(line.split()[1])
    raise RuntimeError(f"{path} has no summary: line")


def serve(port: int, server: subprocess.Popen) -> None:
    """Wait until the server accepts; refuse one that exits at startup."""
    for _ in range(READY_TICKS):
        if server.poll() is not None:
            raise RuntimeError(f"the server exited {server.returncode} at startup")
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        time.sleep(0.1)
    raise RuntimeError(f"the server did not start within {READY_TICKS // 10} s")


def reset(tree: Path, database: str) -> None:
    """Lay the schema down again, so a workload always starts from nothing."""
    for command in (["dropdb", "--if-exists", database], ["createdb", database],
                    ["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", database,
                     "-f", str(tree / "backend/schema.sql"), "-c", USERS,
                     "-c", f"ALTER DATABASE {database} SET synchronous_commit = off"]):
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def workload_env(port: int, database: str, store: Path) -> dict[str, str]:
    """The env every measured child runs with, server and CLI alike."""
    values = {"DOCS_HUB_DATABASE_URL_DOCS": f"postgresql:///{database}",
              "DOCS_HUB_DATABASE_URL_AUTH": f"postgresql:///{database}",
              "DOCS_HUB_STORE_ROOT": str(store), "DOCS_HUB_SESSION_SECRET": "speed-secret",
              "DOCS_HUB_COOKIE_SECURE": "0", "DOCS_HUB_API_KEY": KEY,
              "DOCS_HUB_URL": f"http://127.0.0.1:{port}"}
    return {**os.environ, **values, **MEASURED_ENV}


def counted(database: str, name: str, scratch: Path) -> int:
    """One workload's count; measure() lays the schema first (the app
    migrates at startup), and the fresh database per workload makes a count
    reproducible standalone and in a full measure."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    store = scratch / f"{name}.store"
    store.mkdir()
    server_prefix = str(scratch / f"{name}.server")
    server = subprocess.Popen(  # pylint: disable=consider-using-with
        [*valgrind_argv(server_prefix), sys.executable, "-m", "uvicorn",
         "backend.app:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"], env=workload_env(port, database, store))
    try:
        serve(port, server)
        execute(name, port, scratch, counted=True,
                env=workload_env(port, database, store))
    finally:
        server.terminate()
        server.wait(timeout=120)
    files = [Path(f"{server_prefix}.{server.pid}")]
    files.extend(scratch.glob(f"{name}.cli.*"))
    return sum(read_count(path) for path in files)


def workload(database: str,
             names: tuple[str, ...] = WORKLOADS) -> dict[str, int]:
    """Count the named workloads; the database must already hold the schema."""
    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as scratch:
        for name in names:
            counts[name] = counted(database, name, Path(scratch))
    return counts


def wall_times(database: str) -> dict[str, float]:
    """Each workload once, native — the only signal for off-CPU latency."""
    with tempfile.TemporaryDirectory() as scratch:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        store = Path(scratch, "store")
        store.mkdir()
        server = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-m", "uvicorn", "backend.app:app", "--host",
             "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            env=workload_env(port, database, store))
        wall: dict[str, float] = {}
        try:
            serve(port, server)
            for name in WORKLOADS:
                started = time.perf_counter()
                execute(name, port, Path(scratch), counted=False,
                        env=workload_env(port, database, store))
                wall[name] = time.perf_counter() - started
        finally:
            server.terminate()
            server.wait(timeout=30)
    return wall


def measure(database: str, out: Path, base_sha: str | None) -> int:
    """Count every workload on a fresh database, time it natively, write the file."""
    counts = {}
    with tempfile.TemporaryDirectory() as scratch:
        for name in WORKLOADS:
            reset(ROOT, database)
            counts[name] = counted(database, name, Path(scratch))
    reset(ROOT, database)
    artifact = {"schema_version": speed_budget.SCHEMA_VERSION,
                "base_sha": base_sha or None,
                "toolchain": speed_budget.toolchain(),
                "counts": counts,
                "wall_seconds": wall_times(database)}
    payload = json.dumps(artifact, indent=2, sort_keys=True)
    out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


def write_document(path: Path, document: dict) -> None:
    """Replace the budget file's bytes, preserving its mode, in one rename."""
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(payload, encoding="utf-8")
    if path.exists():
        temp.chmod(path.stat().st_mode)
    os.replace(temp, path)


def render_toolchain(result: dict) -> list[str]:
    if not result["toolchain_diff"]:
        return ["toolchain: match"]
    lines = ["toolchain: MISMATCH — a rebaseline commit is required", "",
             "| field | recorded | measured |", "|---|---|---|"]
    lines.extend(f"| {field} | {recorded} | {measured} |" for field, (recorded, measured)
                 in result["toolchain_diff"].items())
    return lines


def render_check(document: dict, result: dict) -> str:
    """The gate table, then the toolchain block."""
    tolerance = f'{document["tolerance_pct"]:g}%'
    chain = "mismatch" if result["toolchain_diff"] else "match"
    lines = [f"### Speed budget ({speed_budget.COUNTER})", "",
             "| workload | count | budget | delta | tolerance | toolchain | verdict |",
             "|---|---|---|---|---|---|---|"]
    for row in result["rows"]:
        count = "-" if row["count"] is None else str(row["count"])
        budget = "-" if row["budget"] is None else str(row["budget"])
        delta = "-" if row["delta"] is None else f'{row["delta"]:+d}'
        lines.append(f"| {row['name']} | {count} | {budget} | {delta} | "
                     f"{tolerance} | {chain} | {row['verdict']} |")
    lines.append("")
    lines.extend(render_toolchain(result))
    return "\n".join(lines)


def render_wall(wall: dict[str, float]) -> str:
    lines = ["### Wall time (non-gating)", "",
             "The instruction counter cannot see off-CPU latency (event-loop "
             "blocking, sleeps, lock waits, work inside Postgres); wall time is "
             "the only signal for it.", "",
             "| workload | seconds |", "|---|---|"]
    lines.extend(f"| {name} | {wall[name]:.3f} |" for name in WORKLOADS
                 if name in wall)
    return "\n".join(lines)


def check_command(args: argparse.Namespace) -> int:
    document = speed_budget.load(args.budget)
    measurement = speed_budget.load_measurement(args.measurements)
    result = speed_budget.check(document, measurement)
    report = render_check(document, result)
    if measurement.get("wall_seconds"):
        report += "\n\n" + render_wall(measurement["wall_seconds"])
    print(report)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(report + "\n")
    return 1 if result["failed"] else 0


def tighten_command(args: argparse.Namespace) -> int:
    document = speed_budget.load(args.budget)
    measurement = speed_budget.load_measurement(args.measurements)
    workloads = speed_budget.tighten(document, measurement)
    if workloads is None:
        print("nothing was tightened: the toolchain differs or a workload "
              "failed its budget")
        return 0
    write_document(args.budget, {**document, "workloads": workloads})
    lowered = ", ".join(name for name in workloads
                        if workloads[name] < document["workloads"][name])
    print(f"tightened {lowered}")
    return 0


def rebaseline_command(args: argparse.Namespace) -> int:
    document = speed_budget.load(args.budget) if args.budget.exists() else None
    measurement = speed_budget.load_measurement(args.measurements)
    write_document(args.budget, speed_budget.rebaseline(document, measurement,
                                                        WORKLOADS))
    print(f"rebaselined {args.budget} from {args.measurements}")
    return 0


def workload_command(args: argparse.Namespace) -> int:
    names = (args.workload,) if args.workload else WORKLOADS
    print(json.dumps(workload(args.database, names)))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="speed.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    count = sub.add_parser("workload", help="count workloads under valgrind")
    count.add_argument("--database", required=True)
    count.add_argument("--workload", choices=WORKLOADS)
    count.set_defaults(func=workload_command)
    measure_parser = sub.add_parser(
        "measure", help="count every workload and write the measurement file")
    measure_parser.add_argument("--database", required=True)
    measure_parser.add_argument("--out", type=Path, required=True)
    measure_parser.add_argument(
        "--base-sha", default=os.environ.get("GITHUB_SHA", ""),
        help="the commit the run measured; defaults to $GITHUB_SHA")
    measure_parser.set_defaults(func=lambda args: measure(
        args.database, args.out, args.base_sha))
    check = sub.add_parser("check", help="compare against the recorded counts")
    check.add_argument("--measurements", type=Path, required=True)
    check.add_argument("--budget", type=Path, default=speed_budget.ARTIFACT)
    check.add_argument("--summary", default="")
    check.set_defaults(func=check_command)
    tighten = sub.add_parser("tighten", help="follow counts down")
    tighten.add_argument("--measurements", type=Path, required=True)
    tighten.add_argument("--budget", type=Path, required=True)
    tighten.set_defaults(func=tighten_command)
    rebase = sub.add_parser("rebaseline", help="rewrite the budget from a run")
    rebase.add_argument("--measurements", type=Path, required=True)
    rebase.add_argument("--budget", type=Path, default=speed_budget.ARTIFACT)
    rebase.set_defaults(func=rebaseline_command)
    try:
        args = parser.parse_args(argv)
        return args.func(args)
    except speed_budget.BudgetError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
