"""Responses fidelity regressions, verified with synthetic offline inputs.

These cases must pass after fixing the represented protocol boundary.
The shared conftest blocks external HTTP and isolates account/configuration state.
"""

import json

import pytest


def _map_completed_part(conv, part):
    from src.responses.mapper import ResponsesMapper

    mapper = ResponsesMapper(conv)
    mapper.start_events()
    mapper.feed_gemini({
        "candidates": [{
            "content": {"role": "model", "parts": [part]},
            "finishReason": "STOP",
        }],
    })
    mapper.finish()
    assert mapper.final["status"] == "completed"
    return mapper.final_with_output_text()


@pytest.mark.parametrize("literal", ["true", "false", "null"])
def test_function_tool_string_arguments_keep_their_type(literal):
    from src.responses.request import convert_request

    conv = convert_request({
        "model": "synthetic-model",
        "input": "synthetic request",
        "tools": [{
            "type": "function",
            "name": "audit_function",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }],
    })
    response = _map_completed_part(conv, {
        "functionCall": {
            "id": "synthetic_call",
            "name": "audit_function",
            "args": {"value": literal},
        },
    })
    arguments = json.loads(response["output"][0]["arguments"])
    assert arguments == {"value": literal}


@pytest.mark.parametrize("literal", ["true", "false", "null"])
def test_custom_tool_returns_the_original_freeform_string(literal):
    from src.responses.request import convert_request

    conv = convert_request({
        "model": "synthetic-model",
        "input": "synthetic request",
        "tools": [{"type": "custom", "name": "audit_custom"}],
    })
    response = _map_completed_part(conv, {
        "functionCall": {
            "id": "synthetic_call",
            "name": "audit_custom",
            "args": {"input": literal},
        },
    })
    item = response["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["input"] == literal


@pytest.mark.asyncio
async def test_signed_thought_replay_preserves_exact_text(monkeypatch):
    from src.responses import encrypted
    from src.responses.request import build_gemini_request, convert_request

    monkeypatch.setenv("RESPONSES_CLAUDE_THINKING_WITH_TOOLS", "1")
    model = "claude-opus-5-5-high"
    thought = "synthetic signed thought  \n"
    signature = "SYNTHETIC_AUDIT_SIGNATURE"
    blob = encrypted.encode(model, "synthetic@example.invalid", signature, thought)
    conv = convert_request({
        "model": model,
        "tools": [{
            "type": "function",
            "name": "audit_function",
            "parameters": {"type": "object", "properties": {}},
        }],
        "input": [
            {"role": "user", "content": "synthetic request"},
            {"type": "reasoning", "encrypted_content": blob},
            {"type": "function_call", "call_id": "synthetic_call",
             "name": "audit_function", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "synthetic_call", "output": "ok"},
        ],
    })
    request, _info = await build_gemini_request(conv)
    replayed = [
        part for content in request["request"]["contents"]
        for part in content["parts"]
        if part.get("thought") and part.get("thoughtSignature") == signature
    ]
    assert len(replayed) == 1
    assert replayed[0]["text"] == thought


@pytest.mark.parametrize("text", ["...", "…"])
def test_real_ellipsis_output_is_not_dropped(text):
    from src.responses.request import convert_request

    conv = convert_request({"model": "synthetic-model", "input": "synthetic request"})
    response = _map_completed_part(conv, {"text": text})
    assert response["output_text"] == text


def test_distinct_long_namespaced_tools_remain_representable():
    from src.responses.request import ResponsesError, convert_request

    common = "audit_function_" + "x" * 40
    names = [common + "_first", common + "_other"]
    assert all(len(name) <= 64 for name in names)
    body = {
        "model": "synthetic-model",
        "input": "synthetic request",
        "tools": [{
            "type": "namespace",
            "name": "audit_namespace",
            "tools": [{
                "type": "function",
                "name": name,
                "parameters": {"type": "object", "properties": {}},
            } for name in names],
        }],
    }
    rejection = None
    try:
        conv = convert_request(body)
    except ResponsesError as error:
        rejection = (error.status, error.param, error.message)
    assert rejection is None, rejection
    assert len(conv.tool_map) == len(names)
    assert {info.name for info in conv.tool_map.values()} == set(names)
