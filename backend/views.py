"""Browser-facing routes: the SPA shell and per-version artifact rendering."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from starlette.responses import HTMLResponse, Response

from backend import docs_repo, render

router = APIRouter()

_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


@router.get("/")
async def index() -> HTMLResponse:
    """The SPA shell, with each asset reference stamped with the file's
    mtime (`styles.css?v=<mtime>`) so a redeploy busts the CDN edge cache."""
    html = (_PUBLIC_DIR / "index.html").read_text()
    for asset in ("styles.css", "app.js"):
        mtime = int((_PUBLIC_DIR / asset).stat().st_mtime)
        html = html.replace(f'"{asset}"', f'"{asset}?v={mtime}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _serve(doc: dict) -> HTMLResponse:
    """A browser-facing page for one stored version.

    An HTML document is served verbatim -- it is trusted agent output. A
    Markdown one is rendered at serve time; the stored source is untouched.
    """
    if doc["format"] == "markdown":
        return HTMLResponse(render.render_markdown(doc["html"], doc["title"]))
    return HTMLResponse(doc["html"])


@router.get("/d/{slug:path}/v{version}")
async def render_version(slug: str, version: int) -> Response:
    doc = docs_repo.get_version(slug, version)
    if doc is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    return _serve(doc)


@router.get("/d/{slug:path}")
async def render_latest(slug: str) -> Response:
    doc = docs_repo.get_latest(slug)
    if doc is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    return _serve(doc)
