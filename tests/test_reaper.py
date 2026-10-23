"""The reaper is a lifespan background task; drive it through the app's
lifespan (TestClient as a context manager runs startup/shutdown) with a
tiny interval, and through `loop` directly for the error path."""
import asyncio
import os
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import suppress

import pytest
from fastapi.testclient import TestClient

from backend import db, docs_repo, reaper
from backend.app import app

KEY = {"x-docs-key": "test-api-key"}


def _backdate(slug):
    with db.docs_conn() as c:
        c.execute("UPDATE docs SET expires_at = now() - interval '1 second' "
                  "WHERE slug=%s", (slug,))
        c.commit()


def _row_count(slug):
    with db.docs_conn() as c:
        return c.execute("SELECT count(*) FROM docs WHERE slug=%s",
                         (slug,)).fetchone()[0]


def test_lifespan_reaper_purges_expired_docs(monkeypatch):
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "0.05")
    with TestClient(app) as c:
        c.post("/api/publish",
               data={"slug": "reap/me", "title": "R", "from": "analyst",
                     "ttl": "1h"},
               files={"file": ("d.html", b"<h1>x</h1>", "text/html")},
               headers=KEY)
        _backdate("reap/me")
        deadline = time.monotonic() + 5
        while _row_count("reap/me") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _row_count("reap/me") == 0
        assert not os.path.exists(
            os.path.join(os.environ["STORE_ROOT"], "reap/me"))


@pytest.mark.parametrize("purge_fails", [False, True])
@pytest.mark.parametrize("cancel_inner", [False, True])
# Keep the thread handshakes and shutdown-order verdict together in one regression.
# pylint: disable-next=too-many-statements
def test_lifespan_waits_for_purge_before_closing_pools(
        monkeypatch: pytest.MonkeyPatch, purge_fails: bool, cancel_inner: bool) -> None:
    """Cancelling the await cannot stop its worker thread: shutdown must
    drain it before closing pools, even if the purge fails while draining.
    Release follows observed cancellation, even when shutdown starts late.
    """
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "0.05")
    started = threading.Event()
    release = threading.Event()
    entered = threading.Event()
    shutdown_go = threading.Event()
    closed = threading.Event()
    observing = threading.Event()
    order: list[str] = []
    errors: list[Exception] = []
    tasks: list[asyncio.Task] = []
    real_close_pools = db.close_pools
    real_start = reaper.start

    def purge() -> int:
        started.set()
        assert release.wait(10), "purge was never released"
        order.append("purge-finished")
        if purge_fails:
            raise RuntimeError("purge failed during shutdown")
        return 0

    def close_pools() -> None:
        order.append("close_pools")
        real_close_pools()
        closed.set()

    def start(interval: float | None = None) -> asyncio.Task:
        task = real_start(interval)
        tasks.append(task)
        return task

    def lifecycle() -> None:
        try:
            with TestClient(app):
                entered.set()
                assert shutdown_go.wait(10), "shutdown was never requested"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)

    async def release_purge() -> None:
        task = tasks[0]
        observing.set()
        async with asyncio.timeout(10):
            while not task.done():
                if task.cancelling() >= 1:
                    # Deliver cancellation before observing the live drain.
                    await asyncio.sleep(0)
                    if cancel_inner:
                        jobs = [pending for pending in asyncio.all_tasks()
                                if getattr(pending.get_coro(), "cr_code", None)
                                is asyncio.to_thread.__code__]
                        assert len(jobs) == 1, "expected the blocked purge job"
                        jobs[0].cancel()
                        # Deliver job cancellation and its reaper wakeup before release.
                        await asyncio.sleep(0)
                        await asyncio.sleep(0)
                    if not task.done():
                        release.set()
                    return
                await asyncio.sleep(0.001)

    monkeypatch.setattr(docs_repo, "purge_expired", purge)
    monkeypatch.setattr(db, "close_pools", close_pools)
    monkeypatch.setattr(reaper, "start", start)
    thread = threading.Thread(target=lifecycle)
    thread.start()
    try:
        assert entered.wait(10), "lifespan never entered"
        assert started.wait(10), "purge never started"
        releaser = asyncio.run_coroutine_threadsafe(release_purge(), tasks[0].get_loop())
        assert observing.wait(10), "cancellation observer never started"
        # Hold the lifecycle past the old timed release to exercise late shutdown.
        assert not release.wait(0.6), "purge released before shutdown"
        shutdown_go.set()
        try:
            releaser.result(10)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(exc)
        if not release.is_set():
            assert closed.wait(10), "reaper stopped without closing pools"
    finally:
        release.set()
        shutdown_go.set()
        thread.join(10)
    assert not thread.is_alive(), "shutdown did not finish"
    assert order == ["purge-finished", "close_pools"]
    assert not errors
    assert order.count("close_pools") == 1
    # pylint: disable-next=protected-access
    assert db._DOCS is None
    # pylint: disable-next=protected-access
    assert db._AUTH is None


