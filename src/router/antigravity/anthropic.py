"""
Anthropic Router - Handles Anthropic/Claude format API requests via Antigravity
通过Antigravity处理Anthropic/Claude格式请求的路由模块
"""

# 标准库
import copy
import json
import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 第三方库
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# 本地模块 - 配置和日志
from config import get_anti_truncation_max_attempts, get_api_password
from log import log

# 本地模块 - 工具和认证
from src.utils import (
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    authenticate_bearer,
)

# 本地模块 - 基础路由工具
from src.router.stream_passthrough import (
    build_streaming_response_or_error,
    prepend_async_item,
    read_first_async_item,
    close_async_iterator,
    ManagedStreamingResponse,
)

# 本地模块 - 数据模型
from src.models import ClaudeRequest, model_to_dict

# 本地模块 - Token估算
from src.token_estimator import estimate_input_tokens

# 本地模块 - API 层
from src.api.antigravity import non_stream_request, stream_request

# 本地模块 - 转换器
from src.converter.anthropic2gemini import (
    anthropic_to_gemini_request,
    gemini_to_anthropic_response,
    gemini_stream_to_anthropic_stream,
    aggregate_gemini_to_anthropic,
    AnthropicStreamError,
    _normalize_function_name,
)
from src.converter.antigravity_fix import normalize_antigravity_request
from src.responses import encrypted as replay_state
from src.responses.request import _arguments_digest
from src.converter.thoughtSignature_fix import encode_tool_id_with_signature
from src.converter.anti_truncation import (
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
)


# ==================== 路由器初始化 ====================

router = APIRouter()


# ==================== API 路由 ====================

def _native_error(status: int, message: str, error_type: str = "api_error") -> JSONResponse:
    return JSONResponse(status_code=status, content={"type": "error", "error": {"type": error_type, "message": message}})


def _upstream_error(response: Response) -> JSONResponse:
    status = response.status_code
    message = f"Upstream request failed (HTTP {status})"
    try:
        data = json.loads(response.body)
        error = data.get("error", data)
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
    except (ValueError, TypeError, AttributeError):
        pass
    if status in (401, 403):
        status = 502
    kind = "rate_limit_error" if status == 429 else "invalid_request_error" if status in (400, 404, 422) else "api_error"
    result = _native_error(status if status >= 400 else 502, message, kind)
    if response.headers.get("retry-after"):
        result.headers["Retry-After"] = response.headers["retry-after"]
    return result


