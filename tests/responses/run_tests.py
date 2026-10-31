"""
[local patch] Offline tests for the Responses API implementation (no upstream calls).

Run (project venv, from repo root, with the instance .env sourced so config can init):
    set -a; source .env; set +a; .venv/bin/python tests/responses/run_tests.py
Exit code 0 = all passed.
"""
import asyncio
import copy
import inspect
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
FIX = Path(__file__).parent / "fixtures"

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ======================================================================= contract
@test
def contract_upstream_signatures():
    from src.responses import upstream_api as u
    expect = {
        "convert_openai_to_gemini_request": ["openai_request"],
        "normalize_antigravity_request": ["request"],
        "_convert_usage_metadata": ["usage_metadata"],
        "_reverse_transform_args": ["args"],
        "_normalize_function_name": ["name"],
        "resolve_profile": ["model"],
        "_apply_thinking": ["profile", "generation_config", "return_thoughts"],
        "apply_anti_truncation": ["payload"],
        "classify": ["model_name"],
    }
    for name, params in expect.items():
        got = list(inspect.signature(getattr(u, name)).parameters)
        assert got[:len(params)] == params, (name, got)
    ps = inspect.signature(u.stream_request).parameters
    assert "session_key_override" in ps and "on_credential" in ps
    assert u.SKIP_THOUGHT_SIGNATURE_VALIDATOR == "skip_thought_signature_validator"
    assert u.MAX_OUTPUT_TOKENS == 64000


# ======================================================================= request equivalence
async def chat_payload(chat):
    from src.converter.openai2gemini import convert_openai_to_gemini_request
    from src.converter.antigravity_fix import normalize_antigravity_request
    g = await convert_openai_to_gemini_request(copy.deepcopy(chat))
    g["model"] = chat["model"]
    n = await normalize_antigravity_request(g)
    return {"model": n.pop("model"), "request": n}


async def resp_payload(body):
    from src.responses.request import convert_request, build_gemini_request
    conv = convert_request(body)
    api, info = await build_gemini_request(conv)
    return api, info, conv


def strip_volatile(p):
    return json.loads(json.dumps(p))


def assert_tool_result_text_difference(chat, responses, call_id, text, chat_result):
    """Validate one documented difference, then compare all other payload fields.

    Responses preserves opaque tool-output text; the existing Chat adapter
    parses JSON-looking results. Do not discard functionResponse fields or
    normalize unasserted differences merely to make the equivalence test pass.
    """
    def result(payload):
        matches = [part["functionResponse"]
                   for content in payload["request"]["contents"]
                   for part in content["parts"]
                   if part.get("functionResponse", {}).get("id") == call_id]
        assert len(matches) == 1, (call_id, matches)
        return matches[0]

    c, r = result(chat), result(responses)
    assert c["response"] == chat_result, c
    assert r["response"] == {"result": text}, r
    c["response"] = {"result": text}


