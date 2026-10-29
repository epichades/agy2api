#!/usr/bin/env python3
"""Private local control for one foreground agy2api supervisor (POSIX).

Only the process group started by this supervisor is signalled. No port lookup,
PID file kill, shell evaluation, or dotenv loading is performed here.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time


class OperationError(RuntimeError):
    """An operator-safe diagnostic that never contains source values or paths."""


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OperationError("Service state must be an owned, non-symlink directory")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise OperationError("Service state directory must have mode 700")


def stop_child(child: subprocess.Popen | None, timeout: float) -> None:
    if child is None or child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def control(state: Path, command: str, timeout: float) -> int:
    private_directory(state)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout + 8)
        try:
            client.connect(str(state / "control.sock"))
            client.sendall((command + "\n").encode())
            response = b""
            while len(response) < 1024:
                part = client.recv(1024 - len(response))
                if not part:
                    break
                response += part
        except (OSError, TimeoutError):
            print("No responding managed supervisor; use loop.sh or your service manager", file=sys.stderr)
            return 1
    data = json.loads(response)
    if command == "stop" and data.get("ok"):
        deadline = time.monotonic() + 5
        while (state / "control.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        if (state / "control.sock").exists():
            raise OperationError("Supervisor did not finish stopping")
    print(json.dumps(data))
    return 0 if data.get("ok") else 1


def supervise(root: Path, state: Path, timeout: float, restart_delay: float) -> int:
    private_directory(state)
    # Keep the lock open only in this process. A killed supervisor cannot leave
    # an orphan holding it; shutdown still terminates its owned child group.
    lock = os.open(state / "supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        print("A managed supervisor is already running", file=sys.stderr)
        return 1
    python = root / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        os.close(lock)
        raise OperationError("Missing .venv; install locked dependencies before starting")
    sock_path = state / "control.sock"
    if sock_path.exists() or sock_path.is_symlink():
        if not stat.S_ISSOCK(sock_path.lstat().st_mode):
            os.close(lock)
            raise OperationError("Unexpected object at private control socket path")
        sock_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    selector = selectors.DefaultSelector()
    child = None
    stopping = False
    next_start = 0.0

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    old_handlers = {signum: signal.signal(signum, request_stop) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        listener.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        listener.listen(4)
        listener.setblocking(False)
        selector.register(listener, selectors.EVENT_READ)
        while not stopping:
            if child is not None and child.poll() is not None:
                child = None
                next_start = time.monotonic() + restart_delay
            if child is None and time.monotonic() >= next_start:
                child = subprocess.Popen([str(python), "web.py"], cwd=root, start_new_session=True)
            for key, events in selector.select(timeout=0.2):
                connection, address = listener.accept()
                with connection:
                    connection.settimeout(1)
                    try:
                        command = connection.recv(32).strip().decode("ascii")
                        if command not in {"status", "stop", "restart"}:
                            result = {"ok": False, "error": "unknown_command"}
                        elif command == "status":
                            result = {"ok": True, "state": "running" if child and child.poll() is None else "waiting"}
                        else:
                            stop_child(child, timeout)
                            child = None
                            if command == "stop":
                                stopping = True
                            else:
                                child = subprocess.Popen([str(python), "web.py"], cwd=root, start_new_session=True)
                            result = {"ok": True, "state": "stopped" if stopping else "restarted", "readiness_checked": False}
                        connection.sendall(json.dumps(result).encode())
                    except (OSError, ValueError):
                        # An interrupted operator client must not stop serving.
                        pass
        return 0
    finally:
        stop_child(child, timeout)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        selector.close()
        listener.close()
        sock_path.unlink(missing_ok=True)
        os.close(lock)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "stop", "restart"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--stop-timeout", type=float, default=20.0)
    parser.add_argument("--restart-delay", type=float, default=5.0)
    args = parser.parse_args()
    if args.stop_timeout <= 0 or args.restart_delay < 0:
        parser.error("timeouts must be positive and restart delay nonnegative")
    root = args.root.absolute()
    state = (args.state_dir or root / "data/service-state").absolute()
    try:
        if args.command == "run":
            return supervise(root, state, args.stop_timeout, args.restart_delay)
        return control(state, args.command, args.stop_timeout)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Avoid printing private paths, child command arguments or environment.
        print("Service operation failed: " + (str(exc) if isinstance(exc, OperationError) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
