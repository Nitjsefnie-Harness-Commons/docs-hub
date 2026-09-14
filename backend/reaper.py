"""Background purge of expired documents.

Reads already hide an expired doc the moment its time passes (docs_repo's
live filter); this loop is what actually removes the rows and the blobs.
Runs once at startup, then every PURGE_INTERVAL_SECONDS (default 60). One
failed purge is logged and the loop carries on — a transient DB outage must
not leave the service without a reaper until the next restart.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os

from backend import docs_repo

_log = logging.getLogger(__name__)
DEFAULT_INTERVAL = 60.0


async def loop(interval: float) -> None:
    while True:
        try:
            removed = await asyncio.to_thread(docs_repo.purge_expired)
            if removed:
                _log.info("purged %d expired doc(s)", removed)
        except Exception:  # pylint: disable=broad-exception-caught
            # A reaper that dies on the first transient error is worse than
            # none: the loop is the only thing that ever frees the blobs.
            _log.exception("purge_expired failed; will retry")
        await asyncio.sleep(interval)


def _interval_from_env() -> float:
    """PURGE_INTERVAL_SECONDS as a positive finite float, or DEFAULT_INTERVAL
    when it is unset.

    Rejecting 0, a negative value and inf is the point: asyncio.sleep of any
    of those is a bare yield (or a wait that never ends), so a typo would
    either spin purge_expired against the DB continuously or silently disable
    the reaper for the process's whole life. Failing at startup is louder
    than either.
    """
    raw = os.environ.get("PURGE_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_INTERVAL
    try:
        interval = float(raw)
    except ValueError:
        interval = math.nan  # falls into the one rejection below
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("PURGE_INTERVAL_SECONDS must be a positive number, "
                         f"got {raw!r}")
    return interval


def start(interval: float | None = None) -> asyncio.Task:
    """Schedule the loop on the running event loop and return the task so the
    caller can cancel it at shutdown."""
    if interval is None:
        interval = _interval_from_env()
    return asyncio.get_running_loop().create_task(loop(interval))