TOOLS_R = [
    {"type": "function", "name": "calc", "description": "calc", "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}},
    {"type": "function", "name": "get_weather", "description": "weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
]
TOOLS_C = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in TOOLS_R]
IMG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

PAIRS = [
    ("string input + sampling", "gemini-3-flash",
     {"input": "hello", "temperature": 0.3, "top_p": 0.9},
     {"messages": [{"role": "user", "content": "hello"}], "temperature": 0.3, "top_p": 0.9}),
    ("instructions + developer + multi-turn", "gemini-3-flash",
     {"instructions": "Be terse.", "input": [
         {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Dev rule."}]},
         {"role": "user", "content": "Q1"},
         {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "A1"}]},
         {"role": "user", "content": [{"type": "input_text", "text": "Q2"}]}]},
     {"messages": [{"role": "system", "content": "Be terse."},
                   {"role": "developer", "content": [{"type": "text", "text": "Dev rule."}]},
                   {"role": "user", "content": "Q1"},
                   {"role": "assistant", "content": [{"type": "text", "text": "A1"}]},
                   {"role": "user", "content": [{"type": "text", "text": "Q2"}]}]}),
    ("mid-conversation system demoted like chat", "gemini-3-flash",
     {"input": [{"role": "user", "content": "Q1"}, {"role": "system", "content": "late system"}, {"role": "user", "content": "Q2"}]},
     {"messages": [{"role": "user", "content": "Q1"}, {"role": "system", "content": "late system"}, {"role": "user", "content": "Q2"}]}),
    ("image data URL", "gemini-3-flash",
     {"input": [{"role": "user", "content": [{"type": "input_text", "text": "what?"}, {"type": "input_image", "image_url": IMG}]}]},
     {"messages": [{"role": "user", "content": [{"type": "text", "text": "what?"}, {"type": "image_url", "image_url": {"url": IMG}}]}]}),
    ("tools + parallel function calls + outputs (gemini)", "gemini-3-flash",
     {"input": [{"role": "user", "content": "weather x2"},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "checking"}]},
                {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": "{\"city\":\"A\"}"},
                {"type": "function_call", "call_id": "c2", "name": "get_weather", "arguments": "{\"city\":\"B\"}"},
                {"type": "function_call_output", "call_id": "c1", "output": "sunny"},
                {"type": "function_call_output", "call_id": "c2", "output": "{\"t\":3}"}],
      "tools": TOOLS_R, "tool_choice": "auto"},
     {"messages": [{"role": "user", "content": "weather x2"},
                   {"role": "assistant", "content": [{"type": "text", "text": "checking"}], "tool_calls": [
                       {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"A\"}"}},
                       {"id": "c2", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"B\"}"}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
                   {"role": "tool", "tool_call_id": "c2", "content": "{\"t\":3}"}],
      "tools": TOOLS_C, "tool_choice": "auto"}),
    ("tool_choice function", "gemini-3-flash",
     {"input": "x", "tools": TOOLS_R, "tool_choice": {"type": "function", "name": "calc"}},
     {"messages": [{"role": "user", "content": "x"}], "tools": TOOLS_C, "tool_choice": {"type": "function", "function": {"name": "calc"}}}),
    ("json_schema text.format (chat dict built directly: pydantic would drop it)", "gemini-3-flash",
     {"input": "x", "text": {"format": {"type": "json_schema", "name": "o", "schema": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}}}},
     {"messages": [{"role": "user", "content": "x"}], "response_format": {"type": "json_schema", "json_schema": {"name": "o", "schema": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}}}}),
    ("claude no tools", "claude-opus-5-5-low",
     {"input": [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"}, {"role": "user", "content": "Q2"}]},
     {"messages": [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": [{"type": "text", "text": "A1"}]}, {"role": "user", "content": "Q2"}]}),
]


@test
def request_equivalence_pairs():
    for label, model, rbody, chat in PAIRS:
        rb = {"model": model, **rbody}
        cb = {"model": model, **chat}
        c = strip_volatile(run(chat_payload(cb)))
        r, info, conv = run(resp_payload(rb))
        r = strip_volatile(r)
        if label == "tools + parallel function calls + outputs (gemini)":
            assert_tool_result_text_difference(c, r, "c1", "sunny", {"result": "sunny"})
            assert_tool_result_text_difference(c, r, "c2", '{"t":3}', {"t": 3})
        assert c == r, f"{label}:\nchat={json.dumps(c)[:1500]}\nresp={json.dumps(r)[:1500]}"
        if "json_schema" in label:
            gc = r["request"]["generationConfig"]
            assert gc.get("responseMimeType") == "application/json" and "responseSchema" in gc, gc


@test
def request_equivalence_claude_tools_documented_diff():
    """Claude + tool history: the ONLY differences vs chat are the intended ones."""
    from src.converter.thoughtSignature_fix import SKIP_THOUGHT_SIGNATURE_VALIDATOR as S
    rb = {"model": "claude-opus-5-5-high", "input": [
        {"role": "user", "content": "go"},
        {"type": "function_call", "call_id": "toolu_1", "name": "calc", "arguments": "{\"expr\":\"1+1\"}"},
        {"type": "function_call_output", "call_id": "toolu_1", "output": "2"}], "tools": TOOLS_R}
    cb = {"model": "claude-opus-5-5-high", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "calc", "arguments": "{\"expr\":\"1+1\"}"}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "2"}], "tools": TOOLS_C}
    c = strip_volatile(run(chat_payload(cb)))
    r, info, conv = run(resp_payload(rb))
    r = strip_volatile(r)
    # Shared normalization now preserves all tools and keeps thinking with tools.
    assert [d["name"] for t in c["request"]["tools"] for d in t["functionDeclarations"]] == ["calc", "get_weather"]
    assert r["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
    assert_tool_result_text_difference(c, r, "toolu_1", "2", {"result": 2})
    assert c == r, f"\nchat={json.dumps(c)[:2000]}\nresp={json.dumps(r)[:2000]}"



@test
def request_equivalence_flag_off_matches_chat_except_tool_split():
    os.environ["RESPONSES_CLAUDE_THINKING_WITH_TOOLS"] = "0"
    try:
        rb = {"model": "claude-opus-5-5-high", "input": "hi", "tools": TOOLS_R[:1]}
        cb = {"model": "claude-opus-5-5-high", "messages": [{"role": "user", "content": "hi"}], "tools": TOOLS_C[:1]}
        c = strip_volatile(run(chat_payload(cb)))
        r, info, conv = run(resp_payload(rb))
        assert c == strip_volatile(r)
    finally:
        os.environ.pop("RESPONSES_CLAUDE_THINKING_WITH_TOOLS", None)


@test
def max_output_tokens_applied_after_normalize():
    r, info, conv = run(resp_payload({"model": "gemini-3-flash", "input": "x", "max_output_tokens": 77}))
    assert r["request"]["generationConfig"]["maxOutputTokens"] == 77
    c = run(chat_payload({"model": "gemini-3-flash", "messages": [{"role": "user", "content": "x"}], "max_tokens": 77}))
    assert c["request"]["generationConfig"]["maxOutputTokens"] == 77
    r, *_ = run(resp_payload({"model": "gemini-3-flash", "input": "x", "max_output_tokens": 999999}))
    assert r["request"]["generationConfig"]["maxOutputTokens"] == 64000


@test
def reasoning_replay_order_and_signature():
    from src.responses import encrypted as enc
    b1 = enc.encode("claude-opus-5-5-high", "acct@example.com", "SIG1", "thinking one")
    b2 = enc.encode("claude-opus-5-5-high", "acct@example.com", "SIG2", "thinking two")
    rb = {"model": "claude-opus-5-5-high", "include": ["reasoning.encrypted_content"], "tools": TOOLS_R, "input": [
        {"role": "user", "content": "go"},
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "thinking one"}], "encrypted_content": b1},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "first"}]},
        {"type": "function_call", "call_id": "t1", "name": "calc", "arguments": "{\"expr\":\"1\"}"},
        {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": b2},
        {"type": "function_call", "call_id": "t2", "name": "get_weather", "arguments": "{\"city\":\"X\"}"},
        {"type": "function_call_output", "call_id": "t1", "output": "1"},
        {"type": "function_call_output", "call_id": "t2", "output": "ok"}]}
    r, info, conv = run(resp_payload(rb))
    model_parts = [c for c in r["request"]["contents"] if c["role"] == "model"]
    assert len(model_parts) == 1
    seq = [("thought:" + p["thoughtSignature"]) if p.get("thought") else ("fc:" + p["functionCall"]["name"]) if "functionCall" in p else ("text:" + p["text"]) for p in model_parts[0]["parts"]]
    assert seq == ["thought:SIG1", "text:first", "fc:calc", "thought:SIG2", "fc:get_weather"], seq
    assert all("thoughtSignature" not in p for p in model_parts[0]["parts"] if "functionCall" in p)
    assert info["replayed"] == 2
    # wrong model / corrupted blob -> dropped and reported, never sent
    rb2 = copy.deepcopy(rb)
    rb2["model"] = "claude-opus-5-5-low"
    from src.responses.request import ResponsesError
    try:
        run(resp_payload(rb2))
    except ResponsesError as error:
        assert error.code == "state_model_mismatch"
    else:
        raise AssertionError("wrong-model replay must be rejected")
    assert enc.decode(b1[:-4] + "AAAA") is None
    # non-claude: not replayed, reported
    rb3 = copy.deepcopy(rb)
    rb3["model"] = "gemini-3-flash"
    try:
        run(resp_payload(rb3))
    except ResponsesError as error:
        assert error.code == "state_model_mismatch"
    else:
        raise AssertionError("cross-model replay must be rejected")