def _validate_capabilities(payload: dict, request: Request):
    allowed = {"model", "messages", "system", "max_tokens", "temperature", "top_p", "top_k", "stream",
               "stop_sequences", "tools", "tool_choice", "metadata", "thinking", "size", "service_tier",
               "context_management", "output_config"}
    if set(payload) - allowed:
        raise ValueError("Unsupported Messages parameter: " + sorted(set(payload) - allowed)[0])
    notes = []
    if payload.get("metadata"):
        notes.append("metadata")
    if payload.get("service_tier"):
        notes.append("service_tier")
    if request.headers.get("anthropic-beta"):
        notes.append("anthropic-beta")
    context = payload.get("context_management")
    if context is not None:
        # Keep-all is the stateless behavior already implemented by this adapter.
        # Actual clearing/compaction requires a separate, explicit implementation.
        if not isinstance(context, dict) or set(context) - {"edits"}:
            raise ValueError("Unsupported context_management parameter")
        edits = context.get("edits", [])
        if not isinstance(edits, list) or any(
            not isinstance(edit, dict)
            or set(edit) != {"type", "keep"}
            or edit.get("type") != "clear_thinking_20251015"
            or edit.get("keep") != "all"
            for edit in edits
        ):
            raise ValueError("Unsupported context_management.edits; only empty edits or clear_thinking keep=all are supported")
        notes.append("context_management(no_op_keep_all)")
    output = payload.get("output_config")
    if output is not None:
        if not isinstance(output, dict) or set(output) - {"effort"}:
            raise ValueError("Unsupported output_config parameter")
        if "effort" in output:
            if not isinstance(output["effort"], str) or output["effort"] not in {"low", "medium", "high", "xhigh", "max"}:
                raise ValueError("Unsupported output_config.effort")
            notes.append("output_config.effort(upstream_model_policy)")
    thinking = payload.get("thinking")
    if thinking is not None:
        if not isinstance(thinking, dict) or not isinstance(thinking.get("type"), str) or thinking["type"] not in {"enabled", "disabled", "adaptive"}:
            raise ValueError("Unsupported thinking configuration")
        if set(thinking) - {"type", "budget_tokens", "display"}:
            raise ValueError("Unsupported thinking parameter")
        if not isinstance(thinking.get("display", "summarized"), str) or thinking.get("display", "summarized") not in {"summarized", "omitted"}:
            raise ValueError("Unsupported thinking.display")
        if thinking.get("type") == "disabled" and "display" in thinking:
            raise ValueError("thinking.display requires enabled or adaptive thinking")
    choice = payload.get("tool_choice")
    if choice is not None:
        if not isinstance(choice, dict) or not isinstance(choice.get("type"), str) or choice["type"] not in {"auto", "any", "tool", "none"}:
            raise ValueError("Unsupported tool_choice")
        if set(choice) - {"type", "name", "disable_parallel_tool_use"}:
            raise ValueError("Unsupported tool_choice parameter")
        if choice.get("type") == "tool" and not isinstance(choice.get("name"), str):
            raise ValueError("tool_choice.name is required")
        if choice.get("disable_parallel_tool_use"):
            notes.append("tool_choice.disable_parallel_tool_use")
    tool_names = {}
    for tool in payload.get("tools") or []:
        if tool.get("type") not in (None, "custom"):
            raise ValueError("Built-in Anthropic tools are not supported")
        if set(tool) - {"type", "name", "description", "input_schema", "cache_control", "strict"}:
            raise ValueError("Unsupported tool definition parameter")
        if not isinstance(tool.get("name"), str) or not tool["name"] or not isinstance(tool.get("input_schema", {}), dict):
            raise ValueError("Tools require a name and object input_schema")
        upstream = _normalize_function_name(tool["name"])
        if upstream in tool_names:
            raise ValueError("Tool names must be unique after normalization")
        tool_names[upstream] = tool["name"]
        if tool.get("strict"):
            notes.append("tools[].strict")

    entries = list(payload.get("tools") or [])
    if isinstance(payload.get("system"), list):
        entries.extend(payload["system"])
    for message in payload.get("messages") or []:
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict):
                    entries.append(block)
                    if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                        entries.extend(block["content"])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "cache_control" in entry:
            notes.append("cache_control")
        if "citations" in entry:
            notes.append("citations")
    return list(dict.fromkeys(notes)), tool_names


