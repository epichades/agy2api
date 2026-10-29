"""Stream delivery is irreversible: recovery must never duplicate tools/content."""

import asyncio
import json

from fastapi import Response
from fastapi.responses import StreamingResponse
from starlette.requests import Request
import pytest


@pytest.fixture
def stream_api(monkeypatch):
    from src.api import antigravity as api

    state = {"retry_enabled": True, "max_retries": 2, "usage": [], "success": [], "credentials": 0}

    async def credentials(**kwargs):
        state["credentials"] += 1
        return "synthetic@example.invalid", {"access_token": "synthetic-token", "project_id": "synthetic-project"}

    async def retry_config():
        return {"retry_enabled": state["retry_enabled"], "max_retries": state["max_retries"], "retry_interval": 0}

    async def url():
        return "https://upstream.invalid"

    async def disabled_codes():
        return []

    async def record_usage(entry):
        state["usage"].append(entry)

    async def record_success(*args, **kwargs):
        state["success"].append(kwargs)

    async def no_detail(*args, **kwargs):
        return ""

    async def noop(*args, **kwargs):
        return None

    async def should_retry(status, email, enabled, attempt, maximum, interval):
        return enabled and attempt < maximum

    for name, fn in {
        "get_valid_credential": credentials,
        "get_retry_config": retry_config,
        "get_antigravity_api_url": url,
        "get_auto_ban_error_codes": disabled_codes,
        "record_usage_log": record_usage,
        "record_api_call_success": record_success,
        "record_api_call_error": noop,
        "_record_request_body_detail": no_detail,
        "handle_error_with_retry": should_retry,
        "parse_and_log_cooldown": noop,
    }.items():
        monkeypatch.setattr(api, name, fn)
    return api, state


