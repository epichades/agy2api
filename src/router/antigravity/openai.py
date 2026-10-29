"""
OpenAI Router - Handles OpenAI format API requests via Antigravity
通过Antigravity处理OpenAI格式请求的路由模块
"""

# 标准库
import codecs
import json
import re
import sys
import uuid
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 第三方库
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# 本地模块 - 配置和日志
from config import get_anti_truncation_max_attempts
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
    close_async_iterator,
    prepend_async_item,
    read_first_async_item,
)

# 本地模块 - 数据模型
from src.models import OpenAIChatCompletionRequest

# 本地模块 - API 层
from src.api.antigravity import non_stream_request, stream_request

# 本地模块 - 转换器
from src.converter.openai2gemini import (
    convert_openai_to_gemini_request,
    _convert_usage_metadata,
    release_openai_stream_state,
    convert_gemini_to_openai_response,
    convert_gemini_to_openai_stream,
)
from src.converter.antigravity_fix import normalize_antigravity_request
from src.converter.anti_truncation import (
    AntiTruncationStreamProcessor,
    apply_anti_truncation,
)


# ==================== 路由器初始化 ====================

router = APIRouter()


# Only implemented request fields may reach the converter. SDK extensions are
# accepted for compatibility, but their values never become upstream parameters.
_CHAT_FORWARD_FIELDS = {
    "model", "messages", "temperature", "top_p", "top_k", "max_tokens",
    "max_completion_tokens", "response_format", "n", "seed", "stream", "tools",
    "tool_choice", "stop", "frequency_penalty", "presence_penalty",
}


def _parameter_name(name):
    # Names are user input too: bound and sanitize them before HTTP headers.
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "unknown_parameter"


def _prepare_chat_request(request):
    payload = request.model_dump(include=_CHAT_FORWARD_FIELDS, exclude_none=True)
    notes = []
    for name in request.model_fields_set - _CHAT_FORWARD_FIELDS - {"stream_options", "parallel_tool_calls", "store"}:
        if getattr(request, name, None) is not None:
            suffix = "(not_stored)" if name == "metadata" else ""
            notes.append(_parameter_name(name) + suffix)
    if request.parallel_tool_calls is False:
        notes.append("parallel_tool_calls(not_enforced)")
    if request.store:
        notes.append("store(not_stored)")
    options = request.stream_options
    if options is not None:
        if not request.stream:
            notes.append("stream_options(requires_stream)")
        else:
            if options.include_obfuscation:
                notes.append("stream_options.include_obfuscation")
            notes.extend("stream_options." + _parameter_name(name)
                         for name in (options.model_extra or {}))
    for name in ("functions", "function_call"):
        if (request.model_extra or {}).get(name):
            raise HTTPException(400, f"Legacy {name} is unsupported; use tools/tool_choice")
    for name in ("audio", "web_search_options"):
        if (request.model_extra or {}).get(name):
            raise HTTPException(400, f"{name} is unsupported by this gateway")
    modalities = (request.model_extra or {}).get("modalities")
    if modalities is not None and modalities != ["text"]:
        raise HTTPException(400, "Only text output modality is supported")
    if request.tools:
        for index, tool in enumerate(request.tools):
            function = tool.function
            if function.strict:
                notes.append("tools.function.strict(not_enforced)")
            notes.extend("tools.function." + _parameter_name(name)
                         for name in (function.model_extra or {}))
            payload["tools"][index]["function"] = function.model_dump(
                include={"name", "description", "parameters"}, exclude_none=True)
    schema = (request.response_format or {}).get("json_schema") or {}
    if isinstance(schema, dict) and schema.get("strict"):
        notes.append("response_format.json_schema.strict(not_enforced)")
    notes = list(dict.fromkeys(sorted(notes)))
    if len(notes) > 64:
        notes = notes[:64] + ["additional_unknown_parameters"]
    return payload, notes


class _SSEDecoder:
    """Decode raw byte fragments or the shared transport's newline-free str lines."""

    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.data = []

    def feed(self, chunk=b"", final=False):
        if isinstance(chunk, str):
            # stream_request(native=False) uses httpx.aiter_lines(), which
            # removes line terminators, including the empty event separator.
            # Anti-truncation may also emit complete frames; keep those intact.
            text = chunk if "\n" in chunk else chunk + "\n"
        else:
            text = self.decoder.decode(chunk, final=final)
        self.buffer += text
        # Bound a malformed/unclosed event, without buffering the whole response.
        if len(self.buffer) + sum(map(len, self.data)) > 16 * 1024 * 1024:
            raise ValueError("SSE event exceeds size limit")
        lines = self.buffer.split("\n")
        self.buffer = lines.pop()
        if final and self.buffer:
            lines.append(self.buffer)
            self.buffer = ""
        events = []
        for line in lines:
            line = line.removesuffix("\r")
            if not line:
                if self.data:
                    events.append("\n".join(self.data))
                    self.data.clear()
            elif line.startswith("data:"):
                self.data.append(line[5:].removeprefix(" "))
        if final and self.data:
            events.append("\n".join(self.data))
            self.data.clear()
        return events


