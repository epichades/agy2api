"""Sensitive request and upstream content stays out of default runtime logs."""

import asyncio
from types import SimpleNamespace

from fastapi import Response


def _enable_debug_logging(monkeypatch):
    import log

    monkeypatch.setenv("ENABLE_LOG", "1")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    log._refresh_config()


def test_converter_normalizes_private_parts_without_printing_them(monkeypatch, capsys):
    from src.converter.antigravity_fix import _clean_contents
    import log

    _enable_debug_logging(monkeypatch)
    try:
        marker = "synthetic-private-prompt-value"
        result = _clean_contents(
            [{"role": "user", "parts": [
                {"text": [{"type": "text", "text": marker}]},
                {"text": {"private": marker}},
                {"thought": marker},
            ]}],
            "synthetic-model",
        )
        assert marker in str(result)
        assert marker not in capsys.readouterr().out
    finally:
        monkeypatch.setenv("ENABLE_LOG", "0")
        log._refresh_config()


def test_anti_truncation_returns_invalid_body_without_logging_it(monkeypatch, capsys):
    from src.converter.anti_truncation import AntiTruncationStreamProcessor
    import log

    _enable_debug_logging(monkeypatch)
    try:
        marker = "synthetic-private-upstream-body"
        processor = AntiTruncationStreamProcessor(lambda _: None, {}, max_attempts=1)
        response = Response(content=f"{{not valid JSON {marker}", status_code=502)
        returned = asyncio.run(processor._handle_non_streaming_response(response))
        assert marker.encode() in returned
        captured = capsys.readouterr()
        assert marker not in captured.out + captured.err
    finally:
        monkeypatch.setenv("ENABLE_LOG", "0")
        log._refresh_config()


def test_oauth_tier_lookup_does_not_log_response_body_or_project(monkeypatch, capsys):
    from src import google_oauth_api as oauth
    import log

    _enable_debug_logging(monkeypatch)
    try:
        marker = "synthetic-private-upstream-field"
        project = "synthetic-private-project-id"

        async def post_async(*args, **kwargs):
            return SimpleNamespace(
                status_code=200,
                text=marker,
                json=lambda: {
                    "currentTier": {"id": "PRO"},
                    "cloudaicompanionProject": project,
                    "privateExtra": marker,
                },
            )

        monkeypatch.setattr(oauth, "post_async", post_async)
        actual_project, tier = asyncio.run(oauth._try_load_code_assist(
            "https://upstream.invalid", {"Authorization": "Bearer synthetic"}
        ))
        assert (actual_project, tier) == (project, "PRO")
        captured = capsys.readouterr()
        assert project not in captured.out + captured.err
        assert marker not in captured.out + captured.err
    finally:
        monkeypatch.setenv("ENABLE_LOG", "0")
        log._refresh_config()


def test_non_stream_error_logs_status_without_persisting_raw_response(monkeypatch, capsys):
    from src.api import antigravity as api
    import log

    _enable_debug_logging(monkeypatch)
    try:
        marker = "synthetic-private-error-content"
        stored = []
        credential_errors = []

        async def credentials(**kwargs):
            return "audit@example.invalid", {
                "access_token": "synthetic-access-token", "project_id": "synthetic-project"
            }

        async def retry_config():
            return {"max_retries": 0, "retry_interval": 0, "retry_enabled": False}

        async def upstream_url():
            return "https://upstream.invalid"

        async def disabled_codes():
            return []

        async def post_async(*args, **kwargs):
            return SimpleNamespace(status_code=400, content=marker.encode(),
                                   text=marker, headers={})

        async def record_error(*args, **kwargs):
            credential_errors.append(kwargs)

        async def record_usage(entry):
            stored.append(entry)

        async def no_detail(*args, **kwargs):
            return ""

        monkeypatch.setattr(api, "get_valid_credential", credentials)
        monkeypatch.setattr(api, "get_retry_config", retry_config)
        monkeypatch.setattr(api, "get_antigravity_api_url", upstream_url)
        monkeypatch.setattr(api, "get_auto_ban_error_codes", disabled_codes)
        monkeypatch.setattr(api, "post_async", post_async)
        monkeypatch.setattr(api, "record_api_call_error", record_error)
        monkeypatch.setattr(api, "record_usage_log", record_usage)
        monkeypatch.setattr(api, "_record_request_body_detail", no_detail)

        result = asyncio.run(api.non_stream_request({
            "model": "synthetic-model", "request": {"contents": []}
        }))
        assert marker.encode() in result.body  # Authenticated client keeps upstream error.
        assert stored and stored[0]["error_message"] == "HTTP 400"
        assert credential_errors and credential_errors[0]["error_message"] == "HTTP 400"
        captured = capsys.readouterr()
        assert marker not in captured.out + captured.err
    finally:
        monkeypatch.setenv("ENABLE_LOG", "0")
        log._refresh_config()


def test_stream_error_logs_status_without_persisting_raw_response(monkeypatch, capsys):
    from src.api import antigravity as api
    import log

    _enable_debug_logging(monkeypatch)
    try:
        marker = "synthetic-private-stream-error"
        stored = []
        credential_errors = []

        async def credentials(**kwargs):
            return "audit@example.invalid", {
                "access_token": "synthetic-access-token", "project_id": "synthetic-project"
            }

        async def retry_config():
            return {"max_retries": 0, "retry_interval": 0, "retry_enabled": False}

        async def upstream_url():
            return "https://upstream.invalid"

        async def disabled_codes():
            return []

        async def upstream(**kwargs):
            yield Response(content=marker, status_code=400)

        async def record_error(*args, **kwargs):
            credential_errors.append(kwargs)

        async def record_usage(entry):
            stored.append(entry)

        async def no_detail(*args, **kwargs):
            return ""

        monkeypatch.setattr(api, "get_valid_credential", credentials)
        monkeypatch.setattr(api, "get_retry_config", retry_config)
        monkeypatch.setattr(api, "get_antigravity_api_url", upstream_url)
        monkeypatch.setattr(api, "get_auto_ban_error_codes", disabled_codes)
        monkeypatch.setattr(api, "stream_post_async", upstream)
        monkeypatch.setattr(api, "record_api_call_error", record_error)
        monkeypatch.setattr(api, "record_usage_log", record_usage)
        monkeypatch.setattr(api, "_record_request_body_detail", no_detail)

        async def consume():
            return [part async for part in api.stream_request({
                "model": "synthetic-model", "request": {"contents": []}
            })]

        responses = asyncio.run(consume())
        assert len(responses) == 1
        assert marker.encode() in responses[0].body
        assert stored and stored[0]["error_message"] == "HTTP 400"
        assert credential_errors and credential_errors[0]["error_message"] == "HTTP 400"
        captured = capsys.readouterr()
        assert marker not in captured.out + captured.err
    finally:
        monkeypatch.setenv("ENABLE_LOG", "0")
        log._refresh_config()