BODY = {"model": "gemini-test", "request": {"contents": [{"role": "user", "parts": [{"text": "synthetic input"}]}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("exception_kind", ["native", "responses"])
async def test_retry_account_validation_precedes_every_transport(stream_api, monkeypatch, status, exception_kind):
    from fastapi import HTTPException
    from src.responses.request import ResponsesError
    api, state = stream_api
    accounts = iter(["original@example.invalid", "foreign@example.invalid"])
    selected, transports, closed = [], [], []

    async def credentials(**kwargs):
        account = next(accounts)
        return account, {"access_token": account, "project_id": "synthetic-project"}

    def validate(account, payload):
        selected.append(account)
        if account != "original@example.invalid":
            if exception_kind == "native":
                raise HTTPException(409, "Synthetic account mismatch")
            raise ResponsesError(409, "Synthetic account mismatch", code="state_account_mismatch")

    async def upstream(**kwargs):
        transports.append(kwargs["headers"]["Authorization"])
        try:
            yield Response(content="synthetic rate-limit", status_code=status)
        finally:
            closed.append(True)

    monkeypatch.setattr(api, "get_valid_credential", credentials)
    monkeypatch.setattr(api, "stream_post_async", upstream)
    with pytest.raises(HTTPException if exception_kind == "native" else ResponsesError):
        _ = [item async for item in api.stream_request(BODY, on_credential=validate)]
    assert selected == ["original@example.invalid", "foreign@example.invalid"]
    assert transports == ["Bearer original@example.invalid"] and closed == [True]
    assert len(state["usage"]) == 1 and state["success"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_enabled", [False, True])
async def test_delivered_content_is_never_replayed(stream_api, monkeypatch, retry_enabled):
    api, state = stream_api
    state["retry_enabled"] = retry_enabled
    calls, closed = [], []

    async def upstream(**kwargs):
        calls.append(1)
        try:
            yield "already delivered"
            raise ConnectionError("synthetic-private-error")
        finally:
            closed.append(1)

    monkeypatch.setattr(api, "stream_post_async", upstream)
    chunks = [x async for x in api.stream_request(BODY)]
    assert calls == closed == [1]
    assert chunks[0] == "already delivered"
    assert len(chunks) == 2 and isinstance(chunks[1], Response)
    assert chunks[1].status_code == 502
    assert b"synthetic-private-error" not in chunks[1].body
    assert [(x["status_code"], x["success"]) for x in state["usage"]] == [(502, False)]
    assert state["success"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_enabled,expected_calls", [(False, 1), (True, 2)])
async def test_pre_delivery_transport_retry_obeys_flag(stream_api, monkeypatch, retry_enabled, expected_calls):
    api, state = stream_api
    state["retry_enabled"] = retry_enabled
    calls, closed = [], []

    async def upstream(**kwargs):
        calls.append(1)
        try:
            if len(calls) == 1:
                raise ConnectionError("synthetic-private-error")
            yield "complete"
        finally:
            closed.append(1)

    monkeypatch.setattr(api, "stream_post_async", upstream)
    chunks = [x async for x in api.stream_request(BODY)]
    assert len(calls) == len(closed) == expected_calls
    if retry_enabled:
        assert chunks == ["complete"]
        assert [(x["status_code"], x["success"]) for x in state["usage"]] == [(500, False), (200, True)]
        assert len(state["success"]) == 1
    else:
        assert len(chunks) == 1 and chunks[0].status_code == 500
        assert state["success"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_enabled,expected_calls", [(False, 1), (True, 2)])
async def test_empty_stream_retry_obeys_flag(stream_api, monkeypatch, retry_enabled, expected_calls):
    api, state = stream_api
    state["retry_enabled"] = retry_enabled
    calls = []

    async def upstream(**kwargs):
        calls.append(1)
        if len(calls) > 1:
            yield "complete"

    monkeypatch.setattr(api, "stream_post_async", upstream)
    chunks = [x async for x in api.stream_request(BODY)]
    assert len(calls) == expected_calls
    assert chunks == ["complete"] if retry_enabled else chunks[0].status_code == 500


@pytest.mark.asyncio
async def test_disabled_http_retry_does_not_spawn_credential_prewarm(stream_api, monkeypatch):
    api, state = stream_api
    state["retry_enabled"] = False
    closed = []

    async def upstream(**kwargs):
        try:
            yield Response(content="synthetic authenticated upstream error", status_code=429)
        finally:
            closed.append(1)

    monkeypatch.setattr(api, "stream_post_async", upstream)
    chunks = [x async for x in api.stream_request(BODY)]
    assert chunks[0].status_code == 429
    assert chunks[0].body == b"synthetic authenticated upstream error"
    assert state["credentials"] == 1 and closed == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_http_errors_can_recover_before_delivery(stream_api, monkeypatch, status):
    api, state = stream_api
    calls, closed = [], []

    async def upstream(**kwargs):
        calls.append(1)
        try:
            if len(calls) == 1:
                yield Response(content="synthetic temporary error", status_code=status)
            else:
                yield "complete"
        finally:
            closed.append(1)

    monkeypatch.setattr(api, "stream_post_async", upstream)
    assert [chunk async for chunk in api.stream_request(BODY)] == ["complete"]
    assert calls == closed == [1, 1]
    assert [(entry["status_code"], entry["success"]) for entry in state["usage"]] == [(status, False), (200, True)]


@pytest.mark.asyncio
async def test_http_error_after_delivery_is_terminal(stream_api, monkeypatch):
    api, state = stream_api
    calls = []

    async def upstream(**kwargs):
        calls.append(1)
        yield "already delivered"
        yield Response(content="synthetic upstream error", status_code=503)

    monkeypatch.setattr(api, "stream_post_async", upstream)
    chunks = [x async for x in api.stream_request(BODY)]
    assert calls == [1]
    assert chunks[0] == "already delivered" and chunks[1].status_code == 503
    assert state["success"] == []
    assert state["usage"][0]["success"] is False


@pytest.mark.asyncio
async def test_consumer_close_releases_upstream_without_success(stream_api, monkeypatch):
    api, state = stream_api
    closed = []

    async def upstream(**kwargs):
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(1)

    monkeypatch.setattr(api, "stream_post_async", upstream)
    gen = api.stream_request(BODY)
    assert await anext(gen) == "first"
    await gen.aclose()
    assert closed == [1] and state["success"] == []
    assert [(x["status_code"], x["success"]) for x in state["usage"]] == [(499, False)]


@pytest.mark.asyncio
async def test_task_cancellation_closes_blocked_upstream(stream_api, monkeypatch):
    api, state = stream_api
    blocked = asyncio.Event()
    closed = []

    async def upstream(**kwargs):
        try:
            yield "first"
            blocked.set()
            await asyncio.Event().wait()
        finally:
            closed.append(1)

    async def consume():
        return [x async for x in api.stream_request(BODY)]

    monkeypatch.setattr(api, "stream_post_async", upstream)
    task = asyncio.create_task(consume())
    await asyncio.wait_for(blocked.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [1] and state["success"] == []
    assert [(x["status_code"], x["success"]) for x in state["usage"]] == [(499, False)]


@pytest.mark.asyncio
async def test_asgi_cancel_scope_cannot_interrupt_cleanup_or_failure_accounting(stream_api, monkeypatch):
    import anyio
    from contextlib import asynccontextmanager
    from src import httpx_client
    api, state = stream_api
    closed = []

    async def record_usage(entry):
        await anyio.sleep(0)
        state["usage"].append(entry)

    class FakeResponse:
        status_code = 200

        async def aiter_lines(self):
            yield "first"
            scope.cancel()
            await anyio.sleep(0)

    class FakeClient:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            try:
                yield FakeResponse()
            finally:
                await anyio.sleep(0)
                closed.append(1)

    @asynccontextmanager
    async def client(**kwargs):
        yield FakeClient()

    monkeypatch.setattr(api, "record_usage_log", record_usage)
    monkeypatch.setattr(httpx_client, "get_streaming_client", client)
    with anyio.CancelScope() as scope:
        _ = [chunk async for chunk in api.stream_request(BODY)]
    assert closed == [1]
    assert [(x["status_code"], x["success"]) for x in state["usage"]] == [(499, False)]


@pytest.mark.asyncio
async def test_prefetch_error_releases_nested_generator():
    from src.router.stream_passthrough import build_streaming_response_or_error
    closed = []

    async def source():
        try:
            yield Response(status_code=503)
        finally:
            closed.append(1)

    response = await build_streaming_response_or_error(source())
    assert response.status_code == 503 and closed == [1]


@pytest.mark.asyncio
async def test_prepend_consumer_close_releases_prefetched_source():
    from src.router.stream_passthrough import prepend_async_item
    closed = []

    async def source():
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(1)

    iterator = source()
    wrapper = prepend_async_item(await anext(iterator), iterator)
    assert await anext(wrapper) == "first"
    await wrapper.aclose()
    assert closed == [1]


def _data(parts, finish=None):
    candidate = {"content": {"role": "model", "parts": parts}}
    if finish is not None:
        candidate["finishReason"] = finish
    return "data: " + json.dumps({"candidates": [candidate]}) + "\n\n"


@pytest.mark.asyncio
async def test_responses_delivers_one_tool_then_failed_on_transport_error(stream_api, monkeypatch):
    from src.router.antigravity import responses as route
    api, state = stream_api
    calls, closed = [], []

    async def upstream(**kwargs):
        calls.append(1)
        try:
            yield _data([{"functionCall": {"name": "run", "args": {"value": "safe"}, "id": "synthetic-call"}}])
            raise ConnectionError("synthetic-private-transport-detail")
        finally:
            closed.append(1)

    monkeypatch.setattr(api, "stream_post_async", upstream)
    monkeypatch.setattr(route, "stream_request", api.stream_request)
    body = {"model": "gemini-test", "input": "synthetic request", "stream": True,
            "tools": [{"type": "function", "name": "run", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}]}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": []}, receive)
    response = await route.create_response(request, _token="synthetic")
    chunks = [x async for x in response.body_iterator]
    text = "".join(x.decode() if isinstance(x, bytes) else x for x in chunks)
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    completed_calls = [e for e in events if e["type"] == "response.output_item.done" and e["item"]["type"] == "function_call"]
    assert len(completed_calls) == 1
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["status"] == "failed"
    assert calls == closed == [1]
    assert "synthetic-private-transport-detail" not in text


@pytest.mark.asyncio
async def test_anti_truncation_transport_failure_is_not_continued_or_completed():
    from src.converter.anti_truncation import AntiTruncationStreamProcessor
    calls, closed = [], []

    async def source():
        try:
            yield _data([{"text": "already delivered"}])
            raise ConnectionError("synthetic-private-error")
        finally:
            closed.append(1)

    async def request(payload):
        calls.append(1)
        return StreamingResponse(source())

    processor = AntiTruncationStreamProcessor(request, BODY, max_attempts=3)
    chunks = [x async for x in processor.process_stream()]
    text = "".join(x.decode() if isinstance(x, bytes) else x for x in chunks)
    assert calls == closed == [1]
    assert text.count("already delivered") == 1
    assert '"code": 502' in text and '"finishReason":"STOP"' not in text
    assert "synthetic-private-error" not in text


@pytest.mark.asyncio
async def test_anti_truncation_never_continues_a_delivered_real_tool():
    from src.converter.anti_truncation import AntiTruncationStreamProcessor
    calls, closed = [], []

    async def source():
        try:
            yield _data([{"functionCall": {"name": "run", "args": {}, "id": "synthetic-call"}}], "OTHER")
        finally:
            closed.append(1)

    async def request(payload):
        calls.append(1)
        return StreamingResponse(source())

    processor = AntiTruncationStreamProcessor(request, BODY, max_attempts=3)
    chunks = [x async for x in processor.process_stream()]
    text = "".join(x.decode() if isinstance(x, bytes) else x for x in chunks)
    assert calls == closed == [1]
    assert text.count("synthetic-call") == 1


@pytest.mark.asyncio
async def test_anti_truncation_consumer_close_releases_upstream():
    from src.converter.anti_truncation import AntiTruncationStreamProcessor
    closed = []

    async def source():
        try:
            yield _data([{"text": "first"}])
            yield _data([{"text": "second"}])
        finally:
            closed.append(1)

    async def request(payload):
        return StreamingResponse(source())

    processor = AntiTruncationStreamProcessor(request, BODY, max_attempts=3)
    gen = processor.process_stream()
    await anext(gen)
    await gen.aclose()
    assert closed == [1]