def _restore_native_replay(payload: dict):
    """Authenticate opaque omitted-thinking blocks before translating history.

    Account/model checks follow normalization and credential selection. A blob
    is never treated as an upstream signature when authentication fails.
    """
    restored = copy.deepcopy(payload)
    owners = []
    call_ids = {}
    signed_calls = {}
    for message in restored.get("messages") or []:
        if not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            original = block.get("id")
            if not isinstance(original, str):
                continue
            if original.startswith("call_agy1_"):
                raise ValueError("Legacy native function replay state is not supported")
            if not original.startswith(replay_state.CALL_ID_PREFIX):
                continue
            state = replay_state.decode(replay_state.PREFIX + original[len(replay_state.CALL_ID_PREFIX):])
            if state is None or state.get("k") != "function_call":
                raise ValueError("Invalid native function replay state")
            name = block.get("name")
            args = block.get("input")
            if not isinstance(name, str) or not isinstance(args, dict) or state["n"] != _normalize_function_name(name) or state["g"] != _arguments_digest(args):
                raise ValueError("Signed native function history was modified")
            if state["id"] in signed_calls and signed_calls[state["id"]] != state:
                raise ValueError("Conflicting native function replay state")
            signed_calls[state["id"]] = state
            call_ids[original] = encode_tool_id_with_signature(state["id"], state["s"])
            block["id"] = call_ids[original]
            owners.append(state)
    for message in restored.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                original = block.get("tool_use_id")
                if isinstance(original, str) and original.startswith((replay_state.CALL_ID_PREFIX, "call_agy1_")):
                    if original not in call_ids:
                        raise ValueError("Native function replay result requires its original tool_use")
                    block["tool_use_id"] = call_ids[original]
                continue
            if block.get("type") != "thinking":
                continue
            signature = block.get("signature", block.get("thoughtSignature"))
            if "signature" in block and "thoughtSignature" in block and block["signature"] != block["thoughtSignature"]:
                raise ValueError("thinking signature aliases disagree")
            if not isinstance(signature, str):
                continue  # the converter reports malformed native blocks
            if signature.startswith(("agy1.", replay_state.PREFIX)):
                state = replay_state.decode(signature)
                if state is None or state.get("k") is not None:
                    raise ValueError("Invalid or legacy native thinking replay state")
                if block.get("thinking") != "":
                    raise ValueError("Encrypted native thinking must be replayed unchanged with empty thinking")
                block["thinking"] = state["t"]
                block["signature"] = state["s"]
                block.pop("thoughtSignature", None)
                owners.append(state)
            elif block.get("thinking") == "":
                raise ValueError("Empty native thinking requires authenticated server replay state")
    return restored, owners


