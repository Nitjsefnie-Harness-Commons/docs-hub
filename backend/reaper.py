"""Background purge of expired documents.

Reads already hide an expired doc the moment its time passes (docs_repo's
live filter); this loop is what actually removes the rows and the blobs.
Runs once at startup, then every DOCS_HUB_PURGE_INTERVAL_SECONDS (default 60). One
failed purge is logged and the loop carries on — a transient DB outage must
not leave the service without a reaper until the next restart.
Cancellation drains the purge's thread-completion signal before unwinding:
runner shutdown can cancel every asyncio task, so a cancelled job alone cannot
prove its worker has stopped. Otherwise it could reopen pools after shutdown
closes them. Repeated cancellation and purge failures must not mask this drain.
"""
from __future__ import annotations

import asyncio
import logging
import math
from contextlib import suppress
from threading import Event, Lock

from backend import docs_repo, settings

_log = logging.getLogger(__name__)
DEFAULT_INTERVAL = 60.0


def _purge(state: dict[str, bool], lock: Lock, done: Event) -> int:
    # Under this lock, purge starts and drains or skips; it cannot run after reaper exit.
    with lock:
        if state["skip"]:
            return 0
        state["started"] = True
    try:
        return docs_repo.purge_expired()
    finally:
        done.set()


async def loop(interval: float) -> None:
    while True:
        lock = Lock()
        state = {"started": False, "skip": False}
        done = Event()
        job = asyncio.ensure_future(asyncio.to_thread(_purge, state, lock, done))
        try:
            removed = await asyncio.shield(job)
            if removed:
                _log.info("purged %d expired doc(s)", removed)
        except asyncio.CancelledError:
            with lock:
                if not state["started"]:
                    state["skip"] = True
                started = state["started"]
            if not started:
                job.cancel()
            while started and not done.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.05)
            with suppress(Exception, asyncio.CancelledError):
                await job
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            # A reaper that dies on the first transient error is worse than
            # none: the loop is the only thing that ever frees the blobs.
            _log.exception("purge_expired failed; will retry")
        await asyncio.sleep(interval)


def _interval_from_env() -> float:
    """DOCS_HUB_PURGE_INTERVAL_SECONDS as a positive finite float, or DEFAULT_INTERVAL
    when it is unset or empty.

    Rejecting 0, a negative value and inf is the point: asyncio.sleep of any
    of those is a bare yield (or a wait that never ends), so a typo would
    either spin purge_expired against the DB continuously or silently disable
    the reaper for the process's whole life. Failing at startup is louder
    than either.
    """
    raw = settings.setting("PURGE_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_INTERVAL
    try:
        interval = float(raw)
    except ValueError:
        interval = math.nan  # falls into the one rejection below
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("DOCS_HUB_PURGE_INTERVAL_SECONDS must be a positive number, "
                         f"got {raw!r}")
    return interval


def start(interval: float | None = None) -> asyncio.Task:
    """Schedule the loop on the running event loop and return the task so the
    caller can cancel it at shutdown."""
    if interval is None:
        interval = _interval_from_env()
    return asyncio.get_running_loop().create_task(loop(interval))
