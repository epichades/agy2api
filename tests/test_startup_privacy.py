"""Starting the app must preserve the checkout and stop on dependency errors."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _run_start(tmp_path, fail_sync=False):
    checkout = tmp_path / "synthetic-checkout"
    checkout.mkdir()
    (checkout / "start.sh").write_bytes((ROOT / "start.sh").read_bytes())
    loop = checkout / "loop.sh"
    loop.write_text('#!/bin/sh\nprintf "managed-loop\\n" >> "$AUDIT_COMMANDS"\n')
    loop.chmod(0o755)
    commands = tmp_path / "commands"
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$AUDIT_COMMANDS"\n'
        + ('if [ "$1" = sync ]; then exit 7; fi\n' if fail_sync else "")
    )
    uv.chmod(0o755)
    git = tmp_path / "git"
    git.write_text('#!/bin/sh\nprintf "git %s\\n" "$*" >> "$AUDIT_COMMANDS"\nexit 99\n')
    git.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "AUDIT_COMMANDS": str(commands)}
    result = subprocess.run(["bash", str(checkout / "start.sh")], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    return result, commands.read_text().splitlines()


def test_start_runs_current_checkout_without_git_reset(tmp_path):
    result, commands = _run_start(tmp_path)
    assert result.returncode == 0
    assert commands == ["sync --locked --no-dev", "managed-loop"]


def test_dependency_failure_prevents_app_start(tmp_path):
    result, commands = _run_start(tmp_path, fail_sync=True)
    assert result.returncode == 7
    assert commands == ["sync --locked --no-dev"]
