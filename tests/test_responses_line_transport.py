"""Exercise the Responses route with the real httpx line-stream boundary."""

from contextlib import asynccontextmanager
import json

from fastapi import Request
import httpx
import pytest


def _request(streaming):
    body = json.dumps({"model": "gemini-test", "input": "synthetic request", "stream": streaming}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [],
                    "client": ("127.0.0.1", 1)}, receive)


async def _run_line_transport(monkeypatch, streaming, wire):
    from src import httpx_client
    from src.router.antigravity import responses as route

    transported = []

    @asynccontextmanager
    async def client(**kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=wire))
        async with httpx.AsyncClient(transport=transport) as instance:
            yield instance

    async def upstream(body, native, **kwargs):
        assert native is False
        async for line in httpx_client.stream_post_async(
            "https://synthetic.invalid/stream", body, native=native,
        ):
            transported.append(line)
            yield line

    monkeypatch.setattr(httpx_client, "get_streaming_client", client)
    monkeypatch.setattr(route, "stream_request", upstream)
    response = await route.create_response(_request(streaming), _token="synthetic")
    if streaming:
        chunks = [chunk async for chunk in response.body_iterator]
        events = [json.loads(line[6:]) for chunk in chunks for line in chunk.decode().splitlines()
                  if line.startswith("data: ")]
        result = events[-1]["response"]
    else:
        events = []
        result = json.loads(response.body)
    assert all(isinstance(line, str) and "\n" not in line for line in transported)
    assert "" in transported  # Real aiter_lines exposes empty event separators.
    return response, result, events


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("prefix,multiline", [
    (": keepalive\n\n", False),
    ("event: message\nid: synthetic-event\nretry: 1000\n", False),
    (": keepalive\r\n\r\nevent: message\r\n", True),
])
async def test_responses_accepts_real_line_stream_comments_and_metadata(monkeypatch, streaming, prefix, multiline):
    candidate = {"content": {"parts": [{"text": "合成...  \n"}]}, "finishReason": "STOP"}
    if multiline:
        data = 'data: {"candidates":\ndata: ' + json.dumps([candidate], ensure_ascii=False) + '}\n\n'
    else:
        data = "data: " + json.dumps({"candidates": [candidate]}, ensure_ascii=False) + "\n\n"
    usage = {"usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}}
    wire = (prefix + data + ": after-text\n\n" + "data: " + json.dumps(usage) +
            "\n\ndata: [DONE]\n\n").encode()
    response, result, events = await _run_line_transport(monkeypatch, streaming, wire)
    assert response.status_code == 200
    assert result["status"] == "completed" and result["error"] is None
    assert result["output"][0]["content"][0]["text"] == "合成...  \n"
    assert result["usage"]["total_tokens"] == 5
    if streaming:
        assert events[-1]["type"] == "response.completed"
        assert not any(event["type"] == "response.failed" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_responses_real_line_stream_still_rejects_malformed_json(monkeypatch, streaming):
    wire = b': keepalive\n\ndata: {broken}\n\ndata: {"candidates":[{"finishReason":"STOP"}]}\n\n'
    response, result, events = await _run_line_transport(monkeypatch, streaming, wire)
    assert response.status_code == (200 if streaming else 502)
    assert result["status"] == "failed"
    assert result["error"]["message"] == "Malformed upstream SSE JSON"
    if streaming:
        assert events[-1]["type"] == "response.failed"


def test_responses_fragmented_bytes_keep_comments_separate_from_data():
    from src.responses.mapper import ResponsesMapper
    from src.responses.request import convert_request

    mapper = ResponsesMapper(convert_request({"model": "gemini-test", "input": "synthetic request"}))
    mapper.start_events()
    raw = (': keepalive\n\nevent: message\ndata: ' + json.dumps({"candidates": [{
        "content": {"parts": [{"text": "中文  "}]}, "finishReason": "STOP",
    }]}, ensure_ascii=False) + '\n\n').encode()
    for value in raw:
        mapper.feed_sse_line(bytes([value]))
    mapper.finish()
    assert mapper.final["status"] == "completed"
    assert mapper.final_with_output_text()["output_text"] == "中文  "
