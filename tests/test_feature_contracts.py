"""Offline protocol regression checks with synthetic inputs.
No account, database initialization, or upstream service is needed.
"""

import json
import socket

import httpx
import pytest
from fastapi import Request
from pydantic import ValidationError

from src.converter.anthropic2gemini import (
    convert_messages_to_contents,
    gemini_stream_to_anthropic_stream,
)
from src.converter.openai2gemini import convert_openai_to_gemini_request
from src.models import OpenAIChatCompletionRequest, model_to_dict


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise RuntimeError("Feature contract tests must not access the network")

    async def denied_async(*args, **kwargs):
        denied()

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)


def openai_payload(**parameters):
    return {
        "model": "gemini-test",
        "messages": [{"role": "user", "content": "Return an audit example."}],
        **parameters,
    }


@pytest.mark.asyncio
async def test_openai_basic_text_and_token_limit():
    parsed = OpenAIChatCompletionRequest.model_validate(openai_payload(max_tokens=24))
    converted = await convert_openai_to_gemini_request(model_to_dict(parsed))
    assert converted["contents"] == [
        {"role": "user", "parts": [{"text": "Return an audit example."}]}
    ]
    assert converted["generationConfig"]["maxOutputTokens"] == 24


@pytest.mark.parametrize(
    ("parameter", "value", "upstream_key", "expected"),
    [
        ("max_completion_tokens", 24, "maxOutputTokens", 24),
        ("response_format", {"type": "json_object"}, "responseMimeType", "application/json"),
        ("n", 2, "candidateCount", 2),
        ("seed", 123, "seed", 123),
    ],
)
@pytest.mark.asyncio
async def test_openai_generation_options_survive_request_validation(
    parameter, value, upstream_key, expected
):
    parsed = OpenAIChatCompletionRequest.model_validate(openai_payload(**{parameter: value}))
    converted = await convert_openai_to_gemini_request(model_to_dict(parsed))
    assert converted["generationConfig"].get(upstream_key) == expected


@pytest.mark.asyncio
async def test_openai_string_stop_sequence():
    parsed = OpenAIChatCompletionRequest.model_validate(openai_payload(stop="END"))
    converted = await convert_openai_to_gemini_request(model_to_dict(parsed))
    assert converted["generationConfig"]["stopSequences"] == ["END"]


def test_anthropic_tool_result_preserves_all_text_blocks():
    converted = convert_messages_to_contents(
        [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_audit", "name": "read", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_audit",
                        "content": [
                            {"type": "text", "text": "first audit result"},
                            {"type": "text", "text": "second audit result"},
                        ],
                    }
                ],
            },
        ]
    )
    result = converted[1]["parts"][0]["functionResponse"]["response"]["output"]
    assert "first audit result" in result
    assert "second audit result" in result


@pytest.mark.parametrize(
    "pieces",
    [
        pytest.param(["hello", " world"], id="baseline"),
        pytest.param(["hello", " ", "world", "\n", "next"], id="separate-whitespace"),
    ],
)
@pytest.mark.asyncio
async def test_anthropic_stream_preserves_text_exactly(pieces):
    async def upstream():
        for text in pieces:
            yield (
                "data: "
                + json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]})
                + "\n\n"
            ).encode()
        yield b'data: {"candidates":[{"finishReason":"STOP"}]}\n\n'

    text_deltas = []
    async for event in gemini_stream_to_anthropic_stream(upstream(), "audit-model"):
        data_line = next(line for line in event.decode().splitlines() if line.startswith("data: "))
        delta = json.loads(data_line[6:]).get("delta", {})
        if delta.get("type") == "text_delta":
            text_deltas.append(delta["text"])
    assert "".join(text_deltas) == "".join(pieces)


@pytest.mark.asyncio
async def test_openai_hi_request_obeys_stream_contract(monkeypatch):
    # src.utils probes a release URL during import. Exercise its offline fallback
    # explicitly; the network guard still rejects any other outbound request.
    def offline_release(*args, **kwargs):
        raise httpx.ConnectError("offline audit release lookup")

    monkeypatch.setattr(httpx, "get", offline_release)
    from src.router.antigravity import openai as route

    async def normalize(payload):
        return payload

    async def upstream_stream(**kwargs):
        yield b'data: {"candidates":[{"content":{"parts":[{"text":"Hello"}]},"finishReason":"STOP"}]}\n\n'

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream_stream)
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []})
    parsed = OpenAIChatCompletionRequest.model_validate(
        {"model": "audit-model", "messages": [{"role": "user", "content": "Hi"}], "stream": True}
    )
    response = await route.chat_completions(parsed, request, token="offline-audit")
    assert response.media_type == "text/event-stream"
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks
