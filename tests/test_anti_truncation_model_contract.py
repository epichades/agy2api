"""Anti-truncation must respect model capabilities without disabling thinking."""

import copy
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from src.converter.anti_truncation import apply_anti_truncation


CLAUDE = "claude-opus-5-5-low"
GEMINI = "gemini-3-flash"


def _payload(model=CLAUDE):
    return {"model": model, "request": {
        "contents": [{"role": "user", "parts": [{"text": "synthetic request"}]}],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.3,
                             "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 1024}},
        "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["real_tool"],
                                                 "streamFunctionCallArguments": True},
                       "retrievalConfig": {"latLng": {"latitude": 0, "longitude": 0}}},
        "tools": [{"functionDeclarations": [{"name": "real_tool", "parameters": {"type": "object"}}]}],
    }}


@pytest.mark.parametrize("model", [CLAUDE, "claude-sonnet-5-5-high", "claude-opus-4-6-thinking"])
def test_claude_helper_uses_auto_without_mutating_model_thinking_or_caller_payload(model):
    original = _payload(model)
    before = copy.deepcopy(original)
    result = apply_anti_truncation(original)
    assert original == before
    assert result["model"] == model
    assert result["request"]["generationConfig"] == before["request"]["generationConfig"]
    assert result["request"]["tools"][:-1] == before["request"]["tools"]
    config = result["request"]["toolConfig"]
    assert config["functionCallingConfig"] == {"mode": "AUTO", "streamFunctionCallArguments": True}
    assert config["retrievalConfig"] == before["request"]["toolConfig"]["retrievalConfig"]
    assert result["request"]["tools"][-1]["functionDeclarations"][0]["name"] == "emit_answer"
    assert apply_anti_truncation(result) == result


@pytest.mark.parametrize("builtin", [False, True])
def test_gemini_helper_preserves_any_policy_unless_builtin_tools_need_auto(builtin):
    original = _payload(GEMINI)
    if builtin:
        original["request"]["tools"].append({"googleSearch": {}})
    before = copy.deepcopy(original)
    result = apply_anti_truncation(original)
    config = result["request"]["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == ("AUTO" if builtin else "ANY")
    if builtin:
        assert "allowedFunctionNames" not in config
        assert {"googleSearch": {}} in result["request"]["tools"]
    else:
        assert config["allowedFunctionNames"] == ["real_tool", "emit_answer"]
    assert original == before
    assert result["model"] == GEMINI
    assert result["request"]["generationConfig"] == before["request"]["generationConfig"]


def _wire(parts, finish="STOP", tokens=5):
    values = [
        {"candidates": [{"content": {"parts": parts}, "finishReason": finish}]},
        {"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": tokens - 2,
                           "totalTokenCount": tokens}},
    ]
    return ("".join("data: " + json.dumps(value, ensure_ascii=False) + "\n\n" for value in values)
            + "data: [DONE]\n\n").encode()


