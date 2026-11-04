"""Tests for the bounded CI command runner."""
from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/bounded_apt.py"


def _bounded() -> ModuleType:
    assert _SCRIPT.is_file(), "The bounded runner needs bounded_apt.py"
    spec = importlib.util.spec_from_file_location("bounded_apt", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _finish_on_spawn(bounded: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    original = bounded.subprocess.Popen

    def launch(*args: object, **kwargs: object) -> subprocess.Popen:
        process = original(*args, **kwargs)
        try:
            process.wait(timeout=10)
        except BaseException:
            process.kill()
            process.wait(timeout=3)
            raise
        return process

    monkeypatch.setattr(bounded.subprocess, "Popen", launch)


def test_first_success_prints_the_log(tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str],
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    monkeypatch.setattr(bounded, "time", _Clock())
    _finish_on_spawn(bounded, monkeypatch)
    evidence = tmp_path / "evidence"
    program = ("import sys; print('out', flush=True); print('err', file=sys.stderr); "
               "print('stdin=' + repr(sys.stdin.read()))")
    command = [sys.executable, "-c", program]
    assert bounded.main(["--budget", "2", "--evidence", str(evidence), "--", *command]) == 0
    output = capsys.readouterr()
    assert output.out == "out\nerr\nstdin=''\n" and output.err == ""
    assert (evidence / "attempt-1.log").read_text(encoding="utf-8") == output.out


def test_failed_attempt_is_reported_then_retried(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    monkeypatch.setattr(bounded, "BACKOFF_INITIAL", 0.05, raising=False)
    monkeypatch.setattr(bounded, "time", _Clock())
    _finish_on_spawn(bounded, monkeypatch)
    marker = tmp_path / "started"
    program = ("import pathlib, sys; p = pathlib.Path(sys.argv[1]); "
               "seen = p.exists(); p.touch(); print('success' if seen else 'retry me'); "
               "sys.exit(0 if seen else 7)")
    command = [sys.executable, "-c", program, str(marker)]
    evidence = tmp_path / "evidence"
    assert bounded.main(["--budget", "2", "--evidence", str(evidence), "--", *command]) == 0
    output = capsys.readouterr()
    assert output.out == "success\n"
    assert shlex.join(command) in output.err
    assert "attempt 1" in output.err and "7" in output.err and "retry me" in output.err
    assert (evidence / "attempt-1.log").read_text(encoding="utf-8") == "retry me\n"
    assert (evidence / "attempt-2.log").read_text(encoding="utf-8") == "success\n"


def test_no_new_attempt_starts_after_the_budget(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    monkeypatch.setattr(bounded, "BACKOFF_INITIAL", 1.0, raising=False)
    monkeypatch.setattr(bounded, "time", _Clock())
    _finish_on_spawn(bounded, monkeypatch)
    evidence = tmp_path / "evidence"
    command = [sys.executable, "-c", "import sys; print('failed'); sys.exit(7)"]
    assert bounded.main(["--budget", "0.3", "--evidence", str(evidence), "--", *command]) == 7
    output = capsys.readouterr()
    assert output.out == ""
    assert "attempt 1" in output.err and "7" in output.err
    assert [path.name for path in evidence.glob("attempt-*.log")] == ["attempt-1.log"]


def test_retry_backoff_doubles_caps_and_never_sleeps_past_the_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()

    clock = _Clock()
    monkeypatch.setattr(bounded, "time", clock)
    _finish_on_spawn(bounded, monkeypatch)
    command = [sys.executable, "-c", "import sys; sys.exit(7)"]
    assert bounded.main(["--budget", "100", "--evidence", str(tmp_path / "evidence"),
                         "--", *command]) == 7
    assert clock.sleeps == [1, 2, 4, 8, 16, 30, 30, 9]


def _limits(bounded: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bounded, "POLL_INTERVAL", 0.03, raising=False)
    monkeypatch.setattr(bounded, "KILL_GRACE", 0.12, raising=False)
    monkeypatch.setattr(bounded, "BACKOFF_INITIAL", 2.0, raising=False)


def _assert_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_flood_stops_for_log_size_and_reports_its_repeated_item(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "LOG_CAP", 4096)
    program = ("import os, time; print(os.getpid(), flush=True); "
               "\nwhile True:\n os.write(1, b'delayed item mirror\\n' * 64); "
               "time.sleep(0.002)")
    evidence = tmp_path / "evidence"
    command = [sys.executable, "-c", program]
    assert bounded.main(["--budget", "0.8", "--evidence", str(evidence), "--", *command]) == 124
    output = capsys.readouterr()
    raw = (evidence / "attempt-1.log").read_bytes()
    pid = int(raw.splitlines()[0])
    state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
    assert "reason: log-size" in state and "threshold: 4096 B" in state
    measured = int(re.search(r"measured: (\d+) B", state)[1])
    assert measured > 4096 and f"log-size ({measured} B)" in output.err
    assert "elapsed:" in state and "log size:" in state
    assert f"pid: {pid}" in state and f"ppid: {os.getpid()}" in state
    for field in ("state:", "name:", "VmRSS:", "VmHWM:", "VmPeak:",
                  "Threads:", "wchan:", "cmdline:"):
        assert field in state
    repeat = re.search(r"(\d+): delayed item mirror", output.err)
    assert repeat is not None and int(repeat[1]) > 3
    assert output.out == ""
    _assert_gone(pid)


def test_allocation_stops_for_memory(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "RSS_CAP", 24 * 1024 * 1024, raising=False)
    program = ("import os, time; print(os.getpid(), flush=True); "
               "data = bytearray(32 * 1024 * 1024); time.sleep(10)")
    evidence = tmp_path / "evidence"
    command = [sys.executable, "-c", program]
    assert bounded.main(["--budget", "0.8", "--evidence", str(evidence), "--", *command]) == 124
    output = capsys.readouterr()
    pid = int((evidence / "attempt-1.log").read_text(encoding="utf-8"))
    state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
    assert "reason: memory" in state and "threshold: 25165824 B" in state
    measured = int(re.search(r"measured: (\d+) B", state)[1])
    assert measured > 25165824 and f"memory ({measured} B)" in output.err
    _assert_gone(pid)


def test_sleeping_attempt_is_stopped_at_the_deadline(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    evidence = tmp_path / "evidence"
    program = "import os, time; print(os.getpid(), flush=True); time.sleep(10)"
    command = [sys.executable, "-c", program]
    start = time.monotonic()
    assert bounded.main(["--budget", "0.4", "--evidence", str(evidence), "--", *command]) == 124
    assert time.monotonic() - start < 2
    output = capsys.readouterr()
    pid = int((evidence / "attempt-1.log").read_text(encoding="utf-8"))
    state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
    assert "reason: deadline" in state and "threshold:" in state and "measured:" in state
    assert "deadline" in output.err
    _assert_gone(pid)


def test_ignoring_sigterm_requires_sigkill(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    evidence = tmp_path / "evidence"
    program = ("import os, signal, time; "
               "signal.signal(signal.SIGTERM, lambda *_: print('term received', flush=True)); "
               "print(os.getpid(), flush=True); time.sleep(10)")
    assert bounded.main(["--budget", "0.4", "--evidence", str(evidence), "--",
                         sys.executable, "-c", program]) == 124
    lines = (evidence / "attempt-1.log").read_text(encoding="utf-8").splitlines()
    assert lines[1:] == ["term received"]
    _assert_gone(int(lines[0]))


def test_memory_is_summed_across_the_process_group(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "RSS_CAP", 48 * 1024 * 1024)
    evidence = tmp_path / "evidence"
    child = "import time; data = bytearray(20 * 1024 * 1024); time.sleep(5)"
    program = ("import os, signal, subprocess, sys, time\n"
               "data = bytearray(20 * 1024 * 1024)\n"
               f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
               "def cleanup(*_):\n child.wait(timeout=3); sys.exit(0)\n"
               "signal.signal(signal.SIGTERM, cleanup)\n"
               "print(os.getpid(), child.pid, flush=True)\n"
               "time.sleep(5)\n")
    assert bounded.main(["--budget", "1", "--evidence", str(evidence), "--",
                         sys.executable, "-c", program]) == 124
    pids = [int(pid) for pid in (evidence / "attempt-1.log").read_text(
        encoding="utf-8").split()]
    assert len(pids) == 2
    state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
    assert "reason: memory" in state
    assert all(f"pid: {pid}\n" in state for pid in pids)
    values = [int(rss) * 1024 for rss in re.findall(r"^VmRSS: (\d+) KiB$", state, re.M)]
    assert len(values) == 2 and max(values) < 48 * 1024 * 1024 < sum(values)
    for pid in pids:
        _assert_gone(pid)


def test_a_stopped_attempt_is_retried_within_the_budget(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "LOG_CAP", 4096)
    monkeypatch.setattr(bounded, "BACKOFF_INITIAL", 0.05)
    evidence = tmp_path / "evidence"
    marker = tmp_path / "started"
    program = ("import os, pathlib, sys, time\n"
               "p = pathlib.Path(sys.argv[1])\n"
               "if p.exists():\n print('recovered'); sys.exit(0)\n"
               "p.touch()\n"
               "while True:\n os.write(1, b'retry\\n' * 128); time.sleep(0.002)\n")
    assert bounded.main(["--budget", "10", "--evidence", str(evidence), "--",
                         sys.executable, "-c", program, str(marker)]) == 0
    output = capsys.readouterr()
    assert output.out == "recovered\n" and "log-size" in output.err
    assert (evidence / "attempt-1-state.txt").is_file()
    assert (evidence / "attempt-2.log").read_text(encoding="utf-8") == "recovered\n"


def test_stat_name_with_spaces_and_parentheses_does_not_change_group_identity(
        tmp_path: Path) -> None:
    bounded = _bounded()
    process = tmp_path / "123"
    process.mkdir()
    (process / "stat").write_text("123 (worker ) with spaces) S 52 123 123\n", encoding="utf-8")
    (process / "status").write_text(
        "VmRSS:\t7 kB\nVmHWM:\t9 kB\nVmPeak:\t10 kB\nThreads:\t2\n", encoding="utf-8")
    (process / "wchan").write_text("waiting\n", encoding="utf-8")
    (process / "cmdline").write_bytes(b"worker\0arg\0")
    result = bounded._process(process, 123)  # pylint: disable=protected-access
    assert result is not None
    assert (result.pid, result.ppid, result.state, result.name, result.rss) == (
        123, 52, "S", "worker ) with spaces", 7 * 1024)
    assert result.cmdline == "worker arg"
    assert bounded._process(process, 124) is None  # pylint: disable=protected-access


def test_a_process_vanishing_during_snapshot_is_skipped(tmp_path: Path) -> None:
    bounded = _bounded()
    process = tmp_path / "123"
    process.mkdir()
    (process / "stat").write_text("123 (worker) S 52 123 123\n", encoding="utf-8")
    (process / "status").write_text("VmRSS:\t7 kB\n", encoding="utf-8")
    assert bounded._process(process, 123) is None  # pylint: disable=protected-access


def _cleanup_group(log: Path) -> None:
    if not log.exists():
        return
    pids = log.read_text(encoding="utf-8").split()
    if len(pids) == 2 and all(pid.isdecimal() for pid in pids):
        parent, member = (int(pid) for pid in pids)
        try:
            group = os.getpgid(member)
        except ProcessLookupError:
            pass
        else:
            if group == parent and group != os.getpgrp():
                os.killpg(group, signal.SIGKILL)


def test_orphaned_group_member_is_reaped_before_returning(tmp_path: Path) -> None:
    child = ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "print('ready',flush=True); time.sleep(5)")
    program = ("import os,subprocess,sys,time\n"
               f"child=subprocess.Popen([sys.executable,'-c',{child!r}],"
               "stdout=subprocess.PIPE,text=True)\n"
               "assert child.stdout.readline().strip() == 'ready'\n"
               "print(os.getpid(),child.pid,flush=True)\n"
               "time.sleep(5)\n")
    runner = ("import sys; from scripts.ci import bounded_apt as b; "
              "b.POLL_INTERVAL=.03; b.KILL_GRACE=.12; b.BACKOFF_INITIAL=2; "
              "raise SystemExit(b.main(sys.argv[1:]))")
    harness = ("import ctypes,os,pathlib,signal,subprocess,sys\n"
               "libc=ctypes.CDLL(None,use_errno=True)\n"
               "assert libc.prctl(36,1,0,0,0) == 0\n"
               "args=[sys.executable,'-c',sys.argv[3],'--budget','0.6',"
               "'--evidence',sys.argv[1],'--',sys.executable,'-c',sys.argv[2]]\n"
               "result=subprocess.run(args,capture_output=True,text=True,timeout=5)\n"
               "pids=[int(p) for p in (pathlib.Path(sys.argv[1])/'attempt-1.log')"
               ".read_text().split()]\n"
               "alive=[]\n"
               "for pid in pids:\n"
               " try: os.kill(pid,0)\n"
               " except ProcessLookupError: pass\n"
               " else: alive.append(pid)\n"
               "for pid in alive:\n"
               " try:\n"
               "  if os.getpgid(pid)==pids[0] and pids[0]!=os.getpgrp(): "
               "os.killpg(pids[0],signal.SIGKILL)\n"
               " except ProcessLookupError: pass\n"
               " try: os.waitpid(pid,os.WNOHANG)\n"
               " except ChildProcessError: pass\n"
               "print('status',result.returncode,'survivors',len(alive))\n"
               "print(result.stderr,file=sys.stderr)\n"
               "sys.exit(0 if result.returncode==124 and not alive else 1)\n")
    evidence = tmp_path / "evidence"
    log = evidence / "attempt-1.log"
    try:
        result = subprocess.run([sys.executable, "-c", harness, str(evidence), program, runner],
                                capture_output=True, text=True, check=False, timeout=8)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "status 124 survivors 0\n"
        parent, member = (int(pid) for pid in log.read_text(encoding="utf-8").split())
        state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
        assert "reason: deadline" in state
        assert f"pid: {parent}\n" in state and f"pid: {member}\n" in state
    finally:
        _cleanup_group(log)


@pytest.mark.parametrize("initial", [0, 1])
def test_main_preserves_the_callers_subreaper_setting(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: int) -> None:
    bounded = _bounded()
    monkeypatch.setattr(bounded, "time", _Clock())
    _finish_on_spawn(bounded, monkeypatch)
    libc = ctypes.CDLL(None, use_errno=True)
    original = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(original), 0, 0, 0) == 0
    assert libc.prctl(36, initial, 0, 0, 0) == 0
    try:
        assert bounded.main(["--budget", "2", "--evidence", str(tmp_path / "evidence"),
                             "--", sys.executable, "-c", "print('finished')"]) == 0
        actual = ctypes.c_int()
        assert libc.prctl(37, ctypes.byref(actual), 0, 0, 0) == 0
        assert actual.value == initial
    finally:
        assert libc.prctl(36, original.value, 0, 0, 0) == 0


def test_reaping_many_group_members_still_honours_the_cleanup_deadline(
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    clock = SimpleNamespace(now=0)

    def waitid(*_args: int) -> SimpleNamespace:
        clock.now += 1
        assert clock.now <= 10, "reaping continued beyond the deadline"
        return SimpleNamespace(si_pid=1000 + clock.now)

    def sleep(seconds: float) -> None:
        clock.now += seconds

    fake_os = SimpleNamespace(P_PGID=2, WEXITED=4, WNOHANG=1, WNOWAIT=8,
                              waitid=waitid, waitpid=lambda *_: None, killpg=lambda *_: None)
    monkeypatch.setattr(bounded, "os", fake_os)
    monkeypatch.setattr(bounded, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    child = SimpleNamespace(pid=123, poll=lambda: None)
    assert bounded._wait_group(child, 5) is False  # pylint: disable=protected-access
    assert 1 <= clock.now <= 6


def test_no_attempt_is_spawned_if_log_creation_consumes_the_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    original_open = Path.open
    original_popen = subprocess.Popen
    launches = []

    def slow_open(path: Path, *args: object, **kwargs: object):
        if path.name == "attempt-1.log" and args == ("wb",):
            time.sleep(0.2)
        return original_open(path, *args, **kwargs)

    def record_launch(*args: object, **kwargs: object):
        launches.append(True)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_open)
    monkeypatch.setattr(bounded.subprocess, "Popen", record_launch)
    assert bounded.main(["--budget", "0.1", "--evidence", str(tmp_path / "evidence"), "--",
                         sys.executable, "-c", "print('too late')"]) == 124
    assert not launches


def test_failure_summary_contains_the_last_ten_and_three_most_repeated_lines(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "time", _Clock())
    _finish_on_spawn(bounded, monkeypatch)
    program = ("import sys; print('alpha\\n' * 5 + 'bravo\\n' * 3 + 'charlie\\n' * 2 + "
               "'\\n'.join(f'tail-{i}' for i in range(12))); sys.exit(9)")
    assert bounded.main(["--budget", "0.3", "--evidence", str(tmp_path / "evidence"), "--",
                         sys.executable, "-c", program]) == 9
    output = capsys.readouterr().err
    last = output.split("last 10 lines:\n", 1)[1].split("most repeated lines:\n", 1)[0]
    repeated = output.split("most repeated lines:\n", 1)[1]
    assert last.splitlines() == [f"tail-{i}" for i in range(2, 12)]
    assert repeated.splitlines() == ["5: alpha", "3: bravo", "2: charlie"]


def test_failure_summary_truncates_each_line_it_prints(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "time", _Clock())
    _finish_on_spawn(bounded, monkeypatch)
    program = "import sys; print(('x' * 5000 + '\\n') * 2 + 'short'); sys.exit(9)"
    assert bounded.main(["--budget", "0.3", "--evidence", str(tmp_path / "evidence"), "--",
                         sys.executable, "-c", program]) == 9
    output = capsys.readouterr().err
    clipped = "x" * 500 + " ...[4500 more bytes]"
    last = output.split("last 10 lines:\n", 1)[1].split("most repeated lines:\n", 1)[0]
    repeated = output.split("most repeated lines:\n", 1)[1]
    assert last.splitlines() == [clipped, clipped, "short"]
    assert repeated.splitlines() == [f"2: {clipped}", "1: short"]
    assert len(output) < 2000


def test_an_oversized_log_summary_contains_its_actual_last_lines(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = _bounded()
    _limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "LOG_CAP", 4096)
    program = ("import os, time\n"
               "old = b'old line\\n' * 900\n"
               "end = ''.join(f'ending-{i}\\n' for i in range(12)).encode()\n"
               "os.write(1, old + end)\n"
               "time.sleep(10)\n")
    evidence = tmp_path / "evidence"
    assert bounded.main(["--budget", "0.8", "--evidence", str(evidence), "--",
                         sys.executable, "-c", program]) == 124
    output = capsys.readouterr().err
    assert "log-size" in output
    last = output.split("last 10 lines:\n", 1)[1].split("most repeated lines:\n", 1)[0]
    assert last.splitlines() == [f"ending-{i}" for i in range(2, 12)]
    log = evidence / "attempt-1.log"
    expected = log.read_bytes()[:4096].decode()
    assert bounded._read_log(log) == expected  # pylint: disable=protected-access


@pytest.mark.parametrize("budget", ["nan", "inf", "-inf"])
def test_non_finite_budget_is_rejected(tmp_path: Path, budget: str) -> None:
    bounded = _bounded()
    with pytest.raises(SystemExit) as error:
        bounded.main([f"--budget={budget}", "--evidence", str(tmp_path / "evidence"), "--",
                      sys.executable, "-c", "print('unexpected')"])
    assert error.value.code == 2