def _sse_json(data):
    return ("data: " + json.dumps(data) + "\n\n").encode("utf-8")


def _chat_http_error(response):
    """A pre-stream HTTP error is JSON, regardless of provider SSE headers."""
    headers = {}
    if "retry-after" in response.headers:
        headers["retry-after"] = response.headers["retry-after"]
    try:
        content = json.loads(response.body)
        if not isinstance(content, dict) or "error" not in content:
            raise ValueError("Expected an upstream JSON error")
        # Rebuild representation headers: the upstream response may advertise
        # SSE or still carry compression/length headers for its decoded body.
        return JSONResponse(content=content, status_code=response.status_code, headers=headers)
    except (ValueError, TypeError, UnicodeError):
        # An HTML/proxy/malformed body can contain private deployment details.
        return JSONResponse(status_code=response.status_code, headers=headers, content={
            "error": {"message": "Upstream request failed", "type": "server_error", "code": "upstream_stream_error"}
        })


async def _chat_stream(source, model, include_usage, notes):
    response_id = str(uuid.uuid4())
    decoder = _SSEDecoder()
    latest = None
    usage = None
    usage_incomplete = False
    seen, finished = set(), set()
    started = False
    ended = False

    def convert(payload):
        nonlocal latest, usage, usage_incomplete
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("Expected an upstream object")
        data = raw.get("response", raw)
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError("Invalid upstream event")
        if data.get("x_agy_usage_incomplete"):
            usage_incomplete = True
            usage = None
        if data.get("usageMetadata"):
            usage = _convert_usage_metadata(data["usageMetadata"])
        converted = convert_gemini_to_openai_stream("data: " + payload, model, response_id)
        if not converted:
            raise ValueError("Invalid upstream event")
        latest = json.loads(converted[6:])
        for choice in latest["choices"]:
            index = choice["index"]
            seen.add(index)
            if choice.get("finish_reason"):
                finished.add(index)
        latest.pop("usage", None)
        if include_usage:
            latest["usage"] = None
        if notes and not started:
            latest["x_agy_ignored_params"] = notes
        return _sse_json(latest) if latest["choices"] else None

    try:
        async for chunk in source:
            if isinstance(chunk, Response):
                if not started:
                    yield _chat_http_error(chunk)
                else:
                    # Do not forward provider error bodies (which may contain
                    # credential/URL details) or fabricate a successful finish.
                    yield _sse_json({"error": {"message": "Upstream stream failed", "type": "server_error", "code": "upstream_stream_error"}})
                    yield b"data: [DONE]\n\n"
                return
            for payload in decoder.feed(chunk):
                if payload == "[DONE]":
                    ended = True
                    break
                event = convert(payload)
                if event:
                    started = True
                    yield event
            if ended:
                break
        if not ended:
            for payload in decoder.feed(final=True):
                if payload == "[DONE]":
                    break
                event = convert(payload)
                if event:
                    started = True
                    yield event
        if not seen or not seen.issubset(finished):
            raise ValueError("Upstream stream ended before its finish event")
        if include_usage:
            final = {key: latest[key] for key in ("id", "object", "created", "model")}
            final.update(choices=[], usage=usage)
            if usage is None:
                reason = "upstream_usage_incomplete" if usage_incomplete else "upstream_usage_unavailable"
                final["x_agy_ignored_params"] = notes + [f"stream_options.include_usage({reason})"]
            yield _sse_json(final)
        yield b"data: [DONE]\n\n"
    except Exception as error:
        log.warning(f"[ANTIGRAVITY-OPENAI] Stream failed: {type(error).__name__}")
        failure = {"error": {"message": "Upstream stream could not be completed", "type": "server_error", "code": "upstream_stream_error"}}
        if started:
            yield _sse_json(failure)
            yield b"data: [DONE]\n\n"
        else:
            yield JSONResponse(status_code=502, content=failure)
    finally:
        release_openai_stream_state(response_id)
        await close_async_iterator(source)


# ==================== API 路由 ====================