@test
def request_rejections_and_ignores():
    from src.responses.request import ResponsesError, convert_request
    bad = [
        ({"previous_response_id": "resp_1"}, "previous_response_id"),
        ({"conversation": "c1"}, "conversation"),
        ({"background": True}, "background"),
        ({"tools": [{"type": "web_search"}]}, "tools[0].type"),
        ({"tools": [{"type": "web_search_preview"}]}, "tools[0].type"),
        ({"tools": [{"type": "code_interpreter", "container": {"type": "auto"}}]}, "tools[0].type"),
        ({"input": [{"type": "item_reference", "id": "x"}]}, "input[0].type"),
        ({"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}]}, "input[0].content[0]"),
        ({"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://x/y.png"}]}]}, "input[0].content[0].image_url"),
        ({"some_new_field": 1}, "some_new_field"),
    ]
    for extra, param in bad:
        try:
            convert_request({"model": "gemini-3-flash", "input": "x", **extra})
        except ResponsesError as e:
            assert e.status == 400 and e.param == param, (extra, e.param, e.message)
        else:
            raise AssertionError(f"expected 400 for {extra}")
    conv = convert_request({"model": "claude-opus-5-5-high", "input": "x", "reasoning": {"effort": "high", "summary": "auto"},
                            "text": {"verbosity": "low"}, "parallel_tool_calls": False, "store": True,
                            "service_tier": "priority", "safety_identifier": "u", "user": "u", "prompt_cache_retention": "24h",
                            "truncation": "auto", "metadata": {"k": "v"}, "client_metadata": {"a": "b"},
                            "include": ["reasoning.encrypted_content", "message.output_text.logprobs"]})
    for k in ("reasoning.effort", "text.verbosity", "parallel_tool_calls", "store(not_stored)", "service_tier",
              "safety_identifier", "user", "prompt_cache_retention", "truncation=auto", "client_metadata",
              "include:message.output_text.logprobs"):
        assert k in conv.ignored, (k, conv.ignored)
    assert conv.real_model == "claude-opus-5-5-high" and conv.echo["metadata"] == {"k": "v"} and conv.echo["store"] is False
    conv = convert_request({"model": "x", "input": "y", "reasoning": {"summary": "none"}})
    assert conv.summary_enabled is False