@pytest.mark.parametrize("purge_fails", [False, True])
def test_loop_drains_purge_through_repeated_cancellation(
        monkeypatch: pytest.MonkeyPatch, purge_fails: bool) -> None:
    """Another cancellation must leave the worker protected until it exits;
    returning CancelledError early would let lifespan close its pools.
    """
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def purge() -> int:
        started.set()
        assert release.wait(10), "purge was never released"
        order.append("purge-finished")
        if purge_fails:
            raise RuntimeError("purge failed during shutdown")
        return 0

    monkeypatch.setattr(docs_repo, "purge_expired", purge)

    async def run() -> None:
        task = asyncio.create_task(reaper.loop(60))
        task.add_done_callback(lambda _task: order.append("task-finished"))
        try:
            assert await asyncio.to_thread(started.wait, 10), "purge never started"
            task.cancel()
            await asyncio.sleep(0)
            assert task.cancelling() == 1 and not task.done(), "reaper never drained"
            task.cancel()
            # Let cancellation and the completion callback run before release.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 10)
            await asyncio.sleep(0)
            assert order == ["purge-finished", "task-finished"]
        finally:
            release.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())


def _run_shutdown_probe(mode: str) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent("""
        import asyncio
        import sys
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import suppress
        from backend import docs_repo, reaper

        mode = sys.argv[1]
        queued = mode == "queued"
        pre_entry = mode.startswith("pre-entry")
        started = threading.Event()
        submitted = threading.Event()
        purge_queued = threading.Event()
        before_entry = threading.Event()
        decision_checked = threading.Event()
        decision_continue = threading.Event()
        release = threading.Event()
        futures = []

        def purge():
            started.set()
            print("purge-started", flush=True)
            assert release.wait(10), "runner never cancelled both tasks"
            print("purge-finished", flush=True)
            return 0

        def blocker():
            assert release.wait(10), "queued job was not cancelled"
            print("blocker-finished", flush=True)

        class ObservedExecutor(ThreadPoolExecutor):
            def submit(self, func, *args, **kwargs):
                def invoke():
                    if pre_entry:
                        before_entry.set()
                        assert release.wait(10), "wrapper was never released"
                    return func(*args, **kwargs)

                future = super().submit(invoke)
                futures.append(future)
                if queued and submitted.is_set():
                    # Release the sole worker only after its queued purge is cancelled.
                    future.add_done_callback(
                        lambda done: release.set() if done.cancelled() else None)
                    purge_queued.set()
                    print("purge-queued", flush=True)
                submitted.set()
                return future

        class PausedState(dict):
            def __init__(self, state):
                self.state = state

            def __getitem__(self, key):
                value = self.state[key]
                if key == "skip":
                    decision_checked.set()
                    assert decision_continue.wait(10), "start decision never resumed"
                return value

            def __setitem__(self, key, value):
                self.state[key] = value

        class WitnessLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if not self.lock.acquire(blocking=False):
                    decision_continue.set()
                    self.lock.acquire()

            def __exit__(self, *exc):
                self.lock.release()

        if mode == "decision":
            real_purge = reaper._purge

            def pause_decision(state, lock, done):
                return real_purge(PausedState(state), lock, done)

            reaper._purge = pause_decision
            reaper.Lock = WitnessLock

        def release_after_cancellation(tasks):
            deadline = time.monotonic() + 2
            while not all(task.cancelling() for task in tasks):
                assert time.monotonic() < deadline, "runner did not cancel its tasks"
                time.sleep(0.001)
            release.set()

        async def main():
            loop = asyncio.get_running_loop()
            executor = ObservedExecutor(max_workers=1)
            loop.set_default_executor(executor)
            if queued:
                executor.submit(blocker)
            docs_repo.purge_expired = purge
            task = asyncio.create_task(reaper.loop(60))
            # Observe submission itself, without queueing another executor job.
            async with asyncio.timeout(2):
                if queued:
                    while not purge_queued.is_set():
                        await asyncio.sleep(0.001)
                elif pre_entry:
                    while not before_entry.is_set():
                        await asyncio.sleep(0.001)
                elif mode == "decision":
                    while not decision_checked.is_set():
                        await asyncio.sleep(0.001)
                else:
                    while not started.is_set():
                        await asyncio.sleep(0.001)
            if pre_entry or mode == "decision":
                assert futures[0].running(), "executor has not claimed the job"
                if mode == "pre-entry-outer":
                    task.cancel()
                else:
                    for pending in asyncio.all_tasks():
                        if pending is not asyncio.current_task():
                            pending.cancel()
                try:
                    if mode == "decision":
                        async with asyncio.timeout(2):
                            while not task.done() and not started.is_set():
                                await asyncio.sleep(0.001)
                        if task.done():
                            print("reaper-finished", flush=True)
                            decision_continue.set()
                            release.set()
                            return
                        release.set()
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(task), 1)
                    print("reaper-finished", flush=True)
                finally:
                    decision_continue.set()
                    release.set()
            elif not queued:
                pending = [t for t in asyncio.all_tasks()
                           if t is not asyncio.current_task()]
                threading.Thread(target=release_after_cancellation, args=(pending,)).start()
            # Returning makes asyncio.run cancel BOTH the reaper and its job.

        asyncio.run(main())
        if pre_entry and not started.is_set():
            print("skipped", flush=True)
        print("runner-finished", flush=True)
    """)
    return subprocess.run([sys.executable, "-c", script, mode],
                          capture_output=True, text=True, check=False, timeout=5)