@router.post("/v1/chat/completions")
async def chat_completions(
    openai_request: OpenAIChatCompletionRequest,
    request: Request,
    token: str = Depends(authenticate_bearer)
):
    """
    处理OpenAI格式的聊天完成请求（流式和非流式）

    Args:
        openai_request: OpenAI格式的请求体
        token: Bearer认证令牌
    """
    log.debug(f"[ANTIGRAVITY-OPENAI] Request for model: {openai_request.model}")

    # 转换为字典
    normalized_dict, notes = _prepare_chat_request(openai_request)

    # 处理模型名称和功能检测
    use_anti_truncation = is_anti_truncation_model(openai_request.model)
    real_model = get_base_model_from_feature_model(openai_request.model)

    # 获取流式标志
    is_streaming = bool(openai_request.stream)
    choice = normalized_dict.get("tool_choice")
    if (is_streaming and use_anti_truncation and "claude" in real_model.lower()
            and choice is not None and choice != "auto"
            and not (isinstance(choice, dict) and choice.get("type") == "auto")):
        notes.append("tool_choice(anti_truncation_auto)")
    response_headers = {"x-agy-ignored-params": ",".join(notes)} if notes else {}

    # 对于抗截断模型的非流式请求，给出警告
    if use_anti_truncation and not is_streaming:
        log.warning("抗截断功能仅在流式传输时有效，非流式请求将忽略此设置")

    # 更新模型名为真实模型名
    normalized_dict["model"] = real_model

    # 转换为 Gemini 格式 (使用 converter)
    try:
        gemini_dict = await convert_openai_to_gemini_request(normalized_dict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # convert_openai_to_gemini_request 不包含 model 字段，需要手动添加
    gemini_dict["model"] = real_model

    # 规范化 Gemini 请求 (使用 antigravity 模式)
    try:
        gemini_dict = await normalize_antigravity_request(gemini_dict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # This endpoint accepts OpenAI roles, but the Antigravity content schema
    # accepts only USER/MODEL. Catch a conversion regression before HTTP send.
    for content in gemini_dict.get("contents", []):
        if not isinstance(content, dict) or content.get("role") not in ("user", "model"):
            raise HTTPException(
                status_code=400, detail="Unrepresentable content role for Antigravity"
            )

    # 准备API请求格式 - 提取model并将其他字段放入request中
    api_request = {
        "model": gemini_dict.pop("model"),
        "request": gemini_dict
    }

    # 请求日志上下文（api_format + 客户端 IP + 客户端原始模型名）
    log_ctx = {
        "api_format": "openai",
        "client_ip": request.client.host if request.client else "",
        "client_model": openai_request.model,
    }

    # ========== 非流式请求 ==========
    if not is_streaming:
        # 调用 API 层的非流式请求
        response = await non_stream_request(body=api_request, log_ctx=log_ctx)

        # 检查响应状态码
        status_code = getattr(response, "status_code", 200)

        # 提取响应体
        if hasattr(response, "body"):
            response_body = response.body.decode() if isinstance(response.body, bytes) else response.body
        elif hasattr(response, "content"):
            response_body = response.content.decode() if isinstance(response.content, bytes) else response.content
        else:
            response_body = str(response)

        try:
            gemini_response = json.loads(response_body)
        except Exception as e:
            log.error(f"Failed to parse Gemini response: {e}")
            raise HTTPException(status_code=500, detail="Response parsing failed")

        # 转换为 OpenAI 格式
        openai_response = convert_gemini_to_openai_response(
            gemini_response,
            real_model,
            status_code
        )

        if notes:
            openai_response["x_agy_ignored_params"] = notes
        return JSONResponse(content=openai_response, status_code=status_code, headers=response_headers)

    # The same framing/usage/error policy applies to regular and anti-truncation
    # streams. Only raw upstream generation differs.
    async def anti_truncation_source():
        max_attempts = await get_anti_truncation_max_attempts()
        payload = apply_anti_truncation(api_request)
        first_stream = stream_request(body=payload, native=False, log_ctx=log_ctx)
        processor_stream = None
        try:
            try:
                first = await read_first_async_item(first_stream)
            except StopAsyncIteration:
                return
            if isinstance(first, Response):
                yield first
                return
            pending = True

            async def request_wrapper(next_payload):
                nonlocal pending
                if pending:
                    pending = False
                    iterator = prepend_async_item(first, first_stream)
                else:
                    iterator = stream_request(body=next_payload, native=False, log_ctx=log_ctx)
                return StreamingResponse(iterator, media_type="text/event-stream")

            processor = AntiTruncationStreamProcessor(
                request_wrapper, payload, max_attempts, aggregate_usage=True)
            processor_stream = processor.process_stream()
            async for chunk in processor_stream:
                yield chunk
        finally:
            if processor_stream is not None:
                await close_async_iterator(processor_stream)
            await close_async_iterator(first_stream)

    source = (anti_truncation_source() if use_anti_truncation else
              stream_request(body=api_request, native=False, log_ctx=log_ctx))
    include_usage = bool(openai_request.stream_options and openai_request.stream_options.include_usage)
    response = await build_streaming_response_or_error(
        _chat_stream(source, real_model, include_usage, notes))
    response.headers.update(response_headers)
    return response