@test
def custom_and_namespace_tools_bidirectional():
    from src.responses.request import convert_request
    from src.responses.mapper import ResponsesMapper
    body = {"model": "gemini-3-flash", "tools": [
        {"type": "custom", "name": "apply_patch", "description": "patch", "format": {"type": "grammar", "syntax": "lark", "definition": "start: x"}},
        {"type": "namespace", "name": "multi_agent_v1", "description": "ns", "tools": [
            {"type": "function", "name": "spawn_agent", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}}}]}],
        "input": [{"role": "user", "content": "x"},
                  {"type": "custom_tool_call", "call_id": "k1", "name": "apply_patch", "input": "*** Begin Patch\n"},
                  {"type": "custom_tool_call_output", "call_id": "k1", "output": "done"},
                  {"type": "function_call", "call_id": "k2", "name": "spawn_agent", "namespace": "multi_agent_v1", "arguments": "{\"task\":\"t\"}"},
                  {"type": "function_call_output", "call_id": "k2", "output": "ok"}]}
    conv = convert_request(body)
    names = [t["function"]["name"] for t in conv.chat["tools"]]
    assert names == ["apply_patch", "multi_agent_v1__spawn_agent"], names
    assert conv.chat["tools"][0]["function"]["parameters"]["required"] == ["input"]
    tcs = [tc for m in conv.chat["messages"] if m.get("tool_calls") for tc in m["tool_calls"]]
    assert json.loads(tcs[0]["function"]["arguments"]) == {"input": "*** Begin Patch\n"}
    assert tcs[1]["function"]["name"] == "multi_agent_v1__spawn_agent"
    assert "tools[].format.grammar(not_enforced)" in conv.ignored
    m = ResponsesMapper(conv)
    m.start_events()
    evs = m.feed_gemini({"candidates": [{"content": {"parts": [
        {"functionCall": {"id": "x1", "name": "apply_patch", "args": {"input": "*** Begin Patch\n*** End Patch"}}},
        {"functionCall": {"id": "x2", "name": "multi_agent_v1__spawn_agent", "args": {"task": "t"}}}]}, "finishReason": "STOP"}]})
    evs += m.finish()
    out = m.final["output"]
    assert out[0]["type"] == "custom_tool_call" and out[0]["name"] == "apply_patch" and out[0]["input"] == "*** Begin Patch\n*** End Patch"
    assert out[1]["type"] == "function_call" and out[1]["name"] == "spawn_agent" and out[1]["namespace"] == "multi_agent_v1"
    assert any(e["type"] == "response.custom_tool_call_input.done" for e in evs)