@pytest.fixture
async def gateway(monkeypatch):
    from src import httpx_client
    from src.api import antigravity as api
    from src.converter import antigravity_fix
    from src.router.antigravity import openai as route
    from src.router.antigravity import responses as responses_route

    state = SimpleNamespace(requests=[], attempts=[_wire([{"text": "SYNTHETIC_OK"}])])

    def upstream(request):
        state.requests.append(json.loads(request.content))
        assert len(state.requests) <= len(state.attempts), "unexpected duplicate upstream request"
        content = state.attempts[len(state.requests) - 1]
        response_args = {"stream": content} if isinstance(content, httpx.AsyncByteStream) else {"content": content}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, **response_args)

    @asynccontextmanager
    async def streaming_client(**kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            yield client

    monkeypatch.setattr(httpx_client, "get_streaming_client", streaming_client)
    monkeypatch.setattr(api, "get_valid_credential", AsyncMock(return_value=(
        "synthetic@example.invalid", {"access_token": "synthetic-token", "project_id": "synthetic-project"})))
    monkeypatch.setattr(api, "get_retry_config", AsyncMock(return_value={
        "retry_enabled": False, "max_retries": 0, "retry_interval": 0}))
    monkeypatch.setattr(api, "get_antigravity_api_url", AsyncMock(return_value="https://synthetic.invalid"))
    monkeypatch.setattr(api, "get_auto_ban_error_codes", AsyncMock(return_value=[]))
    for name in ("remember_validated_credential", "record_api_call_success", "record_api_call_error",
                 "record_usage_log"):
        monkeypatch.setattr(api, name, AsyncMock())
    monkeypatch.setattr(api, "_record_request_body_detail", AsyncMock(return_value=""))
    monkeypatch.setattr(antigravity_fix, "get_return_thoughts_to_frontend", AsyncMock(return_value=True))
    monkeypatch.setattr(route, "get_anti_truncation_max_attempts", AsyncMock(return_value=2))
    monkeypatch.setattr(responses_route, "get_anti_truncation_max_attempts", AsyncMock(return_value=2))
    # Keep the real converter, normalizer, shared request wrapper and HTTPX line
    # transport. Only account storage and the external HTTP peer are synthetic.
    app = FastAPI()
    app.include_router(route.router)
    app.include_router(responses_route.router)
    app.dependency_overrides[route.authenticate_bearer] = lambda: "synthetic-auth"
    app.dependency_overrides[responses_route.authenticate_bearer] = lambda: "synthetic-auth"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic.invalid") as client:
        yield client, state


async def _call(client, model=CLAUDE, **extra):
    return await client.post("/v1/chat/completions", json={
        "model": "抗截断-" + model, "messages": [{"role": "user", "content": "synthetic request"}],
        "max_tokens": 1024, "temperature": 0.3, "stream": True,
        "stream_options": {"include_usage": True}, **extra,
    })


def _events(response):
    assert response.status_code == 200, response.text
    assert response.text.count("data: [DONE]") == 1 and response.text.endswith("data: [DONE]\n\n")
    chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    assert all("error" not in chunk for chunk in chunks)
    assert chunks[-1]["choices"] == []
    assert all(chunk["usage"] is None for chunk in chunks[:-1])
    return chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("model,mode", [(CLAUDE, "AUTO"), (GEMINI, "ANY")])
async def test_route_normalization_and_real_producer_keep_model_policy(gateway, model, mode):
    client, state = gateway
    chunks = _events(await _call(client, model))
    assert len(state.requests) == 1
    sent = state.requests[0]
    assert sent["model"] == model
    config = sent["request"]["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == mode
    if model == CLAUDE:
        assert "allowedFunctionNames" not in config
        assert sent["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
    else:
        assert "emit_answer" in config["allowedFunctionNames"]
    assert sent["request"]["generationConfig"]["maxOutputTokens"] == 1024
    assert sent["request"]["generationConfig"]["temperature"] == 0.3
    assert "".join(choice.get("delta", {}).get("content", "")
                   for chunk in chunks for choice in chunk["choices"]) == "SYNTHETIC_OK"
    assert chunks[-1]["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_claude_auto_emit_answer_still_becomes_text_once(gateway):
    client, state = gateway
    state.attempts = [_wire([{"functionCall": {"name": "emit_answer", "args": {"content": "synthetic final"}}}])]
    chunks = _events(await _call(client))
    assert len(state.requests) == 1
    choices = [choice for chunk in chunks for choice in chunk["choices"]]
    assert "".join(choice.get("delta", {}).get("content", "") for choice in choices) == "synthetic final"
    assert not any(choice.get("delta", {}).get("tool_calls") for choice in choices)
    assert chunks[-1]["usage"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_claude_auto_continuation_keeps_model_thinking_and_policy(gateway):
    client, state = gateway
    state.attempts = [_wire([{"text": "first "}], "MAX_TOKENS", 5), _wire([{"text": "second"}], tokens=7)]
    chunks = _events(await _call(client))
    assert len(state.requests) == 2
    for sent in state.requests:
        assert sent["model"] == CLAUDE
        assert sent["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
        assert sent["request"]["toolConfig"]["functionCallingConfig"] == {"mode": "AUTO"}
    assert state.requests[0]["request"]["generationConfig"] == state.requests[1]["request"]["generationConfig"]
    assert len(state.requests[1]["request"]["contents"]) > len(state.requests[0]["request"]["contents"])
    assert "".join(choice.get("delta", {}).get("content", "")
                   for chunk in chunks for choice in chunk["choices"]) == "first second"
    assert chunks[-1]["usage"]["total_tokens"] == 12


@pytest.mark.asyncio
async def test_claude_auto_real_tool_is_delivered_once_without_continuation(gateway):
    client, state = gateway
    state.attempts = [_wire([{"functionCall": {
        "name": "real_tool", "id": "synthetic-call", "args": {"value": "true"},
    }}], "OTHER")]
    chunks = _events(await _call(client, tools=[{"type": "function", "function": {
        "name": "real_tool", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }}]))
    assert len(state.requests) == 1
    calls = [call for chunk in chunks for choice in chunk["choices"]
             for call in choice.get("delta", {}).get("tool_calls", [])]
    assert len(calls) == 1 and calls[0]["function"]["name"] == "real_tool"
    assert json.loads(calls[0]["function"]["arguments"]) == {"value": "true"}
    assert chunks[-1]["usage"]["total_tokens"] == 5


def _data(value):
    return ("data: " + json.dumps(value) + "\n\n").encode()


def _tool_frame(finish=None):
    candidate = {"content": {"parts": [{"functionCall": {
        "name": "real_tool", "id": "synthetic-call", "args": {"value": "true"},
    }}]}, "index": 0}
    if finish is not None:
        candidate["finishReason"] = finish
    return _data({"candidates": [candidate]})


async def _call_real_tool(client, protocol):
    function = {"name": "real_tool", "parameters": {
        "type": "object", "properties": {"value": {"type": "string"}},
    }}
    if protocol == "chat":
        return await _call(client, tools=[{"type": "function", "function": function}], tool_choice="required")
    return await client.post("/v1/responses", json={
        "model": "抗截断-" + CLAUDE, "input": "synthetic request", "stream": True,
        "tools": [{"type": "function", **function}], "tool_choice": "required", "max_output_tokens": 1024,
    })


def _real_tool_events(response, protocol):
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    if protocol == "chat":
        calls = [call for event in events for choice in event.get("choices", [])
                 for call in choice.get("delta", {}).get("tool_calls", [])]
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "real_tool"
        assert json.loads(calls[0]["function"]["arguments"]) == {"value": "true"}
    else:
        calls = [event["item"] for event in events if event["type"] == "response.output_item.added"
                 and event["item"]["type"] == "function_call"]
        assert len(calls) == 1 and calls[0]["name"] == "real_tool"
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("separate_finish", [False, True])
async def test_real_tool_finish_frame_and_late_usage_survive_once(gateway, protocol, separate_finish):
    client, state = gateway
    wire = _tool_frame(None if separate_finish else "STOP")
    if separate_finish:
        wire += _data({"candidates": [{"index": 0, "finishReason": "STOP"}]})
    wire += _data({"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}})
    state.attempts = [wire + b"data: [DONE]\n\n"]
    response = await _call_real_tool(client, protocol)
    events = _real_tool_events(response, protocol)
    assert len(state.requests) == 1
    if protocol == "chat":
        finishes = [choice["finish_reason"] for event in events for choice in event.get("choices", [])
                    if choice.get("finish_reason")]
        assert finishes == ["tool_calls"]
        assert all("error" not in event for event in events)
        assert events[-1]["choices"] == [] and events[-1]["usage"]["total_tokens"] == 5
        assert response.text.count("data: [DONE]") == 1
    else:
        assert [event["type"] for event in events if event["type"] in ("response.completed", "response.failed")] == ["response.completed"]
        assert events[-1]["response"]["status"] == "completed"
        assert events[-1]["response"]["usage"]["total_tokens"] == 5


class _ToolThenTransportFailure(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield _tool_frame()
        raise httpx.ReadError("synthetic transport failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("failure", ["missing_finish", "malformed_json", "transport"])
async def test_real_tool_failure_never_invents_finish_or_replays(gateway, protocol, failure):
    client, state = gateway
    if failure == "transport":
        wire = _ToolThenTransportFailure()
    else:
        wire = _tool_frame()
        if failure == "malformed_json":
            wire += b"data: {broken}\n\n"
            wire += _data({"candidates": [{"index": 0, "finishReason": "STOP"}]})
        wire += b"data: [DONE]\n\n"
    state.attempts = [wire]
    response = await _call_real_tool(client, protocol)
    events = _real_tool_events(response, protocol)
    assert len(state.requests) == 1
    if protocol == "chat":
        assert any(event.get("error", {}).get("code") == "upstream_stream_error" for event in events)
        assert not any(choice.get("finish_reason") for event in events for choice in event.get("choices", []))
        assert response.text.count("data: [DONE]") == 1
    else:
        assert events[-1]["type"] == "response.failed"
        assert events[-1]["response"]["status"] == "failed"
        assert not any(event["type"] == "response.completed" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("has_finish", [False, True])
async def test_real_tool_does_not_bypass_later_synthetic_tool_conversion(gateway, protocol, has_finish):
    client, state = gateway
    wire = _tool_frame() + _data({"candidates": [{"content": {"parts": [{"functionCall": {
        "name": "emit_answer", "args": {"content": "synthetic final"},
    }}]}}]})
    if has_finish:
        wire += _data({"candidates": [{"index": 0, "finishReason": "STOP"}]})
    wire += _data({"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}})
    state.attempts = [wire + b"data: [DONE]\n\n"]
    response = await _call_real_tool(client, protocol)
    events = _real_tool_events(response, protocol)
    assert len(state.requests) == 1
    assert '"emit_answer"' not in response.text
    if protocol == "chat":
        assert any("error" in event for event in events) is not has_finish
        text = "".join(choice.get("delta", {}).get("content", "")
                       for event in events for choice in event.get("choices", []))
        assert text == "synthetic final"
        finishes = [choice["finish_reason"] for event in events for choice in event.get("choices", [])
                    if choice.get("finish_reason")]
        assert finishes == (["tool_calls"] if has_finish else [])
    else:
        assert events[-1]["type"] == ("response.completed" if has_finish else "response.failed")
        messages = [item for item in events[-1]["response"]["output"] if item["type"] == "message"]
        assert messages[0]["content"][0]["text"] == "synthetic final"
