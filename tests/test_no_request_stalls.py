import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from threading import Timer

import httpx
import psycopg
import pytest
from psycopg_pool import PoolTimeout

from backend import db
from backend.app import app

KEY = {"x-docs-key": "test-api-key"}


async def _publish(client: httpx.AsyncClient, slug: str) -> httpx.Response:
    return await client.post(
        "/api/publish", data={"slug": slug, "title": slug, "from": "analyst"},
        files={"file": ("d.html", b"<h1>x</h1>", "text/html")}, headers=KEY)


async def _wait_for_pool_waiters(count: int) -> None:
    async with asyncio.timeout(3):
        while db.docs_pool().get_stats()["requests_waiting"] < count:
            await asyncio.sleep(0.005)


@asynccontextmanager
async def _waiting_publishes(client: httpx.AsyncClient,
                             slugs: list[str]) -> AsyncIterator[None]:
    with psycopg.connect(os.environ["DATABASE_URL_DOCS"], autocommit=True) as conn:
        try:
            for slug in slugs:
                conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (slug,))
            # A blocked event loop cannot run asyncio timeouts or release locks.
            watchdog = Timer(5, conn.execute, args=("SELECT pg_advisory_unlock_all()",))
            watchdog.start()
            try:
                publishes = [asyncio.create_task(_publish(client, slug)) for slug in slugs]
                try:
                    async with asyncio.timeout(3):
                        while conn.execute(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = current_database() "
                                "AND wait_event = 'advisory' "
                                "AND query LIKE 'SELECT pg_advisory_xact_lock%%'"
                        ).fetchone()[0] < min(len(slugs), 8):
                            await asyncio.sleep(0.005)
                    await _wait_for_pool_waiters(max(min(len(slugs), 40) - 8, 0))
                    assert all(not task.done() for task in publishes)
                    yield
                    assert watchdog.is_alive(), "event loop stalled until safety unlock"
                finally:
                    conn.execute("SELECT pg_advisory_unlock_all()")
                    responses = await asyncio.wait_for(asyncio.gather(*publishes), timeout=30)
                for result in responses:
                    assert result.status_code == 200, result.text
            finally:
                watchdog.cancel()
                watchdog.join()
        finally:
            conn.execute("SELECT pg_advisory_unlock_all()")


async def _assert_fast(client: httpx.AsyncClient, path: str) -> None:
    started = time.monotonic()
    response = await asyncio.wait_for(client.get(path, headers=KEY), timeout=1)
    assert response.status_code == 200
    assert time.monotonic() - started < 1


async def _health_while_publishes_wait(slugs: list[str]) -> None:
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with _waiting_publishes(client, slugs):
            await _assert_fast(client, "/health")


def test_publish_waiting_on_slug_lock_does_not_stall_health() -> None:
    asyncio.run(_health_while_publishes_wait(["held/doc"]))


def test_more_publish_waiters_than_worker_threads_do_not_stall_health() -> None:
    asyncio.run(_health_while_publishes_wait([f"held/doc{i}" for i in range(45)]))


@pytest.mark.parametrize("path", ["/login", "/index.html", "/api/whoami"])
def test_db_waiters_leave_unrelated_worker_requests_responsive(path: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async with _waiting_publishes(client, [f"held/probe{i}" for i in range(45)]):
                await _assert_fast(client, path)

    asyncio.run(run())


def test_forty_db_requests_reach_the_pool_timeout_without_worker_queueing() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            pool = db.docs_pool()
            assert pool.max_size == 8
            assert pool.timeout == 10
            with ExitStack() as held:
                for _ in range(8):
                    held.enter_context(db.docs_conn())
                requests = [asyncio.create_task(client.get("/api/list", headers=KEY))
                            for _ in range(40)]
                started = time.monotonic()
                try:
                    await _wait_for_pool_waiters(40)
                    results = await asyncio.wait_for(
                        asyncio.gather(*requests, return_exceptions=True), 15)
                    assert all(isinstance(result, PoolTimeout) for result in results)
                    assert 9 <= time.monotonic() - started < 15
                finally:
                    held.close()
                    await asyncio.gather(*requests, return_exceptions=True)

    asyncio.run(run())


def test_anonymous_public_lookup_waiting_for_pool_does_not_stall_health() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await _publish(client, "public/doc")).status_code == 200
            response = await client.post("/api/doc/public/doc/public",
                                         json={"public": True}, headers=KEY)
            assert response.status_code == 200
            lookup = None
            try:
                async with _waiting_publishes(client, [f"held/public{i}" for i in range(8)]):
                    lookup = asyncio.create_task(client.get("/d/public/doc"))
                    await _wait_for_pool_waiters(1)
                    assert not lookup.done()
                    await _assert_fast(client, "/health")
            finally:
                if lookup is not None:
                    response = await asyncio.wait_for(lookup, 15)
            assert response.status_code == 200
            assert response.text == "<h1>x</h1>"

    asyncio.run(run())
