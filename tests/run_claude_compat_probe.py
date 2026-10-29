"""Installed Claude Code -> native Messages -> synthetic Gemini HTTP/SSE probe.

Run with the project's Python and an installed ``claude`` on PATH:
    python tests/run_claude_compat_probe.py
    AGY_AUDIT_MAX_THINKING_TOKENS=0 AGY_AUDIT_CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1 python tests/run_claude_compat_probe.py

Uses --bare, an isolated config directory/cwd, no session persistence, no MCP
servers and a synthetic loopback API key. It runs a text turn with the full
default function tool set, then a signed harmless Bash printf tool cycle.
No real model/account is used. Request bodies, client stderr and private
configuration are never printed or saved; output contains field names, HTTP
statuses and synthetic result booleans only. Requires loopback socket access.
The first command verifies the installed client's default omitted-thinking
mode. The second also supports clients configured to disable thinking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-local-audit-key"
MODEL = "claude-opus-5-5-high"
THOUGHT = "Synthetic signed reasoning  \n"
THOUGHT_SIGNATURE = "SYNTHETIC_REASONING_SIGNATURE"
FUNCTION_SIGNATURE = "SYNTHETIC_FUNCTION_SIGNATURE"
TOOL_ID = "synthetic-tool-call"
TOOL_ARGS = {"command": "printf SYNTHETIC_TOOL_OK", "description": "Synthetic compatibility probe", "run_in_background": False}


def client_environment(folder: Path, port: int) -> dict[str, str]:
    # No provider credentials, account/session variables, proxies or developer
    # settings are inherited. HOME is preserved rather than repurposed.
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    for name in ("HOME", "TMPDIR", "SYSTEMROOT"):
        if name in os.environ:
            environment[name] = os.environ[name]
    environment.update({
        "CLAUDE_CONFIG_DIR": str(folder / "config"),
        "ANTHROPIC_API_KEY": KEY,
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
        "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
        "all_proxy": "http://127.0.0.1:9", "no_proxy": "127.0.0.1,localhost",
    })
    for name in ("MAX_THINKING_TOKENS", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"):
        if os.getenv("AGY_AUDIT_" + name) is not None:
            environment[name] = os.environ["AGY_AUDIT_" + name]
    return environment


def run_client(executable: str, folder: Path, port: int, tool_cycle: bool) -> dict:
    prompt = ("Execute only the provided harmless synthetic printf tool call, then reply SYNTHETIC_OK."
              if tool_cycle else "Reply only SYNTHETIC_OK. Do not call tools.")
    command = [
        executable, "--bare", "--print", "--no-session-persistence",
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--no-chrome", "--tools", "default",
        "--output-format", "json", "--model", MODEL,
        "--system-prompt", "You are participating in a synthetic local API compatibility test.",
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--allowedTools", "Bash(printf SYNTHETIC_TOOL_OK)",
        "--prompt-suggestions", "false", prompt,
    ]
    process = subprocess.Popen(command, cwd=folder, env=client_environment(folder, port),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    timed_out, stdout = False, ""
    try:
        stdout, _ = process.communicate(timeout=35)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError):
        result = {}
    return {"client_exited_successfully": process.returncode == 0,
            "client_process_stopped": process.poll() is not None,
            "timed_out": timed_out, "client_reported_error": bool(result.get("is_error", True)),
            "synthetic_reply_received": result.get("result", "").strip() == "SYNTHETIC_OK"}


def run_probe() -> dict:
    executable = shutil.which("claude")
    if executable is None:
        return {"client_available": False}
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="agy2api-claude-probe-") as directory:
        folder = Path(directory)
        folder.chmod(0o700)
        # Isolate project imports before config/log/credential modules load.
        os.environ.update(PYTHON_DOTENV_DISABLED="1", ENABLE_LOG="0", LOG_FILE="",
                          LOG_CONSOLE="0", API_PASSWORD=KEY, PANEL_PASSWORD="synthetic-panel-key",
                          CREDENTIALS_DIR=str(folder / "server-state"), RESPONSES_STATE_KEY="")
        import dotenv
        import httpx
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        with patch.object(dotenv, "load_dotenv", return_value=False), patch.object(
                httpx.HTTPTransport, "handle_request", side_effect=httpx.ConnectError("offline audit")):
            import config
            config._config_cache = {}
            config._config_initialized = True
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from src.router.antigravity import anthropic as route

        active = {"tool_cycle": False, "request_metadata": [], "upstream_count": 0,
                  "signed_thought_preserved": False, "thought_signature_preserved": False,
                  "function_signature_preserved": False, "tool_args_preserved": False,
                  "tool_result_received": False, "tool_result_success": False}

        async def fake_stream(body, native=False, log_ctx=None, **kwargs):
            if kwargs.get("on_credential"):
                kwargs["on_credential"]("synthetic@example.invalid", body)
            active["upstream_count"] += 1
            request = body["request"]
            parts, finish = [{"text": "SYNTHETIC_OK"}], "STOP"
            if active["tool_cycle"] and active["upstream_count"] == 1:
                declarations = [item for group in request.get("tools", [])
                                for item in group.get("functionDeclarations", [])]
                bash = next((item for item in declarations if item.get("name") == "Bash"), None)
                if bash is None:
                    yield route._native_error(502, "Synthetic Bash function unavailable")
                    return
                parts = [{"thought": True, "text": THOUGHT},
                         {"thought": True, "text": "", "thoughtSignature": THOUGHT_SIGNATURE},
                         {"functionCall": {"id": TOOL_ID, "name": bash["name"], "args": TOOL_ARGS},
                          "thoughtSignature": FUNCTION_SIGNATURE}]
                finish = "OTHER"
            elif active["tool_cycle"]:
                history = [part for message in request.get("contents", []) for part in message.get("parts", [])]
                active["signed_thought_preserved"] = any(p.get("thought") is True and p.get("text") == THOUGHT for p in history)
                active["thought_signature_preserved"] = any(p.get("thoughtSignature") == THOUGHT_SIGNATURE for p in history)
                active["function_signature_preserved"] = any(p.get("thoughtSignature") == FUNCTION_SIGNATURE and p.get("functionCall", {}).get("id") == TOOL_ID for p in history)
                active["tool_args_preserved"] = any(p.get("functionCall", {}).get("args") == TOOL_ARGS for p in history)
                results = [p["functionResponse"] for p in history if "functionResponse" in p]
                active["tool_result_received"] = any(r.get("id") == TOOL_ID and "SYNTHETIC_TOOL_OK" in json.dumps(r.get("response")) for r in results)
                active["tool_result_success"] = any(r.get("id") == TOOL_ID and r.get("response", {}).get("is_error", False) is False for r in results)
                if not all(active[key] for key in ("signed_thought_preserved", "thought_signature_preserved", "function_signature_preserved", "tool_args_preserved", "tool_result_received", "tool_result_success")):
                    yield route._native_error(502, "Synthetic signed tool replay failed")
                    return
            payload = {"response": {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
                                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}}
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

        route.stream_request = fake_stream
        app = FastAPI()
        app.include_router(route.router)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    tools = [tool for tool in body.get("tools", []) if isinstance(tool, dict)]
                    record = {"keys": sorted(body), "tool_types": sorted({str(tool.get("type", "custom")) for tool in tools}),
                              "tool_keys": sorted({key for tool in tools for key in tool}),
                              "has_function_tools": bool(tools), "status_code": 500}
                    record["context_management_keys"] = sorted(body.get("context_management") or {})
                    edits = (body.get("context_management") or {}).get("edits") or []
                    record["context_management_has_edits"] = bool(edits)
                    record["context_management_edit_types"] = sorted({edit.get("type", "") for edit in edits if isinstance(edit, dict)})
                    record["context_management_clear_thinking_keep_all"] = bool(edits) and all(isinstance(edit, dict) and edit.get("type") == "clear_thinking_20251015" and edit.get("keep") == "all" for edit in edits)
                    record["thinking_keys"] = sorted(body.get("thinking") or {})
                    record["thinking_display_omitted"] = (body.get("thinking") or {}).get("display") == "omitted"
                    record["output_config_keys"] = sorted(body.get("output_config") or {})
                    replayed_thinking = [block for message in body.get("messages", [])
                                         if isinstance(message.get("content"), list)
                                         for block in message["content"] if block.get("type") == "thinking"]
                    record["native_hidden_replay_opaque"] = bool(replayed_thinking) and all(block.get("thinking") == "" and str(block.get("signature", "")).startswith("agy2.") for block in replayed_thinking)
                    replayed_tools = [block for message in body.get("messages", [])
                                      if isinstance(message.get("content"), list)
                                      for block in message["content"] if block.get("type") == "tool_use"]
                    record["signed_native_tool_replay_opaque"] = bool(replayed_tools) and all(str(block.get("id", "")).startswith("call_agy2_") for block in replayed_tools)
                    active["request_metadata"].append(record)
                    headers = {"Authorization": "Bearer " + KEY}
                    for name in ("anthropic-beta", "anthropic-version"):
                        if self.headers.get(name):
                            headers[name] = self.headers[name]
                    response = asgi_client.post(self.path, json=body, headers=headers)
                    record["status_code"] = response.status_code
                    record["thinking_plaintext_absent_from_response"] = THOUGHT.strip().encode() not in response.content and THOUGHT_SIGNATURE.encode() not in response.content
                    record["function_signature_absent_from_response"] = FUNCTION_SIGNATURE.encode() not in response.content and b"__thought__" not in response.content
                    record["ignored_parameter_keys"] = response.headers.get("x-agy-ignored-params", "").split(",") if response.headers.get("x-agy-ignored-params") else []
                    if response.status_code >= 400:
                        error = response.json().get("error", {})
                        message = error.get("message", "") if isinstance(error, dict) else ""
                        # Print only field identifiers, never an upstream/client body.
                        match = re.fullmatch(r"Unsupported Messages parameter: ([A-Za-z0-9_]+)", message)
                        if match:
                            record["rejected_parameter_key"] = match.group(1)
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
                    self.send_header("Content-Length", str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
                except Exception:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        outcomes = {}
        with TestClient(app) as asgi_client:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for name, tool_cycle in (("text", False), ("signed_bash_tool_cycle", True)):
                    client_folder = folder / name
                    client_folder.mkdir(mode=0o700)
                    active.update(tool_cycle=tool_cycle, request_metadata=[], upstream_count=0)
                    result = run_client(executable, client_folder, server.server_address[1], tool_cycle)
                    result["request_metadata"] = list(active["request_metadata"])
                    result["upstream_called"] = active["upstream_count"] > 0
                    result["expected_upstream_call_count"] = active["upstream_count"] == (2 if tool_cycle else 1)
                    if tool_cycle:
                        result.update({key: active[key] for key in ("signed_thought_preserved", "thought_signature_preserved", "function_signature_preserved", "tool_args_preserved", "tool_result_received", "tool_result_success")})
                    outcomes[name] = result
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                outcomes["loopback_server_stopped"] = not thread.is_alive()
        return outcomes


def main() -> int:
    try:
        outcomes = run_probe()
    except Exception:
        outcomes = {"probe_completed": False}
    print(json.dumps(outcomes, ensure_ascii=False, indent=2, sort_keys=True))
    successes = [outcomes.get(name, {}) for name in ("text", "signed_bash_tool_cycle")]
    required = ("client_exited_successfully", "client_process_stopped", "synthetic_reply_received",
                "upstream_called", "expected_upstream_call_count")
    replay = ("signed_thought_preserved", "thought_signature_preserved", "function_signature_preserved",
              "tool_args_preserved", "tool_result_received", "tool_result_success")
    hidden_records = [record for result in successes for record in result.get("request_metadata", [])
                      if record.get("thinking_display_omitted")]
    passed = (outcomes.get("loopback_server_stopped")
              and all(all(result.get(key) for key in required) and not result.get("timed_out")
                      and not result.get("client_reported_error") for result in successes)
              and all(outcomes["signed_bash_tool_cycle"].get(key) for key in replay)
              and (not hidden_records or any(record.get("native_hidden_replay_opaque") and record.get("signed_native_tool_replay_opaque")
                                            for record in outcomes["signed_bash_tool_cycle"].get("request_metadata", [])))
              and all(record.get("thinking_plaintext_absent_from_response") and record.get("function_signature_absent_from_response")
                      for record in hidden_records))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
