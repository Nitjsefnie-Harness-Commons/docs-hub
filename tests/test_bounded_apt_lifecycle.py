"""Process-group lifetime and retained-log limits for the CI runner."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import pytest

import test_bounded_apt as f

# pylint: disable=protected-access  # Drive the same in-process runner as its other tests.


def _cleanup(pid_file: Path) -> None:
    if not pid_file.exists():
        return
    leader, member = (int(pid) for pid in pid_file.read_text(encoding="utf-8").split())
    try:
        group = os.getpgid(member)
        if group == leader and group != os.getpgrp():
            os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        deadline = time.monotonic() + 3
        result = os.waitpid(member, os.WNOHANG)
        while result == (0, 0) and time.monotonic() < deadline:
            time.sleep(0.01)
            result = os.waitpid(member, os.WNOHANG)
        assert result != (0, 0), f"member {member} survived cleanup"
    except ChildProcessError:
        pass


def _wait_process(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=10)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        raise


@pytest.mark.parametrize("leader_status", [0, 7])
def test_leader_exit_waits_for_member_before_success_or_retry(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch, leader_status: int) -> None:
    bounded = f._bounded()
    monkeypatch.setattr(bounded, "POLL_INTERVAL", 0.03)
    monkeypatch.setattr(bounded, "BACKOFF_INITIAL", 0.02)
    release_read, release_write = os.pipe()
    marker, pids, evidence = tmp_path / "started", tmp_path / "pids", tmp_path / "evidence"
    member = "import os,sys; os.read(int(sys.argv[1]),1); print('member finished',flush=True)"
    program = ("import os,pathlib,subprocess,sys\n"
               "marker,pids=map(pathlib.Path,sys.argv[1:3])\n"
               "if marker.exists():\n print('retry succeeded'); sys.exit(0)\n"
               "marker.touch()\n"
               "fd=int(sys.argv[3])\n"
               "child=subprocess.Popen([sys.executable,'-c',sys.argv[4],str(fd)],"
               "pass_fds=(fd,))\n"
               "pids.write_text(f'{os.getpid()} {child.pid}')\n"
               "print('leader finished',flush=True)\n"
               "sys.exit(int(sys.argv[5]))\n")
    command = [sys.executable, "-c", program, str(marker), str(pids), str(release_read),
               member, str(leader_status)]
    original_popen = bounded.subprocess.Popen
    original_group = bounded._group
    launches: list[int] = []
    released = False
    overlap = False

    def launch(*args: object, **kwargs: object) -> subprocess.Popen:
        nonlocal released, overlap
        if launches and not released:
            overlap = True
            os.write(release_write, b"x")
            released = True
        kwargs["pass_fds"] = (release_read,)
        process = original_popen(*args, **kwargs)
        launches.append(process.pid)
        _wait_process(process)
        return process

    def sample(group: int):
        nonlocal released
        processes = original_group(group)
        if launches and group == launches[0] and not released:
            assert any(process.pid != group for process in processes)
            os.write(release_write, b"x")
            released = True
        return processes

    monkeypatch.setattr(bounded.subprocess, "Popen", launch)
    monkeypatch.setattr(bounded, "_group", sample)
    try:
        assert bounded.main(["--budget", "15", "--evidence", str(evidence), "--", *command]) == 0
        output = capsys.readouterr()
        assert released and not overlap
        first = (evidence / "attempt-1.log").read_text(encoding="utf-8")
        assert first.splitlines() == ["leader finished", "member finished"]
        if leader_status == 0:
            assert output.out == first and len(launches) == 1
        else:
            assert output.out == "retry succeeded\n" and len(launches) == 2
            assert "exit status 7" in output.err
        for pid in (int(value) for value in pids.read_text(encoding="utf-8").split()):
            f._assert_gone(pid)
    finally:
        os.close(release_read)
        os.close(release_write)
        _cleanup(pids)


@pytest.mark.parametrize("leader_status", [0, 7])
def test_exited_leaders_stalled_member_is_stopped_at_deadline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leader_status: int) -> None:
    bounded = f._bounded()
    f._limits(bounded, monkeypatch)
    pids, evidence = tmp_path / "pids", tmp_path / "evidence"
    member = ("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              "print('ready',flush=True); time.sleep(30)")
    program = ("import os,pathlib,subprocess,sys\n"
               "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]],"
               "stdout=subprocess.PIPE,text=True)\n"
               "assert child.stdout.readline().strip()=='ready'\n"
               "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')\n"
               "sys.exit(int(sys.argv[3]))\n")
    command = [sys.executable, "-c", program, str(pids), member, str(leader_status)]
    original_popen = bounded.subprocess.Popen

    def launch(*args: object, **kwargs: object) -> subprocess.Popen:
        process = original_popen(*args, **kwargs)
        _wait_process(process)
        return process

    monkeypatch.setattr(bounded.subprocess, "Popen", launch)
    try:
        assert bounded.main(["--budget", "1.2", "--evidence", str(evidence), "--", *command]) == 124
        state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
        assert "reason: deadline" in state
        for pid in (int(value) for value in pids.read_text(encoding="utf-8").split()):
            f._assert_gone(pid)
        assert len(list(evidence.glob("attempt-*.log"))) == 1
    finally:
        _cleanup(pids)


@pytest.mark.parametrize(("reason", "leader_status"), [("log-size", 0), ("memory", 7)])
def test_exited_leaders_member_still_hits_resource_guards(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, leader_status: int) -> None:
    bounded = f._bounded()
    f._limits(bounded, monkeypatch)
    if reason == "log-size":
        monkeypatch.setattr(bounded, "LOG_CAP", 4096)
        action = "while True:\n os.write(1,b'blocked item\\n'*512); time.sleep(.002)\n"
    else:
        monkeypatch.setattr(bounded, "RSS_CAP", 24 * 1024 * 1024)
        action = "data=bytearray(32*1024*1024); time.sleep(30)\n"
    member = "import os,sys,time\nsys.stdin.buffer.read()\n" + action
    pids, evidence = tmp_path / "pids", tmp_path / "evidence"
    program = ("import os,pathlib,subprocess,sys\n"
               "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]],stdin=subprocess.PIPE)\n"
               "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')\n"
               "sys.exit(int(sys.argv[3]))\n")
    command = [sys.executable, "-c", program, str(pids), member, str(leader_status)]
    original_popen = bounded.subprocess.Popen

    def launch(*args: object, **kwargs: object) -> subprocess.Popen:
        process = original_popen(*args, **kwargs)
        _wait_process(process)
        return process

    monkeypatch.setattr(bounded.subprocess, "Popen", launch)
    try:
        assert bounded.main(["--budget", "2", "--evidence", str(evidence), "--", *command]) == 124
        state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
        assert f"reason: {reason}" in state
        for pid in (int(value) for value in pids.read_text(encoding="utf-8").split()):
            f._assert_gone(pid)
    finally:
        _cleanup(pids)


def test_a_group_that_survives_cleanup_is_never_retried(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = f._bounded()
    f._limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "LOG_CAP", 4096)
    monkeypatch.setattr(bounded, "BACKOFF_INITIAL", 0.02)
    monkeypatch.setattr(bounded, "_stop", lambda _child: None)
    original_popen = bounded.subprocess.Popen
    launched: list[subprocess.Popen] = []

    def launch(*args: object, **kwargs: object) -> subprocess.Popen:
        process = original_popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(bounded.subprocess, "Popen", launch)
    program = "import os,time\nwhile True:\n os.write(1,b'line\\n'*1024); time.sleep(.002)\n"
    try:
        assert bounded.main(["--budget", "1.5", "--evidence", str(tmp_path / "evidence"),
                             "--", sys.executable, "-c", program]) == 124
        assert len(launched) == 1 and launched[0].poll() is None
    finally:
        for process in launched:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _LogicalChild:
    def __init__(self, clock: _Clock) -> None:
        self.pid = 123
        self.clock = clock
        self.finished = Event()
        self.polls = 0

    def poll(self) -> int | None:
        self.polls += 1
        assert self.polls < 20, "watchdog failed to advance its logical clock"
        return 0 if self.finished.is_set() else None

    def wait(self, timeout: float) -> int:
        self.clock.now = min(0.9, self.clock.now + timeout)
        if self.clock.now == 0.9:
            self.finished.set()
            return 0
        raise subprocess.TimeoutExpired("logical child", timeout)


def _exercise_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                       deadline: float, expected: int) -> None:
    bounded = f._bounded()
    clock = _Clock()
    child = _LogicalChild(clock)
    log, state = tmp_path / "attempt.log", tmp_path / "state.txt"
    log.write_text("finished\n", encoding="utf-8")
    stopped = []
    monkeypatch.setattr(bounded, "time", clock)
    monkeypatch.setattr(bounded, "POLL_INTERVAL", 0.25)
    monkeypatch.setattr(bounded, "_reap_group", lambda *_: None)
    monkeypatch.setattr(bounded, "_group", lambda *_: [])
    monkeypatch.setattr(bounded, "_signal", lambda *_: False)
    monkeypatch.setattr(bounded, "_stop", stopped.append)
    status, _result = bounded._watch(child, log, state, 0, deadline)
    assert status == expected
    if expected == 0:
        assert child.finished.is_set() and clock.now == 0.9 and not stopped
        assert not state.exists()
    else:
        assert not child.finished.is_set() and clock.now == 0.5 and stopped == [child]
        assert "reason: deadline" in state.read_text(encoding="utf-8")


def test_success_near_the_deadline_is_not_stopped(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exercise_deadline(tmp_path, monkeypatch, 1.0, 0)


def test_deadline_before_logical_completion_stops(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exercise_deadline(tmp_path, monkeypatch, 0.5, 124)


def test_success_during_a_slow_snapshot_is_not_misclassified_as_a_timeout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = f._bounded()
    clock = _Clock()
    child = _LogicalChild(clock)
    log, state = tmp_path / "attempt.log", tmp_path / "state.txt"
    log.write_text("finished\n", encoding="utf-8")

    def sample(_group: int) -> list:
        clock.now = 0.1
        child.finished.set()
        clock.now = 1.5
        return []

    monkeypatch.setattr(bounded, "time", clock)
    monkeypatch.setattr(bounded, "_reap_group", lambda *_: None)
    monkeypatch.setattr(bounded, "_group", sample)
    monkeypatch.setattr(bounded, "_signal", lambda *_: False)
    assert bounded._watch(child, log, state, 0, 1) == (0, "exit status 0")
    assert child.finished.is_set() and not state.exists()


@pytest.mark.parametrize("status", [0, 7])
@pytest.mark.parametrize("cap", [15, 16])
def test_natural_exit_retains_bounded_head_and_tail(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, cap: int) -> None:
    bounded = f._bounded()
    monkeypatch.setattr(bounded, "LOG_CAP", cap)
    monkeypatch.setattr(bounded, "BACKOFF_INITIAL", 2.0)
    monkeypatch.setattr(bounded, "time", _Clock())
    original_popen = bounded.subprocess.Popen
    modes: list[int] = []

    def launch(*args: object, **kwargs: object) -> subprocess.Popen:
        modes.append(os.fstat(kwargs["stdout"].fileno()).st_mode & 0o777)
        process = original_popen(*args, **kwargs)
        _wait_process(process)
        return process

    monkeypatch.setattr(bounded.subprocess, "Popen", launch)
    raw = b"AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"
    command = [sys.executable, "-c", f"import os,sys; os.write(1,{raw!r}); sys.exit({status})"]
    evidence = tmp_path / "evidence"
    assert bounded.main(["--budget", "1", "--evidence", str(evidence), "--", *command]) == status
    half = cap // 2
    marker = f"\n... {len(raw) - 2 * half} bytes elided ...\n".encode()
    log = evidence / "attempt-1.log"
    assert log.read_bytes() == raw[:half] + marker + raw[-half:]
    assert len(log.read_bytes()) <= cap + len(marker)
    assert log.stat().st_mode & 0o777 == modes[0]


def test_sigterm_ignoring_flood_has_bounded_retained_log(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = f._bounded()
    f._limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "LOG_CAP", 4096)
    program = ("import os,signal,time\n"
               "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
               "print(os.getpid(),flush=True)\n"
               "while True:\n os.write(1,b'delayed item mirror\\n'*512); time.sleep(.002)\n")
    evidence = tmp_path / "evidence"
    assert bounded.main(["--budget", "1.5", "--evidence", str(evidence), "--",
                         sys.executable, "-c", program]) == 124
    output = capsys.readouterr()
    raw = (evidence / "attempt-1.log").read_bytes()
    state = (evidence / "attempt-1-state.txt").read_text(encoding="utf-8")
    marker = re.search(rb"\n?\.\.\. (\d+) bytes elided \.\.\.\n", raw)
    assert marker is not None
    assert len(raw) <= 4096 + len(marker[0])
    assert int(marker[1]) > 0 and "reason: log-size" in state and "log-size" in output.err
    assert raw[-128:].count(b"delayed item mirror") > 1
    f._assert_gone(int(raw.splitlines()[0]))


@pytest.mark.parametrize("stage", ["write", "replace"])
def test_failed_compaction_preserves_original_log(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    bounded = f._bounded()
    monkeypatch.setattr(bounded, "LOG_CAP", 16)
    log = tmp_path / "attempt-1.log"
    original = b"0123456789" * 5
    log.write_bytes(original)

    if stage == "replace":
        def fail_replace(*_args: object) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(bounded.os, "replace", fail_replace)
    else:
        original_temp = bounded.tempfile.NamedTemporaryFile

        class FailingWrite:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.file = original_temp(*args, **kwargs)
                self.name = self.file.name

            def __enter__(self):
                self.file.__enter__()
                return self

            def __exit__(self, *args: object):
                return self.file.__exit__(*args)

            def fileno(self) -> int:
                return self.file.fileno()

            def write(self, _data: bytes) -> None:
                raise OSError("write failed")

        monkeypatch.setattr(bounded.tempfile, "NamedTemporaryFile", FailingWrite)
    with pytest.raises(OSError, match=f"{stage} failed"):
        bounded._compact_log(log)
    assert log.read_bytes() == original
    assert list(tmp_path.iterdir()) == [log]


def test_state_write_failure_still_bounds_the_stopped_attempt_log(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bounded = f._bounded()
    f._limits(bounded, monkeypatch)
    monkeypatch.setattr(bounded, "LOG_CAP", 4096)

    def fail_state(*_args: object) -> None:
        raise OSError("state write failed")

    monkeypatch.setattr(bounded, "_state", fail_state)
    program = ("import os,time; print(os.getpid(),flush=True)\n"
               "while True:\n os.write(1,b'line\\n'*1024); time.sleep(.002)\n")
    evidence = tmp_path / "evidence"
    with pytest.raises(OSError, match="state write failed"):
        bounded.main(["--budget", "2", "--evidence", str(evidence), "--",
                      sys.executable, "-c", program])
    raw = (evidence / "attempt-1.log").read_bytes()
    marker = re.search(rb"\n?\.\.\. (\d+) bytes elided \.\.\.\n", raw)
    assert marker is not None and len(raw) <= 4096 + len(marker[0])
    f._assert_gone(int(raw.splitlines()[0]))