@pytest.mark.parametrize("queued", [False, True])
def test_runner_shutdown_drains_running_or_cancels_queued_purge(queued: bool) -> None:
    """Runner shutdown cancels the inner task too; a subprocess bounds a
    spinning drain, including when no purge thread ever started.
    """
    result = _run_shutdown_probe("queued" if queued else "running")
    assert result.returncode == 0, result.stderr
    assert "runner-finished" in result.stdout
    if queued:
        assert "purge-queued" in result.stdout
        assert "blocker-finished" in result.stdout
        assert "purge-started" not in result.stdout
    else:
        assert "purge-finished" in result.stdout


@pytest.mark.parametrize("cancel_inner", [False, True])
def test_cancellation_before_wrapper_entry_skips_purge(cancel_inner: bool) -> None:
    """An executor future can be running before the wrapper enters: shutdown
    must prevent that wrapper from touching a database after the reaper exits.
    """
    result = _run_shutdown_probe("pre-entry" if cancel_inner else "pre-entry-outer")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["reaper-finished", "skipped", "runner-finished"]


def test_cancellation_during_start_decision_drains_purge() -> None:
    """Pause after checking skip; lock contention lets the worker commit its
    start before cancellation reads it, while a check-then-act mutant loses it.
    """
    result = _run_shutdown_probe("decision")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "purge-started", "purge-finished", "reaper-finished", "runner-finished"]


def test_loop_survives_a_failing_purge(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db went away")
        return 0

    monkeypatch.setattr(docs_repo, "purge_expired", boom)

    async def run():
        task = asyncio.get_running_loop().create_task(reaper.loop(0.01))
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert len(calls) >= 2


def test_start_reads_the_interval_from_the_environment(monkeypatch):
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "123.5")
    seen = {}

    async def fake_loop(interval):
        seen["interval"] = interval

    monkeypatch.setattr(reaper, "loop", fake_loop)

    async def run():
        task = reaper.start()
        await task

    asyncio.run(run())
    assert seen["interval"] == 123.5


@pytest.mark.parametrize("raw", ["0", "-5", "soon"])
def test_start_rejects_a_non_positive_or_unparseable_interval(monkeypatch, raw):
    """Zero or a negative interval turns asyncio.sleep into a bare yield, so
    the reaper would spin on the DB forever; an unparseable one used to blow
    up deep inside the lifespan. Both fail loudly at start()."""
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", raw)

    async def run():
        reaper.start()

    with pytest.raises(ValueError, match="PURGE_INTERVAL_SECONDS"):
        asyncio.run(run())


def test_lifespan_closes_the_pools_when_the_interval_is_bad(monkeypatch):
    """reaper.start() runs inside the lifespan's try, so a rejected interval
    still unwinds through db.close_pools() instead of leaving the pools (and
    their non-daemon worker threads) open."""
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError, match="PURGE_INTERVAL_SECONDS"):
        with TestClient(app):
            pass
    # The pool global is the only observable proof the finally ran.
    # pylint: disable-next=protected-access
    assert db._DOCS is None


def test_lifespan_closes_the_pools_when_the_schema_check_fails(monkeypatch):
    """schema_check() runs inside the lifespan's try too, so a stale
    deployment's RuntimeError unwinds through db.close_pools() rather than
    leaking the pools migrate() just opened."""
    def boom():
        raise RuntimeError("docs.expires_at is missing")

    monkeypatch.setattr(db, "schema_check", boom)
    with pytest.raises(RuntimeError, match="expires_at"):
        with TestClient(app):
            pass
    # The pool global is the only observable proof the finally ran.
    # pylint: disable-next=protected-access
    assert db._DOCS is None
