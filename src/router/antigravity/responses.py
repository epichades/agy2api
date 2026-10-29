"""
[local patch] OpenAI Responses API router: POST /v1/responses
(also reachable as /antigravity/v1/responses through src/compat_prefix.py).

Stateless: store is never honoured, previous_response_id/conversation are rejected.
Routing, exact-model selection, 404/429 and retries are the shared upstream
stream_request (non-stream client requests are aggregated from the upstream stream);
the requested model is never rewritten.
"""
import json
import math
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from src.router.stream_passthrough import ManagedStreamingResponse as StreamingResponse, close_async_iterator, prepend_async_item

from config import get_anti_truncation_max_attempts
from log import log
from src.responses import encrypted as enc
from src.responses.mapper import ResponsesMapper, sse
from src.responses.request import (
    ResponsesError,
    build_gemini_request,
    convert_request,
    history_fallback_enabled,
    pck_session_key,
    strip_replayed_thoughts,
    strip_signatures_on_account_switch,
)
from src.responses.upstream_api import (
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
    authenticate_bearer,
    classify,
    stream_request,
)

router = APIRouter()

_SIG_ERR_RE = re.compile(r"signature|thinking", re.I)


def _short(email: Optional[str]) -> str:
    return (email[:3] + "***") if email else "-"


def _hdr_value(items: List[str]) -> str:
    return ",".join(items).encode("ascii", "replace").decode("ascii")[:2000]