# ======================================================================= response superset
def load_lines(name):
    return [l for l in (FIX / name).read_text().splitlines() if l.startswith("data: ")]


def chat_stream_collect(lines, model="m"):
    from src.converter.openai2gemini import convert_gemini_to_openai_stream
    rid = "chatcmpl-test-" + os.urandom(4).hex()
    content, reasoning, calls, usage, finish = "", "", [], None, None
    for l in lines:
        s = convert_gemini_to_openai_stream(l, model, rid)
        if not s:
            continue
        d = json.loads(s[len("data: "):])
        for ch in d["choices"]:
            de = ch["delta"]
            content += de.get("content") or ""
            reasoning += de.get("reasoning_content") or ""
            for tc in de.get("tool_calls") or []:
                calls.append((tc["id"], tc["function"]["name"], json.loads(tc["function"]["arguments"])))
            finish = ch.get("finish_reason") or finish
        usage = d.get("usage") or usage
    return content, reasoning, calls, usage, finish


def resp_stream_collect(lines, model="m", summary=True, encrypted=False):
    from src.responses.request import convert_request
    from src.responses.mapper import ResponsesMapper
    body = {"model": model, "input": "x", "reasoning": {"summary": "auto" if summary else "none"}}
    if encrypted:
        body["include"] = ["reasoning.encrypted_content"]
    conv = convert_request(body)
    m = ResponsesMapper(conv, account_ref=lambda: "acct@example.com")
    evs = m.start_events()
    for l in lines:
        evs += m.feed_sse_line(l)
    evs += m.finish()
    return m, evs


def usage_eq(chat_u, resp_u):
    if chat_u is None:
        return resp_u is None
    return (chat_u["prompt_tokens"] == resp_u["input_tokens"] and chat_u["completion_tokens"] == resp_u["output_tokens"]
            and chat_u["total_tokens"] == resp_u["total_tokens"]
            and (chat_u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) == resp_u["input_tokens_details"]["cached_tokens"]
            and (chat_u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) == resp_u["output_tokens_details"]["reasoning_tokens"])


@test
def response_superset_recorded_fixtures():
    for fx, model in (("claude_thinking_tool.sse", "claude-opus-5-5-high"), ("gemini_text.sse", "gemini-3-flash"),
                      ("gemini_parallel_tools.sse", "gemini-3-flash")):
        lines = load_lines(fx)
        content, reasoning, calls, usage, finish = chat_stream_collect(lines, model)
        m, evs = resp_stream_collect(lines, model, encrypted=True)
        out = m.final["output"]
        r_text = "".join(p["text"] for it in out if it["type"] == "message" for p in it["content"])
        r_reason = "".join(s["text"] for it in out if it["type"] == "reasoning" for s in it["summary"])
        from src.responses import encrypted as enc
        def original_call_id(value):
            if value.startswith(enc.CALL_ID_PREFIX):
                return enc.decode(enc.PREFIX + value[len(enc.CALL_ID_PREFIX):])["id"]
            return value
        r_calls = [(original_call_id(it["call_id"]), it["name"], json.loads(it["arguments"])) for it in out if it["type"] == "function_call"]
        assert r_text == content, (fx, r_text, content)
        assert r_reason == reasoning, (fx, r_reason, reasoning)
        assert r_calls == calls, (fx, r_calls, calls)
        assert usage_eq(usage, m.final["usage"]), (fx, usage, m.final["usage"])
        assert m.final["status"] == "completed" and finish in ("stop", "tool_calls"), (fx, finish)
        assert m.model_version == model
        validate_events(evs)
        if fx.startswith("claude"):
            kinds = [it["type"] for it in out]
            assert kinds == ["reasoning", "message", "function_call"], kinds  # order preserved
            assert out[0]["encrypted_content"].startswith("agy2."), out[0]
        if fx == "gemini_parallel_tools.sse":
            assert len(r_calls) == 2 and r_calls[0][0] != r_calls[1][0]


