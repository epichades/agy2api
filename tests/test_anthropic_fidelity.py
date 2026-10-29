"""Native Messages preserves signed/tool history and reports upstream failures."""

import asyncio
import copy
import json

from fastapi import Response
from starlette.requests import Request
import pytest

from src.converter.anthropic2gemini import (
    AnthropicStreamError,
    aggregate_gemini_to_anthropic,
    anthropic_to_gemini_request,
    build_generation_config,
    convert_messages_to_contents,
    gemini_stream_to_anthropic_stream,
    gemini_to_anthropic_response,
)


@pytest.fixture(autouse=True)
def synthetic_native_replay_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("RESPONSES_STATE_KEY", Fernet.generate_key().decode())


def frame(parts=None, finish=None, usage=None):
    candidate = {"content": {"role": "model", "parts": parts or []}}
    if finish is not None:
        candidate["finishReason"] = finish
    data = {"candidates": [candidate]}
    if usage is not None:
        data["usageMetadata"] = usage
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def events(chunks):
    text = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def history():
    return [
        {"role": "user", "content": "Q \n"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "think \n", "signature": "opaque-synthetic-signature"},
            {"type": "text", "text": " checking "},
            {"type": "tool_use", "id": "call-a", "name": "read", "input": {"nested": {"value": None}, "list": [None, "true", False]}},
            {"type": "text", "text": " between "},
            {"type": "tool_use", "id": "call-b", "name": "read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "before "},
            {"type": "tool_result", "tool_use_id": "call-a", "content": [{"type": "text", "text": "first\n"}, {"type": "text", "text": " second "}]},
            {"type": "text", "text": " middle "},
            {"type": "tool_result", "tool_use_id": "call-b", "content": None, "is_error": True},
            {"type": "text", "text": "after"},
        ]},
    ]


@pytest.mark.asyncio
async def test_request_history_is_not_mutated_or_reordered():
    payload = {"model": "claude-test", "messages": history(), "max_tokens": 256}
    before = copy.deepcopy(payload)
    converted = await anthropic_to_gemini_request(payload)
    assert payload == before
    contents = converted["contents"]
    assert [content["role"] for content in contents] == ["user", "model", "user"]
    assert [next(key for key in part if key in {"thought", "text", "functionCall"}) for part in contents[1]["parts"]] == ["thought", "text", "functionCall", "text", "functionCall"]
    assert contents[1]["parts"][0] == {"thought": True, "text": "think \n", "thoughtSignature": "opaque-synthetic-signature"}
    assert contents[1]["parts"][2]["functionCall"]["args"] == {"nested": {"value": None}, "list": [None, "true", False]}
    assert ["functionResponse" in part for part in contents[2]["parts"]] == [False, True, False, True, False]
    assert contents[2]["parts"][1]["functionResponse"]["response"]["output"] == "first\n second "
    assert contents[2]["parts"][3]["functionResponse"]["response"]["is_error"] is True


@pytest.mark.parametrize("signature_key", ["signature", "thoughtSignature"])
def test_thinking_signature_alias_is_opaque_and_exact(signature_key):
    block = {"type": "thinking", "thinking": " \n", signature_key: "s"}
    result = convert_messages_to_contents([{ "role": "assistant", "content": [block]}])
    assert result[0]["parts"][0] == {"thought": True, "text": " \n", "thoughtSignature": "s"}


@pytest.mark.parametrize("block", [
    {"type": "thinking", "thinking": "unsigned"},
    {"type": "thinking", "thinking": "t", "signature": "a", "thoughtSignature": "b"},
    {"type": "redacted_thinking", "data": "opaque"},
    {"type": "document", "source": {"data": "synthetic"}},
])
def test_unmappable_blocks_are_rejected_instead_of_dropped(block):
    with pytest.raises(ValueError):
        convert_messages_to_contents([{"role": "assistant", "content": [block]}])


def test_multimodal_tool_result_is_explicitly_rejected():
    messages = history()
    messages[2]["content"][1]["content"].append({"type": "image", "source": {}})
    with pytest.raises(ValueError, match="text blocks only"):
        convert_messages_to_contents(messages)


