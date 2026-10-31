"""Normalization preserves supported payload values and rejects unsupported prefill."""
import copy

import pytest

from src.converter.antigravity_fix import normalize_antigravity_request


@pytest.mark.asyncio
async def test_normalizer_keeps_text_signatures_limits_and_all_tools():
    request = {
        "model": "claude-opus-5-5-high",
        "contents": [
            {"role": "model", "parts": [{"thought": True, "text": "signed  \n", "thoughtSignature": "SYNTHETIC_SIGNATURE"},
                                          {"functionCall": {"id": "call_1", "name": "one", "args": {"value": None}}}]},
            {"role": "user", "parts": [{"functionResponse": {"id": "call_1", "name": "one", "response": {"output": "ok"}}},
                                         {"text": "user  \n"}]},
        ],
        "generationConfig": {"maxOutputTokens": 24, "topK": 3, "stopSequences": ["END"]},
        "tools": [{"functionDeclarations": [{"name": "one", "parameters": {"type": "object"}},
                                               {"name": "two", "parameters": {"type": "object"}}]}],
    }
    before = copy.deepcopy(request)
    result = await normalize_antigravity_request(request)
    assert request == before
    assert result["contents"][0]["parts"][0] == request["contents"][0]["parts"][0]
    assert result["contents"][-1]["parts"][-1]["text"] == "user  \n"
    assert result["generationConfig"]["maxOutputTokens"] == 24
    assert result["generationConfig"]["topK"] == 3
    assert result["generationConfig"]["stopSequences"] == ["END"]
    assert "thinkingConfig" in result["generationConfig"]
    assert [d["name"] for t in result["tools"] for d in t["functionDeclarations"]] == ["one", "two"]


@pytest.mark.asyncio
async def test_normalizer_preserves_gemini_signature_and_empty_signed_part():
    result = await normalize_antigravity_request({"model": "gemini-test", "contents": [
        {"role": "model", "parts": [{"text": "", "thought": True, "thought_signature": "SYNTHETIC_GEMINI_SIG"}]},
        {"role": "user", "parts": [{"text": "continue"}]},
    ]})
    part = result["contents"][0]["parts"][0]
    assert part == {"text": "", "thought": True, "thoughtSignature": "SYNTHETIC_GEMINI_SIG"}


@pytest.mark.asyncio
async def test_prefill_is_rejected_instead_of_silently_deleted():
    with pytest.raises(ValueError, match="prefill"):
        await normalize_antigravity_request({"model": "claude-test", "contents": [
            {"role": "user", "parts": [{"text": "prompt"}]},
            {"role": "model", "parts": [{"text": "unfinished"}]},
        ]})


@pytest.mark.asyncio
async def test_shared_system_merge_preserves_whitespace_content():
    from src.converter.utils import merge_system_messages
    original = {"system": [{"type": "text", "text": " \n"}], "messages": [
        {"role": "developer", "content": "\t"},
        {"role": "user", "content": "prompt"},
    ]}
    before = copy.deepcopy(original)
    merged = await merge_system_messages(original)
    assert original == before
    assert merged["systemInstruction"]["parts"] == [{"text": " \n"}, {"text": "\t"}]


def test_shared_response_extraction_keeps_literal_ellipsis():
    from src.converter.utils import extract_content_and_reasoning
    text, reasoning, _images = extract_content_and_reasoning([
        {"text": "..."}, {"text": "…", "thought": True}, {"text": " \n"},
    ])
    assert text == "... \n"
    assert reasoning == "…"
