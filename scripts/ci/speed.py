#!/usr/bin/env python3
"""Fail when this head's service or CLI is slower than the last release's.

The default mode runs on the head checkout. Each run resets its side's own
database from that side's schema, then runs `workload` under that side's own
interpreter from that side's tree. Runs interleave in release-head-head-release
blocks. Each workload's verdict is the median of its per-block head/release
ratios, so one block spoiled by a noisy neighbour cannot decide it; the gate
fails when that median exceeds 1 + --max-regression. Wall time is compared
only within one job because a runner's speed differs between jobs far more
than the threshold.

    speed.py --base DIR --base-python PY --head DIR --head-python PY
    speed.py workload --database NAME
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

WORKLOADS = ("service.publish", "service.list", "service.versions",
             "cli.transfer", "cli.text")
KEY = "speed-key"
CLIENTS = 4
USERS = ("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, "
         "config JSONB NOT NULL DEFAULT '{}'::jsonb)")


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
    """One keep-alive connection to a side's server."""

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


def cli(module: ModuleType, *argv: str) -> None:
    sys.argv = ["docs-hub", *argv]
    with contextlib.redirect_stdout(io.StringIO()):
        code = module.main()
    if code != 0:
        raise RuntimeError(f"docs-hub {' '.join(argv)} exited {code}")


