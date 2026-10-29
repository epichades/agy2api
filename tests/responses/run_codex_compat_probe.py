"""Synthetic Codex HTTP/SSE probe; no real model calls or stored request bodies.

Run with project Python and an installed codex executable on PATH:
    python tests/responses/run_codex_compat_probe.py
    AGY_AUDIT_DISABLE_APPS=1 python tests/responses/run_codex_compat_probe.py
    AGY_AUDIT_TOOL_CYCLE=1 python tests/responses/run_codex_compat_probe.py

Uses an ephemeral CLI session, ignores user config/rules, and disables analytics.
Apps/plugins remain in the first run so tool-set compatibility is actually tested.
Only request field names, tool types, status, and synthetic outcome are printed.
Requires permission to bind/connect to 127.0.0.1; it does not modify Codex config.
"""
import asyncio
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
state_directory = tempfile.TemporaryDirectory(prefix='agy2api-codex-state-')
os.environ.update(ENABLE_LOG='0', LOG_FILE='', API_PASSWORD='synthetic-local-audit-key', PANEL_PASSWORD='synthetic-panel-key', CREDENTIALS_DIR=state_directory.name, RESPONSES_STATE_KEY='')
model = os.getenv('AGY_AUDIT_MODEL', 'claude-opus-5-5-high')
tool_cycle = os.getenv('AGY_AUDIT_TOOL_CYCLE') == '1'
import httpx
import config
config._config_cache = {}
config._config_initialized = True

with patch.object(httpx.HTTPTransport, 'handle_request', side_effect=httpx.ConnectError('offline audit')):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.router.antigravity import responses as route

requests_seen = []
upstream_seen = []
tool_verified = False

async def fake_stream(body, native=False, log_ctx=None, session_key_override=None, on_credential=None):
    global tool_verified
    upstream_seen.append({'model': body.get('model'), 'session_key_supplied': bool(session_key_override)})
    if on_credential:
        on_credential('synthetic@example.invalid', body)
    inner = body['request']
    parts, finish = [{'text': 'SYNTHETIC_OK'}], 'STOP'
    if tool_cycle and len(upstream_seen) == 1:
        declarations = [d for t in inner.get('tools', []) for d in t.get('functionDeclarations', [])]
        command_tool = next((d for d in declarations if d.get('name') == 'exec_command' or d.get('name', '').endswith('__exec_command')), None)
        if command_tool is None:
            yield route._error_json(502, 'Synthetic audit command tool unavailable', 'server_error', 'server_error')
            return
        parts = [{'thought': True, 'text': 'Synthetic signed reasoning  \n', 'thoughtSignature': 'SYNTHETIC_REASONING_SIGNATURE'},
                 {'functionCall': {'id': 'synthetic-tool-call', 'name': command_tool['name'], 'args': {'cmd': 'printf SYNTHETIC_TOOL_OK', 'max_output_tokens': 50, 'yield_time_ms': 1000}}, 'thoughtSignature': 'SYNTHETIC_FUNCTION_SIGNATURE'}]
        finish = 'OTHER'
    elif tool_cycle:
        history_parts = [p for c in inner.get('contents', []) for p in c.get('parts', [])]
        results = [p['functionResponse'] for p in history_parts if 'functionResponse' in p]
        signatures = {p.get('thoughtSignature') for p in history_parts}
        exact_thought = any(p.get('thought') and p.get('text') == 'Synthetic signed reasoning  \n' for p in history_parts)
        tool_verified = (any(r.get('id') == 'synthetic-tool-call' and 'SYNTHETIC_TOOL_OK' in json.dumps(r.get('response')) for r in results)
                         and 'SYNTHETIC_FUNCTION_SIGNATURE' in signatures and exact_thought)
        if not tool_verified:
            yield route._error_json(502, 'Synthetic signed tool replay failed', 'server_error', 'server_error')
            return
    payload = {'response': {'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': finish}], 'modelVersion': 'synthetic-model-version', 'usageMetadata': {'promptTokenCount': 1, 'candidatesTokenCount': 1, 'totalTokenCount': 2}}}
    yield 'data: ' + json.dumps(payload) + '\n\n'

route.stream_request = fake_stream
app = FastAPI()
app.include_router(route.router)
asgi_client = TestClient(app)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.loads(raw)
        kinds = sorted({str(t.get('type')) for t in body.get('tools', []) if isinstance(t, dict)})
        requests_seen.append({'path': self.path, 'keys': sorted(body), 'tool_types': kinds, 'model': body.get('model')})
        response = asgi_client.post(self.path, json=body, headers={'Authorization': 'Bearer synthetic-local-audit-key'})
        requests_seen[-1]['status_code'] = response.status_code
        if response.status_code >= 400:
            error = response.json().get('error', {})
            requests_seen[-1]['error'] = {k: error.get(k) for k in ('type', 'code', 'param')}
        self.send_response(response.status_code)
        self.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
        self.send_header('Content-Length', str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
port = server.server_address[1]
provider = f'{{name="Synthetic local audit",base_url="http://127.0.0.1:{port}/v1",env_key="AGY_AUDIT_KEY",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0,supports_websockets=false}}'
env = os.environ.copy()
env.update(AGY_AUDIT_KEY='synthetic-local-audit-key', HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9', ALL_PROXY='http://127.0.0.1:9', NO_PROXY='127.0.0.1,localhost')
with tempfile.TemporaryDirectory(prefix='agy2api-codex-client-') as folder:
    prompt = 'Execute only the provided harmless synthetic printf tool call, then reply SYNTHETIC_OK.' if tool_cycle else 'Reply only SYNTHETIC_OK. Do not call tools.'
    cmd = [shutil.which('codex') or sys.exit('codex executable not found'), 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only', '--json', '-C', folder, '-m', model, '-c', 'model_provider="agy-review"', '-c', 'model_providers.agy-review=' + provider, '-c', 'web_search="disabled"', prompt]
    cmd[2:2] = ['-c', 'analytics.enabled=false', '-c', 'otel.exporter="none"']
    if os.getenv('AGY_AUDIT_DISABLE_APPS') == '1':
        cmd[2:2] = ['-c', 'features.apps=false', '-c', 'features.plugins=false']
    probe_exit = 1
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=35)
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        result_summary = {'apps_and_plugins_disabled': os.getenv('AGY_AUDIT_DISABLE_APPS') == '1', 'tool_cycle': tool_cycle, 'signed_tool_replay_verified': tool_verified, 'exit_code': result.returncode, 'request_metadata': requests_seen, 'upstream_metadata': upstream_seen, 'client_event_types': [e.get('type') for e in events], 'synthetic_reply_received': any(e.get('item', {}).get('text') == 'SYNTHETIC_OK' for e in events), 'client_error_count': sum(e.get('type') == 'error' for e in events)}
        if not requests_seen:
            result_summary['startup_diagnostic'] = 'No request reached the local endpoint; raw CLI diagnostics are intentionally omitted.'
        print(json.dumps(result_summary, ensure_ascii=False, indent=2))
        probe_exit = 0 if result.returncode == 0 and result_summary['synthetic_reply_received'] and (not tool_cycle or tool_verified) else 1
    finally:
        server.shutdown()
        server.server_close()
        state_directory.cleanup()
    raise SystemExit(probe_exit)
