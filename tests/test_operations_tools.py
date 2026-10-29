"""Synthetic subprocess checks for private supervision and rollback snapshots."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts/service_control.py"
SNAPSHOT = ROOT / "scripts/deployment_snapshot.py"


def wait_until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.03)
    raise AssertionError("Synthetic subprocess did not reach its expected state")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group supervisor")
def test_supervisor_controls_only_owned_child_and_forces_bounded_stop(tmp_path):
    root = tmp_path / "checkout with spaces"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    (root / "web.py").write_text('''import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with Path("starts").open("a") as log:
    log.write(str(os.getpid()) + "\\n")
while True: time.sleep(1)
''')
    # macOS pytest temp roots can exceed the Unix socket pathname limit.
    state = Path(tempfile.mkdtemp(prefix="agy-svc-", dir="/tmp"))
    command = [sys.executable, str(CONTROL), "--root", str(root), "--state-dir", str(state), "--stop-timeout", "0.2", "--restart-delay", "0.1"]
    supervisor = subprocess.Popen([*command, "run"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        wait_until(lambda: (root / "starts").exists() or supervisor.poll() is not None)
        assert (root / "starts").exists(), supervisor.communicate(timeout=1)[1].decode()
        second = subprocess.run([*command, "run"], capture_output=True, text=True, timeout=5)
        assert second.returncode == 1
        assert "already running" in second.stderr
        restart = subprocess.run([*command, "restart"], capture_output=True, text=True, timeout=5)
        assert restart.returncode == 0
        assert json.loads(restart.stdout)["readiness_checked"] is False
        wait_until(lambda: len((root / "starts").read_text().splitlines()) == 2)
        assert unrelated.poll() is None
        before = time.monotonic()
        stop = subprocess.run([*command, "stop"], capture_output=True, text=True, timeout=5)
        assert stop.returncode == 0
        supervisor.wait(timeout=5)
        assert time.monotonic() - before < 4
        assert unrelated.poll() is None
        assert not (state / "control.sock").exists()
        missing = subprocess.run([*command, "restart"], capture_output=True, text=True, timeout=5)
        assert missing.returncode == 1
        assert "No responding managed supervisor" in missing.stderr
    finally:
        supervisor.terminate() if supervisor.poll() is None else None
        supervisor.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)
        import shutil
        shutil.rmtree(state)


@pytest.mark.skipif(os.name != "posix", reason="POSIX service state permissions")
def test_service_control_refuses_untrusted_state_directory(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    result = subprocess.run([sys.executable, str(CONTROL), "status", "--state-dir", str(state)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Operation" not in result.stdout
    assert str(state) not in result.stderr


def test_snapshot_captures_external_credentials_env_and_restores_only_to_new_stage(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    (project / "web.py").write_text("SYNTHETIC_OLD_SOURCE")
    (project / "runner").symlink_to(sys.executable)
    credentials = tmp_path / "private-state"
    credentials.mkdir()
    (credentials / "responses-state.key").write_text("SYNTHETIC_PRIVATE_KEY")
    env = tmp_path / "private.env"
    env.write_text("RESPONSES_STATE_KEY=SYNTHETIC_ENV_KEY\n")
    with sqlite3.connect(credentials / "credentials.db") as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE synthetic (value TEXT)")
        database.execute("INSERT INTO synthetic VALUES ('original')")
    snapshot = tmp_path / "snapshot"
    command = [sys.executable, str(SNAPSHOT), "create", "--project", str(project), "--credentials-dir", str(credentials), "--env-file", str(env), "--database", "sqlite", "--output", str(snapshot)]
    denied = subprocess.run(command, capture_output=True, text=True)
    assert denied.returncode == 1 and not snapshot.exists()
    created = subprocess.run([*command, "--service-stopped"], capture_output=True, text=True)
    assert created.returncode == 0, created.stdout
    assert "SYNTHETIC_PRIVATE_KEY" not in created.stdout and str(env) not in created.stdout
    assert snapshot.stat().st_mode & 0o777 == 0o700
    for item in snapshot.iterdir():
        assert item.stat().st_mode & 0o777 == 0o600
    (project / "web.py").write_text("SYNTHETIC_NEW_SOURCE")
    with sqlite3.connect(credentials / "credentials.db") as database:
        database.execute("INSERT INTO synthetic VALUES ('new write')")
    stage = tmp_path / "rollback-stage"
    restored = subprocess.run([sys.executable, str(SNAPSHOT), "restore", "--snapshot", str(snapshot), "--destination", str(stage)], capture_output=True, text=True)
    assert restored.returncode == 0, restored.stdout
    assert (project / "web.py").read_text() == "SYNTHETIC_NEW_SOURCE"
    assert (stage / "project/web.py").read_text() == "SYNTHETIC_OLD_SOURCE"
    assert (stage / "project/runner").is_symlink()
    assert (stage / "credentials/responses-state.key").read_text() == "SYNTHETIC_PRIVATE_KEY"
    assert (stage / "environment-0").read_bytes() == env.read_bytes()
    with sqlite3.connect(stage / "credentials.sqlite") as database:
        assert database.execute("SELECT value FROM synthetic").fetchall() == [("original",)]
    with sqlite3.connect(credentials / "credentials.db") as database:
        assert database.execute("SELECT COUNT(*) FROM synthetic").fetchone()[0] == 2
    repeated = subprocess.run([sys.executable, str(SNAPSHOT), "restore", "--snapshot", str(snapshot), "--destination", str(stage)], capture_output=True, text=True)
    assert repeated.returncode == 1
    with (snapshot / "snapshot.tar.gz").open("ab") as archive:
        archive.write(b"corrupt")
    checked = subprocess.run([sys.executable, str(SNAPSHOT), "verify", "--snapshot", str(snapshot)], capture_output=True, text=True)
    assert checked.returncode == 1


def test_snapshot_postgresql_requires_external_backup_and_refuses_recursive_target(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    credentials = project / "creds"
    credentials.mkdir()
    command = [sys.executable, str(SNAPSHOT), "create", "--project", str(project), "--credentials-dir", str(credentials), "--database", "postgresql", "--output", str(tmp_path / "backup"), "--service-stopped"]
    assert subprocess.run(command, capture_output=True).returncode == 1
    external = tmp_path / "database.dump"
    external.write_text("SYNTHETIC_EXTERNAL_DATABASE")
    command[command.index("--output") + 1] = str(project / "backup")
    assert subprocess.run([*command, "--external-database-backup", str(external)], capture_output=True).returncode == 1
    assert not (project / "backup").exists()


def test_build_identity_explicit_unknown_and_safe_response(monkeypatch):
    from src.panel import version
    monkeypatch.setenv("AGY_BUILD_SHA", "a" * 40)
    assert version._read_build_identity() == {"commit": "a" * 40, "source": "environment", "dirty": None}
    monkeypatch.setenv("AGY_BUILD_SHA", "SYNTHETIC_PRIVATE_INVALID_VALUE")
    bad = version._read_build_identity()
    assert bad == {"commit": None, "source": "invalid_environment", "dirty": None}
    monkeypatch.delenv("AGY_BUILD_SHA")
    monkeypatch.setattr(version.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("private path")))
    assert version._read_build_identity() == {"commit": None, "source": "unavailable", "dirty": None}
    identity = {"commit": "a" * 40, "source": "environment", "dirty": None}
    monkeypatch.setattr(version, "_BUILD_IDENTITY", identity)
    response = asyncio.run(version.get_version_info())
    payload = json.loads(response.body)
    assert payload["success"] is True
    assert {"version", "full_hash", "message", "date"} <= payload.keys()
    assert payload["build"] == identity