def timed(action: Callable[[], object], repeats: int = 3) -> float:
    """Best of `repeats`, so a pause inside one run does not become its sample."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        action()
        best = min(best, time.perf_counter() - start)
    return best


def measure(port: int, scratch: Path) -> dict[str, float]:
    os.environ.update({"DOCS_HUB_URL": f"http://127.0.0.1:{port}", "DOCS_HUB_API_KEY": KEY})
    sys.path.insert(0, os.getcwd())
    from docs_hub_cli import cli as module
    services = [Service(port) for _ in range(CLIENTS)]
    small, large = document(24, 1), document(256, 2)
    slugs = [f"speed/doc-{n}" for n in range(96)]
    (scratch / "large.html").write_bytes(large)
    out = str(scratch / "out")

    def clients(work: Callable[[Service, list[str]], object]) -> None:
        # Each client owns one connection and every version of its own slugs.
        with ThreadPoolExecutor(CLIENTS) as pool:
            list(pool.map(lambda client: work(services[client], slugs[client::CLIENTS]),
                          range(CLIENTS)))

    def publish_all() -> None:
        clients(lambda service, mine: [service.publish(slug, small + bytes([65 + version]))
                                       for version in range(3) for slug in mine])

    def transfer(count: int = 40) -> None:
        for _ in range(count):
            cli(module, "publish", str(scratch / "large.html"), "--slug", "speed/large",
                "--title", "Large", "--from", "speed")
            cli(module, "get", "speed/large", "-o", out)

    def text(count: int = 10) -> None:
        for _ in range(count):
            cli(module, "get", "speed/large", "--text-only", "-o", out)

    services[0].publish("speed/warm", small)
    services[0].call("GET", "/api/list")
    services[0].call("GET", "/api/versions/speed/warm")
    transfer(2)
    text(2)
    return {
        "service.publish": timed(publish_all),
        "service.list": timed(lambda: clients(
            lambda service, mine: [service.call("GET", "/api/list") for _ in mine * 4])),
        "service.versions": timed(lambda: clients(
            lambda service, mine: [service.call("GET", f"/api/versions/{slug}")
                                   for slug in mine * 8])),
        "cli.transfer": timed(transfer),
        "cli.text": timed(text),
    }


def workload(database: str) -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory() as scratch:
        url = f"postgresql:///{database}"
        store = Path(scratch, "store")
        store.mkdir()
        values = {"DATABASE_URL_DOCS": url, "DATABASE_URL_AUTH": url, "STORE_ROOT": str(store),
                  "SESSION_SECRET": "speed-secret", "COOKIE_SECURE": "0"}
        # v0.3.2 reads only the unprefixed names; later trees prefer DOCS_HUB_.
        env = {**os.environ, **values, **{f"DOCS_HUB_{k}": v for k, v in values.items()},
               "DOCS_HUB_API_KEY": KEY}
        server = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"], env=env)
        try:
            for _ in range(300):
                if server.poll() is not None:
                    raise RuntimeError(f"the server exited {server.returncode} at startup")
                with contextlib.suppress(OSError):
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("the server did not start within 30 s")
            print(json.dumps(measure(port, Path(scratch))))
        finally:
            server.terminate()
            server.wait(timeout=30)
    return 0


def reset(tree: Path, database: str) -> None:
    for command in (["dropdb", "--if-exists", database], ["createdb", database],
                    ["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", database,
                     "-f", str(tree / "backend/schema.sql"), "-c", USERS,
                     "-c", f"ALTER DATABASE {database} SET synchronous_commit = off"]):
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def compare(base: dict[str, list[float]], head: dict[str, list[float]],
            max_regression: float) -> tuple[list[str], list[str]]:
    """Report rows and the workloads over the threshold; a missing block fails."""
    rows, failed = [], []
    for name in WORKLOADS:
        pairs = list(zip(base.get(name, []), head.get(name, [])))
        if not pairs or len(base[name]) != len(head[name]):
            rows.append(f"| {name} | - | - | - | missing blocks |")
            failed.append(name)
            continue
        change = statistics.median(after / before for before, after in pairs) - 1
        verdict = "FAIL" if change > max_regression else "ok"
        if verdict == "FAIL":
            failed.append(name)
        rows.append(f"| {name} | {statistics.median(base[name]):.3f} | "
                    f"{statistics.median(head[name]):.3f} | {change:+.1%} | {verdict} |")
    return rows, failed


def gate(args: argparse.Namespace) -> int:
    sides = {"base": (args.base, args.base_python), "head": (args.head, args.head_python)}
    samples: dict[str, dict[str, list[float]]] = {"base": {}, "head": {}}
    script = str(Path(__file__).resolve())
    # Block 0 is discarded: a cold page cache would land on whichever side runs first.
    # Each measured block runs release, head, head, release, so neither side always
    # runs first; a side's block sample is the mean of its two runs.
    for block in range(args.blocks + 1):
        runs: dict[str, list[dict[str, float]]] = {"base": [], "head": []}
        for side in ("base", "head") if block == 0 else ("base", "head", "head", "base"):
            tree, python = sides[side]
            database = f"docs_speed_{side}"
            reset(tree, database)
            done = subprocess.run([str(python), script, "workload", "--database", database],
                                  cwd=tree, check=True, stdout=subprocess.PIPE, text=True)
            runs[side].append(json.loads(done.stdout.splitlines()[-1]))
            print(f"block {block}{' (warm-up)' if block == 0 else ''} {side}: " + ", ".join(
                f"{name} {seconds:.3f}s" for name, seconds in runs[side][-1].items()))
        for side, times in runs.items():
            for name in times[0] if block else ():
                samples[side].setdefault(name, []).append(
                    statistics.mean(run[name] for run in times))
    rows, failed = compare(samples["base"], samples["head"], args.max_regression)
    report = "\n".join([
        f"### Speed: {args.label} -> head, {args.blocks} interleaved blocks", "",
        "| workload | release median (s) | head median (s) | median block change | verdict |",
        "|---|---|---|---|---|", *rows, "",
        f"A workload fails when its median block change exceeds +{args.max_regression:.0%}."])
    print(report)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(report + "\n")
    if failed:
        print(f"Slower than {args.label} beyond the threshold: {', '.join(failed)}",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["workload"]:
        parser = argparse.ArgumentParser(prog="speed.py workload")
        parser.add_argument("--database", required=True)
        return workload(parser.parse_args(argv[1:]).database)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for side in ("base", "head"):
        parser.add_argument(f"--{side}", type=Path, required=True)
        parser.add_argument(f"--{side}-python", type=Path, required=True)
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--max-regression", type=float, default=0.30)
    parser.add_argument("--label", default="release")
    parser.add_argument("--summary", default="")
    return gate(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
