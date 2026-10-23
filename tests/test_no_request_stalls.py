import asyncio
import os
import time
from threading import Timer

import httpx
import psycopg

from backend.app import app

KEY = {"x-docs-key": "test-api-key"}


async def _publish(client: httpx.AsyncClient, slug: str) -> httpx.Response:
    return await client.post(
        "/api/publish", data={"slug": slug, "title": slug, "from": "analyst"},
        files={"file": ("d.html", b"<h1>x</h1>", "text/html")}, headers=KEY)


async def _health_while_publishes_wait(slugs: list[str]) -> None:
    with psycopg.connect(os.environ["DATABASE_URL_DOCS"], autocommit=True) as conn:
        try:
            for slug in slugs:
                conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (slug,))
            # A blocked event loop cannot run asyncio timeouts or release locks.
            watchdog = Timer(5, conn.execute, args=("SELECT pg_advisory_unlock_all()",))
            watchdog.start()
            try:
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://test") as client:
                    publishes = [asyncio.create_task(_publish(client, slug)) for slug in slugs]
                    try:
                        await asyncio.sleep(0.2)
                        started = time.monotonic()
                        response = await asyncio.wait_for(client.get("/health"), timeout=1)
                        assert response.status_code == 200
                        assert time.monotonic() - started < 1
                        assert watchdog.is_alive(), "event loop stalled until safety unlock"
                        assert all(not task.done() for task in publishes)
                    finally:
                        for slug in slugs:
                            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (slug,))
                        responses = await asyncio.wait_for(asyncio.gather(*publishes), timeout=30)
                    for result in responses:
                        assert result.status_code == 200, result.text
            finally:
                watchdog.cancel()
                watchdog.join()
        finally:
            conn.execute("SELECT pg_advisory_unlock_all()")


def test_publish_waiting_on_slug_lock_does_not_stall_health() -> None:
    asyncio.run(_health_while_publishes_wait(["held/doc"]))


def test_more_publish_waiters_than_worker_threads_do_not_stall_health() -> None:
    asyncio.run(_health_while_publishes_wait([f"held/doc{i}" for i in range(45)]))
