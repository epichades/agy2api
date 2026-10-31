"""Chat choices keep independent tool numbering and finish reasons."""

import json

import pytest

from src.converter.openai2gemini import _STREAM_TOOL_INDEX
from src.router.antigravity.openai import _chat_stream


def _tool(index, name):
    return {"index": index, "content": {"parts": [{"functionCall": {
        "name": name, "id": "synthetic-" + name, "args": {"name": name},
    }}]}}


def _frame(*candidates):
    return ("data: " + json.dumps({"candidates": candidates}) + "\n\n").encode()


async def _events(frames):
    async def source():
        for frame in frames:
            yield frame
        yield b"data: [DONE]\n\n"

    chunks = [chunk async for chunk in _chat_stream(source(), "gemini-test", False, [])]
    assert chunks[-1] == b"data: [DONE]\n\n"
    events = [json.loads(chunk[6:]) for chunk in chunks[:-1]]
    assert not any("error" in event for event in events)
    assert events[-1]["id"] not in _STREAM_TOOL_INDEX
    return [choice for event in events for choice in event["choices"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_together", [False, True])
async def test_plain_choice_does_not_inherit_another_choices_tool_finish(finish_together):
    frames = [_frame(_tool(0, "first"), {"index": 1, "content": {"parts": [{"text": "plain"}]}})]
    if finish_together:
        frames += [_frame({"index": 1, "finishReason": "STOP"}, {"index": 0, "finishReason": "STOP"})]
    else:
        frames += [_frame({"index": 1, "finishReason": "STOP"}), _frame({"index": 0, "finishReason": "STOP"})]
    choices = await _events(frames)
    finishes = {choice["index"]: choice["finish_reason"] for choice in choices if choice["finish_reason"]}
    assert finishes == {0: "tool_calls", 1: "stop"}


@pytest.mark.asyncio
async def test_finished_choice_does_not_reset_another_choices_tool_indices():
    choices = await _events([
        _frame(_tool(0, "zero_first"), _tool(1, "one_first")),
        _frame(_tool(0, "zero_second")),
        _frame({"index": 0, "finishReason": "STOP"}),
        _frame(_tool(1, "one_second")),
        _frame({"index": 1, "finishReason": "STOP"}),
    ])
    for index, names in ((0, ["zero_first", "zero_second"]), (1, ["one_first", "one_second"])):
        calls = [call for choice in choices if choice["index"] == index
                 for call in choice["delta"].get("tool_calls", [])]
        assert [call["index"] for call in calls] == [0, 1]
        assert [call["function"]["name"] for call in calls] == names
    assert [choice["finish_reason"] for choice in choices if choice["finish_reason"]] == ["tool_calls", "tool_calls"]


@pytest.mark.asyncio
async def test_cancelling_multiple_choices_releases_all_tool_state_and_source():
    closed = []

    async def source():
        try:
            yield _frame(_tool(0, "first"), _tool(1, "second"))
            pytest.fail("Cancellation must not consume another upstream frame")
        finally:
            closed.append(True)

    stream = _chat_stream(source(), "gemini-test", False, [])
    first = json.loads((await anext(stream))[6:])
    assert first["id"] in _STREAM_TOOL_INDEX
    await stream.aclose()
    assert first["id"] not in _STREAM_TOOL_INDEX
    assert closed == [True]
