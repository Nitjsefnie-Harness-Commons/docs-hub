"""HMAC session-cookie mint/verify + the auth middleware.

Cookie token: <user_id>.<issued_at>.<nonce>.<hmac>, signed with the
server-side SESSION_SECRET. Agent requests skip the cookie and present
the shared API key instead.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from backend import docs_repo

SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600

_PUBLIC_PATHS = {"/health", "/login", "/logout"}

# A /d/<slug>/v<n> path is the per-version render route — it stays auth-gated
# even for public docs. Only the hash-less /d/<slug> (latest) route opens.
_VERSION_PATH = re.compile(r"^/d/.+/v\d+$")


def _secret() -> bytes:
    return os.environ["SESSION_SECRET"].encode("utf-8")


def make_session_token(user_id: int) -> str:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(10)
    payload = f"{user_id}.{issued_at}.{nonce}"
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 4:
        return None
    raw_uid, raw_ts, nonce, sig = parts
    try:
        user_id = int(raw_uid)
        issued_at = int(raw_ts)
    except ValueError:
        return None
    now = int(time.time())
    if issued_at > now + 60 or now - issued_at > SESSION_COOKIE_MAX_AGE:
        return None
    payload = f"{raw_uid}.{raw_ts}.{nonce}"
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return user_id


def request_api_key(request: Request) -> str:
    """Extract the agent API key from either header form."""
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.headers.get("x-docs-key", "").strip()


def has_valid_api_key(request: Request) -> bool:
    expected = os.environ.get("DOCS_HUB_API_KEY", "")
    got = request_api_key(request)
    return bool(expected) and bool(got) and hmac.compare_digest(got, expected)


async def auth_middleware(request: Request, call_next):
    """Allow: public paths; valid API key (agent); valid cookie (human).
    Publishing additionally requires the API key (checked in api.py)."""
    path = request.url.path
    if path in _PUBLIC_PATHS:
        return await call_next(request)

    if has_valid_api_key(request):
        request.state.principal = "agent"
        request.state.user_id = None
        return await call_next(request)

    cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    user_id = verify_session_token(cookie) if cookie else None
    if user_id is not None:
        request.state.principal = "human"
        request.state.user_id = user_id
        return await call_next(request)

    # Anonymous read of a PUBLIC doc: only the hash-less /d/<slug> latest
    # route opens. Everything else — /d/<slug>/v<n>, /api/*, the SPA shell —
    # falls through to the normal denial below.
    if path.startswith("/d/") and not _VERSION_PATH.match(path):
        if await run_in_threadpool(docs_repo.is_public, path[len("/d/"):]):
            request.state.principal = "anonymous"
            request.state.user_id = None
            return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


DELETE_TOKEN_TTL = 300  # seconds — a confirm token is good for 5 minutes


def _canonical_filters(filters: dict) -> str:
    """Stable string for the active (non-empty) filter values, sorted by key.
    `confirm_token` is excluded so a confirm request body produces the same
    canonical form as its preview request."""
    items = sorted(
        (k, str(v).strip())
        for k, v in filters.items()
        if k != "confirm_token" and str(v).strip()
    )
    return "&".join(f"{k}={v}" for k, v in items)


def make_delete_token(filters: dict) -> str:
    """HMAC token binding a delete to its exact filter set, format
    <issued_at>.<hmac>."""
    issued_at = int(time.time())
    payload = f"{issued_at}.{_canonical_filters(filters)}"
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{issued_at}.{sig}"


def verify_delete_token(token: str, filters: dict) -> bool:
    """True iff `token` was minted for this filter set within the TTL."""
    parts = token.split(".")
    if len(parts) != 2:
        return False
    raw_ts, sig = parts
    try:
        issued_at = int(raw_ts)
    except ValueError:
        return False
    now = int(time.time())
    if issued_at > now + 60 or now - issued_at > DELETE_TOKEN_TTL:
        return False
    payload = f"{issued_at}.{_canonical_filters(filters)}"
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
