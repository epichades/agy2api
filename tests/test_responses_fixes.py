"""Offline regression checks at the Responses replay and conversion boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
import pytest

from src.responses import encrypted as enc
from src.responses.mapper import ResponsesMapper
from src.responses.request import ResponsesError, build_gemini_request, convert_request


MODEL = "claude-opus-5-5-high"
ACCOUNT = "synthetic@example.invalid"


def response_body(**extra):
    return {"model": MODEL, "input": "synthetic request", **extra}


def mapped(parts, **extra):
    conv = convert_request(response_body(**extra))
    mapper = ResponsesMapper(conv, account_ref=lambda: ACCOUNT)
    mapper.start_events()
    mapper.feed_gemini({"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]})
    mapper.finish()
    return mapper


def test_authenticated_state_is_private_stable_and_independent_of_api_password(monkeypatch):
    monkeypatch.delenv("RESPONSES_STATE_KEY", raising=False)
    text = "SYNTHETIC_PRIVATE_THOUGHT"
    blob = enc.encode(MODEL, ACCOUNT, "SYNTHETIC_SIGNATURE", text)
    assert blob.startswith("agy2.") and text not in blob
    monkeypatch.setenv("API_PASSWORD", "different-client-visible-password")
    assert enc.decode(blob) == {"v": 2, "m": MODEL, "a": enc.account_hash(ACCOUNT),
                                "s": "SYNTHETIC_SIGNATURE", "t": text}
    key_file = Path(os.environ["CREDENTIALS_DIR"]) / "responses-state.key"
    first = key_file.read_bytes()
    assert enc.initialize_key() == first
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o777 == 0o600


def test_concurrent_key_initialization_never_observes_partial_or_different_keys(monkeypatch):
    monkeypatch.delenv("RESPONSES_STATE_KEY", raising=False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: enc.initialize_key(), range(24)))
    assert len(set(keys)) == 1
    assert not list(Path(os.environ["CREDENTIALS_DIR"]).glob(".responses-state-*.tmp"))


@pytest.mark.parametrize("suffix", ["!", "\n", "A"])
def test_noncanonical_or_tampered_state_is_rejected(suffix):
    blob = enc.encode(MODEL, ACCOUNT, "SYNTHETIC_SIGNATURE", "synthetic thought")
    assert enc.decode(blob + suffix) is None
    changed = blob[:-12] + ("A" if blob[-12] != "A" else "B") + blob[-11:]
    assert enc.decode(changed) is None


@pytest.mark.parametrize("blob", ["agy1.synthetic_legacy", "foreign.synthetic", "agy2.bad"])
def test_invalid_or_legacy_replay_is_explicitly_rejected(blob):
    with pytest.raises(ResponsesError) as error:
        convert_request(response_body(input=[{"type": "reasoning", "encrypted_content": blob}]))
    assert error.value.status == 400
    assert error.value.code == "invalid_encrypted_content"


def test_wrong_model_state_is_rejected_instead_of_dropped():
    blob = enc.encode(MODEL, ACCOUNT, "SYNTHETIC_SIGNATURE", "synthetic thought")
    with pytest.raises(ResponsesError, match="different model"):
        convert_request({"model": "gemini-3-flash", "input": [{"type": "reasoning", "encrypted_content": blob}]})


def test_oversized_state_is_rejected_before_key_access(monkeypatch):
    def unexpected():
        raise AssertionError("oversized blob must not reach crypto/key loading")
    monkeypatch.setattr(enc, "initialize_key", unexpected)
    assert enc.decode("agy2." + "A" * enc.MAX_BLOB_CHARS) is None


def test_unsafe_key_permissions_and_symlink_are_rejected(monkeypatch, tmp_path):
    monkeypatch.delenv("RESPONSES_STATE_KEY", raising=False)
    if os.name != "posix":
        pytest.skip("POSIX filesystem permissions")
    enc.initialize_key()
    key_file = Path(os.environ["CREDENTIALS_DIR"]) / "responses-state.key"
    key_file.chmod(0o644)
    with pytest.raises(enc.StateKeyError, match="permissions"):
        enc.initialize_key()
    key_file.chmod(0o600)
    actual = tmp_path / "private-key"
    key_file.rename(actual)
    key_file.symlink_to(actual)
    with pytest.raises(enc.StateKeyError, match="regular file"):
        enc.initialize_key()


def test_summary_none_does_not_expose_thinking_in_metadata_or_plaintext():
    text, signature = "SYNTHETIC_PRIVATE_THOUGHT", "SYNTHETIC_PRIVATE_SIGNATURE"
    mapper = mapped([{"thought": True, "text": text, "thoughtSignature": signature}, {"text": "answer"}],
                    reasoning={"summary": "none"}, include=["reasoning.encrypted_content"])
    raw = json.dumps(mapper.final)
    assert text not in raw and signature not in raw
    reason = mapper.final["output"][0]
    assert reason["summary"] == []
    assert enc.decode(reason["encrypted_content"])["t"] == text


@pytest.mark.asyncio
async def test_gemini_function_signature_and_original_call_id_survive_tool_roundtrip():
    model = "gemini-3-flash"
    body = {"model": model, "input": "go", "tools": [{"type": "function", "name": "f",
             "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}]}
    mapper = ResponsesMapper(convert_request(body), account_ref=lambda: ACCOUNT)
    mapper.start_events()
    mapper.feed_gemini({"candidates": [{"content": {"parts": [{
        "functionCall": {"id": "original-call", "name": "f", "args": {"value": "true"}},
        "thoughtSignature": "SYNTHETIC_FUNCTION_SIGNATURE",
    }]}, "finishReason": "STOP"}]})
    mapper.finish()
    call = mapper.final["output"][0]
    assert call["call_id"].startswith(enc.CALL_ID_PREFIX)
    replay = convert_request({**body, "input": [
        {"role": "user", "content": "go"}, call,
        {"type": "function_call_output", "call_id": call["call_id"], "output": "ok"},
    ]})
    payload, _ = await build_gemini_request(replay)
    parts = [p for c in payload["request"]["contents"] for p in c["parts"]]
    function = next(p for p in parts if "functionCall" in p)
    result = next(p for p in parts if "functionResponse" in p)
    assert function["thoughtSignature"] == "SYNTHETIC_FUNCTION_SIGNATURE"
    assert function["functionCall"]["args"] == {"value": "true"}
    assert function["functionCall"]["id"] == result["functionResponse"]["id"] == "original-call"


def test_modified_signed_function_call_is_rejected():
    body = response_body(tools=[{"type": "function", "name": "f"}])
    mapper = ResponsesMapper(convert_request(body), account_ref=lambda: ACCOUNT)
    mapper.start_events()
    mapper.feed_gemini({"candidates": [{"content": {"parts": [{
        "functionCall": {"id": "original-call", "name": "f", "args": {"value": "true"}},
        "thoughtSignature": "SYNTHETIC_FUNCTION_SIGNATURE",
    }]}, "finishReason": "STOP"}]})
    mapper.finish()
    call = mapper.final["output"][0]
    with pytest.raises(ResponsesError, match="modified"):
        convert_request({**body, "input": [{"role": "user", "content": "go"}, {**call, "arguments": '{"value":false}'}]})


@pytest.mark.parametrize("bad", ["data: {broken}\n\n", 'data: [1,2]\n\n', 'data: {"candidates":"bad"}\n\n'])
def test_malformed_sse_cannot_complete_after_a_later_stop(bad):
    mapper = ResponsesMapper(convert_request(response_body()))
    mapper.start_events()
    mapper.feed_sse_line(bad)
    mapper.feed_sse_line('data: {"candidates":[{"finishReason":"STOP"}]}\n\n')
    mapper.finish()
    assert mapper.final["status"] == "failed"


def test_fragmented_sse_utf8_preserves_exact_text():
    mapper = ResponsesMapper(convert_request(response_body()))
    mapper.start_events()
    raw = ('data: ' + json.dumps({"candidates": [{"content": {"parts": [{"text": "合成...  \n"}]},
           "finishReason": "STOP"}]}, ensure_ascii=False) + '\n\n').encode()
    for byte in raw:
        mapper.feed_sse_line(bytes([byte]))
    mapper.finish()
    assert mapper.final_with_output_text()["output_text"] == "合成...  \n"
    assert mapper.final["status"] == "completed"


def test_grounding_and_citation_metadata_are_retained_without_raw_thoughts():
    mapper = ResponsesMapper(convert_request(response_body()))
    grounding = {"groundingChunks": [{"web": {"uri": "https://example.invalid", "title": "synthetic"}}]}
    mapper.start_events()
    mapper.feed_gemini({"candidates": [{"content": {"parts": [{"text": "answer"}]},
        "groundingMetadata": grounding, "finishReason": "STOP"}]})
    mapper.finish()
    assert mapper.final["x_agy_upstream"]["groundingMetadata"] == grounding


def test_output_cap_reports_effective_value_and_namespace_context_survives():
    conv = convert_request(response_body(max_output_tokens=100000, tools=[{
        "type": "namespace", "name": "context", "description": "SYNTHETIC_NAMESPACE_CONTEXT",
        "tools": [{"type": "function", "name": "f", "description": "own"}],
    }]))
    assert conv.echo["max_output_tokens"] == 64000
    assert "max_output_tokens(capped_at_64000)" in conv.ignored
    assert "SYNTHETIC_NAMESPACE_CONTEXT" in conv.chat["tools"][0]["function"]["description"]


@pytest.mark.parametrize("field", ["reasoning", "text", "stream_options"])
def test_unknown_nested_parameters_are_explicitly_rejected(field):
    with pytest.raises(ResponsesError) as error:
        convert_request(response_body(**{field: {"unknown": True}}))
    assert error.value.param == field + ".unknown"


def test_image_detail_is_reported_and_malformed_function_history_rejected():
    conv = convert_request(response_body(input=[{"role": "user", "content": [{
        "type": "input_image", "image_url": "data:image/png;base64,c3ludGhldGlj", "detail": "high",
    }]}]))
    assert "input[0].content[0].detail(not_supported)" in conv.ignored
    with pytest.raises(ResponsesError, match="valid JSON"):
        convert_request(response_body(input=[{"type": "function_call", "call_id": "c", "name": "f", "arguments": '{"bad"'}]))


def _request(body):
    raw = json.dumps(body).encode()
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [],
                    "client": ("127.0.0.1", 1)}, receive)


@pytest.mark.asyncio
async def test_account_mismatch_does_not_contact_upstream_or_drop_history(monkeypatch):
    from src.router.antigravity import responses as route
    blob = enc.encode(MODEL, ACCOUNT, "SYNTHETIC_SIGNATURE", "synthetic thought")
    contacted = []
    async def fake_stream(body, on_credential=None, **kwargs):
        on_credential("other@example.invalid", body)
        contacted.append(True)
        yield 'data: {"candidates":[{"finishReason":"STOP"}]}'
    monkeypatch.setattr(route, "stream_request", fake_stream)
    monkeypatch.delenv("RESPONSES_ALLOW_HISTORY_FALLBACK", raising=False)
    response = await route.create_response(_request(response_body(input=[
        {"role": "user", "content": "go"}, {"type": "reasoning", "encrypted_content": blob},
        {"role": "user", "content": "next"},
    ])), _token="synthetic")
    assert response.status_code == 409
    assert not contacted
    assert json.loads(response.body)["error"]["code"] == "state_account_mismatch"


@pytest.mark.asyncio
async def test_signature_failure_is_not_retried_without_explicit_opt_in(monkeypatch):
    from src.router.antigravity import responses as route
    calls = []
    async def fake_stream(**kwargs):
        calls.append(True)
        yield Response(content='{"error":{"message":"Invalid thinking signature"}}', status_code=400)
    monkeypatch.setattr(route, "stream_request", fake_stream)
    monkeypatch.delenv("RESPONSES_ALLOW_HISTORY_FALLBACK", raising=False)
    response = await route.create_response(_request(response_body()), _token="synthetic")
    assert response.status_code == 400 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_explicit_history_fallback_is_reported_and_releases_each_attempt(monkeypatch, streaming):
    from src.router.antigravity import responses as route
    import copy
    blob = enc.encode(MODEL, ACCOUNT, "SYNTHETIC_SIGNATURE", "synthetic thought")
    attempts, closed = [], []
    async def fake_stream(body, **kwargs):
        attempts.append(copy.deepcopy(body))
        index = len(attempts)
        try:
            if index == 1:
                yield Response(content='{"error":{"message":"Invalid thinking signature"}}', status_code=400)
            else:
                yield 'data: {"candidates":[{"content":{"parts":[{"text":"answer"}]},"finishReason":"STOP"}]}\n\n'
        finally:
            closed.append(index)
    monkeypatch.setattr(route, "stream_request", fake_stream)
    monkeypatch.setenv("RESPONSES_ALLOW_HISTORY_FALLBACK", "1")
    response = await route.create_response(_request(response_body(stream=streaming, input=[
        {"role": "user", "content": "go"}, {"type": "reasoning", "encrypted_content": blob},
        {"role": "user", "content": "next"},
    ])), _token="synthetic")
    assert response.status_code == 200
    assert "thinking_history_dropped" in response.headers["x-agy-ignored-params"]
    if streaming:
        chunks = [chunk async for chunk in response.body_iterator]
        final = json.loads(next(line[6:] for chunk in chunks for line in chunk.decode().splitlines()
                                if line.startswith('data: ') and '"type":"response.completed"' in line))["response"]
    else:
        final = json.loads(response.body)
    assert "thinking_history_dropped" in final["x_agy_ignored_params"]
    assert len(attempts) == 2 and sorted(closed) == [1, 2]
    assert any(p.get("thought") for c in attempts[0]["request"]["contents"] for p in c["parts"])
    assert not any(p.get("thought") for c in attempts[1]["request"]["contents"] for p in c["parts"])


@pytest.mark.asyncio
async def test_response_generator_close_releases_prefetched_upstream(monkeypatch):
    from src.router.antigravity import responses as route
    closed = []
    async def fake_stream(**kwargs):
        try:
            yield 'data: {"candidates":[{"content":{"parts":[{"text":"first"}]}}]}\n\n'
            yield 'data: {"candidates":[{"finishReason":"STOP"}]}\n\n'
        finally:
            closed.append(True)
    monkeypatch.setattr(route, "stream_request", fake_stream)
    response = await route.create_response(_request(response_body(stream=True)), _token="synthetic")
    await response.body_iterator.__anext__()
    await response.body_iterator.aclose()
    assert closed == [True]


def test_invalid_frame_cannot_deliver_later_tool_events():
    mapper = ResponsesMapper(convert_request(response_body()))
    mapper.start_events()
    events = mapper.feed_sse_line('data: {bad}\n\ndata: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"f","args":{}}}]},"finishReason":"STOP"}]}\n\n')
    mapper.finish()
    assert mapper.final["status"] == "failed"
    assert not any(event["type"].startswith("response.function_call") for event in events)
    assert not any(item["type"] == "function_call" for item in mapper.final["output"])


@pytest.mark.parametrize("part", [
    {"inlineData": {"mimeType": "image/png", "data": "SYNTHETIC_PUBLIC_IMAGE"}},
    {"executableCode": {"language": "PYTHON", "code": "print('synthetic public code')"}},
    {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "synthetic public result"}},
])
@pytest.mark.parametrize("summary", ["none", "auto"])
def test_multimodal_metadata_cannot_expose_raw_signatures_or_replay_state(part, summary):
    import copy
    private = "SYNTHETIC_PRIVATE_MULTIMODAL_SIGNATURE"
    public_part = copy.deepcopy(part)
    part = {**part, "thoughtSignature": private, "encrypted_content": private, "private_state": private}
    mapper = mapped([part], reasoning={"summary": summary})
    assert mapper.final["status"] == "completed"
    assert mapper.final["x_agy_upstream"]["multimodal_parts"] == [public_part]
    assert private not in json.dumps(mapper.final)
    assert "upstream.multimodal_private_fields(not_replayed)" in mapper.final["x_agy_ignored_params"]


def test_nested_signature_and_thought_siblings_are_omitted_but_public_structure_remains():
    private = "SYNTHETIC_PRIVATE_NESTED_STATE"
    part = {"inlineData": {"mimeType": "image/png", "data": "SYNTHETIC_PUBLIC_IMAGE", "extra": {
        "public": "keep", "thought_signature": private,
        "siblings": [{"type": "reasoning", "text": private},
                     {"thought": True, "text": private},
                     {"url": "https://example.invalid", "signature_delta": private}],
    }}}
    mapper = mapped([part], reasoning={"summary": "none"})
    kept = mapper.final["x_agy_upstream"]["multimodal_parts"][0]["inlineData"]
    assert kept == {"mimeType": "image/png", "data": "SYNTHETIC_PUBLIC_IMAGE", "extra": {
        "public": "keep", "siblings": [{"url": "https://example.invalid"}],
    }}
    assert private not in json.dumps(mapper.final)


def test_other_metadata_fields_use_the_same_recursive_private_state_boundary():
    private = "SYNTHETIC_PRIVATE_METADATA_STATE"
    mapper = ResponsesMapper(convert_request(response_body(reasoning={"summary": "none"})))
    mapper.start_events()
    mapper.feed_gemini({"promptFeedback": {"public": "keep", "private_state": private},
        "candidates": [{"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP",
                        "groundingMetadata": {"sources": [{"url": "https://example.invalid",
                                                            "thoughtSignature": private,
                                                            "detail": {"thinking_text": private}}]}}]})
    mapper.finish()
    assert private not in json.dumps(mapper.final)
    assert mapper.final["x_agy_upstream"]["promptFeedback"] == {"public": "keep"}
    assert mapper.final["x_agy_upstream"]["groundingMetadata"] == {
        "sources": [{"url": "https://example.invalid", "detail": {}}]}
    assert "upstream.metadata_private_fields(omitted)" in mapper.final["x_agy_ignored_params"]
