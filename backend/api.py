"""/api/* routes. Publishing requires the agent API key; reads accept the
key or a human cookie (the middleware already enforced one of the two)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Form, Request, UploadFile
from starlette.responses import HTMLResponse, JSONResponse, Response

from backend import docs_repo, session

router = APIRouter(prefix="/api")


def _require_agent(request: Request) -> JSONResponse | None:
    if not session.has_valid_api_key(request):
        return JSONResponse({"ok": False, "error": "API key required"},
                            status_code=401)
    return None


@router.post("/publish")
async def publish(request: Request, file: UploadFile,
                  slug: str = Form(...), title: str = Form(...),
                  tags: str = Form(""), project: str = Form(""),
                  from_: str = Form(..., alias="from")) -> Response:
    denied = _require_agent(request)
    if denied is not None:
        return denied
    html = await file.read()
    if not html:
        return JSONResponse({"ok": False, "error": "empty file"}, status_code=400)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        res = docs_repo.publish(slug, title, tag_list, project or None,
                                from_, html)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({
        "ok": True, "slug": res["slug"], "version": res["version"],
        "url": f"/d/{res['slug']}",
    })


@router.get("/doc/{slug:path}")
async def get_doc(slug: str) -> Response:
    doc = docs_repo.get_latest(slug)
    if doc is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return HTMLResponse(doc["html"])


@router.post("/doc/{slug:path}/public")
async def set_doc_public(slug: str, body: dict = Body(...)) -> JSONResponse:
    """Flip a slug's public flag. Auth is enforced by the middleware (agent
    key or human cookie); an anonymous caller never reaches this handler.
    Public docs are readable by anyone at /d/<slug>; everything else stays
    gated."""
    public = body.get("public")
    if not isinstance(public, bool):
        return JSONResponse({"ok": False, "error": "public must be a boolean"},
                            status_code=400)
    if not docs_repo.set_public(slug, public):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "slug": slug, "public": public})


@router.get("/list")
async def api_list(project: str = "", agent: str = "") -> JSONResponse:
    docs = docs_repo.list_docs(project or None, agent or None)
    for d in docs:
        d["updated_at"] = d["updated_at"].isoformat()
        d["expires_at"] = d["expires_at"].isoformat() if d["expires_at"] else None
    return JSONResponse({"ok": True, "docs": docs})


@router.get("/versions/{slug:path}")
async def api_versions(slug: str) -> JSONResponse:
    vs = docs_repo.list_versions(slug)
    if not vs:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    for v in vs:
        v["created_at"] = v["created_at"].isoformat()
    return JSONResponse({"ok": True, "slug": slug, "versions": vs})


_FILTER_KEYS = ("slug", "project", "author", "tag", "q",
                "updated_before", "updated_after")


@router.post("/delete")
async def delete(_request: Request,
                 body: dict = Body(default={})) -> JSONResponse:
    """Delete whole docs by filter. Two-step: a call with no `confirm_token`
    previews and returns a token; a call echoing a valid token executes.
    Accepts the agent API key or a human session cookie (the middleware has
    already admitted one of the two)."""
    filters = {
        k: str(body[k]).strip()
        for k in _FILTER_KEYS
        if body.get(k) is not None and str(body[k]).strip()
    }
    if not filters:
        return JSONResponse({"ok": False, "error": "at least one filter required"},
                            status_code=400)
    matched = docs_repo.find_docs(filters)
    token = body.get("confirm_token")
    if not token:
        return JSONResponse({
            "ok": True, "preview": True, "count": len(matched),
            "matched": matched,
            "confirm_token": session.make_delete_token(filters),
        })
    if not session.verify_delete_token(str(token), filters):
        return JSONResponse(
            {"ok": False, "error": "confirm token invalid or expired"},
            status_code=409)
    deleted = docs_repo.delete_docs([m["slug"] for m in matched])
    return JSONResponse({"ok": True, "preview": False, "deleted": deleted})


@router.get("/whoami")
async def whoami(request: Request) -> JSONResponse:
    """Identify the current principal for the SPA top bar."""
    return JSONResponse({
        "ok": True,
        "principal": request.state.principal,
        "user_id": request.state.user_id,
    })


@router.get("/tags")
async def api_tags(project: str = "") -> JSONResponse:
    """Distinct tags in use with their counts (most-used first), so agents
    can pick from established tags instead of inventing new ones."""
    tags = docs_repo.list_tags(project or None)
    return JSONResponse({"ok": True, "tags": tags})