def test_function_signature_survives_native_tool_id_roundtrip():
    data = {"candidates": [{"content": {"parts": [{"functionCall": {
        "name": "read", "id": "call-a", "args": {"value": None}},
        "thoughtSignature": "synthetic-function-signature"}]}, "finishReason": "OTHER"}]}
    response = gemini_to_anthropic_response(data, "gemini-test")
    assert response["content"][0]["id"] == "call-a__thought__synthetic-function-signature"
    contents = convert_messages_to_contents([
        {"role": "assistant", "content": response["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": response["content"][0]["id"], "content": "done"}]},
    ])
    assert contents[0]["parts"][0]["thoughtSignature"] == "synthetic-function-signature"
    assert contents[1]["parts"][0]["functionResponse"]["id"] == "call-a"


def signed_output():
    return [
        frame([{"thought": True, "text": "想 \n"}]),
        frame([{"thought": True, "text": "", "thoughtSignature": "signed-one"}]),
        frame([{"text": "..."}, {"text": " "}, {"text": "…"}]),
        frame([{"functionCall": {"name": "read", "id": "call-a", "args": {"value": None, "items": [None, "true"]}}}]),
        frame([{"thought": True, "text": "second", "thoughtSignature": "signed-two"}]),
        frame([{"text": "after\n"}], "OTHER", {"promptTokenCount": 10, "cachedContentTokenCount": 2, "candidatesTokenCount": 3, "thoughtsTokenCount": 4}),
    ]


@pytest.mark.asyncio
async def test_stream_signatures_close_the_correct_thinking_blocks():
    async def source():
        for chunk in signed_output():
            yield chunk.encode()

    result = events([chunk async for chunk in gemini_stream_to_anthropic_stream(source(), "claude-test")])
    starts = [e for e in result if e["type"] == "content_block_start"]
    assert [e["content_block"]["type"] for e in starts] == ["thinking", "text", "tool_use", "thinking", "text"]
    signatures = [e for e in result if e.get("delta", {}).get("type") == "signature_delta"]
    assert [(e["index"], e["delta"]["signature"]) for e in signatures] == [(0, "signed-one"), (3, "signed-two")]
    assert "".join(e["delta"]["thinking"] for e in result if e.get("delta", {}).get("type") == "thinking_delta") == "想 \nsecond"
    assert "".join(e["delta"]["text"] for e in result if e.get("delta", {}).get("type") == "text_delta") == "... …after\n"
    assert result[-2]["delta"]["stop_reason"] == "tool_use"
    assert result[-1]["type"] == "message_stop"
    tool_args = next(e["delta"]["partial_json"] for e in result if e.get("delta", {}).get("type") == "input_json_delta")
    assert json.loads(tool_args) == {"value": None, "items": [None, "true"]}


