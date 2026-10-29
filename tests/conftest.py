"""Keep audit collection and execution offline and isolated from local accounts."""

import socket
from unittest.mock import patch

import httpx
import pytest


def pytest_configure(config):
    # A developer's real .env is never part of synthetic regression inputs.
    dotenv_guard = patch("dotenv.load_dotenv", return_value=False)
    dotenv_guard.start()
    config.add_cleanup(dotenv_guard.stop)
    # src.utils performs a release lookup at import time, before fixtures run.
    guard = patch.object(
        httpx.HTTPTransport, "handle_request",
        side_effect=httpx.ConnectError("offline audit: outbound HTTP disabled"),
    )
    guard.start()
    config.add_cleanup(guard.stop)


@pytest.fixture(autouse=True)
def isolated_audit(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise RuntimeError("offline audit: outbound network disabled")

    async def denied_async(*args, **kwargs):
        denied()

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)
    for name in (
        "POSTGRESQL_URI", "REDIS_URL", "PROXY", "API_PASSWORD", "PANEL_PASSWORD",
        "IP_WHITELIST", "IP_WHITELIST_ENABLED", "PATH_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path / "creds"))
    import config
    monkeypatch.setattr(config, "_config_cache", {})
    monkeypatch.setattr(config, "_config_initialized", True)
