"""The operator's privacy-notice page and link injection."""
from __future__ import annotations

import os
import re
import stat

from fastapi import APIRouter
from starlette.responses import HTMLResponse, Response

from backend import render

router = APIRouter()

_CSP = "sandbox"

_TITLE = "Privacy notice"
_BODY_CLOSE = re.compile(rb"</body\s*>", re.IGNORECASE)


def content() -> bytes | None:
    """The one availability check both consumers call."""
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


def link(class_: str | None = None) -> str:
    if content() is None:
        return ""
    attr = f' class="{class_}"' if class_ else ""
    return f'<a{attr} href="/privacy">Privacy notice</a>'


def inject_link(body: bytes) -> bytes:
    """One link before the last </body>; unchanged when unavailable."""
    anchor = link().encode("utf-8")
    if not anchor:
        return body
    closes = list(_BODY_CLOSE.finditer(body))
    if not closes:
        return body + anchor
    at = closes[-1].start()
    return body[:at] + anchor + body[at:]


@router.get("/privacy")
async def privacy_page() -> Response:
    data = content()
    if data is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    return HTMLResponse(render.render_markdown(data, _TITLE),
                        headers={"Content-Security-Policy": _CSP})