@pytest.mark.asyncio
async def test_nonstream_aggregation_preserves_order_signatures_and_last_usage():
    async def source():
        for chunk in signed_output():
            yield chunk.encode()

    result = await aggregate_gemini_to_anthropic(source(), "claude-test")
    assert [block["type"] for block in result["content"]] == ["thinking", "text", "tool_use", "thinking", "text"]
    assert result["content"][0] == {"type": "thinking", "thinking": "想 \n", "signature": "signed-one"}
    assert result["content"][1]["text"] == "... …"
    assert result["content"][2]["input"] == {"value": None, "items": [None, "true"]}
    assert result["usage"]["input_tokens"] == 8 and result["usage"]["output_tokens"] == 7
    assert result["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_byte_split_utf8_and_sse_frames_roundtrip():
    raw = "".join(signed_output()).encode()

    async def source():
        for value in raw:
            yield bytes([value])

    result = await aggregate_gemini_to_anthropic(source(), "claude-test")
    assert result["content"][0]["thinking"] == "想 \n"


@pytest.mark.asyncio
async def test_omitted_display_retains_signatures_but_not_thinking_text():
    from src.responses import encrypted as enc
    async def source():
        for chunk in signed_output():
            yield chunk.encode()

    def encode(signature, text):
        return enc.encode("claude-test", "synthetic@example.invalid", signature, text)

    result = await aggregate_gemini_to_anthropic(source(), "claude-test", display_thinking=False, signature_encoder=encode)
    block = result["content"][0]
    assert block["thinking"] == "" and block["signature"].startswith("agy2.")
    assert enc.decode(block["signature"])["t"] == "想 \n"
    assert "signed-one" not in json.dumps(result) and "second" not in json.dumps(result)
    chunks = [chunk async for chunk in gemini_stream_to_anthropic_stream(source(), "claude-test", display_thinking=False, signature_encoder=encode)]
    assert "想" not in b"".join(chunks).decode() and "signed-one" not in b"".join(chunks).decode()
    stream = events(chunks)
    assert all(e["delta"]["thinking"] == "" for e in stream if e.get("delta", {}).get("type") == "thinking_delta")
    assert any(e.get("delta", {}).get("type") == "signature_delta" for e in stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [
    [b"data: {not JSON}\n\n", frame([], "STOP").encode()],
    [frame([{"text": "unfinished"}]).encode()],
    [frame([{"thought": True, "text": "unsigned"}], "STOP").encode()],
    [frame([{"thought": True, "text": "", "thoughtSignature": "orphan"}], "STOP").encode()],
])
async def test_bad_streams_never_emit_success(chunks):
    async def source():
        for chunk in chunks:
            yield chunk

    result = events([chunk async for chunk in gemini_stream_to_anthropic_stream(source(), "claude-test")])
    assert result[-1]["type"] == "error"
    assert not any(event["type"] == "message_stop" for event in result)
    with pytest.raises(AnthropicStreamError):
        await aggregate_gemini_to_anthropic(source(), "claude-test")


def test_generation_defaults_do_not_invent_stop_sequences_or_sampling():
    assert build_generation_config({}) == {}
    assert build_generation_config({"temperature": 0, "stop_sequences": []}) == {"temperature": 0, "stopSequences": []}


@pytest.mark.asyncio
async def test_native_nonstream_hi_is_real_inference_through_stream(monkeypatch):
    from src.models import ClaudeRequest
    from src.router.antigravity import anthropic as route
    calls, closed = [], []

    async def normalize(payload):
        return payload

    async def upstream(**kwargs):
        calls.append(kwargs)
        try:
            for chunk in signed_output():
                yield chunk
        finally:
            closed.append(1)

    async def forbidden(**kwargs):
        raise AssertionError("Native non-stream must aggregate upstream stream")

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream)
    monkeypatch.setattr(route, "non_stream_request", forbidden)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    response = await route.messages(ClaudeRequest(model="claude-test", messages=[{"role": "user", "content": "Hi"}]), request, _token="synthetic")
    data = json.loads(response.body)
    assert response.status_code == 200 and len(calls) == 1 and closed == [1]
    assert calls[0]["body"]["request"]["contents"][0]["parts"] == [{"text": "Hi"}]
    assert data["content"][0]["signature"] == "signed-one" and data["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_native_rejects_unmappable_capabilities_before_inference(monkeypatch):
    from src.models import ClaudeRequest
    from src.router.antigravity import anthropic as route

    async def forbidden(**kwargs):
        raise AssertionError("Unsupported capability must not call upstream")

    monkeypatch.setattr(route, "stream_request", forbidden)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    payload = ClaudeRequest.model_validate({"model": "claude-test", "messages": [{"role": "user", "content": "test"}], "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": {"type": "thinking_turns", "value": 1}}]}})
    response = await route.messages(payload, request, _token="synthetic")
    assert response.status_code == 400 and "context_management" in json.loads(response.body)["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [{}, {"edits": []}, {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}])
async def test_native_client_noop_context_and_effort_are_reported_without_changing_history(monkeypatch, context):
    from src.models import ClaudeRequest
    from src.router.antigravity import anthropic as route
    captured = []

    async def normalize(payload):
        return payload

    async def upstream(**kwargs):
        captured.append(kwargs["body"])
        yield frame([{"text": "SYNTHETIC_OK"}], "STOP")

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    payload = ClaudeRequest.model_validate({"model": "claude-opus-5-5-high", "messages": [{"role": "user", "content": " exact  \n"}], "context_management": context, "output_config": {"effort": "max"}})
    response = await route.messages(payload, request, _token="synthetic")
    assert response.status_code == 200
    assert captured[0]["model"] == "claude-opus-5-5-high"
    assert captured[0]["request"]["contents"][0]["parts"] == [{"text": " exact  \n"}]
    assert "context_management(no_op_keep_all)" in response.headers["x-agy-ignored-params"]
    assert "output_config.effort(upstream_model_policy)" in response.headers["x-agy-ignored-params"]


@pytest.mark.parametrize("extra", [
    {"context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all", "trigger": {"type": "input_tokens", "value": 1}}]}},
    {"context_management": {"edits": [{"type": "clear_tool_uses_20250919", "keep": "all"}]}},
    {"context_management": {"edits": None}},
    {"output_config": {"format": {"type": "json_schema"}}},
    {"output_config": {"effort": []}},
    {"thinking": {"type": []}},
    {"thinking": {"type": "adaptive", "display": []}},
    {"tool_choice": {"type": []}},
])
def test_native_client_hints_do_not_enable_real_context_edits_or_structured_output(extra):
    from src.router.antigravity.anthropic import _validate_capabilities
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    with pytest.raises(ValueError):
        _validate_capabilities({"model": "claude-test", "messages": [], **extra}, request)