@test
def response_nonstream_matches_chat():
    from src.converter.openai2gemini import convert_gemini_to_openai_response
    from src.responses.request import convert_request
    from src.responses.mapper import ResponsesMapper
    for fx, model in (("claude_thinking_tool.sse", "claude-opus-5-5-high"), ("gemini_parallel_tools.sse", "gemini-3-flash")):
        parts, finish, usage = [], None, None
        for l in load_lines(fx):
            r = json.loads(l[6:])["response"]
            for c in r.get("candidates", []):
                parts += c.get("content", {}).get("parts", [])
                finish = c.get("finishReason") or finish
            usage = r.get("usageMetadata") or usage
        body = {"response": {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
                             "usageMetadata": usage, "modelVersion": model}}
        chat = convert_gemini_to_openai_response(copy.deepcopy(body), model, 200)
        m = ResponsesMapper(convert_request({"model": model, "input": "x"}), account_ref=lambda: "synthetic@example.invalid")
        m.start_events(); m.feed_gemini(body); m.finish()
        f = m.final_with_output_text()
        msg = chat["choices"][0]["message"]
        assert f["output_text"] == (msg.get("content") or ""), (f["output_text"], msg.get("content"))
        calls = [(t["id"], t["function"]["name"], json.loads(t["function"]["arguments"])) for t in msg.get("tool_calls") or []]
        from src.responses import encrypted as enc
        def original_id(value):
            return enc.decode(enc.PREFIX + value[len(enc.CALL_ID_PREFIX):])["id"] if value.startswith(enc.CALL_ID_PREFIX) else value
        assert calls == [(original_id(i["call_id"]), i["name"], json.loads(i["arguments"])) for i in f["output"] if i["type"] == "function_call"]
        assert usage_eq(chat.get("usage"), f["usage"])


def synth(parts, finish=None, usage=None):
    c = {"content": {"role": "model", "parts": parts}}
    if finish:
        c["finishReason"] = finish
    r = {"candidates": [c], "modelVersion": "gemini-3-flash"}
    if usage:
        r["usageMetadata"] = usage
    return "data: " + json.dumps({"response": r})


