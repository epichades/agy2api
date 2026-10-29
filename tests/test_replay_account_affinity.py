"""Authenticated replay survives affinity eviction without changing accounts."""
import json
import time

import pytest
from fastapi import HTTPException, Request, Response

from src.responses import encrypted as enc
from src.responses.request import _arguments_digest

MODEL = "claude-opus-5-5-high"
A = "owner-a@example.invalid"
B = "owner-b@example.invalid"
KEY = "synthetic-owner-thread"


@pytest.fixture
def routing(monkeypatch):
    from src import credential_manager as manager, session_cache
    from src.api import antigravity as api, antigravity_routing as selector
    from src.router.antigravity import anthropic, responses

    state = {"enabled": {A: {}, B: {}}, "lookups": [], "transports": [],
             "retry": False, "on_transport": None, "no_token": set()}

    async def mode():
        return "cache"

    async def enabled():
        return {key: value for key, value in state["enabled"].items() if not value.get("disabled")}

    async def lists():
        return {key: {"models": {MODEL}, "quota": {}} for key in state["enabled"]}

    async def credential(email):
        state["lookups"].append(email)
        if email in state["no_token"]:
            return None
        return {"access_token": email, "project_id": "synthetic-project"}

    async def retry_config():
        return {"retry_enabled": state["retry"], "max_retries": 1, "retry_interval": 0}

    async def url():
        return "https://synthetic.invalid"

    async def wrap(body, model, project):
        return {"request": body, "model": model, "project": project}, None

    async def upstream(**kwargs):
        state["transports"].append(kwargs["headers"]["Authorization"])
        if state["on_transport"]:
            response = state["on_transport"]()
            if response:
                yield response
                return
        yield 'data: {"candidates":[{"content":{"parts":[{"text":"SYNTHETIC_OK"}]},"finishReason":"STOP"}]}\n\n'

    async def noop(*args, **kwargs):
        return None

    async def codes():
        return []

    async def can_retry(status, email, enabled, attempt, maximum, interval):
        return enabled and attempt < maximum

    monkeypatch.setattr(session_cache, "_memory_store", {})
    monkeypatch.setattr(session_cache, "_redis_client", None)
    monkeypatch.setattr(manager, "get_credential_select_mode", mode)
    monkeypatch.setattr(selector, "_enabled_states", enabled)
    monkeypatch.setattr(selector, "get_model_lists", lists)
    monkeypatch.setattr(selector, "_get_cred_with_fresh_token", credential)
    monkeypatch.setattr(selector, "_soft_cooldowns", {})
    monkeypatch.setattr(selector.random, "shuffle", lambda pool: pool.sort(reverse=True))
    for name, value in {"get_retry_config": retry_config, "get_antigravity_api_url": url,
                        "wrap_cli_request": wrap, "stream_post_async": upstream,
                        "record_usage_log": noop, "record_api_call_success": noop,
                        "record_api_call_error": noop, "_record_request_body_detail": noop,
                        "get_auto_ban_error_codes": codes, "parse_and_log_cooldown": noop,
                        "handle_error_with_retry": can_retry}.items():
        monkeypatch.setattr(api, name, value)
    monkeypatch.setattr(api, "_derive_session_key", lambda *args: KEY)
    monkeypatch.setattr(responses, "pck_session_key", lambda *args: KEY)
    monkeypatch.setattr(responses, "stream_request", api.stream_request)
    monkeypatch.setattr(anthropic, "stream_request", api.stream_request)
    monkeypatch.setenv("RESPONSES_ALLOW_HISTORY_FALLBACK", "0")
    return state, manager, session_cache, api


def request(body, path):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": path, "headers": []}, receive)


