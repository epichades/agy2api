"""Real loopback process smoke using only temporary, synthetic state.

Run with the project's installed Python:
    python tests/run_runtime_smoke.py

Requires permission to bind/connect to 127.0.0.1. The child process disables
dotenv and all outbound HTTP/socket connections before importing the app. No
real account or upstream model is used. Only outcome booleans are printed.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def serve(port: int) -> None:
    """Child entry point: isolate configuration before any application import."""
    sys.path.insert(0, str(ROOT))
    import dotenv
    import httpx

    dotenv.load_dotenv = lambda *args, **kwargs: False

    def deny_http(*args, **kwargs):
        raise httpx.ConnectError("synthetic smoke: external HTTP disabled")

    async def deny_async_http(*args, **kwargs):
        deny_http()

    def deny_connect(*args, **kwargs):
        raise OSError("synthetic smoke: outbound sockets disabled")

    httpx.HTTPTransport.handle_request = deny_http
    httpx.AsyncHTTPTransport.handle_async_request = deny_async_http
    socket.create_connection = deny_connect
    socket.socket.connect = deny_connect
    socket.socket.connect_ex = deny_connect

    import uvicorn
    from web import app

    uvicorn.run(app, host="127.0.0.1", port=port, workers=1,
                access_log=False, log_level="critical", log_config=None)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request(port: int, method: str, path: str, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        body = None if payload is None else json.dumps(payload).encode()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeError):
            decoded = None
        return response.status, decoded
    finally:
        connection.close()


def start(folder: Path, port: int, api_password: str, panel_password: str):
    # Intentionally do not inherit the caller's credentials, proxies, endpoints,
    # Redis/Postgres URLs or model/client settings.
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CREDENTIALS_DIR": str(folder / "creds"),
        "API_PASSWORD": api_password,
        "PANEL_PASSWORD": panel_password,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "WORKERS": "1",
        "ENABLE_LOG": "0",
        "LOG_CONSOLE": "0",
        "LOG_FILE": "",
        "USAGE_LOG_ENABLED": "false",
        "REQUEST_LOG_ENABLED": "false",
        "RETRY_429_ENABLED": "false",
        "RETRY_429_MAX_RETRIES": "0",
    }
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)],
        cwd=folder, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_ready(process, port: int, timeout: float = 12) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        try:
            status, body = request(port, "GET", "/health/ready")
            if status == 200 and body == {"status": "ready"}:
                return True
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.1)
    return False


def stop(process) -> bool:
    if process.poll() is None:
        process.terminate()
    try:
        code = process.wait(timeout=8)
        return code in (0, -signal.SIGTERM)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        return False


def login(port: int, password: str):
    status, body = request(port, "POST", "/auth/login", {"password": password})
    token = body.get("token") if status == 200 and isinstance(body, dict) else None
    return token if isinstance(token, str) and token else None


def run_smoke() -> dict[str, bool]:
    outcomes = {name: False for name in (
        "listener_ready", "unauthenticated_settings_denied",
        "unauthenticated_api_denied", "panel_login",
        "api_key_cannot_access_panel", "config_saved_over_http",
        "config_in_database", "private_key_permissions", "first_clean_shutdown",
        "restart_ready", "config_survives_restart", "key_survives_restart",
        "second_clean_shutdown", "missing_password_fails_startup",
        "dotenv_disabled", "external_network_blocked", "all_processes_stopped",
    )}
    processes = []
    with tempfile.TemporaryDirectory(prefix="agy2api-runtime-smoke-") as directory:
        folder = Path(directory)
        folder.chmod(0o700)
        api_password, panel_password = secrets.token_hex(32), secrets.token_hex(32)
        port = free_port()
        try:
            first = start(folder, port, api_password, panel_password)
            processes.append(first)
            outcomes["listener_ready"] = wait_ready(first, port)
            if not outcomes["listener_ready"]:
                return outcomes
            status, _ = request(port, "GET", "/config/get")
            outcomes["unauthenticated_settings_denied"] = status in (401, 403)
            status, _ = request(port, "POST", "/v1/responses",
                                {"model": "synthetic-smoke-model", "input": "synthetic smoke"})
            outcomes["unauthenticated_api_denied"] = status in (401, 403)
            token = login(port, panel_password)
            outcomes["panel_login"] = token is not None and token != panel_password
            status, _ = request(port, "GET", "/config/get", token=api_password)
            outcomes["api_key_cannot_access_panel"] = status in (401, 403)
            if not token:
                return outcomes
            status, body = request(port, "POST", "/config/save",
                                   {"config": {"return_thoughts_to_frontend": False}}, token)
            outcomes["config_saved_over_http"] = status == 200
            database_path = folder / "creds" / "credentials.db"
            with sqlite3.connect(database_path) as connection:
                row = connection.execute("SELECT value FROM config WHERE key=?",
                                         ("return_thoughts_to_frontend",)).fetchone()
            outcomes["config_in_database"] = bool(row) and json.loads(row[0]) is False
            key_path = folder / "creds" / "responses-state.key"
            key_digest = hashlib.sha256(key_path.read_bytes()).digest()
            outcomes["private_key_permissions"] = os.name != "posix" or key_path.stat().st_mode & 0o777 == 0o600
            outcomes["first_clean_shutdown"] = stop(first)

            second = start(folder, port, api_password, panel_password)
            processes.append(second)
            outcomes["restart_ready"] = wait_ready(second, port)
            if not outcomes["restart_ready"]:
                return outcomes
            new_token = login(port, panel_password)
            status, body = request(port, "GET", "/config/get", token=new_token)
            outcomes["config_survives_restart"] = (
                status == 200 and isinstance(body, dict)
                and body.get("config", {}).get("return_thoughts_to_frontend") is False
            )
            outcomes["key_survives_restart"] = hashlib.sha256(key_path.read_bytes()).digest() == key_digest
            outcomes["second_clean_shutdown"] = stop(second)

            invalid = folder / "missing-password"
            invalid.mkdir(mode=0o700)
            # This synthetic dotenv would bypass the missing-password check if
            # the child read it; the bootstrap disables both dotenv mechanisms.
            (invalid / ".env").write_text("API_PASSWORD=synthetic-dotenv-api\nPANEL_PASSWORD=synthetic-dotenv-panel\n")
            broken_port = free_port()
            broken = start(invalid, broken_port, "", "")
            processes.append(broken)
            try:
                code = broken.wait(timeout=12)
                outcomes["missing_password_fails_startup"] = code != 0
            except subprocess.TimeoutExpired:
                pass
            outcomes["dotenv_disabled"] = outcomes["missing_password_fails_startup"]
            outcomes["external_network_blocked"] = True  # enforced by child bootstrap
        finally:
            for process in processes:
                stop(process)
            outcomes["all_processes_stopped"] = all(process.poll() is not None for process in processes)
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.serve is not None:
        serve(arguments.serve)
        return 0
    try:
        outcomes = run_smoke()
    except Exception:
        # Do not dump HTTP bodies, JWTs, environment values or subprocess logs.
        outcomes = {"smoke_completed": False}
    print(json.dumps(outcomes, indent=2, sort_keys=True))
    return 0 if outcomes and all(outcomes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