@router.post("/v1/messages")
async def messages(claude_request: ClaudeRequest, request: Request, _token: str = Depends(authenticate_bearer)):
    normalized = model_to_dict(claude_request)
    try:
        notes, tool_names = _validate_capabilities(normalized, request)
        normalized, owners = _restore_native_replay(normalized)
        real_model = get_base_model_from_feature_model(claude_request.model)
        normalized["model"] = real_model
        gemini = await anthropic_to_gemini_request(normalized)
        requested_generation = gemini.get("generationConfig", {}).copy()
        gemini["model"] = real_model
        gemini = await normalize_antigravity_request(gemini)
        if any(owner["m"] != gemini.get("model") for owner in owners):
            raise ValueError("Native thinking replay model differs from the selected model")
        display_thinking = (normalized.get("thinking") or {}).get("display") != "omitted"
        if not display_thinking or owners:
            replay_state.initialize_key()
        effective_generation = gemini.get("generationConfig") or {}
        if normalized.get("thinking") and requested_generation.get("thinkingConfig") != effective_generation.get("thinkingConfig"):
            notes.append("thinking(upstream_model_policy)")
        if requested_generation.get("maxOutputTokens") != effective_generation.get("maxOutputTokens"):
            notes.append("max_tokens(upstream_limit)")
    except ValueError as error:
        return _native_error(400, str(error), "invalid_request_error")
    except replay_state.StateKeyError:
        return _native_error(500, "Private replay state key is unavailable")
    api_request = {"model": gemini.pop("model"), "request": gemini}
    log_ctx = {"api_format": "anthropic", "client_ip": request.client.host if request.client else "", "client_model": claude_request.model}
    headers = {"x-agy-ignored-params": ",".join(dict.fromkeys(notes))} if notes else {}
    selected = {"account": None}

    def on_credential(email: str, final_payload: dict):
        if not email or any(owner["a"] != replay_state.account_hash(email) for owner in owners):
            raise HTTPException(status_code=409, detail="Selected account differs from native replay history; retain the original account or restart the conversation")
        selected["account"] = email

    def encode_signature(signature: str, text: str):
        return replay_state.encode(api_request["model"], selected["account"], signature, text)

    def encode_function(tool_id: str, name: str, args: dict, signature: str):
        blob = replay_state.encode(api_request["model"], selected["account"], signature, "",
                                   call_id=tool_id, function_name=name, arguments_hash=_arguments_digest(args))
        return replay_state.CALL_ID_PREFIX + blob[len(replay_state.PREFIX):]

    def stream(payload):
        return stream_request(body=payload, native=False, log_ctx=log_ctx, on_credential=on_credential)
    use_anti_truncation = is_anti_truncation_model(claude_request.model) and claude_request.stream
    if is_anti_truncation_model(claude_request.model) and not claude_request.stream:
        notes.append("anti_truncation(non_stream)")
        headers["x-agy-ignored-params"] = ",".join(dict.fromkeys(notes))
    if use_anti_truncation:
        api_request = apply_anti_truncation(api_request)
    iterator = stream(api_request)
    try:
        first = await read_first_async_item(iterator)
    except StopAsyncIteration:
        await close_async_iterator(iterator)
        return _native_error(502, "Upstream returned no data")
    except HTTPException as error:
        await close_async_iterator(iterator)
        return _native_error(error.status_code, str(error.detail), "invalid_request_error")
    except Exception as error:
        await close_async_iterator(iterator)
        log.error(f"[ANTHROPIC] Upstream request failed: {type(error).__name__}")
        return _native_error(502, "Upstream request failed")
    if isinstance(first, Response):
        await close_async_iterator(iterator)
        response = _upstream_error(first)
        response.headers.update(headers)
        return response
    source = prepend_async_item(first, iterator)
    if use_anti_truncation:
        first_pending = True

        async def continuation(payload):
            nonlocal first_pending
            if first_pending:
                first_pending = False
                body_stream = source
            else:
                body_stream = stream(payload)
            return ManagedStreamingResponse(body_stream, media_type="text/event-stream")

        source_stream = AntiTruncationStreamProcessor(continuation, api_request, await get_anti_truncation_max_attempts()).process_stream()
    else:
        source_stream = source
    if not claude_request.stream:
        try:
            response = await aggregate_gemini_to_anthropic(source_stream, claude_request.model, tool_names, display_thinking, encode_signature, encode_function)
            return JSONResponse(content=response, headers=headers)
        except AnthropicStreamError as error:
            response = _native_error(error.status_code, str(error))
            response.headers.update(headers)
            return response
        except Exception as error:
            log.error(f"[ANTHROPIC] Response aggregation failed: {type(error).__name__}")
            return _native_error(502, "Upstream stream failed")
        finally:
            await close_async_iterator(source_stream)
    return ManagedStreamingResponse(
        gemini_stream_to_anthropic_stream(source_stream, claude_request.model, tool_names=tool_names,
                                        display_thinking=display_thinking, signature_encoder=encode_signature,
                                        function_encoder=encode_function),
        media_type="text/event-stream", headers=headers)


@router.post("/v1/messages/count_tokens")
async def count_tokens(
    request: Request,
    _token: str = Depends(authenticate_bearer)
):
    """
    处理Anthropic格式的token计数请求
    
    Args:
        request: FastAPI请求对象
        _token: Bearer认证令牌（由Depends验证）
    
    Returns:
        JSONResponse: 包含input_tokens的响应
    """
    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error", "message": "Request body must be valid JSON"}}
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error", "message": "请求体必须为 JSON object"}}
        )

    if not payload.get("model") or not isinstance(payload.get("messages"), list):
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error", "message": "缺少必填字段：model / messages"}}
        )

    log.debug("[ANTHROPIC] Token estimate request accepted")

    # 简单估算
    input_tokens = 0
    try:
        input_tokens = estimate_input_tokens(payload)
    except Exception as e:
        log.error(f"[ANTHROPIC] Token estimation failed: {type(e).__name__}")

    return JSONResponse(content={"input_tokens": input_tokens}, headers={"x-agy-token-count-estimated": "true"})
