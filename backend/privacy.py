"""The operator-supplied privacy notice: page route and link injection.

`DOCS_HUB_PRIVACY_NOTICE_FILE` names the operator's Markdown file. When it
is set and names a readable, regular, non-empty file, `GET /privacy` serves
it rendered as Markdown, and the sign-in form and every anonymously served
public document carry a link to it. Every other value — unset, empty,
missing, unreadable, a directory, empty — behaves exactly like
unconfigured: the route 404s and no link appears anywhere. The notice text
is the operator's, read from disk per request (an edit takes effect
without a restart); nothing is stored in the repository.
"""
from __future__ import annotations

import os
import re
import stat

from fastapi import APIRouter
from starlette.responses import HTMLResponse, Response

from backend import render

router = APIRouter()

# Hub-authored text with no scripts: stricter than the stored-artifact
# sandbox in backend/views.py — no allow-scripts, no allow-modals.
_CSP = "sandbox"

_TITLE = "Privacy notice"
_LINK = b'<a href="/privacy">Privacy notice</a>'
_BODY_CLOSE = re.compile(rb"</body\s*>", re.IGNORECASE)


def content() -> bytes | None:
    """The notice's Markdown bytes when it is available, else None.

    This is the one availability check: the route and the link injection
    both read it, so the page and every link can never disagree."""
    path = os.environ.get("DOCS_HUB_PRIVACY_NOTICE_FILE", "")
    if not path:
        return None
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return data or None


def inject_link(body: bytes) -> bytes:
    """`body` with one privacy link inserted before its last `</body>` (any
    case), appended when it has none; unchanged when the notice is
    unavailable. The caller's stored bytes are never rewritten — this
    shapes only the bytes one response carries."""
    if content() is None:
        return body
    closes = list(_BODY_CLOSE.finditer(body))
    if not closes:
        return body + _LINK
    at = closes[-1].start()
    return body[:at] + _LINK + body[at:]


@router.get("/privacy")
async def privacy_page() -> Response:
    data = content()
    if data is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    return HTMLResponse(render.render_markdown(data, _TITLE),
                        headers={"Content-Security-Policy": _CSP})
