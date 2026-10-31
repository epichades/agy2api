"""
Anthropic 到 Gemini 格式转换器

提供请求体、响应和流式转换的完整功能。
"""
from __future__ import annotations

import codecs
import copy
import json
import os
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from fastapi import Response
from log import log
from src.converter.utils import merge_system_messages
from src.converter.openai2gemini import _normalize_function_name
from src.router.stream_passthrough import close_async_iterator

from src.converter.thoughtSignature_fix import (
    decode_tool_id_and_signature,
    encode_tool_id_with_signature,
    is_internal_placeholder_text,
    is_skip_thought_signature_placeholder,
    SKIP_THOUGHT_SIGNATURE_VALIDATOR,
)

DEFAULT_TEMPERATURE = 0.4
_DEBUG_TRUE = {"1", "true", "yes", "on"}

# ============================================================================
# Thinking 块验证和清理
# ============================================================================

# 最小有效签名长度
MIN_SIGNATURE_LENGTH = 10


def has_valid_thoughtsignature(block: Dict[str, Any]) -> bool:
    """
    检查 thinking 块是否有有效签名
    
    Args:
        block: content block 字典
        
    Returns:
        bool: 是否有有效签名
    """
    if not isinstance(block, dict):
        return True
    
    block_type = block.get("type")
    if block_type not in ("thinking", "redacted_thinking"):
        return True  # 非 thinking 块默认有效
    
    thinking = block.get("thinking", "")
    thoughtsignature = block.get("thoughtSignature")
    
    # 空 thinking + 任意 thoughtsignature = 有效 (trailing signature case)
    if not thinking and thoughtsignature is not None:
        return True
    
    # 有内容 + 足够长度的 thoughtsignature = 有效
    if thoughtsignature and isinstance(thoughtsignature, str) and len(thoughtsignature) >= MIN_SIGNATURE_LENGTH:
        return True
    
    return False


def sanitize_thinking_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """
    清理 thinking 块,只保留必要字段(移除 cache_control 等)
    
    Args:
        block: content block 字典
        
    Returns:
        清理后的 block 字典
    """
    if not isinstance(block, dict):
        return block
    
    block_type = block.get("type")
    if block_type not in ("thinking", "redacted_thinking"):
        return block
    
    # 重建块,移除额外字段
    sanitized: Dict[str, Any] = {
        "type": block_type,
        "thinking": block.get("thinking", "")
    }
    
    thoughtsignature = block.get("thoughtSignature")
    if thoughtsignature:
        sanitized["thoughtSignature"] = thoughtsignature
    
    return sanitized


def remove_trailing_unsigned_thinking(blocks: List[Dict[str, Any]]) -> None:
    """
    移除尾部的无签名 thinking 块
    
    Args:
        blocks: content blocks 列表 (会被修改)
    """
    if not blocks:
        return
    
    # 从后向前扫描
    end_index = len(blocks)
    for i in range(len(blocks) - 1, -1, -1):
        block = blocks[i]
        if not isinstance(block, dict):
            break
        
        block_type = block.get("type")
        if block_type in ("thinking", "redacted_thinking"):
            if not has_valid_thoughtsignature(block):
                end_index = i
            else:
                break  # 遇到有效签名的 thinking 块,停止
        else:
            break  # 遇到非 thinking 块,停止
    
    if end_index < len(blocks):
        removed = len(blocks) - end_index
        del blocks[end_index:]
        log.debug(f"Removed {removed} trailing unsigned thinking block(s)")


