"""SDK request compatibility and complete Chat SSE contracts, without live accounts."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse


MODEL = "gemini-3-flash"
PRIVATE_VALUE = "synthetic-private-metadata-value"


def payload(**extra):
    return {"model": MODEL, "messages": [{"role": "user", "content": "Synthetic request"}], **extra}


def event(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]


@pytest.fixture
async def gateway(monkeypatch):
    from src.router.antigravity import openai as route

    state = SimpleNamespace(calls=[], chunks=None, closed=False)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.authenticate_bearer] = lambda: "synthetic-auth"

    async def identity(body):
        return body

    async def non_stream(**kwargs):
        state.calls.append(kwargs["body"])
        return Response(content=json.dumps({
            "candidates": [{"content": {"parts": [{"text": "SYNTHETIC_OK"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
        }), media_type="application/json")

    async def stream(**kwargs):
        state.calls.append(kwargs["body"])
        try:
            chunks = state.chunks if state.chunks is not None else [
                event({"response": {"candidates": [{"content": {"parts": [{"text": "SYNTHETIC_OK"}]}}]}}),
                event({"candidates": [{"finishReason": "STOP"}]}),
                event({"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}}),
                b"data: [DONE]\n\n",
            ]
            if hasattr(state, "attempts"):
                chunks = state.attempts[len(state.calls) - 1]
            for chunk in chunks:
                yield chunk
        finally:
            state.closed = True

    monkeypatch.setattr(route, "normalize_antigravity_request", identity)
    monkeypatch.setattr(route, "non_stream_request", non_stream)
    monkeypatch.setattr(route, "stream_request", stream)
    monkeypatch.setattr(route, "get_anti_truncation_max_attempts", AsyncMock(return_value=1))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic.invalid") as client:
        yield client, state


@pytest.mark.parametrize("extra", [
    {"stream_options": {"include_usage": True}},
    {"parallel_tool_calls": False},
    {"reasoning_effort": "high"},
    {"metadata": {"private": PRIVATE_VALUE}},
    {"metadata": None, "stream_options": None, "parallel_tool_calls": None, "reasoning_effort": None},
    {"store": False, "service_tier": "auto", "logprobs": True, "top_logprobs": 3},
    {"prompt_cache_key": PRIVATE_VALUE, "prompt_cache_retention": "24h", "safety_identifier": PRIVATE_VALUE},
    {"verbosity": "low", "logit_bias": {"123": 1.0}},
    {"sdk_extension": {"private": PRIVATE_VALUE}},
    {"stream": None},
])
@pytest.mark.asyncio
async def test_sdk_fields_are_accepted_without_forwarding_values(gateway, extra):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(**extra))
    assert response.status_code == 200, response.text
    assert len(state.calls) == 1
    assert state.calls[0]["model"] == MODEL
    assert PRIVATE_VALUE not in json.dumps(state.calls)
    assert PRIVATE_VALUE not in response.text
    assert not any(key in state.calls[0]["request"]["generationConfig"] for key in extra)


@pytest.mark.asyncio
async def test_ignored_fields_are_reported_and_generation_options_survive(gateway):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(
        temperature=0.3, max_completion_tokens=21, top_k=7,
        parallel_tool_calls=False, reasoning_effort="low", metadata={"private": PRIVATE_VALUE},
        sdk_extension={"private": PRIVATE_VALUE}, _preserve_tool_arguments=False,
    ))
    assert response.status_code == 200
    assert state.calls[0]["model"] == MODEL
    gc = state.calls[0]["request"]["generationConfig"]
    assert gc == {"temperature": 0.3, "maxOutputTokens": 21, "topK": 7}
    notes = response.json()["x_agy_ignored_params"]
    assert {"parallel_tool_calls(not_enforced)", "reasoning_effort", "metadata(not_stored)", "sdk_extension", "unknown_parameter"} <= set(notes)
    assert set(response.headers["x-agy-ignored-params"].split(",")) == set(notes)
    assert "_preserve_tool_arguments" not in json.dumps(state.calls)


@pytest.mark.parametrize("extra", [
    {"messages": "not-a-message-array"}, {"messages": [{"role": "bogus", "content": "test"}]},
    {"model": None}, {"stream": "true"}, {"temperature": "invalid"},
    {"stream_options": {"include_usage": "yes"}}, {"parallel_tool_calls": "no"},
    {"stream_options": []}, {"metadata": "invalid"},
])
@pytest.mark.asyncio
async def test_invalid_core_or_declared_field_types_still_fail(gateway, extra):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(**extra))
    assert response.status_code == 422
    assert not state.calls


@pytest.mark.parametrize("options", [None, {"include_usage": False}, {"include_usage": True}])
@pytest.mark.asyncio
async def test_stream_usage_is_opt_in_and_has_a_separate_final_chunk(gateway, options):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(stream=True, stream_options=options))
    assert response.status_code == 200
    chunks = events(response)
    assert state.closed
    assert response.text.endswith("data: [DONE]\n\n")
    if options and options["include_usage"]:
        assert all(chunk["usage"] is None for chunk in chunks[:-1])
        assert chunks[-1]["choices"] == []
        assert chunks[-1]["usage"]["total_tokens"] == 5
        assert len({chunk["id"] for chunk in chunks}) == 1
        assert all(chunk["model"] == MODEL for chunk in chunks)
    else:
        assert all("usage" not in chunk for chunk in chunks)
        assert all(chunk["choices"] for chunk in chunks)


@pytest.mark.asyncio
async def test_stream_extensions_report_only_names(gateway):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(
        stream=True, stream_options={"include_usage": True, "future_option": PRIVATE_VALUE},
        metadata={"private": PRIVATE_VALUE}, reasoning_effort="high", parallel_tool_calls=False,
        sdk_extension=PRIVATE_VALUE,
    ))
    assert response.status_code == 200
    assert "stream_options.future_option" in response.headers["x-agy-ignored-params"]
    assert "metadata(not_stored)" in events(response)[0]["x_agy_ignored_params"]
    assert PRIVATE_VALUE not in response.text + json.dumps(state.calls)


@pytest.mark.asyncio
async def test_sse_split_utf8_and_multiple_events_preserve_text(gateway):
    client, state = gateway
    raw = b": keepalive\r\n\r\n" + event({"candidates": [{"content": {"parts": [{"text": "中文 …  "}]}}]})
    raw += event({"candidates": [{"finishReason": "STOP"}], "usageMetadata": {"totalTokenCount": 5}})
    state.chunks = [raw[:17], *[raw[index:index+1] for index in range(17, len(raw))]]
    response = await client.post("/v1/chat/completions", json=payload(stream=True, stream_options={"include_usage": True}))
    assert response.status_code == 200
    text = "".join(choice["delta"].get("content", "") for chunk in events(response) for choice in chunk["choices"])
    assert text == "中文 …  "
    assert events(response)[-1]["choices"] == []
    assert state.closed


@pytest.mark.parametrize("anti_truncation", [False, True])
@pytest.mark.parametrize("include_usage", [False, True])
@pytest.mark.parametrize("control_fields", [False, True])
@pytest.mark.asyncio
async def test_chat_uses_real_httpx_line_transport(gateway, monkeypatch, anti_truncation, include_usage, control_fields):
    """Exercise the producer, not a mock returning already-framed SSE bytes."""
    from src import httpx_client
    from src.router.antigravity import openai as route

    client, state = gateway
    content = {"parts": [{"text": "中文 …  "}]}
    if anti_truncation:
        content = {"parts": [{"functionCall": {"name": "emit_answer", "args": {"content": "中文 …  "}}}]}
    wire = b": heartbeat\r\n\r\nid: synthetic\r\nevent: message\r\nretry: 1000\r\n" if control_fields else b""
    wire += event({"response": {"candidates": [{"content": content}]}})
    wire += event({"candidates": [{"finishReason": "STOP"}]})
    wire += event({"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}})
    wire += b"data: [DONE]\r\n\r\n"
    observed = []

    @asynccontextmanager
    async def transport_client(**kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})
        )) as upstream:
            yield upstream

    async def stream(**kwargs):
        state.calls.append(kwargs["body"])
        source = httpx_client.stream_post_async(
            "https://synthetic.invalid/stream", kwargs["body"], native=kwargs["native"])
        try:
            async for chunk in source:
                observed.append(chunk)
                yield chunk
        finally:
            await source.aclose()
            state.closed = True

    monkeypatch.setattr(httpx_client, "get_streaming_client", transport_client)
    monkeypatch.setattr(route, "stream_request", stream)
    response = await client.post("/v1/chat/completions", json=payload(
        model=("抗截断-" if anti_truncation else "") + MODEL, stream=True,
        stream_options={"include_usage": include_usage}, metadata={"private": PRIVATE_VALUE},
        parallel_tool_calls=False, reasoning_effort="low", sdk_extension=PRIVATE_VALUE,
    ))
    assert response.status_code == 200, response.text
    chunks = events(response)
    assert not any("error" in chunk for chunk in chunks)
    assert "".join(choice["delta"].get("content", "") for chunk in chunks for choice in chunk["choices"]) == "中文 …  "
    assert any(choice["finish_reason"] for chunk in chunks for choice in chunk["choices"])
    assert response.text.endswith("data: [DONE]\n\n")
    assert all(isinstance(chunk, str) and "\n" not in chunk for chunk in observed)
    assert "" in observed and "data: [DONE]" in observed
    assert (": heartbeat" in observed) is control_fields
    assert len(state.calls) == 1 and state.closed
    assert PRIVATE_VALUE not in response.text + json.dumps(state.calls)
    if include_usage:
        assert all(chunk["usage"] is None for chunk in chunks[:-1])
        assert chunks[-1]["choices"] == []
        assert chunks[-1]["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    else:
        assert all("usage" not in chunk for chunk in chunks)


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.asyncio
async def test_malformed_json_in_line_transport_is_not_silently_skipped(gateway, partial):
    client, state = gateway
    state.chunks = []
    if partial:
        state.chunks.extend(event({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}).decode().splitlines())
    state.chunks.extend([": heartbeat", "", "data: malformed", "", "data: [DONE]", ""])
    response = await client.post("/v1/chat/completions", json=payload(stream=True, stream_options={"include_usage": True}))
    assert response.status_code == (200 if partial else 502)
    chunks = events(response) if partial else [response.json()]
    assert chunks[-1]["error"]["code"] == "upstream_stream_error"
    assert not any(chunk.get("usage") for chunk in chunks)
    assert state.closed


@pytest.mark.parametrize("failure", [b"data: malformed\n\n", b"data: [DONE]\n\n", Response(status_code=503, content='{"error":"synthetic"}')])
@pytest.mark.asyncio
async def test_failed_stream_does_not_emit_successful_usage(gateway, failure):
    client, state = gateway
    state.chunks = [event({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}), failure,
                    event({"candidates": [{"finishReason": "STOP"}], "usageMetadata": {"totalTokenCount": 5}})]
    response = await client.post("/v1/chat/completions", json=payload(stream=True, stream_options={"include_usage": True}))
    chunks = events(response)
    assert response.status_code == 200
    assert any("error" in chunk for chunk in chunks)
    assert not any(chunk.get("usage") for chunk in chunks)
    assert not any(choice.get("finish_reason") for chunk in chunks for choice in chunk.get("choices", []))
    assert state.closed


@pytest.mark.asyncio
async def test_predata_upstream_error_keeps_http_status(gateway):
    client, state = gateway
    state.chunks = [Response(status_code=429, content='{"error":"synthetic"}', headers={"retry-after":"7"})]
    response = await client.post("/v1/chat/completions", json=payload(stream=True))
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.headers["content-type"] == "application/json"
    assert state.closed


@pytest.mark.parametrize("anti_truncation", [False, True])
@pytest.mark.parametrize("response_type", [Response, JSONResponse])
@pytest.mark.asyncio
async def test_predata_json_error_does_not_advertise_an_sse_stream(gateway, anti_truncation, response_type):
    client, state = gateway
    error = {"error": {"code": 400, "message": "synthetic rejection", "status": "INVALID_ARGUMENT"}}
    content = json.dumps(error) if response_type is Response else error
    state.chunks = [response_type(status_code=400, content=content, headers={
        "content-type": "text/event-stream; charset=utf-8", "content-length": "9999",
        "content-encoding": "gzip", "transfer-encoding": "chunked", "connection": "keep-alive",
        "retry-after": "7", "set-cookie": "synthetic-private-cookie",
    })]
    response = await client.post("/v1/chat/completions", json=payload(
        model=("抗截断-" if anti_truncation else "") + MODEL, stream=True,
        metadata={"private": PRIVATE_VALUE},
    ))
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert response.headers["retry-after"] == "7"
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["x-agy-ignored-params"] == "metadata(not_stored)"
    assert not any(name in response.headers for name in (
        "content-encoding", "transfer-encoding", "connection", "set-cookie"))
    assert response.json() == error
    assert "data:" not in response.text and "[DONE]" not in response.text
    assert len(state.calls) == 1 and state.closed


@pytest.mark.parametrize("anti_truncation", [False, True])
@pytest.mark.asyncio
async def test_predata_non_json_error_is_sanitized_json(gateway, anti_truncation):
    client, state = gateway
    state.chunks = [Response(status_code=503, content=f"<html>{PRIVATE_VALUE}</html>", headers={
        "content-type": "text/event-stream", "retry-after": "9",
    })]
    response = await client.post("/v1/chat/completions", json=payload(
        model=("抗截断-" if anti_truncation else "") + MODEL, stream=True))
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert response.headers["retry-after"] == "9"
    assert response.json()["error"]["code"] == "upstream_stream_error"
    assert PRIVATE_VALUE not in response.text
    assert state.closed


@pytest.mark.asyncio
async def test_parallel_false_does_not_discard_tools_or_change_string_arguments(gateway):
    client, state = gateway
    state.chunks = [event({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "first", "args": {"value": "false"}, "id": "synthetic-a"}},
        {"functionCall": {"name": "second", "args": {"value": "null"}, "id": "synthetic-b"}},
    ]}}]}), event({"candidates": [{"finishReason": "STOP"}]})]
    response = await client.post("/v1/chat/completions", json=payload(stream=True, parallel_tool_calls=False))
    calls = [call for chunk in events(response) for choice in chunk.get("choices", []) for call in choice.get("delta", {}).get("tool_calls", [])]
    assert [call["index"] for call in calls] == [0, 1]
    assert [json.loads(call["function"]["arguments"])["value"] for call in calls] == ["false", "null"]
    assert "parallel_tool_calls(not_enforced)" in response.headers["x-agy-ignored-params"]


@pytest.mark.asyncio
async def test_tool_strict_extensions_are_reported_without_leaking_values(gateway):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(tools=[{
        "type":"function", "function":{"name":"synthetic_tool", "parameters":{"type":"object"}, "strict":True, "vendor_option":PRIVATE_VALUE}}]))
    assert response.status_code == 200
    assert "tools.function.strict(not_enforced)" in response.headers["x-agy-ignored-params"]
    assert "tools.function.vendor_option" in response.headers["x-agy-ignored-params"]
    assert PRIVATE_VALUE not in json.dumps(state.calls)


@pytest.mark.asyncio
async def test_unknown_parameter_names_cannot_inject_headers(gateway):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(**{"bad\r\nX-Injected: yes": PRIVATE_VALUE}))
    assert response.status_code == 200
    assert response.headers["x-agy-ignored-params"] == "unknown_parameter"
    assert "x-injected" not in response.headers
    assert PRIVATE_VALUE not in response.text


@pytest.mark.asyncio
async def test_stream_close_releases_upstream_and_tool_index():
    from src.router.antigravity.openai import _chat_stream
    from src.converter.openai2gemini import _STREAM_TOOL_INDEX
    closed = []

    async def source():
        try:
            yield event({"candidates": [{"content": {"parts": [{"functionCall":{"name":"synthetic","args":{},"id":"synthetic-call"}}]}}]})
            yield event({"candidates": [{"finishReason": "STOP"}]})
        finally:
            closed.append(True)

    stream = _chat_stream(source(), MODEL, False, [])
    first = json.loads((await anext(stream)).decode()[6:])
    assert first["id"] in _STREAM_TOOL_INDEX
    await stream.aclose()
    assert closed == [True]
    assert first["id"] not in _STREAM_TOOL_INDEX


@pytest.mark.asyncio
async def test_anti_truncation_uses_same_usage_contract(gateway):
    client, state = gateway
    state.chunks = [event({"candidates": [{"content": {"parts": [{"functionCall": {"name": "emit_answer", "args": {"content": "synthetic answer"}}}]}, "finishReason": "STOP"}],
                           "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}}), b"data: [DONE]\n\n"]
    response = await client.post("/v1/chat/completions", json=payload(model="抗截断-" + MODEL, stream=True, stream_options={"include_usage": True}))
    chunks = events(response)
    assert response.status_code == 200
    assert not any("error" in chunk for chunk in chunks), chunks
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["total_tokens"] == 5
    assert all(chunk["usage"] is None for chunk in chunks[:-1])
    assert "emit_answer" not in response.text
    assert "synthetic answer" in response.text
    assert state.closed


@pytest.mark.asyncio
async def test_missing_usage_is_reported_instead_of_fabricated(gateway):
    client, state = gateway
    state.chunks = [event({"candidates": [{"content": {"parts": [{"text": "Synthetic"}]}, "finishReason": "STOP"}]})]
    response = await client.post("/v1/chat/completions", json=payload(stream=True, stream_options={"include_usage": True}))
    final = events(response)[-1]
    assert final["choices"] == [] and final["usage"] is None
    assert "stream_options.include_usage(upstream_usage_unavailable)" in final["x_agy_ignored_params"]


@pytest.mark.asyncio
async def test_truncated_utf8_before_first_event_is_http_502(gateway):
    client, state = gateway
    state.chunks = [b'data: {"candidates": ["\xe4']
    response = await client.post("/v1/chat/completions", json=payload(stream=True))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_stream_error"
    assert state.closed


@pytest.mark.parametrize("extra", [{"functions": [{"name": "synthetic"}]}, {"function_call": "auto"}, {"audio": {"voice": "synthetic"}}, {"modalities": ["audio"]}])
@pytest.mark.asyncio
async def test_unimplemented_content_and_legacy_tools_are_explicit_errors(gateway, extra):
    client, state = gateway
    response = await client.post("/v1/chat/completions", json=payload(**extra))
    assert response.status_code == 400
    assert not state.calls


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.asyncio
async def test_anti_truncation_preserves_separate_usage_only_after_stop(gateway, wrapped):
    client, state = gateway
    usage = {"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}}
    if wrapped:
        usage = {"response": usage}
    state.chunks = [
        event({"candidates": [{"content": {"parts": [{"functionCall": {"name": "emit_answer", "args": {"content": "synthetic answer"}}}]}}]}),
        event({"candidates": [{"finishReason": "STOP"}]}),
        # A later normal-content chunk is still suppressed; a usage field does
        # not make its text an allowed second answer.
        event({"candidates": [{"content": {"parts": [{"text": "do not repeat this"}]}}], "usageMetadata": {"totalTokenCount": 999}}),
        event(usage),
        b"data: [DONE]\n\n",
    ]
    response = await client.post("/v1/chat/completions", json=payload(
        model="抗截断-" + MODEL, stream=True, stream_options={"include_usage": True}))
    chunks = events(response)
    assert response.status_code == 200
    assert len(state.calls) == 1
    assert not any("error" in chunk for chunk in chunks)
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["total_tokens"] == 5
    assert all(chunk["usage"] is None for chunk in chunks[:-1])
    assert response.text.count("synthetic answer") == 1
    assert "do not repeat this" not in response.text
    assert "emit_answer" not in response.text
    assert "upstream_usage_unavailable" not in response.text
    assert state.closed


@pytest.mark.asyncio
async def test_anti_truncation_usage_does_not_turn_late_failure_into_success(gateway):
    client, state = gateway
    state.chunks = [
        event({"candidates": [{"content": {"parts": [{"functionCall": {"name": "emit_answer", "args": {"content": "synthetic answer"}}}]}}]}),
        event({"usageMetadata": {"totalTokenCount": 5}}),
        Response(status_code=503, content='{"error":"synthetic failure"}'),
    ]
    response = await client.post("/v1/chat/completions", json=payload(
        model="抗截断-" + MODEL, stream=True, stream_options={"include_usage": True}))
    chunks = events(response)
    assert response.status_code == 200
    assert len(state.calls) == 1
    assert any("error" in chunk for chunk in chunks)
    assert not any(chunk.get("usage") for chunk in chunks)
    assert not any(choice.get("finish_reason") for chunk in chunks for choice in chunk.get("choices", []))
    assert state.closed


def synthetic_attempt(text, finish, usage=None, earlier_usage=None):
    chunks = []
    if earlier_usage is not None:
        chunks.append(event({"usageMetadata": earlier_usage}))
    chunks.append(event({"candidates": [{"content": {"parts": [{"functionCall": {
        "name": "emit_answer", "args": {"content": text}}}]}, "finishReason": finish}]}))
    if usage is not None:
        chunks.append(event({"usageMetadata": usage}))
    chunks.append(b"data: [DONE]\n\n")
    return chunks


@pytest.mark.asyncio
async def test_anti_truncation_uses_last_cumulative_usage_within_each_attempt(gateway):
    client, state = gateway
    state.attempts = [synthetic_attempt("one", "STOP",
        usage={"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
        earlier_usage={"promptTokenCount": 2, "candidatesTokenCount": 0, "totalTokenCount": 2})]
    response = await client.post("/v1/chat/completions", json=payload(
        model="抗截断-" + MODEL, stream=True, stream_options={"include_usage": True}))
    final = events(response)[-1]
    assert response.status_code == 200
    assert len(state.calls) == 1
    assert final["choices"] == []
    assert final["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}


@pytest.mark.asyncio
async def test_anti_truncation_sums_completed_attempt_totals_once(gateway, monkeypatch):
    from src.router.antigravity import openai as route
    monkeypatch.setattr(route, "get_anti_truncation_max_attempts", AsyncMock(return_value=2))
    client, state = gateway
    state.attempts = [
        synthetic_attempt("first ", "MAX_TOKENS", usage={"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
                          earlier_usage={"promptTokenCount": 2, "totalTokenCount": 2}),
        synthetic_attempt("second", "STOP", usage={"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7},
                          earlier_usage={"promptTokenCount": 3, "totalTokenCount": 3}),
    ]
    response = await client.post("/v1/chat/completions", json=payload(
        model="抗截断-" + MODEL, stream=True, stream_options={"include_usage": True}))
    chunks = events(response)
    assert response.status_code == 200
    assert len(state.calls) == 2
    assert not any("error" in chunk for chunk in chunks)
    assert chunks[-1]["usage"] == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    assert all(chunk["usage"] is None for chunk in chunks[:-1])
    assert response.text.count('"content": "first "') == 1
    assert response.text.count('"content": "second"') == 1


@pytest.mark.parametrize("missing", [0, 1])
@pytest.mark.asyncio
async def test_anti_truncation_missing_attempt_usage_is_explicitly_incomplete(gateway, monkeypatch, missing):
    from src.router.antigravity import openai as route
    monkeypatch.setattr(route, "get_anti_truncation_max_attempts", AsyncMock(return_value=2))
    client, state = gateway
    totals = [{"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
              {"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7}]
    totals[missing] = None
    state.attempts = [synthetic_attempt("first ", "MAX_TOKENS", totals[0]),
                      synthetic_attempt("second", "STOP", totals[1])]
    response = await client.post("/v1/chat/completions", json=payload(
        model="抗截断-" + MODEL, stream=True, stream_options={"include_usage": True}))
    chunks = events(response)
    assert response.status_code == 200
    assert len(state.calls) == 2
    assert not any("error" in chunk for chunk in chunks)
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"] is None
    assert "stream_options.include_usage(upstream_usage_incomplete)" in chunks[-1]["x_agy_ignored_params"]
    assert not any(chunk.get("usage") for chunk in chunks)
