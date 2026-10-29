"""Offline stability regression checks with synthetic data.

All credentials are synthetic. Network requests and global database access are
replaced, and persistence checks use pytest's temporary directory.
"""

import asyncio
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _fresh_config(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_credential_cooldown_and_config_round_trip(tmp_path, monkeypatch):
    from src.storage.database import Database

    monkeypatch.delenv("POSTGRESQL_URI", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path / "creds"))

    async def check():
        db = Database()
        await db.initialize()
        try:
            await db.store_credential("audit@example.invalid", {
                "access_token": "synthetic-access-token",
                "refresh_token": "synthetic-refresh-token",
                "expiry": "2099-01-01T00:00:00+00:00",
            })
            credential = await db.get_credential("audit@example.invalid")
            assert credential["expiry"] == "2099-01-01T00:00:00+00:00"
            await db.set_model_cooldown("audit@example.invalid", "gemini-test", 9e12)
            assert await db.get_available_credential_by_email(
                "audit@example.invalid", "gemini-other"
            ) is None
            assert await db.get_available_credential_by_email(
                "audit@example.invalid", "claude-test"
            ) is not None
            await db.set_config("audit_value", {"enabled": True})
            await db.reload_config_cache()
            assert await db.get_config("audit_value") == {"enabled": True}
        finally:
            await db.close()

    asyncio.run(check())


def test_stream_disconnect_does_not_replay_delivered_content(monkeypatch):
    from src.api import antigravity as api

    calls = []

    async def noop(*args, **kwargs):
        return None

    async def empty(*args, **kwargs):
        return ""

    async def credentials(**kwargs):
        return "audit@example.invalid", {
            "access_token": "synthetic-token", "project_id": "synthetic-project"
        }

    async def retry_config():
        return {"max_retries": 1, "retry_interval": 0, "retry_enabled": False}

    async def upstream_url():
        return "https://upstream.invalid"

    async def error_codes():
        return [403]

    async def upstream(**kwargs):
        calls.append(kwargs)
        yield 'data: {"text":"already delivered"}'
        if len(calls) == 1:
            raise ConnectionError("synthetic disconnect after the first chunk")

    monkeypatch.setattr(api, "get_valid_credential", credentials)
    monkeypatch.setattr(api, "get_retry_config", retry_config)
    monkeypatch.setattr(api, "get_antigravity_api_url", upstream_url)
    monkeypatch.setattr(api, "get_auto_ban_error_codes", error_codes)
    monkeypatch.setattr(api, "stream_post_async", upstream)
    monkeypatch.setattr(api, "record_api_call_success", noop)
    monkeypatch.setattr(api, "record_usage_log", noop)
    monkeypatch.setattr(api, "_record_request_body_detail", empty)

    async def check():
        chunks = [chunk async for chunk in api.stream_request({
            "model": "synthetic-model", "request": {"contents": []}
        })]
        assert len(calls) == 1, f"upstream was called {len(calls)} times"
        assert chunks.count('data: {"text":"already delivered"}') == 1

    asyncio.run(check())


def test_password_update_reaches_other_worker(monkeypatch):
    from src.storage import database

    monkeypatch.delenv("API_PASSWORD", raising=False)

    class SharedDatabase:
        data = {"api_password": "synthetic-initial"}

        async def get_all_config(self):
            return self.data.copy()

        async def reload_config_cache(self):
            pass

    shared = SharedDatabase()

    async def get_database():
        return shared

    monkeypatch.setattr(database, "get_database", get_database)
    saving_worker = _fresh_config("audit_config_saving_worker")
    other_worker = _fresh_config("audit_config_other_worker")

    async def check():
        await saving_worker.init_config()
        await other_worker.init_config()
        shared.data["api_password"] = "synthetic-updated"
        await saving_worker.reload_config()
        assert await saving_worker.get_api_password() == "synthetic-updated"
        assert await other_worker.get_api_password() == "synthetic-updated"

    asyncio.run(check())