def filter_invalid_thinking_blocks(messages: List[Dict[str, Any]]) -> None:
    """
    过滤消息中的无效 thinking 块，并清理所有 thinking 块的额外字段（如 cache_control）

    Args:
        messages: Anthropic messages 列表 (会被修改)
    """
    total_filtered = 0

    for msg in messages:
        # 只处理 assistant 和 model 消息
        role = msg.get("role", "")
        if role not in ("assistant", "model"):
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        original_len = len(content)
        new_blocks: List[Dict[str, Any]] = []

        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue

            block_type = block.get("type")
            if block_type not in ("thinking", "redacted_thinking"):
                new_blocks.append(block)
                continue

            # 所有 thinking 块都需要清理（移除 cache_control 等额外字段）
            # 检查 thinking 块的有效性
            if has_valid_thoughtsignature(block):
                # 有效签名，清理后保留
                new_blocks.append(sanitize_thinking_block(block))
            else:
                # 无效签名，将内容转换为 text 块
                thinking_text = block.get("thinking", "")
                if thinking_text and str(thinking_text).strip():
                    log.info(
                        f"[Claude-Handler] Converting thinking block with invalid thoughtSignature to text. "
                        f"Content length: {len(thinking_text)} chars"
                    )
                    new_blocks.append({"type": "text", "text": thinking_text})
                else:
                    log.debug("[Claude-Handler] Dropping empty thinking block with invalid thoughtSignature")

        msg["content"] = new_blocks
        filtered_count = original_len - len(new_blocks)
        total_filtered += filtered_count

        # 如果过滤后为空,添加一个空文本块以保持消息有效
        if not new_blocks:
            msg["content"] = [{"type": "text", "text": ""}]

    if total_filtered > 0:
        log.debug(f"Filtered {total_filtered} invalid thinking block(s) from history")


# ============================================================================
# 请求验证和提取
# ============================================================================


def _anthropic_debug_enabled() -> bool:
    """检查是否启用 Anthropic 调试模式"""
    return str(os.getenv("ANTHROPIC_DEBUG", "true")).strip().lower() in _DEBUG_TRUE


def _cached_content_token_count(usage_metadata: Any) -> int:
    if not isinstance(usage_metadata, dict):
        return 0
    return int(usage_metadata.get("cachedContentTokenCount", 0) or 0)


def _anthropic_usage_from_metadata(usage_metadata: Any) -> Dict[str, Any]:
    """
    将 Gemini usageMetadata 转换为 Claude usage 字段

    语义对齐（Gemini -> Claude）:
    - Gemini promptTokenCount 包含缓存 token -> Claude input_tokens 不含缓存
      （input_tokens = promptTokenCount - cachedContentTokenCount）
    - Gemini candidatesTokenCount 不含推理 token -> Claude output_tokens 包含推理 token
      （output_tokens = candidatesTokenCount + thoughtsTokenCount）
    - 明细: cache_read_input_tokens / output_tokens_details.thinking_tokens
    """
    if not isinstance(usage_metadata, dict):
        return {"input_tokens": 0, "output_tokens": 0}

    prompt_tokens_total = int(usage_metadata.get("promptTokenCount", 0) or 0)
    cached_tokens = _cached_content_token_count(usage_metadata)
    reasoning_tokens = int(usage_metadata.get("thoughtsTokenCount", 0) or 0)
    usage: Dict[str, Any] = {
        "input_tokens": max(prompt_tokens_total - cached_tokens, 0),
        "output_tokens": (
            int(usage_metadata.get("candidatesTokenCount", 0) or 0)
            + reasoning_tokens
        ),
    }

    if cached_tokens > 0:
        usage["cache_read_input_tokens"] = cached_tokens

    if reasoning_tokens > 0:
        usage["output_tokens_details"] = {"thinking_tokens": reasoning_tokens}

    return usage


def _is_non_whitespace_text(value: Any) -> bool:
    """
    判断文本是否包含"非空白"内容。

    说明：下游（Antigravity/Claude 兼容层）会对纯 text 内容块做校验：
    - text 不能为空字符串
    - text 不能仅由空白字符（空格/换行/制表等）组成
    """
    if value is None:
        return False
    try:
        return bool(str(value).strip())
    except Exception:
        return False


def _remove_nulls_for_tool_input(value: Any) -> Any:
    """
    递归移除 dict/list 中值为 null/None 的字段/元素。

    背景：Roo/Kilo 在 Anthropic native tool 路径下，若收到 tool_use.input 中包含 null，
    可能会把 null 当作真实入参执行（例如"在 null 中搜索"）。
    """
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in value.items():
            if v is None:
                continue
            cleaned[k] = _remove_nulls_for_tool_input(v)
        return cleaned

    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            if item is None:
                continue
            cleaned_list.append(_remove_nulls_for_tool_input(item))
        return cleaned_list

    return value

# ============================================================================
# 2. JSON Schema 清理
# ============================================================================

