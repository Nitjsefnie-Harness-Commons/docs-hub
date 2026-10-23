from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import login
from backend.app import app


def test_concurrent_pruning_preserves_every_recorded_failure(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Control the helper interleaving directly, without database/password work.
    # pylint: disable=protected-access
    snapshot_ready = Event()
    resume_pruning = Event()
    pruning_done = Event()
    start_hammering = Barrier(32)
    mutex = Lock()
    ip = "concurrent"

    class PausedSnapshot(list[float]):
        def __iter__(self) -> Iterator[float]:
            snapshot = self.copy()
            snapshot_ready.set()
            assert resume_pruning.wait(5), "recorder never reached the stale snapshot"
            return iter(snapshot)

    class NotifyingLock:
        def __enter__(self) -> None:
            # Wake a lock-owning pruner before this recorder waits for it.
            if snapshot_ready.is_set() and mutex.locked():
                resume_pruning.set()
            mutex.acquire()

        def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
            mutex.release()

    monkeypatch.setattr(login, "_FAILURES", {ip: PausedSnapshot([login.time.time()])})
    monkeypatch.setattr(login, "_FAILURES_LOCK", NotifyingLock(), raising=False)

    def prune() -> None:
        try:
            login._rate_limited(ip)
        finally:
            pruning_done.set()

    def record_and_prune() -> int:
        login._record_failure(ip)
        # Without synchronization, release pruning only AFTER appending so its
        # stale snapshot deterministically overwrites this completed record.
        resume_pruning.set()
        assert pruning_done.wait(5), "pruner never finished"
        start_hammering.wait(timeout=5)
        for _ in range(100):
            login._rate_limited(ip)
            login._record_failure(ip)
        return 101

    with ThreadPoolExecutor(max_workers=32) as workers:
        pruner = workers.submit(prune)
        try:
            assert snapshot_ready.wait(5), "pruner never captured the failure history"
            records = [workers.submit(record_and_prune)]
            pruner.result(timeout=5)
            records.extend(workers.submit(record_and_prune) for _ in range(31))
            recorded = sum(task.result(timeout=10) for task in records)
        finally:
            resume_pruning.set()
            pruning_done.set()
            start_hammering.abort()

    assert len(login._FAILURES[ip]) == 1 + recorded, "a recorded login failure was lost"
    assert login._rate_limited(ip)
    assert not login._rate_limited("other")


def test_login_throttles_five_recent_recorded_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SimpleNamespace(time=lambda: 1000.0)
    monkeypatch.setattr(login, "time", clock)
    monkeypatch.setattr(login, "_FAILURES", {"testclient": [700.0]})
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/login", data={}).status_code == 401
    assert client.post("/login", data={}).status_code == 429
    monkeypatch.setattr(clock, "time", lambda: 1300.0)
    assert client.post("/login", data={}).status_code == 401