@test
def response_strictly_better_states():
    U = {"promptTokenCount": 5, "candidatesTokenCount": 3, "thoughtsTokenCount": 2, "totalTokenCount": 10, "cachedContentTokenCount": 1}
    cases = [
        ("MALFORMED_FUNCTION_CALL", [synth([{"text": "a"}]), synth([], "MALFORMED_FUNCTION_CALL", U)], "stop", "failed", None),
        ("truncated stream", [synth([{"text": "a"}]), synth([{"text": "b"}])], None, "failed", None),
        ("SAFETY", [synth([{"text": "a"}], "SAFETY", U)], "content_filter", "incomplete", "content_filter"),
        ("PROHIBITED_CONTENT", [synth([{"text": "a"}], "PROHIBITED_CONTENT", U)], "stop", "incomplete", "content_filter"),
        ("MAX_TOKENS", [synth([{"text": "a"}], "MAX_TOKENS", U)], "length", "incomplete", "max_output_tokens"),
        ("OTHER", [synth([{"text": "a"}], "OTHER", U)], "stop", "failed", None),
        ("OTHER after a complete tool call (Claude tool_use)", [synth([{"functionCall": {"id": "t", "name": "f", "args": {}}}], "OTHER", U)], "stop", "completed", None),
    ]
    for label, lines, chat_finish, status, reason in cases:
        _, _, _, _, cf = chat_stream_collect(lines)
        m, evs = resp_stream_collect(lines)
        assert cf == chat_finish, (label, cf)
        assert m.final["status"] == status, (label, m.final["status"])
        assert (m.final["incomplete_details"] or {}).get("reason") == reason, (label, m.final["incomplete_details"])
        if status == "failed":
            assert m.final["error"] and m.final["error"]["code"] == "server_error"
            assert evs[-1]["type"] == "response.failed"
        validate_events(evs)
    m, _ = resp_stream_collect([synth([{"text": "x"}], "STOP", U)])
    assert m.final["usage"] == {"input_tokens": 5, "input_tokens_details": {"cached_tokens": 1}, "output_tokens": 5,
                                "output_tokens_details": {"reasoning_tokens": 2}, "total_tokens": 10}, m.final["usage"]
    # interleaved order preserved (chat merges text, loses order)
    lines = [synth([{"text": "before "}]), synth([{"functionCall": {"id": "c1", "name": "f", "args": {}}}]),
             synth([{"text": "after"}], "STOP")]
    content, *_ = chat_stream_collect(lines)
    m, evs = resp_stream_collect(lines)
    assert content == "before after"
    assert [i["type"] for i in m.final["output"]] == ["message", "function_call", "message"]
    validate_events(evs)


@test
def summary_none_hides_reasoning_and_emit_answer_not_leaked():
    lines = load_lines("claude_thinking_tool.sse")
    m, evs = resp_stream_collect(lines, "claude-opus-5-5-high", summary=False, encrypted=False)
    assert [i["type"] for i in m.final["output"]] == ["message", "function_call"]
    assert not any(e["type"].startswith("response.reasoning") for e in evs)
    m, evs = resp_stream_collect(lines, "claude-opus-5-5-high", summary=False, encrypted=True)
    assert m.final["output"][0]["type"] == "reasoning" and m.final["output"][0]["summary"] == [] and m.final["output"][0]["encrypted_content"]
    validate_events(evs)
    from src.responses.request import convert_request
    from src.responses.mapper import ResponsesMapper
    conv = convert_request({"model": "抗截断-gemini-3-flash", "input": "x", "stream": True})
    assert conv.use_anti_truncation and conv.real_model == "gemini-3-flash"
    m = ResponsesMapper(conv)
    evs = m.start_events()
    evs += m.feed_sse_line(synth([{"functionCall": {"name": "emit_answer", "args": {"content": "final text"}}}], "STOP"))
    evs += m.finish()
    assert [i["type"] for i in m.final["output"]] == ["message"] and m.final["output"][0]["content"][0]["text"] == "final text"
    assert m.final["model"] == "抗截断-gemini-3-flash"
    # error chunk from the anti-truncation processor -> failed
    m = ResponsesMapper(conv)
    evs = m.start_events() + m.feed_sse_line('data: {"error": {"message": "boom", "code": 500}}') + m.finish()
    assert m.final["status"] == "failed" and "boom" in m.final["error"]["message"]


