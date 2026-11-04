#!/usr/bin/env python3
"""Run a command within a budget and retain its output as evidence."""
from __future__ import annotations

import argparse
import ctypes
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LOG_CAP = 8 * 1024 * 1024
RSS_CAP = 1024 ** 3
POLL_INTERVAL = 0.5
KILL_GRACE = 5.0
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 30.0
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def _prctl(option: int, value: object) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@contextmanager
def _subreaper() -> Iterator[None]:
    previous = ctypes.c_int()
    _prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(previous))
    if not previous.value:
        _prctl(PR_SET_CHILD_SUBREAPER, 1)
    try:
        yield
    finally:
        if not previous.value:
            _prctl(PR_SET_CHILD_SUBREAPER, 0)


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    state: str
    name: str
    rss: int
    hwm: str
    peak: str
    threads: str
    wchan: str
    cmdline: str


def _process(path: Path, group: int) -> Process | None:
    try:
        stat = (path / "stat").read_text(encoding="utf-8", errors="replace")
        close = stat.rfind(")")
        fields = stat[close + 1:].split()
        if int(fields[2]) != group:
            return None
        status = dict(line.split(":", 1) for line in
                      (path / "status").read_text(encoding="utf-8", errors="replace").splitlines()
                      if ":" in line)
        rss = int(status.get("VmRSS", "0 kB").split()[0]) * 1024
        return Process(int(path.name), int(fields[1]), fields[0],
                       stat[stat.find("(") + 1:close], rss,
                       status.get("VmHWM", " unavailable").strip().replace("kB", "KiB"),
                       status.get("VmPeak", " unavailable").strip().replace("kB", "KiB"),
                       status.get("Threads", " unavailable").strip(),
                       (path / "wchan").read_text(encoding="utf-8").strip(),
                       (path / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                           "utf-8", errors="replace").strip())
    except (FileNotFoundError, ProcessLookupError):
        return None


def _group(group: int) -> list[Process]:
    processes = []
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            process = _process(path, group)
            if process is not None:
                processes.append(process)
    return processes


def _signal(group: int, sig: int) -> bool:
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        return False
    return True


def _reap_group(child: subprocess.Popen, deadline: float) -> None:
    child.poll()
    while time.monotonic() < deadline:
        try:
            status = os.waitid(os.P_PGID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is None:
                return
            if status.si_pid == child.pid:
                child.poll()
            else:
                os.waitpid(status.si_pid, os.WNOHANG)
        except ChildProcessError:
            return


def _wait_group(child: subprocess.Popen, deadline: float) -> bool:
    while True:
        _reap_group(child, deadline)
        if not _signal(child.pid, 0):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(POLL_INTERVAL, remaining))


def _stop(child: subprocess.Popen) -> None:
    _signal(child.pid, signal.SIGTERM)
    if not _wait_group(child, time.monotonic() + KILL_GRACE):
        _signal(child.pid, signal.SIGKILL)
        if not _wait_group(child, time.monotonic() + KILL_GRACE):
            print(f"process group {child.pid} did not exit after SIGKILL", file=sys.stderr)
    try:
        child.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        print(f"process {child.pid} could not be reaped", file=sys.stderr)


def _state(path: Path, reason: str, threshold: str, measured: str,
           elapsed: float, size: int, processes: list[Process]) -> None:
    lines = [f"reason: {reason}", f"threshold: {threshold}", f"measured: {measured}",
             f"elapsed: {elapsed:.3f} seconds", f"log size: {size} B"]
    for process in processes:
        lines.extend([f"pid: {process.pid}", f"ppid: {process.ppid}",
                      f"state: {process.state}", f"name: {process.name}",
                      f"VmRSS: {process.rss // 1024} KiB", f"VmHWM: {process.hwm}",
                      f"VmPeak: {process.peak}", f"Threads: {process.threads}",
                      f"wchan: {process.wchan}", f"cmdline: {process.cmdline}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _watch(child: subprocess.Popen, log: Path, state: Path, started: float,
           deadline: float) -> tuple[int, str]:
    while True:
        status = child.poll()
        if status is not None:
            return status, f"exit status {status}"
        size = log.stat().st_size
        processes = _group(child.pid)
        rss = sum(process.rss for process in processes)
        status = child.poll()
        if status is not None:
            return status, f"exit status {status}"
        now = time.monotonic()
        if now >= deadline:
            reason = "deadline"
            threshold = f"{deadline - started:.3f} seconds"
            measured = f"{now - started:.3f} seconds"
        elif size > LOG_CAP:
            reason, threshold, measured = "log-size", f"{LOG_CAP} B", f"{size} B"
        elif rss > RSS_CAP:
            reason, threshold, measured = "memory", f"{RSS_CAP} B", f"{rss} B"
        else:
            try:
                child.wait(timeout=max(0, min(POLL_INTERVAL, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
            continue
        _state(state, reason, threshold, measured, now - started, size, processes)
        _stop(child)
        return 124, f"{reason} ({measured})"


def _read_log(path: Path, *, tail: bool = False) -> str:
    with path.open("rb") as output:
        if tail:
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - LOG_CAP))
        return output.read(LOG_CAP).decode("utf-8", errors="replace")


def _report(command: list[str], attempt: int, result: str, log: str) -> None:
    lines = log.splitlines()
    print(f"{shlex.join(command)}: attempt {attempt}: {result}", file=sys.stderr)
    print("last 10 lines:", file=sys.stderr)
    for line in lines[-10:]:
        print(line, file=sys.stderr)
    print("most repeated lines:", file=sys.stderr)
    for line, count in Counter(lines).most_common(3):
        print(f"{count}: {line}", file=sys.stderr)


def _run(command: list[str], evidence: Path, started: float, deadline: float) -> int:
    attempt = 0
    status = 124
    backoff = BACKOFF_INITIAL
    while time.monotonic() < deadline:
        attempt += 1
        log = evidence / f"attempt-{attempt}.log"
        with log.open("wb") as output:
            if time.monotonic() >= deadline:
                break
            # A Popen context manager waits without a bound on exit.
            # pylint: disable-next=consider-using-with
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            try:
                status, result = _watch(child, log,
                                        evidence / f"attempt-{attempt}-state.txt",
                                        started, deadline)
            except BaseException:
                _stop(child)
                raise
        content = _read_log(log, tail=status != 0)
        if status == 0:
            print(content, end="")
            return 0
        _report(command, attempt, result, content)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * 2, BACKOFF_MAX)
    return status


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=float, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not math.isfinite(args.budget):
        parser.error("--budget must be finite")
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    args.evidence.mkdir(parents=True, exist_ok=True)
    with _subreaper():
        return _run(command, args.evidence, started, started + args.budget)


if __name__ == "__main__":
    sys.exit(main())
