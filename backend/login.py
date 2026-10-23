"""/login GET+POST and /logout. Verifies against the shared authdb users
table; mints an HMAC session cookie on success. Rate-limited per IP."""
from __future__ import annotations

import os
import time
from threading import Lock

from fastapi import APIRouter, Form, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from backend import auth, db, session
from backend.workers import run_db

router = APIRouter()

_FAILURES: dict[str, list[float]] = {}
# Five recorded failures within 300 s per IP block login; in-flight attempts are not reserved.
_FAILURES_LOCK = Lock()
_MAX_FAILURES = 5
_WINDOW = 300


def _rate_limited(ip: str) -> bool:
    with _FAILURES_LOCK:
        now = time.time()
        attempts = [t for t in _FAILURES.get(ip, []) if now - t < _WINDOW]
        _FAILURES[ip] = attempts
        return len(attempts) >= _MAX_FAILURES


def _record_failure(ip: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.setdefault(ip, []).append(time.time())


def _load_user_config(user_id: int) -> dict | None:
    with db.auth_conn() as c:
        row = c.execute("SELECT config FROM users WHERE user_id=%s",
                        (user_id,)).fetchone()
    return row[0] if row else None


def _verify_credentials(user_id: int, password: str) -> bool:
    config = _load_user_config(user_id)
    return bool(config and auth.has_web_password(config)
                and auth.verify_web_password(config, password))


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Sign in - docs</title>
<style>
 body{{background:#0b0d10;color:#dde;font-family:sans-serif;display:flex;
   align-items:center;justify-content:center;min-height:100vh;margin:0}}
 form{{background:#14181d;padding:24px 28px;border:1px solid #25303c;
   border-radius:8px;min-width:320px}}
 h1{{margin:0 0 16px;font-size:18px;color:#9bd}}
 label{{display:block;margin:10px 0 4px;font-size:12px;color:#8aa}}
 input{{width:100%;box-sizing:border-box;padding:8px 10px;background:#0e1216;
   color:#dde;border:1px solid #25303c;border-radius:4px}}
 button{{margin-top:18px;width:100%;padding:10px;background:#1f6f9c;color:#fff;
   border:0;border-radius:4px;font-weight:600;cursor:pointer}}
 .err{{color:#e76;font-size:12px;min-height:16px;margin-top:8px}}
</style></head><body>
<form method="post" action="/login">
 <h1>docs.nitjsefni.eu</h1>
 <label>User ID</label>
 <input name="user_id" required inputmode="numeric" pattern="[0-9]+">
 <label>Password</label>
 <input name="password" type="password" required>
 <button type="submit">Sign in</button>
 <div class="err">{err}</div>
</form></body></html>
"""


@router.get("/login")
def login_page() -> HTMLResponse:
    return HTMLResponse(_LOGIN_HTML.format(err=""))


@router.post("/login")
async def login_post(request: Request, user_id: str = Form(""),
                     password: str = Form("")) -> Response:
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        return HTMLResponse(_LOGIN_HTML.format(err="Too many attempts."),
                            status_code=429)
    try:
        uid = int(user_id.strip())
    except ValueError:
        uid = 0
    if uid <= 0 or not await run_db(_verify_credentials, uid, password):
        _record_failure(ip)
        return HTMLResponse(_LOGIN_HTML.format(err="Invalid credentials."),
                            status_code=401)
    token = session.make_session_token(uid)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        session.SESSION_COOKIE_NAME, token, httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "1") == "1",
        samesite="strict", max_age=session.SESSION_COOKIE_MAX_AGE, path="/",
    )
    return resp


@router.get("/logout")
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(session.SESSION_COOKIE_NAME, path="/")
    return resp