def clean_json_schema(schema: Any) -> Any:
    """
    清理 JSON Schema，移除下游不支持的字段，并把验证要求追加到 description。
    """
    if not isinstance(schema, dict):
        return schema

    # 下游不支持的字段
    unsupported_keys = {
        "$schema", "$id", "$ref", "$defs", "definitions", "title",
        "example", "examples", "readOnly", "writeOnly", "default",
        "exclusiveMaximum", "exclusiveMinimum", "oneOf", "anyOf", "allOf",
        "const", "additionalItems", "contains", "patternProperties",
        "dependencies", "propertyNames", "if", "then", "else",
        "contentEncoding", "contentMediaType", "nullable",
    }

    validation_fields = {
        "minLength": "minLength",
        "maxLength": "maxLength",
        "minimum": "minimum",
        "maximum": "maximum",
        "minItems": "minItems",
        "maxItems": "maxItems",
    }
    fields_to_remove = {"additionalProperties"}

    validations: List[str] = []
    for field, label in validation_fields.items():
        if field in schema:
            validations.append(f"{label}: {schema[field]}")

    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in unsupported_keys or key in fields_to_remove or key in validation_fields:
            continue

        if key == "type" and isinstance(value, list):
            # type: ["string", "null"] -> type: "string", nullable: true
            has_null = any(
                isinstance(t, str) and t.strip() and t.strip().lower() == "null" for t in value
            )
            non_null_types = [
                t.strip()
                for t in value
                if isinstance(t, str) and t.strip() and t.strip().lower() != "null"
            ]

            cleaned[key] = non_null_types[0] if non_null_types else "string"
            continue

        if key == "description" and validations:
            cleaned[key] = f"{value} ({', '.join(validations)})"
        elif isinstance(value, dict):
            cleaned[key] = clean_json_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [clean_json_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value

    if validations and "description" not in cleaned:
        cleaned["description"] = f"Validation: {', '.join(validations)}"

    # 如果有 properties 但没有显式 type，则补齐为 object
    if "properties" in cleaned and "type" not in cleaned:
        cleaned["type"] = "object"

    if (
        isinstance(schema.get("properties"), dict)
        and isinstance(cleaned.get("required"), list)
    ):
        nullable_fields = {
            name
            for name, prop in schema["properties"].items()
            if isinstance(prop, dict)
            and isinstance(prop.get("type"), list)
            and any(str(t).lower() == "null" for t in prop["type"])
        }
        if nullable_fields:
            cleaned["required"] = [
                item for item in cleaned["required"]
                if item not in nullable_fields
            ]
            if not cleaned["required"]:
                cleaned.pop("required", None)

    return cleaned


# ============================================================================
# 4. Tools 转换
# ============================================================================

def convert_tools(anthropic_tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """
    将 Anthropic tools[] 转换为下游 tools（functionDeclarations）结构。
    """
    if not anthropic_tools:
        return None

    gemini_tools: List[Dict[str, Any]] = []
    for tool in anthropic_tools:
        name = _normalize_function_name(tool.get("name", "nameless_function"))
        description = tool.get("description", "")
        input_schema = tool.get("input_schema", {}) or {}
        parameters = copy.deepcopy(input_schema)

        gemini_tools.append(
            {
                "functionDeclarations": [
                    {
                        "name": name,
                        "description": description,
                        "parametersJsonSchema": parameters,
                    }
                ]
            }
        )

    return gemini_tools or None


# ============================================================================
# 5. Messages 转换
# ============================================================================

def _extract_tool_result_output(content: Any) -> str:
    """Preserve every text block; never stringify unsupported multimodal results."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if not all(isinstance(part, dict) and part.get("type") == "text"
                   and isinstance(part.get("text"), str) for part in content):
            raise ValueError("tool_result.content supports text blocks only")
        return "".join(part["text"] for part in content)
    raise ValueError("tool_result.content must be a string or text block array")


def convert_messages_to_contents(
    messages: List[Dict[str, Any]], *, include_thinking: bool = True
) -> List[Dict[str, Any]]:
    """Convert content at its original position, retaining signed history exactly."""
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    tool_names: Dict[str, str] = {}
    for message in messages:
        for item in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                if not isinstance(item.get("name"), str) or not item["name"]:
                    raise ValueError("tool_use.name is required")
                tool_names[str(item.get("id", ""))] = _normalize_function_name(item["name"])

    contents: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        if role not in ("user", "assistant", "model"):
            raise ValueError("messages contain an unsupported role")
        content = message.get("content")
        raw_parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(raw_parts, list):
            raise ValueError("message content must be a string or content block array")
        parts: List[Dict[str, Any]] = []
        for item in raw_parts:
            if not isinstance(item, dict):
                raise ValueError("content blocks must be objects")
            kind = item.get("type")
            if not isinstance(kind, str):
                raise ValueError("Anthropic content block type must be a string")
            fields = {
                "text": {"type", "text", "cache_control", "citations"},
                "thinking": {"type", "thinking", "signature", "thoughtSignature", "cache_control"},
                "image": {"type", "source", "cache_control"},
                "tool_use": {"type", "id", "name", "input", "cache_control"},
                "tool_result": {"type", "tool_use_id", "name", "content", "is_error", "cache_control"},
            }
            if kind in fields and set(item) - fields[kind]:
                raise ValueError("Unsupported Anthropic content block parameter")
            if kind == "text":
                if not isinstance(item.get("text"), str):
                    raise ValueError("text blocks require a string")
                parts.append({"text": item["text"]})
            elif kind == "thinking":
                if not include_thinking:
                    raise ValueError("thinking history cannot be silently omitted")
                text = item.get("thinking")
                signature = item.get("signature", item.get("thoughtSignature"))
                if item.get("signature") is not None and item.get("thoughtSignature") is not None and item["signature"] != item["thoughtSignature"]:
                    raise ValueError("thinking signature aliases disagree")
                if role not in ("assistant", "model") or not isinstance(text, str) or not text or not isinstance(signature, str) or not signature or signature == SKIP_THOUGHT_SIGNATURE_VALIDATOR:
                    raise ValueError("thinking history requires original text and signature")
                parts.append({"thought": True, "text": text, "thoughtSignature": signature})
            elif kind == "redacted_thinking":
                raise ValueError("redacted_thinking is not supported by the Antigravity upstream")
            elif kind == "image":
                source = item.get("source")
                if not isinstance(source, dict) or source.get("type") != "base64" or not isinstance(source.get("data"), str) or set(source) - {"type", "media_type", "data"}:
                    raise ValueError("image blocks support base64 sources only")
                parts.append({"inlineData": {"mimeType": source.get("media_type", "image/png"), "data": source["data"]}})
            elif kind == "tool_use":
                encoded_id = item.get("id")
                name = item.get("name")
                args = item.get("input", {})
                if role not in ("assistant", "model") or not isinstance(encoded_id, str) or not encoded_id or not isinstance(name, str) or not name or not isinstance(args, dict):
                    raise ValueError("tool_use requires an id, name and object input in assistant content")
                tool_id, signature = decode_tool_id_and_signature(encoded_id)
                part = {"functionCall": {"id": tool_id, "name": _normalize_function_name(name), "args": copy.deepcopy(args)}}
                if signature and signature != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
                    part["thoughtSignature"] = signature
                parts.append(part)
            elif kind == "tool_result":
                encoded_id = item.get("tool_use_id")
                if role != "user" or not isinstance(encoded_id, str) or not encoded_id:
                    raise ValueError("tool_result requires tool_use_id in user content")
                tool_id, _ = decode_tool_id_and_signature(encoded_id)
                name = tool_names.get(encoded_id)
                if name is None and isinstance(item.get("name"), str):
                    name = _normalize_function_name(item["name"])
                if name is None:
                    raise ValueError("tool_result has no matching tool_use")
                result = {"output": _extract_tool_result_output(item.get("content"))}
                if "is_error" in item:
                    if not isinstance(item["is_error"], bool):
                        raise ValueError("tool_result.is_error must be a boolean")
                    result["is_error"] = item["is_error"]
                parts.append({"functionResponse": {"id": tool_id, "name": name, "response": result}})
            else:
                raise ValueError("Unsupported Anthropic content block type")
        if parts:
            contents.append({"role": "model" if role in ("assistant", "model") else "user", "parts": parts})
    return contents


def reorganize_tool_messages(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    重新组织消息，满足 tool_use/tool_result 约束。
    """
    tool_results: Dict[str, Dict[str, Any]] = {}

    for msg in contents:
        for part in msg.get("parts", []) or []:
            if isinstance(part, dict) and "functionResponse" in part:
                tool_id = (part.get("functionResponse") or {}).get("id")
                if tool_id:
                    tool_results[str(tool_id)] = part

    flattened: List[Dict[str, Any]] = []
    for msg in contents:
        role = msg.get("role")
        for part in msg.get("parts", []) or []:
            flattened.append({"role": role, "parts": [part]})

    new_contents: List[Dict[str, Any]] = []
    i = 0
    while i < len(flattened):
        msg = flattened[i]
        part = msg["parts"][0]

        if isinstance(part, dict) and "functionResponse" in part:
            i += 1
            continue

        if isinstance(part, dict) and "functionCall" in part:
            tool_id = (part.get("functionCall") or {}).get("id")
            new_contents.append({"role": "model", "parts": [part]})

            if tool_id is not None and str(tool_id) in tool_results:
                new_contents.append({"role": "user", "parts": [tool_results[str(tool_id)]]})

            i += 1
            continue

        new_contents.append(msg)
        i += 1

    return new_contents


# ============================================================================
# 7. Tool Choice 转换
# ============================================================================

def convert_tool_choice_to_tool_config(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """
    将 Anthropic tool_choice 转换为 Gemini toolConfig

    Args:
        tool_choice: Anthropic 格式的 tool_choice
            - {"type": "auto"}: 模型自动决定是否使用工具
            - {"type": "any"}: 模型必须使用工具
            - {"type": "tool", "name": "tool_name"}: 模型必须使用指定工具

    Returns:
        Gemini 格式的 toolConfig，如果无效则返回 None
    """
    if not tool_choice:
        return None
    
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        
        if choice_type == "auto":
            return {"functionCallingConfig": {"mode": "AUTO"}}
        elif choice_type == "none":
            return {"functionCallingConfig": {"mode": "NONE"}}
        elif choice_type == "any":
            return {"functionCallingConfig": {"mode": "ANY"}}
        elif choice_type == "tool":
            tool_name = tool_choice.get("name")
            if tool_name:
                return {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [_normalize_function_name(tool_name)],
                    }
                }
    
    # 无效或不支持的 tool_choice，返回 None
    return None


# ============================================================================
# 8. Generation Config 构建
# ============================================================================

def build_generation_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map only client-specified sampling and limits; do not invent stop strings."""
    config: Dict[str, Any] = {}
    names = {"temperature": "temperature", "top_p": "topP", "top_k": "topK",
             "max_tokens": "maxOutputTokens", "stop_sequences": "stopSequences"}
    for client_name, upstream_name in names.items():
        if payload.get(client_name) is not None:
            config[upstream_name] = copy.deepcopy(payload[client_name])
    thinking = payload.get("thinking")
    if thinking:
        kind = thinking.get("type")
        if kind == "enabled":
            budget = thinking.get("budget_tokens")
            if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1024:
                raise ValueError("thinking.enabled requires budget_tokens >= 1024")
            config["thinkingConfig"] = {"includeThoughts": True, "thinkingBudget": budget}
        elif kind == "adaptive":
            config["thinkingConfig"] = {"includeThoughts": True}
        elif kind == "disabled":
            config["thinkingConfig"] = {"includeThoughts": False}
        else:
            raise ValueError("Unsupported thinking configuration")
    return config


# ============================================================================
# 8. 主要转换函数
# ============================================================================

async def anthropic_to_gemini_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 Anthropic 格式请求体转换为 Gemini 格式请求体

    注意: 此函数只负责基础转换，不包含 normalize_gemini_request 中的处理
    (如 thinking config 自动设置、search tools、参数范围限制等)

    Args:
        payload: Anthropic 格式的请求体字典

    Returns:
        Gemini 格式的请求体字典，包含:
        - contents: 转换后的消息内容
        - generationConfig: 生成配置
        - systemInstruction: 系统指令 (如果有)
        - tools: 工具定义 (如果有)
        - toolConfig: 工具调用配置 (如果有 tool_choice)
    """
    # 处理连续的system消息（兼容性模式）
    payload = await merge_system_messages(copy.deepcopy(payload))

    # 提取和转换基础信息
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    
    generation_config = build_generation_config(payload)
    contents = convert_messages_to_contents(messages, include_thinking=True)

    # 转换工具
    tools = convert_tools(payload.get("tools"))
    
    # 转换 tool_choice
    tool_config = convert_tool_choice_to_tool_config(payload.get("tool_choice"))

    # 构建基础请求数据
    gemini_request = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    
    # 如果 merge_system_messages 已经添加了 systemInstruction，使用它
    if "systemInstruction" in payload:
        gemini_request["systemInstruction"] = payload["systemInstruction"]
    
    if tools:
        gemini_request["tools"] = tools

    # 添加 toolConfig（如果有 tool_choice）
    if tool_config:
        gemini_request["toolConfig"] = tool_config

    # 透传图片生成的 size 参数（如 "1024x1536"）
    if "size" in payload and payload["size"]:
        gemini_request["size"] = payload["size"]

    return gemini_request


class AnthropicStreamError(ValueError):
    """A malformed/failed upstream stream cannot become a successful message."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _sse_event(event: str, data: Dict[str, Any]) -> bytes:
    return ("event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")


async def iter_gemini_stream_events(stream):
    """Decode complete SSE frames, including byte/UTF-8 splits and aiter_lines."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    data_lines: List[str] = []
    done = False

    def line_event(line: str):
        nonlocal done, data_lines
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            if not data_lines:
                return None
            raw = "\n".join(data_lines)
            data_lines = []
            if raw.strip() == "[DONE]":
                done = True
                return None
            if done:
                raise AnthropicStreamError("Upstream sent data after its end marker")
            try:
                event = json.loads(raw)
            except (ValueError, TypeError) as error:
                raise AnthropicStreamError("Upstream returned malformed SSE JSON") from error
            if not isinstance(event, dict):
                raise AnthropicStreamError("Upstream SSE payload must be an object")
            return event
        if line.startswith(":") or line.startswith(("event:", "id:", "retry:")):
            return None
        if line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
            return None
        raise AnthropicStreamError("Upstream returned an unsupported SSE field")

    try:
        async for chunk in stream:
            if isinstance(chunk, Response):
                yield chunk
                return
            if isinstance(chunk, str):
                text = chunk
                # The shared API's non-native transport is httpx.aiter_lines().
                if "\n" not in text:
                    text += "\n"
            elif isinstance(chunk, (bytes, bytearray)):
                try:
                    text = decoder.decode(bytes(chunk))
                except UnicodeError as error:
                    raise AnthropicStreamError("Upstream SSE is not valid UTF-8") from error
            else:
                raise AnthropicStreamError("Upstream stream returned an unsupported chunk")
            buffer += text
            if len(buffer) > 8 * 1024 * 1024 or sum(map(len, data_lines)) > 8 * 1024 * 1024:
                raise AnthropicStreamError("Upstream SSE frame is too large")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                event = line_event(line)
                if event is not None:
                    yield event
        try:
            buffer += decoder.decode(b"", final=True)
        except UnicodeError as error:
            raise AnthropicStreamError("Upstream SSE ended inside a UTF-8 character") from error
        if buffer:
            event = line_event(buffer)
            if event is not None:
                yield event
        event = line_event("")
        if event is not None:
            yield event
    finally:
        await close_async_iterator(stream)


class AnthropicStreamState:
    """One ordered mapping shared by native streaming and non-stream aggregation."""

    def __init__(self, model: str, tool_names: Optional[Dict[str, str]] = None, display_thinking: bool = True,
                 signature_encoder: Optional[Callable[[str, str], str]] = None, function_encoder=None):
        self.model = model
        self.id = "msg_" + uuid.uuid4().hex
        self.tool_names = tool_names or {}
        self.display_thinking = display_thinking
        self.signature_encoder = signature_encoder
        self.function_encoder = function_encoder
        self.upstream_signatures = {}
        self.content: List[Dict[str, Any]] = []
        self.current: Optional[int] = None
        self.started = False
        self.usage_metadata: Dict[str, Any] = {}
        self.finish_reason: Optional[str] = None
        self.seen_tool_ids: set[str] = set()

    def start(self) -> List[bytes]:
        if self.started:
            return []
        self.started = True
        return [_sse_event("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": _anthropic_usage_from_metadata(self.usage_metadata)}})]

    def close_block(self) -> List[bytes]:
        if self.current is None:
            return []
        index = self.current
        self.current = None
        return [_sse_event("content_block_stop", {"type": "content_block_stop", "index": index})]

    def open_block(self, kind: str) -> List[bytes]:
        events = self.close_block()
        block = {"type": "thinking", "thinking": "", "signature": ""} if kind == "thinking" else {"type": "text", "text": ""}
        self.current = len(self.content)
        self.content.append(block)
        events.append(_sse_event("content_block_start", {"type": "content_block_start", "index": self.current, "content_block": dict(block)}))
        if kind == "thinking" and not self.display_thinking:
            events.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": self.current,
                "delta": {"type": "thinking_delta", "thinking": ""}}))
        return events

    def feed(self, data: Dict[str, Any]) -> List[bytes]:
        data = data.get("response", data)
        if not isinstance(data, dict):
            raise AnthropicStreamError("Upstream response must be an object")
        if "error" in data:
            error = data["error"]
            message = error.get("message", "Upstream request failed") if isinstance(error, dict) else str(error)
            raise AnthropicStreamError(str(message))
        usage = data.get("usageMetadata")
        if isinstance(usage, dict):
            self.usage_metadata.update(usage)
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            raise AnthropicStreamError("Upstream candidates must be an array")
        if len(candidates) > 1:
            raise AnthropicStreamError("Native Messages supports one upstream candidate")
        candidate = candidates[0] if candidates else {}
        if not isinstance(candidate, dict):
            raise AnthropicStreamError("Upstream candidate must be an object")
        parts = (candidate.get("content") or {}).get("parts", [])
        if not isinstance(parts, list):
            raise AnthropicStreamError("Upstream content parts must be an array")
        if self.finish_reason is not None and parts:
            raise AnthropicStreamError("Upstream returned content after finishReason")
        events = self.start()
        for part in parts:
            if not isinstance(part, dict):
                raise AnthropicStreamError("Upstream content part must be an object")
            if is_skip_thought_signature_placeholder(part):
                continue
            if part.get("thought") is True:
                text = part.get("text", "")
                signature = part.get("thoughtSignature", part.get("thought_signature"))
                if not isinstance(text, str) or signature is not None and not isinstance(signature, str):
                    raise AnthropicStreamError("Upstream thinking has an invalid text or signature")
                if not text and not signature:
                    continue
                if not text and signature and (self.current is None or self.content[self.current]["type"] != "thinking"):
                    # A repeated closing signature is harmless; an orphan is not a new empty block.
                    if self.content and self.content[-1].get("type") == "thinking" and self.upstream_signatures.get(len(self.content) - 1) == signature:
                        continue
                    raise AnthropicStreamError("Upstream returned a signature without its thinking block")
                if self.current is None or self.content[self.current]["type"] != "thinking":
                    events += self.open_block("thinking")
                index = self.current
                self.content[index]["thinking"] += text
                if text and self.display_thinking:
                    events.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": index,
                        "delta": {"type": "thinking_delta", "thinking": text}}))
                if signature:
                    self.upstream_signatures[index] = signature
                    if not self.display_thinking:
                        if self.signature_encoder is None:
                            raise AnthropicStreamError("Omitted thinking requires authenticated replay state")
                        signature = self.signature_encoder(signature, self.content[index]["thinking"])
                    self.content[index]["signature"] = signature
                    events.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": index,
                        "delta": {"type": "signature_delta", "signature": signature}}))
                    events += self.close_block()
            elif "text" in part:
                text = part["text"]
                if not isinstance(text, str):
                    raise AnthropicStreamError("Upstream text must be a string")
                if not text:
                    continue
                if self.current is None or self.content[self.current]["type"] != "text":
                    events += self.open_block("text")
                self.content[self.current]["text"] += text
                events.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": self.current,
                    "delta": {"type": "text_delta", "text": text}}))
            elif "functionCall" in part:
                events += self.close_block()
                function = part["functionCall"]
                if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"] or not isinstance(function.get("args", {}), dict):
                    raise AnthropicStreamError("Upstream returned an invalid function call")
                tool_id = function.get("id") or "toolu_" + uuid.uuid4().hex
                if not isinstance(tool_id, str) or tool_id in self.seen_tool_ids:
                    raise AnthropicStreamError("Upstream reused a function call id")
                self.seen_tool_ids.add(tool_id)
                signature = part.get("thoughtSignature")
                if signature and signature != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
                    if not isinstance(signature, str):
                        raise AnthropicStreamError("Upstream function signature must be a string")
                    if not self.display_thinking:
                        if self.function_encoder is None:
                            raise AnthropicStreamError("Omitted thinking requires authenticated function replay state")
                        tool_id = self.function_encoder(tool_id, function["name"], function.get("args", {}), signature)
                    else:
                        tool_id = encode_tool_id_with_signature(tool_id, signature)
                name = self.tool_names.get(function["name"], function["name"])
                args = copy.deepcopy(function.get("args", {}))
                index = len(self.content)
                block = {"type": "tool_use", "id": tool_id, "name": name, "input": args}
                self.content.append(block)
                events += [
                    _sse_event("content_block_start", {"type": "content_block_start", "index": index,
                        "content_block": {**block, "input": {}}}),
                    _sse_event("content_block_delta", {"type": "content_block_delta", "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(args, ensure_ascii=False, separators=(",", ":"))}}),
                    _sse_event("content_block_stop", {"type": "content_block_stop", "index": index}),
                ]
            else:
                raise AnthropicStreamError("Upstream returned an unsupported native Messages content part")
        finish = candidate.get("finishReason")
        if finish is not None:
            if not isinstance(finish, str) or self.finish_reason not in (None, finish):
                raise AnthropicStreamError("Upstream returned inconsistent finishReason")
            self.finish_reason = finish
        return events

    def stop_reason(self) -> str:
        tools = any(block["type"] == "tool_use" for block in self.content)
        if self.finish_reason in ("STOP", "OTHER") and tools:
            return "tool_use"
        if self.finish_reason == "STOP":
            return "end_turn"
        if self.finish_reason == "MAX_TOKENS":
            return "max_tokens"
        if self.finish_reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}:
            return "refusal"
        if self.finish_reason is None:
            raise AnthropicStreamError("Upstream stream ended without finishReason")
        raise AnthropicStreamError("Upstream stream did not complete successfully")

    def finish(self) -> List[bytes]:
        reason = self.stop_reason()
        for block in self.content:
            if block["type"] == "thinking" and not block.get("signature"):
                raise AnthropicStreamError("Upstream thinking omitted its replay signature")
        return self.close_block() + [
            _sse_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None},
                "usage": _anthropic_usage_from_metadata(self.usage_metadata)}),
            _sse_event("message_stop", {"type": "message_stop"}),
        ]

    def response(self) -> Dict[str, Any]:
        self.finish()
        blocks = copy.deepcopy(self.content)
        if not self.display_thinking:
            for block in blocks:
                if block["type"] == "thinking":
                    block["thinking"] = ""
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": blocks,
                "stop_reason": self.stop_reason(), "stop_sequence": None,
                "usage": _anthropic_usage_from_metadata(self.usage_metadata)}


