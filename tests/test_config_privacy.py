"""Configuration persistence must not echo secrets into save responses or logs."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.models import ConfigSaveRequest
from src.panel import config_routes


@pytest.fixture
def config_storage(monkeypatch):
    stored = {}
    messages = []

    async def set_config(key, value):
        stored[key] = value

    database = SimpleNamespace(set_config=AsyncMock(side_effect=set_config))
    monkeypatch.setattr(config_routes, "get_database", AsyncMock(return_value=database))
    monkeypatch.setattr(config_routes, "get_env_locked_keys", lambda: set())
    monkeypatch.setattr(config_routes.config, "reload_config", AsyncMock())
    monkeypatch.setattr(
        config_routes, "log",
        SimpleNamespace(debug=messages.append, error=messages.append),
    )
    return database, stored, messages


@pytest.mark.asyncio
async def test_save_persists_secrets_without_echoing_them(config_storage):
    _, stored, messages = config_storage
    settings = {
        "api_password": "test-api-private-value",
        "panel_password": "test-panel-private-value",
        "proxy": "http://example-user:private-proxy-value@proxy.invalid:8080",
        "redis_url": "redis://user:private-redis-value@redis.invalid:6379/0",
        "port": 7862,
    }

    response = await config_routes.save_config(ConfigSaveRequest(config=settings), token="test")

    assert stored == settings
    assert json.loads(response.body)["saved_config"] == {"port": 7862}
    recorded = response.body.decode() + "\n".join(messages)
    for key in ("api_password", "panel_password", "proxy", "redis_url"):
        assert settings[key] not in recorded


@pytest.mark.asyncio
async def test_get_config_does_not_return_saved_private_values(config_storage, monkeypatch):
    database, _, _ = config_storage
    secret_values = {
        "api_password": "synthetic-api-secret",
        "panel_password": "synthetic-panel-secret",
        "proxy": "http://user:synthetic-proxy-secret@proxy.invalid:8080",
        "redis_url": "redis://user:synthetic-redis-secret@redis.invalid:6379",
    }
    database.get_all_config = AsyncMock(return_value={**secret_values, "port": 7862})
    for name in dir(config_routes.config):
        if name.startswith("get_") and name not in {"get_database", "get_config_value"}:
            getter = getattr(config_routes.config, name)
            if callable(getter):
                monkeypatch.setattr(config_routes.config, name, AsyncMock(return_value="test"))

    response = await config_routes.get_config(token="synthetic-session")
    body = json.loads(response.body)
    assert response.headers["cache-control"] == "no-store"
    assert body["config"]["port"] == 7862
    assert all(body["private_configured"][key] for key in secret_values)
    assert all(key not in body["config"] for key in secret_values)
    assert all(secret not in response.body.decode() for secret in secret_values.values())


@pytest.mark.asyncio
async def test_save_respects_environment_locks(config_storage, monkeypatch):
    _, stored, _ = config_storage
    monkeypatch.setattr(config_routes, "get_env_locked_keys", lambda: {"api_password", "port"})

    response = await config_routes.save_config(
        ConfigSaveRequest(config={"api_password": "ignored-private-value", "port": 7862,
                                  "panel_password": "saved-private-value"}),
        token="test",
    )

    assert stored == {"panel_password": "saved-private-value"}
    assert json.loads(response.body)["saved_config"] == {}


@pytest.mark.asyncio
async def test_database_failure_does_not_expose_secret_bind_parameters(config_storage):
    database, _, messages = config_storage
    secret = "private-value-in-database-bind-parameters"
    database.set_config.side_effect = RuntimeError(f"database error; parameters: {secret}")

    with pytest.raises(HTTPException) as error:
        await config_routes.save_config(
            ConfigSaveRequest(config={"panel_password": secret}), token="test",
        )

    assert error.value.status_code == 500
    assert secret not in str(error.value.detail)
    assert secret not in "\n".join(messages)
