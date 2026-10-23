"""Environment resolution through the reader and its real backend consumers."""
import asyncio
from http.cookies import SimpleCookie
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from backend import db, login, reaper, session, storage

# These private functions are the consumers whose environment contract is under test.
# pylint: disable=protected-access

_VALUES = [
    ("prefixed", "legacy", "prefixed"),
    ("prefixed", None, "prefixed"),
    ("prefixed", "", "prefixed"),
    (None, "legacy", "legacy"),
    ("", "legacy", "legacy"),
    (None, None, None),
    (None, "", None),
    ("", None, None),
    ("", "", None),
]


def _environment(monkeypatch, name, prefixed, legacy):
    for key, value in ((f"DOCS_HUB_{name}", prefixed), (name, legacy)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


@pytest.mark.parametrize("prefixed,legacy,expected", _VALUES)
@pytest.mark.parametrize("name,consumer", [
    ("STORE_ROOT", storage._store_root),
    ("SESSION_SECRET", session._secret),
])
def test_required_consumers(monkeypatch, name, consumer, prefixed, legacy, expected):
    _environment(monkeypatch, name, prefixed, legacy)
    if expected is None:
        with pytest.raises(RuntimeError, match=f"DOCS_HUB_{name}"):
            consumer()
    else:
        value = consumer()
        assert value == (expected.encode("utf-8") if name == "SESSION_SECRET" else expected)


@pytest.mark.parametrize("prefixed,legacy,expected", _VALUES)
@pytest.mark.parametrize("name,consumer", [
    ("DATABASE_URL_DOCS", db.docs_pool),
    ("DATABASE_URL_AUTH", db.auth_pool),
])
def test_pool_connection_strings(monkeypatch, name, consumer, prefixed, legacy, expected):
    db.close_pools()
    constructor = Mock(spec=db.ConnectionPool)
    monkeypatch.setattr(db, "ConnectionPool", constructor)
    _environment(monkeypatch, name, prefixed, legacy)
    try:
        if expected is None:
            with pytest.raises(RuntimeError, match=f"DOCS_HUB_{name}"):
                consumer()
            constructor.assert_not_called()
        else:
            consumer()
            assert constructor.call_args.args == (expected,)
    finally:
        db.close_pools()


@pytest.mark.parametrize("prefixed,legacy,expected", _VALUES)
@pytest.mark.parametrize("default", [None, "fallback"])
def test_reader(monkeypatch, prefixed, legacy, expected, default):
    from backend.settings import required, setting

    _environment(monkeypatch, "STORE_ROOT", prefixed, legacy)
    assert setting("STORE_ROOT", default) == (expected if expected is not None else default)
    if expected is None:
        with pytest.raises(RuntimeError, match="DOCS_HUB_STORE_ROOT"):
            required("STORE_ROOT")
    else:
        assert required("STORE_ROOT") == expected


@pytest.mark.parametrize("prefixed,legacy,expected", [
    ("0", "1", False),
    ("1", "0", True),
    ("0", None, False),
    ("1", None, True),
    (None, "0", False),
    (None, "1", True),
    ("", "0", False),
    ("", "1", True),
    ("0", "", False),
    ("1", "", True),
    (None, None, True),
    (None, "", True),
    ("", None, True),
    ("", "", True),
])
def test_login_cookie_secure(monkeypatch, prefixed, legacy, expected):
    _environment(monkeypatch, "COOKIE_SECURE", prefixed, legacy)
    monkeypatch.setattr(login, "_verify_credentials", lambda _uid, _password: True)
    request = Request({"type": "http", "client": ("settings-test", 1234)})
    response = asyncio.run(login.login_post(request, user_id="1", password=""))
    assert response.status_code == 303
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    assert bool(cookie["session"]["secure"]) is expected


@pytest.mark.parametrize("prefixed,legacy,expected", [
    ("12.5", "25", 12.5),
    ("12.5", None, 12.5),
    ("12.5", "", 12.5),
    (None, "25", 25.0),
    ("", "25", 25.0),
    (None, None, 60.0),
    (None, "", 60.0),
    ("", None, 60.0),
    ("", "", 60.0),
])
def test_reaper_interval(monkeypatch, prefixed, legacy, expected):
    _environment(monkeypatch, "PURGE_INTERVAL_SECONDS", prefixed, legacy)
    assert reaper._interval_from_env() == expected


@pytest.mark.parametrize("raw", ["0", "-5", "soon", "inf", "nan"])
def test_reaper_rejects_invalid_prefixed_value_over_valid_legacy(monkeypatch, raw):
    _environment(monkeypatch, "PURGE_INTERVAL_SECONDS", raw, "25")
    with pytest.raises(
            ValueError, match="DOCS_HUB_PURGE_INTERVAL_SECONDS must be a positive number"):
        reaper._interval_from_env()
