"""App lifecycle, authentication and static assets with a temporary database."""

import re

from fastapi.testclient import TestClient


def test_app_starts_authenticates_and_serves_built_panel(monkeypatch):
    import config
    from src.storage import database
    from web import app

    monkeypatch.setenv("API_PASSWORD", "synthetic-audit-api-password")
    monkeypatch.setenv("PANEL_PASSWORD", "synthetic-audit-panel-password")
    monkeypatch.setattr(config, "_config_initialized", False)
    monkeypatch.setattr(database, "_database", None)

    with TestClient(app) as client:
        panel = client.get("/")
        assert panel.status_code == 200
        assets = re.findall(r'(?:src|href)="([^\"]*assets/[^\"]+)"', panel.text)
        assert assets
        for asset in assets:
            assert client.get("/" + asset.removeprefix("./")).status_code == 200

        assert client.get("/config/get").status_code in (401, 403)
        assert client.post("/auth/login", json={"password": "incorrect"}).status_code == 401
        login = client.post("/auth/login", json={"password": "synthetic-audit-panel-password"})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        assert client.get("/config/get", headers=headers).status_code == 200
        # An API password must not grant access to credential management.
        assert client.get("/config/get", headers={
            "Authorization": "Bearer synthetic-audit-api-password"
        }).status_code in (401, 403)

        unauthenticated = client.post("/v1/chat/completions", json={
            "model": "synthetic-model", "messages": [{"role": "user", "content": "test"}]
        })
        assert unauthenticated.status_code in (401, 403)
        paths = set(app.openapi()["paths"])
        assert {"/v1/chat/completions", "/v1/messages", "/auth/login"} <= paths

    assert database._database is None