@pytest.mark.asyncio
async def test_native_hidden_signed_tool_roundtrip_uses_encrypted_account_bound_state(monkeypatch):
    from src.models import ClaudeRequest, model_to_dict
    from src.responses import encrypted as enc
    from src.router.antigravity import anthropic as route
    captured, closed = [], []
    thought, signature, function_signature = "private synthetic thought  \n", "synthetic-thought-signature", "synthetic-function-signature"
    args = {"value": None, "background": False}

    async def normalize(payload):
        return payload

    async def upstream(**kwargs):
        try:
            kwargs["on_credential"]("original@example.invalid", kwargs["body"])
            captured.append(copy.deepcopy(kwargs["body"]))
            if len(captured) == 1:
                yield frame([{"thought": True, "text": thought}])
                yield frame([{"thought": True, "text": "", "thoughtSignature": signature}])
                yield frame([{"functionCall": {"id": "call-a", "name": "read", "args": args}, "thoughtSignature": function_signature}], "OTHER")
            else:
                yield frame([{"text": "SYNTHETIC_OK"}], "STOP")
        finally:
            closed.append(True)

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    initial = {"model": "claude-test", "messages": [{"role": "user", "content": "test"}], "thinking": {"type": "adaptive", "display": "omitted"}}
    response = await route.messages(ClaudeRequest.model_validate(initial), request, _token="synthetic")
    assert response.status_code == 200
    data = json.loads(response.body)
    assert thought.encode() not in response.body and signature.encode() not in response.body
    block = data["content"][0]
    assert block["thinking"] == "" and enc.decode(block["signature"])["t"] == thought
    tool = data["content"][1]
    assert tool["id"].startswith(enc.CALL_ID_PREFIX)
    assert function_signature.encode() not in response.body and b"__thought__" not in response.body
    followup = {**initial, "messages": initial["messages"] + [{"role": "assistant", "content": data["content"]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool["id"], "content": [{"type": "text", "text": "first\n"}, {"type": "text", "text": " second "}], "is_error": True}]}]}
    typed = ClaudeRequest.model_validate(followup)
    before = model_to_dict(typed)
    final = await route.messages(typed, request, _token="synthetic")
    assert final.status_code == 200 and model_to_dict(typed) == before
    parts = [p for c in captured[1]["request"]["contents"] for p in c["parts"]]
    assert parts[1] == {"thought": True, "text": thought, "thoughtSignature": signature}
    function = next(p for p in parts if "functionCall" in p)
    assert function["functionCall"]["args"] == args and function["thoughtSignature"] == function_signature
    result = next(p["functionResponse"] for p in parts if "functionResponse" in p)
    assert result["response"] == {"output": "first\n second ", "is_error": True}
    assert closed == [True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("case,status", [("args", 400), ("name", 400), ("model", 400), ("account", 409), ("tamper", 400), ("legacy", 400), ("orphan", 400), ("conflict", 400)])
async def test_native_signed_function_only_history_is_bound_before_transport(monkeypatch, case, status):
    from src.models import ClaudeRequest
    from src.responses import encrypted as enc
    from src.responses.request import _arguments_digest
    from src.router.antigravity import anthropic as route
    args = {"value": None, "background": False}
    blob = enc.encode("claude-test", "original@example.invalid", "synthetic-function-signature", "",
                      call_id="call-a", function_name="read", arguments_hash=_arguments_digest(args))
    tool_id = enc.CALL_ID_PREFIX + blob[len(enc.PREFIX):]
    model, name = "claude-test", "read"
    if case == "args":
        args["background"] = True
    elif case == "name":
        name = "write"
    elif case == "model":
        model = "different-model"
    elif case == "tamper":
        tool_id = tool_id[:-2] + ("AA" if tool_id[-2:] != "AA" else "BB")
    elif case == "legacy":
        tool_id = "call_agy1_synthetic-old-state"
    tools = [{"type": "tool_use", "id": tool_id, "name": name, "input": args}]
    if case == "conflict":
        other = enc.encode("claude-test", "original@example.invalid", "different-signature", "",
                           call_id="call-a", function_name="read", arguments_hash=_arguments_digest(args))
        tools.append({"type": "tool_use", "id": enc.CALL_ID_PREFIX + other[len(enc.PREFIX):], "name": "read", "input": args})
    history = [{"role": "assistant", "content": tools}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "synthetic output"}]}]
    if case == "orphan":
        history = history[1:]
    transports, callbacks = [], []

    async def normalize(payload):
        return payload

    async def upstream(**kwargs):
        callbacks.append(True)
        kwargs["on_credential"]("foreign@example.invalid" if case == "account" else "original@example.invalid", kwargs["body"])
        transports.append(True)
        yield frame([{"text": "must not happen"}], "STOP")

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    response = await route.messages(ClaudeRequest.model_validate({"model": model, "messages": history}), request, _token="synthetic")
    assert response.status_code == status and transports == []
    assert callbacks == ([True] if case == "account" else [])
    assert tool_id.encode() not in response.body and b"synthetic-function-signature" not in response.body


