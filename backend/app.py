"""FastAPI entrypoint for docs-hub."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import api, db, login, reaper, session, views

_REPO_ROOT = Path(__file__).resolve().parent.parent
db.load_dotenv(str(_REPO_ROOT / ".env"))
# Without this the root logger's WARNING default swallows the reaper's INFO
# line, so a purge leaves no trace in the journal. Bare on purpose: basicConfig
# is a no-op once the root logger has handlers, so a uvicorn or test harness
# that configured logging first keeps its own configuration.
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    purge_task: asyncio.Task | None = None
    try:
        # Inside the try: schema_check() raises on a stale deployment, and
        # that unwind still has to close the pools it just opened.
        db.migrate()
        db.schema_check()
        purge_task = reaper.start()
        yield
    finally:
        # Startup can fail before the task exists -- a rejected
        # PURGE_INTERVAL_SECONDS, or a failed schema check -- and the pools
        # still have to be closed.
        if purge_task is not None:
            purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await purge_task
        db.close_pools()


app = FastAPI(title="docs-hub", docs_url=None, redoc_url=None, lifespan=lifespan)
app.middleware("http")(session.auth_middleware)
app.include_router(login.router)
app.include_router(api.router)
app.include_router(views.router)

_PUBLIC_DIR = _REPO_ROOT / "public"


@app.get("/health")
def health() -> dict:
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(_PUBLIC_DIR), html=True), name="spa")
