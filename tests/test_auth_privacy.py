"""Authentication must not send or print panel passwords and OAuth codes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials


@pytest.mark.parametrize("supplied,expected,status", [
    ("synthetic-面板密碼", "synthetic-面板密碼", 200),
    ("synthetic-錯誤密碼", "synthetic-面板密碼", 401),
    ("synthetic-面板密碼", "synthetic-ascii-password", 401),
    ("synthetic-\ud800", "synthetic-ascii-password", 401),
])
@pytest.mark.asyncio
async def test_panel_login_handles_unicode_passwords(monkeypatch, supplied, expected, status):
    import httpx
    from fastapi import FastAPI
    from src import auth, utils
    from src.panel import auth as panel_auth

    monkeypatch.setattr(auth, "get_panel_password", AsyncMock(return_value=expected))
    monkeypatch.setattr(panel_auth, "get_panel_password", AsyncMock(return_value=expected))
    app = FastAPI()
    app.include_router(panel_auth.router)
    import json
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/login", content=json.dumps({"password": supplied}),
                                     headers={"Content-Type": "application/json"})
    assert response.status_code == status
    if status == 200:
        token = response.json()["token"]
        assert token != supplied
        claims = jwt.decode(token, utils._panel_signing_key(expected),
                            algorithms=["HS256"])
        assert claims["sub"] == "panel"


@pytest.mark.asyncio
async def test_panel_session_is_not_the_password_and_expires(monkeypatch):
    from src import utils

    password = "synthetic-private-panel-password"
    monkeypatch.setattr(utils, "get_panel_password", AsyncMock(return_value=password))
    token = utils.create_panel_token(password)
    assert token != password
    assert await utils.verify_panel_token(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    ) == token

    with pytest.raises(HTTPException):
        await utils.verify_panel_token(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=password)
        )
    expired = jwt.encode(
        {"sub": "panel", "iat": 1, "exp": 2, "jti": "synthetic"},
        utils._panel_signing_key(password), algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        await utils.verify_panel_token(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired)
        )


@pytest.mark.asyncio
async def test_oauth_state_is_opaque_and_logs_contain_no_session(monkeypatch):
    from src import auth

    messages = []
    token = "synthetic-private-session-value"

    class FakeFlow:
        def __init__(self, **kwargs):
            self.redirect_uri = kwargs["redirect_uri"]

        def get_auth_url(self, state):
            return "https://provider.invalid/authorize?state=" + state

    monkeypatch.setattr(auth, "find_available_port", AsyncMock(return_value=12345))
    monkeypatch.setattr(auth, "create_callback_server", lambda port: SimpleNamespace(serve_forever=lambda: None))
    monkeypatch.setattr(auth.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(auth, "Flow", FakeFlow)
    monkeypatch.setattr(auth, "cleanup_expired_flows", lambda: None)
    monkeypatch.setattr(
        auth, "log", SimpleNamespace(info=messages.append, warning=messages.append,
                                     error=messages.append, debug=messages.append)
    )
    previous = auth.auth_flows.copy()
    auth.auth_flows.clear()
    try:
        result = await auth.create_auth_url(user_session=token)
        assert result["success"], result.get("error")
        state = parse_qs(urlparse(result["auth_url"]).query)["state"][0]
        assert state == result["state"]
        assert token not in result["auth_url"]
        assert token not in state
        assert auth.auth_flows[state]["user_session"] == token
        assert token not in "\n".join(messages)
    finally:
        auth.auth_flows.clear()
        auth.auth_flows.update(previous)


@pytest.mark.asyncio
async def test_callback_error_never_echoes_code_or_state(monkeypatch):
    from src import auth

    messages = []
    monkeypatch.setattr(
        auth, "log", SimpleNamespace(info=messages.append, warning=messages.append,
                                     error=messages.append, debug=messages.append)
    )
    result = await auth.complete_auth_flow_from_callback_url(
        "http://localhost:12345/?state=synthetic-state&code=synthetic-private-code"
    )
    combined = str(result) + "\n".join(messages)
    assert not result["success"]
    assert "synthetic-private-code" not in combined
    assert "synthetic-state" not in combined


@pytest.mark.asyncio
async def test_unset_panel_password_cannot_authenticate(monkeypatch):
    from src import auth, utils

    monkeypatch.setattr(auth, "get_panel_password", AsyncMock(return_value=""))
    monkeypatch.setattr(utils, "get_panel_password", AsyncMock(return_value=""))
    assert not await auth.verify_password("")
    with pytest.raises(HTTPException):
        await utils.verify_panel_token(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="anything")
        )
