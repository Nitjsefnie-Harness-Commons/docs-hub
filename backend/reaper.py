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


def start(interval: float | None = None) -> asyncio.Task:
    """Schedule the loop on the running event loop and return the task so the
    caller can cancel it at shutdown."""
    if interval is None:
        interval = float(os.environ.get("PURGE_INTERVAL_SECONDS",
                                        DEFAULT_INTERVAL))
    return asyncio.get_running_loop().create_task(loop(interval))