def test_config_initialization_recovers_after_transient_database_failure(monkeypatch):
    from src.storage import database

    monkeypatch.delenv("API_PASSWORD", raising=False)
    calls = []

    class SharedDatabase:
        async def get_all_config(self):
            return {"api_password": "synthetic-stored-password"}

    async def get_database():
        calls.append(True)
        if len(calls) == 1:
            raise ConnectionError("synthetic transient initialization failure")
        return SharedDatabase()

    monkeypatch.setattr(database, "get_database", get_database)
    worker = _fresh_config("audit_config_recovery")

    async def check():
        await worker.init_config()
        await worker.init_config()
        assert await worker.get_api_password() == "synthetic-stored-password"

    asyncio.run(check())


def test_legacy_credential_schema_upgrade_preserves_data(tmp_path, monkeypatch):
    from src.storage.database import Database

    credentials_dir = tmp_path / "legacy-creds"
    credentials_dir.mkdir()
    monkeypatch.setenv("CREDENTIALS_DIR", str(credentials_dir))
    db_path = credentials_dir / "credentials.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE antigravity_credentials "
            "(id INTEGER PRIMARY KEY, filename TEXT, refresh_token TEXT)"
        )
        connection.execute(
            "INSERT INTO antigravity_credentials VALUES (?, ?, ?)",
            (1, "synthetic.json", "synthetic-refresh-token"),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM antigravity_credentials"
        ).fetchone()[0] == 1

    async def check():
        db = Database()
        await db.initialize()
        try:
            with sqlite3.connect(db_path) as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM antigravity_credentials"
                ).fetchone()[0]
            assert remaining == 1, "startup erased the stored legacy credential"
        finally:
            await db.close()

    asyncio.run(check())


def test_freshly_refreshed_expiry_is_reusable_and_json_serializable(tmp_path, monkeypatch):
    import httpx

    from src import credential_manager, google_oauth_api
    from src.storage.database import Database

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path / "refresh-creds"))

    async def token_response(*args, **kwargs):
        return httpx.Response(
            200,
            json={"access_token": "synthetic-refreshed-token", "expires_in": 3600},
            request=httpx.Request("POST", "https://oauth.invalid/token"),
        )

    monkeypatch.setattr(google_oauth_api, "post_async", token_response)

    async def check():
        db = Database()
        await db.initialize()

        async def get_database():
            return db

        monkeypatch.setattr(credential_manager, "get_database", get_database)
        try:
            credential = await credential_manager.refresh_token({
                "access_token": "synthetic-old-token",
                "refresh_token": "synthetic-refresh-token",
                "client_id": "synthetic-client",
                "client_secret": "synthetic-secret",
                "expiry": "2000-01-01T00:00:00+00:00",
            }, "audit@example.invalid")
            assert credential is not None, "refresh must reach persistence successfully"

            # SQLite accepts a datetime in its TEXT column and returns a string.
            # The reproduced defect is the *returned in-memory* credential format.
            stored = await db.get_credential("audit@example.invalid")
            assert isinstance(stored["expiry"], str)
            assert datetime.fromisoformat(stored["expiry"]) > datetime.now(timezone.utc)
            assert await credential_manager.should_refresh_token(credential) is False, (
                "a just-refreshed token is immediately considered in need of refresh"
            )
            assert isinstance(credential["expiry"], str)
            json.dumps(credential)
        finally:
            await db.close()

    asyncio.run(check())


def test_session_affinity_binding_can_be_set_read_and_deleted():
    from src import session_cache

    async def check():
        key = "audit-synthetic-session"
        try:
            await session_cache.set_session_binding(key, "audit@example.invalid")
            assert await session_cache.get_session_binding(key) == "audit@example.invalid"
            await session_cache.delete_session_binding(key)
            assert await session_cache.get_session_binding(key) is None
        finally:
            await session_cache.delete_session_binding(key)

    asyncio.run(check())
