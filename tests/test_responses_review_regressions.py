"""PR review regressions for exact tool results and terminal stream boundaries."""
import json

import pytest

from src.converter.openai2gemini import convert_openai_to_gemini_request
from src.responses.mapper import ResponsesMapper
from src.responses.request import build_gemini_request, convert_request


MODEL = "gemini-3-flash"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["function", "custom_tool"])
@pytest.mark.parametrize("text", [
    "true", "false", "null", "1e999", '"quoted"',
    '  {"first": 1, "first": 2}  \n', '[1,  2]\n', "中文  \n",
])
async def test_responses_tool_result_is_opaque_text(kind, text):
    call = {"type": kind + "_call", "call_id": "synthetic-call", "name": "f"}
    call["arguments" if kind == "function" else "input"] = "{}"
    conv = convert_request({"model": MODEL, "input": [
        {"role": "user", "content": "synthetic request"}, call,
        {"type": kind + "_call_output", "call_id": call["call_id"], "output": text},
    ]})
    payload, _ = await build_gemini_request(conv)
    result = next(part["functionResponse"]["response"]
                  for content in payload["request"]["contents"] for part in content["parts"]
                  if "functionResponse" in part)
    assert result == {"result": text}
    # HTTPX rejects non-finite numbers before sending; all legal output strings
    # must remain representable as strict JSON regardless of their contents.
    json.dumps(payload, allow_nan=False)


@pytest.mark.asyncio
async def test_existing_chat_json_tool_result_mapping_is_unchanged():
    payload = await convert_openai_to_gemini_request({"model": MODEL, "messages": [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "synthetic-call",
         "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "synthetic-call", "content": '{"ok":true}'},
    ]})
    result = next(part["functionResponse"]["response"]
                  for content in payload["contents"] for part in content["parts"]
                  if "functionResponse" in part)
    assert result == {"ok": True}


def mapper():
    return ResponsesMapper(convert_request({"model": MODEL,
        "tools": [{"type": "function", "name": "f"}]}),
        account_ref=lambda: "synthetic@example.invalid")


@pytest.mark.parametrize("signed", [False, True])
def test_duplicate_upstream_call_id_cannot_dispatch_a_second_tool(signed):
    state = mapper()
    part = {"functionCall": {"id": "synthetic-call", "name": "f", "args": {}}}
    if signed:
        part["thoughtSignature"] = "SYNTHETIC_SIGNATURE"
    chunk = {"candidates": [{"content": {"parts": [part]}}]}
    state.feed_gemini(chunk)
    events = state.feed_gemini(chunk)
    state.feed_gemini({"candidates": [{"finishReason": "STOP"}]})
    state.finish()
    assert state.final["status"] == "failed"
    assert len(state.final["output"]) == 1
    assert not any(event["type"] == "response.output_item.added" for event in events)


@pytest.mark.parametrize("part", [
    {"text": "late text"},
    {"functionCall": {"id": "late-call", "name": "f", "args": {}}},
])
def test_content_after_finish_cannot_be_reported_as_success(part):
    state = mapper()
    state.feed_gemini({"candidates": [{"finishReason": "STOP"}]})
    assert state.feed_gemini({"candidates": [{"content": {"parts": [part]}}]}) == []
    state.finish()
    assert state.final["status"] == "failed"
    assert state.final["output"] == []


@pytest.mark.parametrize("finish", ["MAX_TOKENS", 1, {}, []])
def test_finish_reason_cannot_change_or_have_a_nonstring_value(finish):
    state = mapper()
    state.feed_gemini({"candidates": [{"finishReason": "STOP"}]})
    state.feed_gemini({"candidates": [{"finishReason": finish}]})
    state.finish()
    assert state.final["status"] == "failed"


def test_repeated_finish_and_late_usage_are_valid():
    state = mapper()
    state.feed_gemini({"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})
    state.feed_gemini({"candidates": [{"finishReason": "STOP"}]})
    state.feed_gemini({"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}})
    state.finish()
    assert state.final["status"] == "completed"
    assert state.final["usage"]["total_tokens"] == 5


def test_data_after_done_cannot_dispatch_a_tool():
    state = mapper()
    state.feed_sse_line('data: {"candidates":[{"finishReason":"STOP"}]}\n\n')
    state.feed_sse_line("data: [DONE]\n\n")
    events = state.feed_sse_line('data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"f","args":{}}}]}}]}\n\n')
    state.finish()
    assert events == []
    assert state.final["status"] == "failed"
    assert state.final["output"] == []