@pytest.mark.asyncio
async def test_native_hidden_state_key_failure_prevents_inference(monkeypatch):
    from src.models import ClaudeRequest
    from src.router.antigravity import anthropic as route
    monkeypatch.setenv("RESPONSES_STATE_KEY", "synthetic-invalid-key")

    async def normalize(payload):
        return payload

    async def upstream(**kwargs):
        raise AssertionError("Invalid state key must not reach transport")
        yield

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    payload = ClaudeRequest.model_validate({"model": "claude-test", "messages": [{"role": "user", "content": "test"}], "thinking": {"type": "adaptive", "display": "omitted"}})
    response = await route.messages(payload, request, _token="synthetic")
    assert response.status_code == 500 and b"synthetic-invalid-key" not in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize("case,status", [("model", 400), ("account", 409), ("tamper", 400), ("legacy", 400), ("raw_empty", 400), ("foreign_key", 400), ("function_state", 400), ("edited_text", 400)])
async def test_native_hidden_replay_rejects_invalid_state_before_transport(monkeypatch, case, status):
    from cryptography.fernet import Fernet
    from src.models import ClaudeRequest
    from src.responses import encrypted as enc
    from src.router.antigravity import anthropic as route
    transports, callbacks, closed = [], [], []
    blob = enc.encode("claude-test", "original@example.invalid", "synthetic-signature", "private thought")
    model, text = "claude-test", ""
    if case == "model":
        model = "different-model"
    elif case == "tamper":
        blob = blob[:-2] + ("AA" if blob[-2:] != "AA" else "BB")
    elif case == "legacy":
        blob = "agy1.synthetic-legacy-state"
    elif case == "raw_empty":
        blob = "synthetic-external-signature"
    elif case == "foreign_key":
        monkeypatch.setenv("RESPONSES_STATE_KEY", Fernet.generate_key().decode())
    elif case == "function_state":
        blob = enc.encode("claude-test", "original@example.invalid", "synthetic-signature", "", call_id="call-a", function_name="read", arguments_hash="synthetic-hash")
    elif case == "edited_text":
        text = "edited"

    async def normalize(payload):
        return payload

    async def upstream(**kwargs):
        try:
            callbacks.append(True)
            kwargs["on_credential"]("foreign@example.invalid" if case == "account" else "original@example.invalid", kwargs["body"])
            transports.append(True)
            yield frame([{"text": "must not happen"}], "STOP")
        finally:
            closed.append(True)

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    payload = ClaudeRequest.model_validate({"model": model, "messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": text, "signature": blob}]}, {"role": "user", "content": "continue"}]})
    response = await route.messages(payload, request, _token="synthetic")
    assert response.status_code == status and transports == []
    assert callbacks == ([True] if case == "account" else [])
    assert closed == callbacks
    assert b"private thought" not in response.body and blob.encode() not in response.body


