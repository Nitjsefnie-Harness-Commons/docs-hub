"""Run blocking request work without borrowing unrelated request workers."""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import anyio.to_thread

_T = TypeVar("_T")
# Beyond 40 blocking request calls, admission queues here without a pool timeout.
_DB_LIMITER = anyio.CapacityLimiter(40)


# Rendering and credential verification deliberately share the DB work limiter.
async def run_db(func: Callable[..., _T], *args: object) -> _T:
    return await anyio.to_thread.run_sync(func, *args, limiter=_DB_LIMITER)
