"""Browser-facing routes: the SPA shell and per-version artifact rendering."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from starlette.convertors import IntegerConvertor, register_url_convertor
from starlette.responses import HTMLResponse, Response

from backend import docs_repo, privacy, render
from backend.workers import run_db

router = APIRouter()

ARTIFACT_CSP = "sandbox allow-scripts allow-modals"

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

    HTML is served verbatim and trusted to render. The CSP sandbox removes
    its authority to act with the viewer's session while allowing scripts.
    Markdown is rendered at serve time; the stored source is untouched.
    """
    headers = {"Content-Security-Policy": ARTIFACT_CSP}
    if doc["format"] == "markdown":
        return HTMLResponse(render.render_markdown(doc["html"], doc["title"]), headers=headers)
    return HTMLResponse(doc["html"], headers=headers)


class _Int10Convertor(IntegerConvertor):
    """At most 10 digits; a longer tail is slug text."""

    regex = "[0-9]{1,10}"


register_url_convertor("int10", _Int10Convertor())


@router.get("/d/{slug:path}/v{version:int10}")
async def render_version(slug: str, version: int) -> Response:
    doc = await run_db(docs_repo.get_version, slug, version)
    if doc is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    return await run_db(_serve, doc)


@router.get("/d/{slug:path}")
async def render_latest(request: Request, slug: str) -> Response:
    doc = await run_db(docs_repo.get_latest, slug)
    if doc is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    resp = await run_db(_serve, doc)
    # set only for anonymous public-doc reads.
    if getattr(request.state, "principal", None) == "anonymous":
        # body may be memoryview.
        resp.body = privacy.inject_link(bytes(resp.body))
        resp.headers["content-length"] = str(len(resp.body))
    return resp