async def replay(protocol, kind="thinking", second_owner=None):
    from src.models import ClaudeRequest
    from src.router.antigravity import anthropic, responses

    if kind == "thinking":
        blob = enc.encode(MODEL, A, "SYNTHETIC_SIGNATURE", "synthetic thought  \n")
        if protocol == "responses":
            history = [{"type": "reasoning", "encrypted_content": blob}]
            if second_owner:
                history.append({"type": "reasoning", "encrypted_content": enc.encode(MODEL, second_owner, "SYNTHETIC_SECOND", "second")})
            body = {"model": MODEL, "input": [{"role": "user", "content": "start"}, *history, {"role": "user", "content": "continue"}]}
        else:
            history = [{"type": "thinking", "thinking": "", "signature": blob}]
            if second_owner:
                history.append({"type": "thinking", "thinking": "", "signature": enc.encode(MODEL, second_owner, "SYNTHETIC_SECOND", "second")})
            body = {"model": MODEL, "thinking": {"type": "adaptive", "display": "omitted"}, "messages": [
                {"role": "user", "content": "start"}, {"role": "assistant", "content": history}, {"role": "user", "content": "continue"}]}
    else:
        args = {"background": False}
        blob = enc.encode(MODEL, A, "SYNTHETIC_FUNCTION_SIGNATURE", "", call_id="synthetic-call", function_name="read", arguments_hash=_arguments_digest(args))
        call_id = enc.CALL_ID_PREFIX + blob[len(enc.PREFIX):]
        if protocol == "responses":
            body = {"model": MODEL, "input": [{"role": "user", "content": "start"},
                    {"type": "function_call", "call_id": call_id, "name": "read", "arguments": json.dumps(args)},
                    {"type": "function_call_output", "call_id": call_id, "output": "synthetic result"}]}
        else:
            body = {"model": MODEL, "messages": [{"role": "user", "content": "start"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": "read", "input": args}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "synthetic result"}]}]}
    if protocol == "responses":
        return await responses.create_response(request(body, "/v1/responses"), _token="synthetic")
    return await anthropic.messages(ClaudeRequest.model_validate(body), request(body, "/v1/messages"), _token="synthetic")


@pytest.mark.parametrize("protocol", ["responses", "native"])
@pytest.mark.parametrize("kind", ["thinking", "function"])
@pytest.mark.parametrize("cache_state", ["restart", "expired", "foreign"])
@pytest.mark.asyncio
async def test_authenticated_owner_wins_after_cache_loss(routing, protocol, kind, cache_state):
    state, manager, cache, api = routing
    await cache.set_session_binding(KEY, A)
    if cache_state == "restart":
        await cache.close()
    elif cache_state == "expired":
        cache._memory_store[KEY] = (A, cache._now() - 1)
    else:
        await cache.set_session_binding(KEY, B)
    response = await replay(protocol, kind)
    assert response.status_code == 200
    assert state["lookups"] == [A]
    assert state["transports"] == ["Bearer " + A]
    assert await cache.get_session_binding(KEY) == A


@pytest.mark.parametrize("protocol", ["responses", "native"])
@pytest.mark.parametrize("unavailable", ["disabled", "removed", "cooling", "no_token", "model"])
@pytest.mark.asyncio
async def test_unavailable_owner_never_falls_back_to_another_ready_account(routing, protocol, unavailable, monkeypatch):
    state, manager, cache, api = routing
    # Even explicit lossy-history opt-in cannot authorize selecting a foreign
    # owner before validating authenticated replay history.
    monkeypatch.setenv("RESPONSES_ALLOW_HISTORY_FALLBACK", "1")
    if unavailable == "disabled":
        state["enabled"][A]["disabled"] = True
    elif unavailable == "removed":
        state["enabled"].pop(A)
    elif unavailable == "cooling":
        state["enabled"][A]["model_cooldowns"] = {MODEL: time.time() + 600}
    elif unavailable == "no_token":
        state["no_token"].add(A)
    else:
        from src.api import antigravity_routing as selector
        async def lists():
            return {A: {"models": {"different-model"}}, B: {"models": {MODEL}}}
        monkeypatch.setattr(selector, "get_model_lists", lists)
    response = await replay(protocol)
    assert response.status_code == 409
    assert B not in state["lookups"] and state["transports"] == []
    assert await cache.get_session_binding(KEY) is None
    if protocol == "responses":
        assert json.loads(response.body)["error"]["code"] == "state_account_unavailable"


@pytest.mark.parametrize("protocol", ["responses", "native"])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.asyncio
async def test_retry_preserves_owner_boundary(routing, protocol, status):
    state, manager, cache, api = routing
    state["retry"] = True
    def limited():
        state["enabled"][A]["disabled"] = True
        return Response(status_code=status, content='{"error":{"message":"synthetic retry"}}')
    state["on_transport"] = limited
    response = await replay(protocol)
    assert response.status_code == 409
    assert state["lookups"] == [A] and state["transports"] == ["Bearer " + A]
    assert await cache.get_session_binding(KEY) == A


@pytest.mark.parametrize("protocol", ["responses", "native"])
@pytest.mark.asyncio
async def test_mixed_authenticated_owners_rejected_before_credential_selection(routing, protocol):
    state, manager, cache, api = routing
    response = await replay(protocol, second_owner=B)
    assert response.status_code == 400
    assert state["lookups"] == state["transports"] == []
    assert await cache.get_session_binding(KEY) is None


@pytest.mark.parametrize("mode", ["cache", "sticky"])
@pytest.mark.asyncio
async def test_callback_failure_cannot_poison_affinity(routing, monkeypatch, mode):
    state, manager, cache, api = routing
    manager._reset_sticky()
    async def random_mode():
        return mode
    monkeypatch.setattr(manager, "get_credential_select_mode", random_mode)
    def reject(email, payload):
        assert email == B
        raise HTTPException(409, "Synthetic callback rejection")
    with pytest.raises(HTTPException):
        _ = [chunk async for chunk in api.stream_request({"model": MODEL, "request": {"contents": []}}, on_credential=reject)]
    assert manager._sticky_email is None
    assert await cache.get_session_binding(KEY) is None
    assert not state["transports"]


@pytest.mark.asyncio
async def test_stream_rechecks_owner_even_if_a_selector_returns_foreign_account(routing, monkeypatch):
    state, manager, cache, api = routing
    async def unexpected(**kwargs):
        return B, {"access_token": "synthetic-token", "project_id": "synthetic-project"}
    monkeypatch.setattr(api, "get_valid_credential", unexpected)
    chunks = [chunk async for chunk in api.stream_request(
        {"model": MODEL, "request": {"contents": []}}, required_account_hash=enc.account_hash(A))]
    assert len(chunks) == 1 and chunks[0].status_code == 409
    assert not state["transports"] and await cache.get_session_binding(KEY) is None