@pytest.mark.asyncio
async def test_hidden_thought_state_cannot_be_emitted_without_selected_account(monkeypatch):
    from src.models import ClaudeRequest
    from src.router.antigravity import anthropic as route

    async def normalize(payload):
        return payload

    async def upstream(**kwargs):
        yield frame([{"thought": True, "text": "private thought", "thoughtSignature": "synthetic-signature"}], "STOP")

    monkeypatch.setattr(route, "normalize_antigravity_request", normalize)
    monkeypatch.setattr(route, "stream_request", upstream)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    payload = ClaudeRequest.model_validate({"model": "claude-test", "messages": [{"role": "user", "content": "test"}], "thinking": {"type": "adaptive", "display": "omitted"}})
    response = await route.messages(payload, request, _token="synthetic")
    assert response.status_code == 502 and b"private thought" not in response.body and b"synthetic-signature" not in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"thinking": {"type": "adaptive", "display": "updates"}},
    {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
])
async def test_native_cannot_silently_accept_unsupported_display_or_builtin_tools(monkeypatch, extra):
    from src.models import ClaudeRequest
    from src.router.antigravity import anthropic as route

    async def forbidden(**kwargs):
        raise AssertionError("Unsupported capability must not call upstream")

    monkeypatch.setattr(route, "stream_request", forbidden)
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    payload = ClaudeRequest.model_validate({"model": "claude-test", "messages": [{"role": "user", "content": "test"}], **extra})
    response = await route.messages(payload, request, _token="synthetic")
    assert response.status_code == 400


def test_tool_input_fields_are_not_mistaken_for_ignored_cache_hints():
    from src.router.antigravity.anthropic import _validate_capabilities
    request = Request({"type": "http", "method": "POST", "path": "/v1/messages", "headers": []})
    notes, _ = _validate_capabilities({"model": "claude-test", "messages": [{"role": "assistant", "content": [{
        "type": "tool_use", "id": "call-a", "name": "read", "input": {"cache_control": "preserve", "citations": None}}]}]}, request)
    assert notes == []


@pytest.mark.asyncio
async def test_managed_response_closes_when_cancelled_during_send():
    from src.router.stream_passthrough import ManagedStreamingResponse
    closed = []

    async def source():
        try:
            yield b"first"
            yield b"second"
        finally:
            closed.append(1)

    async def send(message):
        if message.get("body"):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await ManagedStreamingResponse(source()).stream_response(send)
    assert closed == [1]
