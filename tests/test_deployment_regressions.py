"""Deployment invariants tested offline with synthetic credentials and storage."""
import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


def test_configuration_reads_observe_other_connection_updates(tmp_path, monkeypatch):
    from src.storage.database import Database
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path / 'shared'))

    async def check():
        first, second = Database(), Database()
        await first.initialize()
        await second.initialize()
        try:
            await first.set_config('api_password', 'synthetic-one')
            assert await second.get_config('api_password') == 'synthetic-one'
            assert (await second.get_all_config())['api_password'] == 'synthetic-one'
            await first.set_config('api_password', 'synthetic-two')
            assert (await second.get_all_config())['api_password'] == 'synthetic-two'
            await first.delete_config('api_password')
            assert 'api_password' not in await second.get_all_config()
        finally:
            await first.close()
            await second.close()
    asyncio.run(check())


def test_database_initialization_failure_can_retry_without_leaking_engine(tmp_path, monkeypatch):
    from src.storage.database import Database
    monkeypatch.setenv('CREDENTIALS_DIR', str(tmp_path / 'retry'))

    async def check():
        database = Database()
        original = database._load_config_cache
        calls = 0
        async def load():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('synthetic initialization failure')
            await original()
        monkeypatch.setattr(database, '_load_config_cache', load)
        with pytest.raises(RuntimeError):
            await database.initialize()
        assert not database._initialized
        assert database._engine is None
        await database.initialize()
        try:
            await database.check_health()
            assert calls == 2
            assert (tmp_path / 'retry' / 'credentials.db').stat().st_mode & 0o777 == 0o600
        finally:
            await database.close()
    asyncio.run(check())


def test_legacy_direct_fields_are_preserved_and_unknown_identity_quarantined(tmp_path, monkeypatch):
    from src.storage.database import Database
    root = tmp_path / 'legacy'
    root.mkdir()
    monkeypatch.setenv('CREDENTIALS_DIR', str(root))
    path = root / 'credentials.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE antigravity_credentials (id INTEGER PRIMARY KEY, filename TEXT, refresh_token TEXT, access_token TEXT, project_id TEXT)')
        connection.executemany('INSERT INTO antigravity_credentials VALUES (?, ?, ?, ?, ?)', [
            (1, 'synthetic@example.invalid.json', 'synthetic-refresh-known', 'synthetic-token', 'synthetic-project'),
            (2, 'opaque.json', 'synthetic-refresh-unknown', '', ''),
        ])
    async def check():
        database = Database()
        await database.initialize()
        try:
            known = await database.get_credential('synthetic@example.invalid')
            assert known['refresh_token'] == 'synthetic-refresh-known'
            assert known['project_id'] == 'synthetic-project'
            with sqlite3.connect(path) as connection:
                old_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'antigravity_credentials_legacy_%' AND type='table'")]
                assert len(old_names) == 1
                assert connection.execute(f'SELECT COUNT(*) FROM {old_names[0]}').fetchone()[0] == 2
                unknown = connection.execute('SELECT refresh_token, disabled FROM antigravity_credentials WHERE email LIKE ?', ('%@unidentified.invalid',)).fetchone()
                assert unknown == ('synthetic-refresh-unknown', 1)
        finally:
            await database.close()
    asyncio.run(check())


def test_startup_fails_closed_on_database_error(monkeypatch):
    import config
    import web
    from src.storage import database
    monkeypatch.setenv('API_PASSWORD', 'synthetic-api')
    monkeypatch.setenv('PANEL_PASSWORD', 'synthetic-panel')
    monkeypatch.setattr(config, '_config_initialized', False)
    monkeypatch.setattr(config, '_config_source', None)
    monkeypatch.setattr(database, 'get_database', AsyncMock(side_effect=RuntimeError('synthetic-private-db-uri')))
    with pytest.raises(RuntimeError, match='Configuration initialization failed') as raised:
        with TestClient(web.app):
            pass
    assert 'synthetic-private-db-uri' not in str(raised.value)
    assert web.app.state.ready is False


def test_unsupported_multiworker_is_rejected_before_serving(monkeypatch):
    import web
    monkeypatch.setenv('WORKERS', '2')
    with pytest.raises(RuntimeError, match='WORKERS must be 1'):
        web.main()
    with pytest.raises(RuntimeError, match='WORKERS must be 1'):
        with TestClient(web.app):
            pass


def test_readiness_checks_live_database_without_exposing_error(monkeypatch):
    import config
    import web
    from src.storage import database
    monkeypatch.setenv('API_PASSWORD', 'synthetic-api')
    monkeypatch.setenv('PANEL_PASSWORD', 'synthetic-panel')
    monkeypatch.setattr(config, '_config_initialized', False)
    monkeypatch.setattr(config, '_config_source', None)
    monkeypatch.setattr(database, '_database', None)
    with TestClient(web.app) as client:
        assert client.get('/health/ready').json() == {'status': 'ready'}
        active = database._database
        monkeypatch.setattr(active, 'check_health', AsyncMock(side_effect=RuntimeError('synthetic-private-db-detail')))
        failed = client.get('/health/ready')
        assert failed.status_code == 503
        assert failed.json() == {'status': 'not_ready'}
        assert 'synthetic-private' not in failed.text
    assert web.app.state.ready is False


def test_configured_redis_failure_is_explicit_and_closes_client(monkeypatch):
    from src import session_cache
    import redis.asyncio
    fake = SimpleNamespace(ping=AsyncMock(side_effect=RuntimeError('synthetic-private-redis-url')), aclose=AsyncMock())
    monkeypatch.setattr(redis.asyncio, 'from_url', lambda *args, **kwargs: fake)
    async def check():
        with pytest.raises(RuntimeError, match='Configured Redis is unavailable') as raised:
            await session_cache.init_redis('redis://synthetic:synthetic@redis.invalid')
        assert 'synthetic-private' not in str(raised.value)
        fake.aclose.assert_awaited_once()
        assert session_cache._get_client() is None
    asyncio.run(check())


def test_restart_script_is_portable_and_does_not_execute_dotenv(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'restart.sh'
    folder = tmp_path / 'checkout with spaces'
    (folder / '.venv/bin').mkdir(parents=True)
    script = folder / 'restart.sh'
    script.write_bytes(source.read_bytes())
    marker = folder / 'must-not-be-created'
    (folder / '.env').write_text(f'touch "{marker}"\n')
    python = folder / '.venv/bin/python'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$*"\n')
    python.chmod(0o755)
    result = subprocess.run(['bash', str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(folder), 'web.py']
    assert not marker.exists()