def gemini_to_anthropic_response(gemini_response: Dict[str, Any], model: str, status_code: int = 200,
                                tool_names: Optional[Dict[str, str]] = None, display_thinking: bool = True,
                                signature_encoder=None, function_encoder=None) -> Dict[str, Any]:
    if not 200 <= status_code < 300:
        return gemini_response
    state = AnthropicStreamState(model, tool_names, display_thinking, signature_encoder, function_encoder)
    state.feed(gemini_response)
    return state.response()


async def aggregate_gemini_to_anthropic(stream, model: str, tool_names=None, display_thinking=True, signature_encoder=None, function_encoder=None):
    state = AnthropicStreamState(model, tool_names, display_thinking, signature_encoder, function_encoder)
    events = iter_gemini_stream_events(stream)
    try:
        async for data in events:
            if isinstance(data, Response):
                raise AnthropicStreamError("Upstream request failed", data.status_code if data.status_code >= 400 else 502)
            state.feed(data)
        return state.response()
    finally:
        await close_async_iterator(events)


async def gemini_stream_to_anthropic_stream(gemini_stream: AsyncIterator[bytes], model: str, status_code: int = 200,
                                           tool_names=None, display_thinking=True, signature_encoder=None, function_encoder=None) -> AsyncIterator[bytes]:
    if not 200 <= status_code < 300:
        try:
            async for chunk in gemini_stream:
                yield chunk
        finally:
            await close_async_iterator(gemini_stream)
        return
    state = AnthropicStreamState(model, tool_names, display_thinking, signature_encoder, function_encoder)
    events = iter_gemini_stream_events(gemini_stream)
    try:
        async for data in events:
            if isinstance(data, Response):
                raise AnthropicStreamError("Upstream request failed", data.status_code)
            for event in state.feed(data):
                yield event
        for event in state.finish():
            yield event
    except Exception as error:
        log.error(f"[ANTHROPIC] Stream conversion failed: {type(error).__name__}")
        for event in state.start():
            yield event
        for event in state.close_block():
            yield event
        message = str(error) if isinstance(error, AnthropicStreamError) else "Upstream stream failed"
        yield _sse_event("error", {"type": "error", "error": {"type": "api_error", "message": message}})
    finally:
        await close_async_iterator(events)
        await close_async_iterator(gemini_stream)