# ======================================================================= event validity
def validate_events(evs):
    assert evs[0]["type"] == "response.created" and evs[1]["type"] == "response.in_progress", [e["type"] for e in evs[:2]]
    assert [e["sequence_number"] for e in evs] == list(range(len(evs))), "sequence numbers"
    assert evs[-1]["type"] in ("response.completed", "response.incomplete", "response.failed")
    assert sum(e["type"] in ("response.completed", "response.incomplete", "response.failed") for e in evs) == 1
    open_items, done_items, open_parts = {}, {}, set()
    for e in evs[2:-1]:
        t = e["type"]
        if t == "response.output_item.added":
            oi = e["output_index"]
            assert oi not in open_items and oi not in done_items and oi == len(done_items) + len(open_items), (t, oi)
            open_items[oi] = e["item"]["id"]
        elif t == "response.output_item.done":
            oi = e["output_index"]
            assert open_items.get(oi) == e["item"]["id"], (t, oi)
            assert not any(k[0] == oi for k in open_parts), "part still open"
            done_items[oi] = e["item"]
            del open_items[oi]
        elif t in ("response.content_part.added", "response.reasoning_summary_part.added"):
            assert open_items.get(e["output_index"]) == e["item_id"]
            open_parts.add((e["output_index"], t.split(".")[1]))
        elif t in ("response.content_part.done", "response.reasoning_summary_part.done"):
            open_parts.discard((e["output_index"], t.split(".")[1].replace("done", "added")))
            k = (e["output_index"], "content_part" if "content_part" in t else "reasoning_summary_part")
            open_parts.discard(k)
        elif t.endswith(".delta") or t.endswith(".done"):
            assert open_items.get(e["output_index"]) == e["item_id"], (t, e)
        else:
            raise AssertionError(f"unexpected event {t}")
    assert not open_items, open_items
    final = evs[-1]["response"]["output"]
    assert final == [done_items[i] for i in sorted(done_items)], "final output == done items"


@test
def event_sequence_validator_rejects_bad_sequences():
    lines = load_lines("claude_thinking_tool.sse")
    m, evs = resp_stream_collect(lines, "claude-opus-5-5-high")
    validate_events(evs)
    bad = copy.deepcopy(evs)
    bad[3]["sequence_number"] = 99
    try:
        validate_events(bad)
    except AssertionError:
        pass
    else:
        raise AssertionError("validator accepted bad sequence")


# ======================================================================= router helpers
@test
def router_error_mapping_429_404_retry_after():
    import time as _t
    from fastapi import Response
    import src.router.antigravity.responses as R

    async def fake_classify(model):
        return [], [("a@x", _t.time() + 120)], ["a@x"]
    orig = R.classify
    R.classify = fake_classify
    try:
        body = json.dumps({"error": {"code": 429, "message": "all exhausted", "status": "RESOURCE_EXHAUSTED"}})
        r = run(R._upstream_error(Response(content=body, status_code=429), "claude-opus-5-5-high", {}))
        d = json.loads(r.body)
        assert r.status_code == 429 and d["error"]["code"] == "rate_limit_exceeded", d
        assert 100 <= int(r.headers["retry-after"]) <= 121, r.headers.get("retry-after")
        body = json.dumps({"error": {"code": 404, "message": "Model 'x' is not available", "status": "NOT_FOUND"}})
        r = run(R._upstream_error(Response(content=body, status_code=404), "x", {"x-agy-ignored-params": "a"}))
        d = json.loads(r.body)
        assert r.status_code == 404 and d["error"]["param"] == "model" and d["error"]["code"] == "model_not_found"
        assert r.headers["x-agy-ignored-params"] == "a"
        r = run(R._upstream_error(Response(content=json.dumps({"error": "x"}), status_code=503), "x", {}))
        assert r.status_code == 503 and json.loads(r.body)["error"]["type"] == "server_error"
        assert R._is_signature_rejection(Response(content='{"error":{"message":"Invalid `signature` in `thinking` block"}}', status_code=400))
        assert not R._is_signature_rejection(Response(content='{"error":{"message":"bad"}}', status_code=400))
    finally:
        R.classify = orig


@test
def strip_replayed_thoughts_keeps_everything_else():
    from src.responses.request import strip_replayed_thoughts
    req = {"contents": [{"role": "user", "parts": [{"text": "q"}]},
                        {"role": "model", "parts": [{"thought": True, "text": "t", "thoughtSignature": "SIG"},
                                                    {"functionCall": {"id": "1", "name": "f", "args": {}}}]},
                        {"role": "model", "parts": [{"thought": True, "text": "t2", "thoughtSignature": "SIG2"}]}]}
    assert strip_replayed_thoughts(req) == 2
    assert req["contents"] == [{"role": "user", "parts": [{"text": "q"}]},
                               {"role": "model", "parts": [{"functionCall": {"id": "1", "name": "f", "args": {}}}]}]


def main():
    import config
    asyncio.set_event_loop(asyncio.new_event_loop())
    run(config.init_config())
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
