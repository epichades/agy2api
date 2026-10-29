"""Client-visible disclosure when Claude anti-truncation relaxes tool choice."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Response
import httpx
import pytest


NOTE = "tool_choice(anti_truncation_auto)"
NON_STREAM_NOTE = "anti_truncation(non_stream)"
TOOL = "synthetic_private_tool_name"
CLAUDE = "claude-opus-5-5-low"
PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "native": "/v1/messages"}


def request_payload(protocol, choice="required", *, model=CLAUDE, anti=True, streaming=True):
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    data = {"model": ("抗截断-" if anti else "") + model, "stream": streaming}
    if protocol == "responses":
        data.update(input="Synthetic request", tools=[{"type": "function", "name": TOOL, "parameters": schema}])
        named = {"type": "function", "name": TOOL}
    elif protocol == "native":
        data.update(messages=[{"role": "user", "content": "Synthetic request"}], max_tokens=1024,
                    tools=[{"name": TOOL, "input_schema": schema}])
        named = {"type": "tool", "name": TOOL}
    else:
        data.update(messages=[{"role": "user", "content": "Synthetic request"}],
                    tools=[{"type": "function", "function": {"name": TOOL, "parameters": schema}}])
        named = {"type": "function", "function": {"name": TOOL}}
    if choice is not None:
        if choice == "named":
            data["tool_choice"] = named
        elif protocol == "native":
            data["tool_choice"] = {"type": "any" if choice == "required" else choice}
        else:
            data["tool_choice"] = choice
    return data


@pytest.fixture
async def gateway(monkeypatch):
    from src.router.antigravity import anthropic, openai, responses

    monkeypatch.setenv("RESPONSES_STATE_KEY", Fernet.generate_key().decode())
    state = SimpleNamespace(calls=[], reject=False, failure_mode=None)
    app = FastAPI()
    output = {"candidates": [{"content": {"parts": [{"text": "Synthetic answer"}]}, "finishReason": "STOP"}],
              "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}}

    async def upstream(**kwargs):
        state.calls.append(copy.deepcopy(kwargs["body"]))
        if state.failure_mode == "empty":
            return
        if state.failure_mode == "exception":
            raise RuntimeError("synthetic upstream failure")
        if state.failure_mode == "http_exception":
            raise HTTPException(status_code=409, detail="synthetic unavailable owner")
        if state.reject:
            yield Response(status_code=400, media_type="text/event-stream", content=json.dumps({
                "error": {"code": 400, "message": "Synthetic tool choice unsupported", "status": "INVALID_ARGUMENT"},
            }))
            return
        if kwargs.get("on_credential"):
            kwargs["on_credential"]("synthetic-account@example.invalid", kwargs["body"])
        yield "data: " + json.dumps(output)
        yield ""
        yield "data: [DONE]"
        yield ""

    async def non_stream(**kwargs):
        state.calls.append(copy.deepcopy(kwargs["body"]))
        if state.reject:
            return Response(status_code=400, media_type="text/event-stream", content=json.dumps({
                "error": {"code": 400, "message": "Synthetic tool choice unsupported", "status": "INVALID_ARGUMENT"},
            }))
        return Response(content=json.dumps(output), media_type="application/json")

    for route in (openai, responses, anthropic):
        app.include_router(route.router)
        app.dependency_overrides[route.authenticate_bearer] = lambda: "synthetic-auth"
        monkeypatch.setattr(route, "stream_request", upstream)
        monkeypatch.setattr(route, "get_anti_truncation_max_attempts", AsyncMock(return_value=1))
    monkeypatch.setattr(openai, "non_stream_request", non_stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic.invalid") as client:
        yield client, state


def body_notes(response, protocol):
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    if protocol == "chat":
        return events[0].get("x_agy_ignored_params", [])
    if protocol == "responses":
        return events[0]["response"].get("x_agy_ignored_params", [])
    return []  # Native Messages uses its existing response-header warning contract.


@pytest.mark.parametrize("protocol", PATHS)
@pytest.mark.parametrize("choice", ["required", "none", "named"])
@pytest.mark.asyncio
async def test_claude_anti_truncation_reports_overridden_explicit_tool_choice(gateway, protocol, choice):
    client, state = gateway
    response = await client.post(PATHS[protocol], json=request_payload(protocol, choice))
    assert response.status_code == 200, response.text
    notes = response.headers["x-agy-ignored-params"].split(",")
    assert notes.count(NOTE) == 1
    assert TOOL not in response.headers["x-agy-ignored-params"]
    if protocol != "native":
        assert NOTE in body_notes(response, protocol)
    assert len(state.calls) == 1
    upstream = state.calls[0]
    assert upstream["model"] == CLAUDE
    config = upstream["request"]["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == "AUTO" and "allowedFunctionNames" not in config
    assert NOTE not in json.dumps(upstream)


@pytest.mark.parametrize("protocol", PATHS)
@pytest.mark.parametrize("case", ["default", "auto", "non_stream", "regular", "gemini"])
@pytest.mark.asyncio
async def test_tool_choice_warning_is_limited_to_active_claude_override(gateway, protocol, case):
    client, state = gateway
    options = {"choice": "required"}
    if case == "default":
        options["choice"] = None
    elif case == "auto":
        options["choice"] = "auto"
    elif case == "non_stream":
        options["streaming"] = False
    elif case == "regular":
        options["anti"] = False
    else:
        options["model"] = "gemini-3-flash"
    response = await client.post(PATHS[protocol], json=request_payload(protocol, **options))
    assert response.status_code == 200, response.text
    assert NOTE not in response.headers.get("x-agy-ignored-params", "")
    assert NOTE not in response.text
    assert len(state.calls) == 1
    if case == "non_stream":
        assert NON_STREAM_NOTE in response.headers.get("x-agy-ignored-params", "").split(",")
        if protocol != "native":
            assert NON_STREAM_NOTE in response.json().get("x_agy_ignored_params", [])
    if case in ("non_stream", "regular", "gemini"):
        assert state.calls[0]["request"]["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"


@pytest.mark.parametrize("protocol", PATHS)
@pytest.mark.parametrize("choice", ["required", "named"])
@pytest.mark.asyncio
async def test_regular_claude_tool_restrictions_are_not_silently_relaxed(gateway, protocol, choice):
    client, state = gateway
    state.reject = True
    response = await client.post(PATHS[protocol], json=request_payload(protocol, choice, anti=False))
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert NOTE not in response.headers.get("x-agy-ignored-params", "")
    assert len(state.calls) == 1
    config = state.calls[0]["request"]["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == "ANY"
    if choice == "named":
        assert config["allowedFunctionNames"] == [TOOL]


@pytest.mark.asyncio
async def test_native_auto_only_keeps_existing_parallel_tool_warning(gateway):
    client, state = gateway
    body = request_payload("native", "auto")
    body["tool_choice"]["disable_parallel_tool_use"] = True
    response = await client.post(PATHS["native"], json=body)
    assert response.status_code == 200, response.text
    notes = response.headers["x-agy-ignored-params"].split(",")
    assert "tool_choice.disable_parallel_tool_use" in notes
    assert NOTE not in notes
    assert state.calls[0]["request"]["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"


@pytest.mark.parametrize("protocol", PATHS)
@pytest.mark.parametrize("choice", ["required", "named"])
@pytest.mark.asyncio
async def test_non_stream_anti_prefix_warning_survives_predata_rejection(gateway, protocol, choice):
    client, state = gateway
    state.reject = True
    response = await client.post(PATHS[protocol], json=request_payload(protocol, choice, streaming=False))
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    notes = response.headers.get("x-agy-ignored-params", "").split(",")
    assert notes.count(NON_STREAM_NOTE) == 1 and NOTE not in notes
    assert TOOL not in response.headers["x-agy-ignored-params"]
    if protocol == "chat":
        assert NON_STREAM_NOTE in response.json()["x_agy_ignored_params"]
    assert len(state.calls) == 1
    upstream = state.calls[0]
    assert upstream["model"] == CLAUDE
    config = upstream["request"]["toolConfig"]["functionCallingConfig"]
    assert config["mode"] == "ANY"
    if choice == "named":
        assert config["allowedFunctionNames"] == [TOOL]
    assert "emit_answer" not in json.dumps(upstream)
    assert NON_STREAM_NOTE not in json.dumps(upstream)


@pytest.mark.parametrize("protocol", PATHS)
@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.asyncio
async def test_regular_non_stream_requests_do_not_report_anti_truncation(gateway, protocol, reject):
    client, state = gateway
    state.reject = reject
    response = await client.post(PATHS[protocol], json=request_payload(protocol, anti=False, streaming=False))
    assert response.status_code == (400 if reject else 200)
    assert NON_STREAM_NOTE not in response.headers.get("x-agy-ignored-params", "")
    assert NON_STREAM_NOTE not in response.text and NOTE not in response.text


@pytest.mark.parametrize("protocol", ["responses", "native"])
@pytest.mark.parametrize("failure_mode", ["empty", "exception", "http_exception"])
@pytest.mark.asyncio
async def test_non_stream_anti_prefix_warning_survives_prefetch_failure(gateway, protocol, failure_mode):
    client, state = gateway
    state.failure_mode = failure_mode
    response = await client.post(PATHS[protocol], json=request_payload(protocol, streaming=False))
    expected = 409 if protocol == "native" and failure_mode == "http_exception" else 502
    assert response.status_code == expected
    assert response.headers["content-type"] == "application/json"
    notes = response.headers.get("x-agy-ignored-params", "").split(",")
    assert notes.count(NON_STREAM_NOTE) == 1 and NOTE not in notes
    assert len(state.calls) == 1