def _error_json(status: int, message: str, err_type: str, code: Optional[str], param: Optional[str] = None,
                headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    return JSONResponse(status_code=status, headers=headers or {},
                        content={"error": {"message": message, "type": err_type, "param": param, "code": code}})


def _responses_error(e: ResponsesError, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    return _error_json(e.status, e.message, e.err_type, e.code, e.param, headers)


def _body_text(resp: Response) -> str:
    body = getattr(resp, "body", b"") or b""
    return body.decode("utf-8", "ignore") if isinstance(body, (bytes, bytearray)) else str(body)


def _upstream_message(text: str) -> str:
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err)
    except Exception:
        return text[:2000] or "upstream error"


async def _retry_after(model: str) -> int:
    try:
        _ready, cooling, _eligible = await classify(model)
        if cooling:
            return max(1, math.ceil(min(u for _, u in cooling) - time.time()))
    except Exception:
        pass
    return 60


async def _upstream_error(resp: Response, model: str, headers: Dict[str, str],
                          has_schema: bool = False) -> JSONResponse:
    status = int(getattr(resp, "status_code", 500) or 500)
    msg = _upstream_message(_body_text(resp))
    if status == 400 and has_schema and "claude" in model.lower() and "tool_choice" in msg:
        return _error_json(400, "text.format json_schema is rejected upstream for this Claude model "
                                "(Antigravity enforces schemas via a forced tool_choice that the model does not "
                                f"support); use text.format json_object or a Gemini model. Upstream: {msg}",
                           "invalid_request_error", "unsupported_parameter", "text.format", headers)
    if status == 404:
        return _error_json(404, msg, "invalid_request_error", "model_not_found", "model", headers)
    if status == 429:
        h = dict(headers)
        h["Retry-After"] = str(await _retry_after(model))
        return _error_json(429, msg, "rate_limit_exceeded", "rate_limit_exceeded", None, h)
    if status == 409:
        try:
            replay_code = json.loads(_body_text(resp)).get("error", {}).get("code")
        except (ValueError, AttributeError):
            replay_code = None
        if replay_code == "state_account_unavailable":
            return _error_json(409, msg, "invalid_request_error", replay_code, "input", headers)
    if status in (401, 403):
        # upstream account credential problem, not the client's API key
        return _error_json(502, f"upstream account authorization failed ({status}): {msg}", "server_error",
                           "server_error", None, headers)
    if 400 <= status < 500:
        return _error_json(status, msg, "invalid_request_error", "upstream_invalid_request", None, headers)
    return _error_json(status if status >= 500 else 500, msg, "server_error", "server_error", None, headers)


def _is_signature_rejection(resp: Any) -> bool:
    return isinstance(resp, Response) and resp.status_code == 400 and bool(_SIG_ERR_RE.search(_body_text(resp)))


@router.post("/v1/responses")
async def create_response(request: Request, _token: str = Depends(authenticate_bearer)):
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Request body must be valid JSON", "invalid_request_error", "invalid_json")
    try:
        conv = convert_request(body)
        api_request, info = await build_gemini_request(conv)
        enc.initialize_key()
    except ResponsesError as error:
        log.warning(f"[RESPONSES] request rejected code={error.code}")
        return _responses_error(error)
    except enc.StateKeyError:
        return _error_json(500, "Responses state key is unavailable", "server_error", "server_error")

    notes: List[str] = []
    has_schema = "responseSchema" in (api_request["request"].get("generationConfig") or {})
    state: Dict[str, Any] = {"account": None}
    session_key = pck_session_key(conv.real_model, conv.prompt_cache_key)
    owners = list(conv.replay.values()) + list(conv.signed_calls.values())
    owner_hashes = {part["a"] for part in owners}
    if len(owner_hashes) > 1:
        return _error_json(400, "Replay history contains different authenticated account owners",
                           "invalid_request_error", "state_account_mismatch", "input")
    required_account_hash = next(iter(owner_hashes), None)

    def on_credential(email: str, final_payload: Dict[str, Any]) -> None:
        state["account"] = email
        foreign = {part["s"] for part in owners if part["a"] != enc.account_hash(email)}
        if not foreign:
            return
        if not history_fallback_enabled() or not strip_signatures_on_account_switch():
            raise ResponsesError(409, "Selected account differs from authenticated replay history; "
                                 "retain the original account or restart the conversation",
                                 param="input", code="state_account_mismatch")
        inner = final_payload.get("request") or {}
        for content in inner.get("contents", []) or []:
            kept = []
            for part in content.get("parts", []) or []:
                if isinstance(part, dict) and part.get("thoughtSignature") in foreign:
                    if part.get("thought") is True:
                        continue
                    part = {k: v for k, v in part.items() if k != "thoughtSignature"}
                kept.append(part)
            content["parts"] = kept
        inner["contents"] = [c for c in inner.get("contents", []) if c.get("parts")]
        if "thinking_history_dropped(account_switch)" not in notes:
            notes.append("thinking_history_dropped(account_switch)")
        log.warning("[RESPONSES] explicit history fallback removed account-bound state")

    def headers() -> Dict[str, str]:
        ignored = conv.ignored + notes
        return {"x-agy-ignored-params": _hdr_value(ignored)} if ignored else {}

    def fallback(response: Any) -> bool:
        if not (history_fallback_enabled() and conv.is_claude and _is_signature_rejection(response)):
            return False
        inner = api_request["request"]
        if "thinking_history_dropped" not in notes and strip_replayed_thoughts(inner):
            notes.append("thinking_history_dropped")
            log.warning("[RESPONSES] explicit signature fallback removed thinking history")
            return True
        generation = inner.get("generationConfig") or {}
        if info["thinking_restored"] and "thinkingConfig" in generation and "thinking_disabled_for_turn" not in notes:
            generation.pop("thinkingConfig", None)
            notes.append("thinking_disabled_for_turn")
            log.warning("[RESPONSES] explicit signature fallback disabled thinking for this turn")
            return True
        return False

    log_ctx = {"api_format": "responses", "client_ip": request.client.host if request.client else "",
               "client_model": conv.requested_model}

    def stream(payload):
        return stream_request(body=payload, native=False, log_ctx=log_ctx,
                              session_key_override=session_key, on_credential=on_credential,
                              required_account_hash=required_account_hash)

    first_payload = apply_anti_truncation(api_request) if conv.stream and conv.use_anti_truncation else api_request
    upstream = None
    for _ in range(3):
        upstream = stream(first_payload)
        try:
            first = await upstream.__anext__()
        except StopAsyncIteration:
            await close_async_iterator(upstream)
            return _error_json(502, "upstream returned no data", "server_error", "server_error", headers=headers())
        except ResponsesError as error:
            await close_async_iterator(upstream)
            return _responses_error(error, headers())
        except Exception as error:
            await close_async_iterator(upstream)
            log.error(f"[RESPONSES] upstream prefetch failed: {type(error).__name__}")
            return _error_json(502, "upstream stream could not start", "server_error", "server_error", headers=headers())
        if not isinstance(first, Response):
            break
        await close_async_iterator(upstream)
        if fallback(first):
            first_payload = apply_anti_truncation(api_request) if conv.stream and conv.use_anti_truncation else api_request
            continue
        return await _upstream_error(first, conv.real_model, headers(), has_schema)

    if conv.stream and conv.use_anti_truncation:
        initial = {"available": True}

        async def wrapper(payload):
            if initial["available"]:
                initial["available"] = False
                source = prepend_async_item(first, upstream)
            else:
                source = stream(payload)
            return StreamingResponse(source, media_type="text/event-stream")

        upstream_iter = AntiTruncationStreamProcessor(
            wrapper, first_payload, await get_anti_truncation_max_attempts()
        ).process_stream()
        initial_events = []
    else:
        upstream_iter = upstream
        initial_events = [first]
        if conv.use_anti_truncation:
            conv.note("anti_truncation(non_stream)")

    mapper = ResponsesMapper(conv, account_ref=lambda: state["account"], warnings_ref=lambda: notes)
    start = mapper.start_events()
    prefetched = []
    for chunk in initial_events:
        prefetched += mapper.feed_sse_line(chunk)

    def done_log():
        log.info(f"[RESPONSES DONE] status={mapper.final['status']} items={mapper.stats} "
                 f"history_fallback={bool(notes)} error={bool(mapper.final.get('error'))}")

    async def feed_remaining():
        try:
            if not mapper.error:
                async for chunk in upstream_iter:
                    if isinstance(chunk, Response):
                        mapper.fail(f"upstream stream failed (HTTP {chunk.status_code})")
                        break
                    for event in mapper.feed_sse_line(chunk):
                        yield event
                    if mapper.error:
                        break
        except ResponsesError as error:
            mapper.fail(error.message, error.code or "server_error")
        except Exception as error:
            log.error(f"[RESPONSES] upstream stream failed: {type(error).__name__}")
            mapper.fail("upstream stream interrupted")
        finally:
            await close_async_iterator(upstream_iter)

    if not conv.stream:
        remaining = feed_remaining()
        try:
            async for _ in remaining:
                pass
        finally:
            await close_async_iterator(remaining)
        mapper.finish()
        done_log()
        response_headers = headers()
        if mapper.model_version:
            response_headers["x-agy-upstream-model-version"] = mapper.model_version
        status_code = 502 if mapper.final["status"] == "failed" else 200
        return JSONResponse(content=mapper.final_with_output_text(), status_code=status_code, headers=response_headers)

    response_headers = headers()
    if mapper.model_version:
        response_headers["x-agy-upstream-model-version"] = mapper.model_version
    response_headers["Cache-Control"] = "no-cache"

    async def generate():
        remaining = feed_remaining()
        try:
            for event in start + prefetched:
                yield sse(event)
            async for event in remaining:
                yield sse(event)
            for event in mapper.finish():
                yield sse(event)
            done_log()
        finally:
            await close_async_iterator(remaining)
            await close_async_iterator(upstream_iter)

    return StreamingResponse(generate(), media_type="text/event-stream", headers=response_headers)
