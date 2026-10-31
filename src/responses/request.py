"""
[local patch] Responses request -> chat-style dict -> Gemini payload.

The chat dict is built by hand (NOT through src.models.OpenAIChatCompletionRequest, whose
pydantic parsing silently drops response_format / max_completion_tokens / ...), then fed
to the exact same upstream converter + normalizer as /v1/chat/completions. Everything the
Responses spec allows but Antigravity cannot honour is either
  * accepted and reported (x-agy-ignored-params header + log), or
  * rejected with HTTP 400 naming the parameter.
Nothing is dropped silently.
"""
import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.responses import encrypted as enc
from src.responses.upstream_api import (
    MAX_OUTPUT_TOKENS,
    SKIP_THOUGHT_SIGNATURE_VALIDATOR,
    _apply_thinking,
    _normalize_function_name,
    convert_openai_to_gemini_request,
    get_base_model_from_feature_model,
    is_anti_truncation_model,
    normalize_antigravity_request,
    resolve_profile,
)


class ResponsesError(Exception):
    def __init__(self, status: int, message: str, param: Optional[str] = None,
                 code: Optional[str] = None, err_type: str = "invalid_request_error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.param = param
        self.code = code
        self.err_type = err_type


def _bad(message: str, param: Optional[str] = None, code: Optional[str] = "unsupported_parameter"):
    raise ResponsesError(400, message, param=param, code=code)


def claude_thinking_with_tools_enabled() -> bool:
    return os.getenv("RESPONSES_CLAUDE_THINKING_WITH_TOOLS", "1").strip().lower() not in ("0", "false", "no", "off")


def history_fallback_enabled() -> bool:
    return os.getenv("RESPONSES_ALLOW_HISTORY_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")


def strip_signatures_on_account_switch() -> bool:
    return os.getenv("RESPONSES_STRIP_SIG_ON_ACCOUNT_SWITCH", "1").strip().lower() not in ("0", "false", "no", "off")


def unknown_params_mode() -> str:
    v = os.getenv("RESPONSES_UNKNOWN_PARAMS", "reject").strip().lower()
    return v if v in ("reject", "ignore") else "reject"


# ---------------------------------------------------------------- field whitelist

# Top-level fields that need server-side state or features Antigravity lacks -> 400 when set.
_STATEFUL_400 = ("previous_response_id", "conversation", "prompt", "context_management", "max_tool_calls")
_KNOWN_TOP = {
    "model", "input", "instructions", "tools", "tool_choice", "temperature", "top_p", "text",
    "max_output_tokens", "stream", "stream_options", "include", "reasoning", "metadata", "store",
    "prompt_cache_key", "parallel_tool_calls", "service_tier", "safety_identifier", "user",
    "prompt_cache_retention", "truncation", "background", "client_metadata", "top_logprobs",
    *_STATEFUL_400,
}
_BUILTIN_TOOL_TYPES_PREFIX = (
    "web_search", "file_search", "code_interpreter", "computer", "image_generation", "mcp",
    "local_shell", "shell", "apply_patch", "tool_search",
)
_SUPPORTED_INCLUDE = {"reasoning.encrypted_content"}

NAMESPACE_SEP = "__"


@dataclass
class ToolInfo:
    kind: str                 # "function" | "custom"
    name: str                 # original (client) name
    namespace: Optional[str] = None


@dataclass
class ConvertedRequest:
    requested_model: str                 # exactly as sent by the client
    real_model: str                      # minus the anti-truncation prefix
    use_anti_truncation: bool
    stream: bool
    chat: Dict[str, Any]
    ignored: List[str] = field(default_factory=list)
    include_encrypted: bool = False
    summary_enabled: bool = True
    max_output_tokens: Optional[int] = None
    tool_map: Dict[str, ToolInfo] = field(default_factory=dict)   # upstream fn name -> client tool
    replay: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # placeholder token -> decoded blob
    replay_dropped: int = 0
    signed_calls: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    prompt_cache_key: Optional[str] = None
    echo: Dict[str, Any] = field(default_factory=dict)
    is_claude: bool = False
    claude_thinking_tools: bool = False

    def note(self, what: str) -> None:
        if what not in self.ignored:
            self.ignored.append(what)


# ---------------------------------------------------------------- helpers

def _check_fields(value: Dict[str, Any], allowed: set, param: str, conv: ConvertedRequest) -> None:
    for key in value:
        if key not in allowed:
            full = f"{param}.{key}"
            if unknown_params_mode() == "reject":
                _bad(f"Unsupported parameter: {full}", full)
            conv.note(full)


def _decode_state(blob: Any, param: str, conv: ConvertedRequest) -> Dict[str, Any]:
    try:
        payload = enc.decode(blob)
    except enc.StateKeyError as exc:
        raise ResponsesError(500, "Responses state key is unavailable", code="server_error", err_type="server_error") from exc
    if payload is None:
        _bad("Invalid or legacy encrypted replay state; restart the conversation", param, "invalid_encrypted_content")
    if payload["m"] != conv.real_model:
        _bad("Encrypted replay state belongs to a different model", param, "state_model_mismatch")
    return payload


def _history_call_id(value: str, param: str, conv: ConvertedRequest) -> str:
    if not value.startswith(enc.CALL_ID_PREFIX):
        return value
    payload = _decode_state(enc.PREFIX + value[len(enc.CALL_ID_PREFIX):], param, conv)
    if payload.get("k") != "function_call":
        _bad("Invalid function-call replay state", param, "invalid_encrypted_content")
    previous = conv.signed_calls.get(payload["id"])
    if previous is not None and previous != payload:
        _bad("Conflicting replay state for a function call", param, "invalid_encrypted_content")
    conv.signed_calls[payload["id"]] = payload
    return payload["id"]


def _arguments_digest(args: Any) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _text_from_parts(parts: Any, param: str, allow_images: bool, role: str, conv: ConvertedRequest) -> Any:
    """Responses content -> chat content (str or list of chat parts)."""
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        _bad(f"{param} must be a string or an array of content parts", param, "invalid_type")
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(parts):
        pp = f"{param}[{i}]"
        if not isinstance(p, dict):
            _bad(f"{pp} must be an object", pp, "invalid_type")
        t = p.get("type")
        if t in ("input_file", "input_audio"):
            _bad(f"{t} is not supported by this gateway", pp, "unsupported_content")
        if t not in ("input_text", "output_text", "text", "refusal", "input_image"):
            _bad(f"Unsupported content part type {t!r}", pp + ".type", "unsupported_content")
        if t in ("input_text", "output_text", "text"):
            allowed = {"type", "text", "annotations", "logprobs"}
        elif t == "refusal":
            allowed = {"type", "refusal"}
        else:
            allowed = {"type", "file_id", "image_url", "detail"}
        _check_fields(p, allowed, pp, conv)
        for key in ("annotations", "logprobs"):
            if p.get(key):
                conv.note(f"{pp}.{key}(not_replayed)")
        if t in ("input_text", "output_text", "text"):
            txt = p.get("text")
            if not isinstance(txt, str):
                _bad(f"{pp}.text must be a string", pp + ".text", "invalid_type")
            out.append({"type": "text", "text": txt})
        elif t == "refusal":
            txt = p.get("refusal")
            if not isinstance(txt, str):
                _bad(f"{pp}.refusal must be a string", pp + ".refusal", "invalid_type")
            out.append({"type": "text", "text": txt})
        elif t == "input_image":
            if not allow_images:
                _bad(f"{pp}: input_image is not supported in a {role} message", pp, "unsupported_content")
            if p.get("file_id"):
                _bad("input_image.file_id is not supported (stateless gateway; send a base64 data URL)",
                     pp + ".file_id", "unsupported_content")
            url = p.get("image_url")
            if not isinstance(url, str) or not url.startswith("data:image/"):
                _bad("input_image.image_url must be a base64 data URL (http(s) URLs are not fetched)",
                     pp + ".image_url", "unsupported_content")
            detail = p.get("detail")
            if detail not in (None, "auto", "low", "high", "original"):
                _bad("Invalid image detail", pp + ".detail", "invalid_value")
            if detail is not None:
                conv.note(f"{pp}.detail(not_supported)")
            out.append({"type": "image_url", "image_url": {"url": url}})
        elif t in ("input_file", "input_audio"):
            _bad(f"{t} is not supported by this gateway", pp, "unsupported_content")
        else:
            _bad(f"Unsupported content part type {t!r}", pp + ".type", "unsupported_content")
    return out


def _flat_name(namespace: Optional[str], name: str) -> str:
    return f"{namespace}{NAMESPACE_SEP}{name}" if namespace else name


def _custom_tool_parameters(tool: Dict[str, Any]) -> Dict[str, Any]:
    desc = "The raw freeform input for this tool, passed through verbatim."
    fmt = tool.get("format")
    if isinstance(fmt, dict) and fmt.get("type") == "grammar":
        desc += (f" It must follow this {fmt.get('syntax', 'grammar')} grammar "
                 f"(not enforced by the gateway):\n{fmt.get('definition', '')}")
    return {"type": "object", "properties": {"input": {"type": "string", "description": desc}},
            "required": ["input"]}


def _convert_tools(tools: Any, conv: ConvertedRequest) -> List[Dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        _bad("tools must be an array", "tools", "invalid_type")
    out: List[Dict[str, Any]] = []

    def add(tool: Dict[str, Any], param: str, namespace: Optional[str], namespace_description: str = ""):
        t = tool.get("type")
        name = tool.get("name")
        if t not in ("function", "custom"):
            if isinstance(t, str) and t.startswith(_BUILTIN_TOOL_TYPES_PREFIX):
                _bad(f"Built-in tool type {t!r} is not supported by this gateway "
                     f"(Codex: set web_search = \"disabled\")", param + ".type")
            _bad(f"Unsupported tool type {t!r}", param + ".type")
        _check_fields(tool, {"type", "name", "description", "parameters", "strict", "format"}, param, conv)
        if not isinstance(name, str) or not name:
            _bad(f"{param}.name is required", param + ".name", "missing_required_parameter")
        flat = _flat_name(namespace, name)
        upstream_name = _normalize_function_name(flat)
        if upstream_name in conv.tool_map:
            _bad(f"Tool name {flat!r} collides with another tool after normalization", param + ".name")
        conv.tool_map[upstream_name] = ToolInfo(kind=t, name=name, namespace=namespace)
        description = tool.get("description") or ""
        if not isinstance(description, str):
            _bad("Tool description must be a string", param + ".description", "invalid_type")
        if namespace_description:
            description = f"Namespace {namespace}: {namespace_description}\n" + description
        if "strict" in tool and not isinstance(tool["strict"], bool):
            _bad("Tool strict must be a boolean", param + ".strict", "invalid_type")
        if tool.get("strict"):
            conv.note("tools[].strict")
        if t == "function":
            fn = {"name": flat, "description": description}
            params = tool.get("parameters")
            if params is not None:
                if not isinstance(params, dict):
                    _bad("Tool parameters must be an object", param + ".parameters", "invalid_type")
                fn["parameters"] = params
            if tool.get("strict"):
                conv.note("tools[].strict")
            out.append({"type": "function", "function": fn})
        else:
            fmt = tool.get("format")
            if fmt is not None:
                if not isinstance(fmt, dict):
                    _bad("Custom tool format must be an object", param + ".format", "invalid_type")
                _check_fields(fmt, {"type", "syntax", "definition"}, param + ".format", conv)
                if fmt.get("type") not in ("text", "grammar"):
                    _bad("Unsupported custom tool format", param + ".format.type")
                if fmt.get("type") == "grammar" and (
                    fmt.get("syntax") not in ("lark", "regex") or not isinstance(fmt.get("definition"), str)
                ):
                    _bad("Grammar format requires syntax and definition", param + ".format", "invalid_value")
            if isinstance(fmt, dict) and fmt.get("type") == "grammar":
                conv.note("tools[].format.grammar(not_enforced)")
            out.append({"type": "function", "function": {
                "name": flat,
                "description": description +
                               "\n[gateway: freeform custom tool; put the raw input in the `input` string]",
                "parameters": _custom_tool_parameters(tool),
            }})

    for i, tool in enumerate(tools):
        param = f"tools[{i}]"
        if not isinstance(tool, dict):
            _bad(f"{param} must be an object", param, "invalid_type")
        if tool.get("type") == "namespace":
            _check_fields(tool, {"type", "name", "description", "tools"}, param, conv)
            ns = tool.get("name")
            if not isinstance(ns, str) or not ns:
                _bad(f"{param}.name is required", param + ".name", "missing_required_parameter")
            inner = tool.get("tools")
            if not isinstance(inner, list):
                _bad(f"{param}.tools must be an array", param + ".tools", "invalid_type")
            for j, sub in enumerate(inner):
                if not isinstance(sub, dict):
                    _bad(f"{param}.tools[{j}] must be an object", f"{param}.tools[{j}]", "invalid_type")
                ns_description = tool.get("description") or ""
                if not isinstance(ns_description, str):
                    _bad("Namespace description must be a string", param + ".description", "invalid_type")
                add(sub, f"{param}.tools[{j}]", ns, ns_description)
        else:
            add(tool, param, None)
    return out


def _convert_tool_choice(tc: Any, conv: ConvertedRequest) -> Any:
    if tc is None:
        return None
    if isinstance(tc, str):
        if tc in ("auto", "none", "required"):
            return tc
        _bad(f"Unsupported tool_choice {tc!r}", "tool_choice")
    if isinstance(tc, dict):
        _check_fields(tc, {"type", "name", "namespace"}, "tool_choice", conv)
        t = tc.get("type")
        if t in ("function", "custom"):
            name = tc.get("name")
            if not isinstance(name, str) or not name:
                _bad("tool_choice.name is required", "tool_choice.name", "missing_required_parameter")
            flat = _flat_name(tc.get("namespace"), name)
            return {"type": "function", "function": {"name": flat}}
        _bad(f"tool_choice type {t!r} is not supported", "tool_choice.type")
    _bad("tool_choice must be a string or object", "tool_choice", "invalid_type")


# ---------------------------------------------------------------- main conversion

def convert_request(body: Dict[str, Any]) -> ConvertedRequest:
    if not isinstance(body, dict):
        _bad("Request body must be a JSON object", None, "invalid_type")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        _bad("model is required", "model", "missing_required_parameter")

    # ---- unknown / stateful / unsupported top-level fields
    unknown = [k for k in body if k not in _KNOWN_TOP]
    for k in _STATEFUL_400:
        if body.get(k) not in (None, "", [], {}):
            _bad(f"{k} is not supported: this gateway is stateless (send the full input each turn)", k)
    if body.get("background"):
        _bad("background is not supported", "background")

    real_model = get_base_model_from_feature_model(model)
    conv = ConvertedRequest(
        requested_model=model,
        real_model=real_model,
        use_anti_truncation=is_anti_truncation_model(model),
        stream=bool(body.get("stream")),
        chat={},
    )
    if "stream" in body and not isinstance(body["stream"], bool):
        _bad("stream must be a boolean", "stream", "invalid_type")
    for key in ("store", "background", "parallel_tool_calls"):
        if key in body and not isinstance(body[key], bool):
            _bad(f"{key} must be a boolean", key, "invalid_type")
    conv.is_claude = "claude" in real_model.lower()
    conv.claude_thinking_tools = conv.is_claude and claude_thinking_with_tools_enabled()

    if unknown:
        if unknown_params_mode() == "reject":
            _bad(f"Unsupported parameter: {unknown[0]} (set RESPONSES_UNKNOWN_PARAMS=ignore on the "
                 f"gateway to accept-and-report unknown parameters)", unknown[0])
        for k in unknown:
            conv.note(k)

    # ---- accept-but-ignore (reported) fields
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict):
        _bad("reasoning must be an object", "reasoning", "invalid_type")
    _check_fields(reasoning, {"effort", "summary", "generate_summary"}, "reasoning", conv)
    if reasoning.get("effort") is not None:
        conv.note("reasoning.effort")  # NEVER changes the model; depth comes from the model id
    summary = reasoning.get("summary", reasoning.get("generate_summary"))
    if summary == "none":
        conv.summary_enabled = False
    elif summary not in (None, "auto", "concise", "detailed"):
        _bad("Invalid reasoning summary", "reasoning.summary", "invalid_value")
    elif summary in ("concise", "detailed"):
        conv.note(f"reasoning.summary={summary}")
    text = body.get("text") or {}
    if not isinstance(text, dict):
        _bad("text must be an object", "text", "invalid_type")
    _check_fields(text, {"verbosity", "format"}, "text", conv)
    if text.get("verbosity") is not None:
        conv.note("text.verbosity")
    if body.get("parallel_tool_calls") is False:
        conv.note("parallel_tool_calls")
    if body.get("service_tier") not in (None, "auto", "default"):
        conv.note("service_tier")
    for k in ("safety_identifier", "user", "prompt_cache_retention", "client_metadata"):
        if body.get(k) is not None:
            conv.note(k)
    if body.get("top_logprobs"):
        conv.note("top_logprobs")
    if body.get("truncation") == "auto":
        conv.note("truncation=auto")
    elif body.get("truncation") not in (None, "disabled", "auto"):
        _bad("truncation must be 'auto' or 'disabled'", "truncation", "invalid_value")
    if body.get("store"):
        conv.note("store(not_stored)")
    so = body.get("stream_options")
    if so is not None:
        if not isinstance(so, dict):
            _bad("stream_options must be an object", "stream_options", "invalid_type")
        _check_fields(so, {"include_obfuscation"}, "stream_options", conv)
    if isinstance(so, dict) and so.get("include_obfuscation"):
        conv.note("stream_options.include_obfuscation")
    md = body.get("metadata")
    if md is not None and not isinstance(md, dict):
        _bad("metadata must be an object", "metadata", "invalid_type")

    include = body.get("include") or []
    if not isinstance(include, list):
        _bad("include must be an array", "include", "invalid_type")
    for inc in include:
        if not isinstance(inc, str):
            _bad("include entries must be strings", "include", "invalid_type")
        if inc in _SUPPORTED_INCLUDE:
            conv.include_encrypted = True
        else:
            conv.note(f"include:{inc}")

    pck = body.get("prompt_cache_key")
    if pck is not None and not isinstance(pck, str):
        _bad("prompt_cache_key must be a string", "prompt_cache_key", "invalid_type")
    conv.prompt_cache_key = pck or None

    mot = body.get("max_output_tokens")
    if mot is not None:
        if not isinstance(mot, int) or isinstance(mot, bool) or mot < 1:
            _bad("max_output_tokens must be a positive integer", "max_output_tokens", "invalid_value")
        conv.max_output_tokens = min(mot, MAX_OUTPUT_TOKENS)
        if mot > MAX_OUTPUT_TOKENS:
            conv.note(f"max_output_tokens(capped_at_{MAX_OUTPUT_TOKENS})")

    # ---- chat dict
    chat: Dict[str, Any] = {"model": real_model, "_preserve_tool_arguments": True}
    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            _bad("instructions must be a string", "instructions", "invalid_type")
        if instructions:
            messages.append({"role": "system", "content": instructions})

    tools = _convert_tools(body.get("tools"), conv)
    _append_input(body.get("input"), messages, conv)
    chat["messages"] = messages
    if tools:
        chat["tools"] = tools
    tc = _convert_tool_choice(body.get("tool_choice"), conv)
    if tc is not None:
        chat["tool_choice"] = tc
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            value = body[k]
            limit = 2 if k == "temperature" else 1
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= limit:
                _bad(f"{k} must be between 0 and {limit}", k, "invalid_value")
            chat[k] = value
    if conv.max_output_tokens is not None:
        chat["max_tokens"] = conv.max_output_tokens

    fmt = text.get("format")
    if fmt is not None:
        if not isinstance(fmt, dict):
            _bad("text.format must be an object", "text.format", "invalid_type")
        _check_fields(fmt, {"type", "name", "schema", "strict", "description"}, "text.format", conv)
        if fmt.get("description"):
            conv.note("text.format.description(not_supported)")
        ft = fmt.get("type")
        if ft == "json_schema":
            if not isinstance(fmt.get("schema"), dict):
                _bad("text.format.schema is required for json_schema", "text.format.schema",
                     "missing_required_parameter")
            chat["response_format"] = {"type": "json_schema", "json_schema": {
                "name": fmt.get("name"), "schema": fmt["schema"], "strict": fmt.get("strict")}}
            if fmt.get("strict"):
                conv.note("text.format.strict")
        elif ft == "json_object":
            chat["response_format"] = {"type": "json_object"}
        elif ft == "text":
            pass  # default; chat path would send responseMimeType=text/plain only if asked explicitly
        else:
            _bad(f"Unsupported text.format.type {ft!r}", "text.format.type", "invalid_value")
    conv.chat = chat

    conv.echo = {
        "instructions": instructions,
        "max_output_tokens": conv.max_output_tokens,
        "metadata": md or {},
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "reasoning": {"effort": reasoning.get("effort"), "summary": summary},
        "temperature": body.get("temperature"),
        "text": {"format": fmt or {"type": "text"}, **({"verbosity": text["verbosity"]} if text.get("verbosity") else {})},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools") or [],
        "top_p": body.get("top_p"),
        "truncation": body.get("truncation") or "disabled",
        "user": body.get("user"),
        "prompt_cache_key": pck,
        "service_tier": "default",
        "store": False,
        "background": False,
        "previous_response_id": None,
    }
    return conv


def _append_input(inp: Any, messages: List[Dict[str, Any]], conv: ConvertedRequest) -> None:
    if inp is None:
        return
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
        return
    if not isinstance(inp, list):
        _bad("input must be a string or an array of items", "input", "invalid_type")

    # (msg, has_tool_results_after) bookkeeping for assistant-turn grouping
    def current_assistant(allow_after_tool_calls: bool) -> Dict[str, Any]:
        last = messages[-1] if messages else None
        if last is not None and last.get("role") == "assistant" and (allow_after_tool_calls or not last.get("tool_calls")):
            return last
        msg = {"role": "assistant", "content": []}
        messages.append(msg)
        return msg

    def add_assistant_content(parts: List[Dict[str, Any]]):
        msg = current_assistant(allow_after_tool_calls=False)
        if not isinstance(msg.get("content"), list):
            msg["content"] = [] if not msg.get("content") else [{"type": "text", "text": msg["content"]}]
        msg["content"].extend(parts)

    for i, item in enumerate(inp):
        param = f"input[{i}]"
        if not isinstance(item, dict):
            _bad(f"{param} must be an object", param, "invalid_type")
        t = item.get("type")
        if t is None and "role" in item:
            t = "message"
        if t == "message":
            _check_fields(item, {"type", "id", "role", "content", "status", "phase"}, param, conv)
            if item.get("phase"):
                conv.note(f"{param}.phase(not_supported)")
            role = item.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                _bad(f"Unsupported role {role!r}", param + ".role", "invalid_value")
            content = _text_from_parts(item.get("content"), param + ".content",
                                       allow_images=(role == "user"), role=role, conv=conv)
            if role == "assistant":
                parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
                parts = [p for p in parts if p.get("text") != ""]
                if parts:
                    add_assistant_content(parts)
            else:
                if role == "developer":
                    conv.note(f"{param}.role(merged_into_system_instruction)")
                if role in ("system", "developer") and any(m.get("role") in ("user", "assistant", "tool") for m in messages):
                    conv.note(f"{param}.role(converted_to_user_context)")
                if role in ("system", "developer") and isinstance(content, list) and any(
                        p.get("type") != "text" for p in content):
                    _bad("system/developer messages may only contain text", param + ".content", "unsupported_content")
                messages.append({"role": role, "content": content})
        elif t in ("function_call", "custom_tool_call"):
            _check_fields(item, {"type", "id", "call_id", "name", "namespace", "arguments", "input", "status"}, param, conv)
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id:
                _bad(f"{param}.call_id is required", param + ".call_id", "missing_required_parameter")
            if not isinstance(name, str) or not name:
                _bad(f"{param}.name is required", param + ".name", "missing_required_parameter")
            call_id = _history_call_id(call_id, param + ".call_id", conv)
            flat = _flat_name(item.get("namespace"), name)
            if t == "function_call":
                args = item.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args)
                try:
                    parsed = json.loads(args)
                except (ValueError, TypeError):
                    _bad("Function-call arguments must be valid JSON", param + ".arguments", "invalid_value")
                if not isinstance(parsed, dict):
                    _bad("Function-call arguments must be a JSON object", param + ".arguments", "invalid_type")
            else:
                raw = item.get("input", "")
                if not isinstance(raw, str):
                    _bad(f"{param}.input must be a string", param + ".input", "invalid_type")
                args = json.dumps({"input": raw}, ensure_ascii=False)
            signed = conv.signed_calls.get(call_id)
            if signed is not None:
                try:
                    decoded_args = json.loads(args)
                except (ValueError, TypeError):
                    _bad("Invalid signed function-call arguments", param + ".arguments", "invalid_encrypted_content")
                if signed["n"] != _normalize_function_name(flat) or signed["g"] != _arguments_digest(decoded_args):
                    _bad("Signed function call was modified", param, "state_tool_mismatch")
            msg = current_assistant(allow_after_tool_calls=True)
            msg.setdefault("tool_calls", []).append(
                {"id": call_id, "type": "function", "function": {"name": flat, "arguments": args}})
            if msg.get("content") == []:
                msg["content"] = None
        elif t in ("function_call_output", "custom_tool_call_output"):
            _check_fields(item, {"type", "id", "call_id", "output", "status"}, param, conv)
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                _bad(f"{param}.call_id is required", param + ".call_id", "missing_required_parameter")
            call_id = _history_call_id(call_id, param + ".call_id", conv)
            out = item.get("output", "")
            if isinstance(out, list):
                texts = []
                for j, p in enumerate(out):
                    if isinstance(p, dict) and p.get("type") in ("input_text", "output_text", "text") \
                            and isinstance(p.get("text"), str):
                        texts.append(p["text"])
                    else:
                        _bad("Tool output arrays may only contain text parts on this gateway",
                             f"{param}.output[{j}]", "unsupported_content")
                out = "".join(texts)
            elif not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": out})
        elif t == "reasoning":
            _handle_reasoning_item(item, param, conv, add_assistant_content)
        elif t == "item_reference":
            _bad("item_reference is not supported: this gateway is stateless", param + ".type")
        else:
            _bad(f"Unsupported input item type {t!r}", param + ".type")

    # empty assistant shells (e.g. only dropped reasoning) are removed
    messages[:] = [m for m in messages
                   if not (m.get("role") == "assistant" and not m.get("content") and not m.get("tool_calls"))]
    for m in messages:
        if m.get("role") == "assistant" and m.get("content") == []:
            m["content"] = None


def _handle_reasoning_item(item: Dict[str, Any], param: str, conv: ConvertedRequest, add_assistant_content) -> None:
    """Claude + flag: replay the signed thinking block at its original position.
    Otherwise reasoning items are not sent upstream (upstream signature policy)."""
    _check_fields(item, {"type", "id", "encrypted_content", "summary", "status", "content"}, param, conv)
    blob = item.get("encrypted_content")
    if conv.is_claude and not conv.claude_thinking_tools:
        _bad("Reasoning replay is disabled for this Claude configuration", param, "unsupported_content")
    if not blob:
        _bad("Reasoning replay requires authenticated encrypted_content", param + ".encrypted_content", "missing_encrypted_content")
    payload = _decode_state(blob, param + ".encrypted_content", conv)
    if payload.get("k") == "function_call" or payload["s"] == SKIP_THOUGHT_SIGNATURE_VALIDATOR:
        _bad("Invalid reasoning replay state", param + ".encrypted_content", "invalid_encrypted_content")
    token = f"\u2063agy-replay:{uuid.uuid4().hex}\u2063"
    conv.replay[token] = payload
    add_assistant_content([{"type": "text", "text": token}])


# ---------------------------------------------------------------- Gemini payload

def pck_session_key(model: str, prompt_cache_key: Optional[str]) -> Optional[str]:
    if not prompt_cache_key:
        return None
    return "pck:" + hashlib.sha256(f"{model}|{prompt_cache_key}".encode("utf-8")).hexdigest()[:32]


def _is_replayed_thought(part: Any) -> bool:
    return isinstance(part, dict) and part.get("thought") is True and bool(part.get("thoughtSignature")) \
        and part.get("thoughtSignature") != SKIP_THOUGHT_SIGNATURE_VALIDATOR


def strip_replayed_thoughts(gemini_request: Dict[str, Any]) -> int:
    """Remove replayed thought parts in place (signature fallback / account switch). Returns count."""
    removed = 0
    contents = gemini_request.get("contents")
    if not isinstance(contents, list):
        return 0
    new_contents = []
    for c in contents:
        if isinstance(c, dict) and isinstance(c.get("parts"), list):
            parts = [p for p in c["parts"] if not _is_replayed_thought(p)]
            removed += len(c["parts"]) - len(parts)
            if not parts:
                continue
            c = {**c, "parts": parts}
        new_contents.append(c)
    gemini_request["contents"] = new_contents
    return removed


async def build_gemini_request(conv: ConvertedRequest) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Returns (api_request {"model","request"}, info). Raises ResponsesError(400) on conversion errors."""
    info: Dict[str, Any] = {"thinking_restored": False, "replayed": 0}
    try:
        gemini = await convert_openai_to_gemini_request(copy.deepcopy(conv.chat))
    except ValueError as exc:
        raise ResponsesError(400, str(exc), code="invalid_request") from exc

    # placeholder tokens -> original signed thought parts (Claude only)
    if conv.replay:
        for c in gemini.get("contents", []):
            for idx, part in enumerate(c.get("parts", []) or []):
                if isinstance(part, dict) and part.get("text") in conv.replay and "functionCall" not in part:
                    payload = conv.replay[part["text"]]
                    c["parts"][idx] = {"thought": True, "text": payload["t"], "thoughtSignature": payload["s"]}
                    info["replayed"] += 1

    gemini["model"] = conv.real_model
    if conv.is_claude:
        _split_claude_tool_declarations(gemini, info)
    for content in gemini.get("contents", []):
        for part in content.get("parts", []) or []:
            call = part.get("functionCall") if isinstance(part, dict) else None
            if isinstance(call, dict) and call.get("id") in conv.signed_calls:
                part["thoughtSignature"] = conv.signed_calls[call["id"]]["s"]
    try:
        normalized = await normalize_antigravity_request(gemini)
    except ValueError as exc:
        raise ResponsesError(400, str(exc), code="unsupported_content", param="input") from exc

    if conv.claude_thinking_tools:
        _claude_thinking_post(normalized, conv, info)

    if conv.max_output_tokens is not None:
        gc = normalized.setdefault("generationConfig", {})
        gc["maxOutputTokens"] = min(conv.max_output_tokens, MAX_OUTPUT_TOKENS)

    original_schemas = {
        _normalize_function_name(tool["function"]["name"]): tool["function"].get("parameters", {})
        for tool in conv.chat.get("tools", [])
    }
    for tool in normalized.get("tools", []) or []:
        for declaration in tool.get("functionDeclarations", []) or []:
            effective = declaration.get("parameters", declaration.get("parametersJsonSchema", {}))
            if declaration.get("name") in original_schemas and effective != original_schemas[declaration["name"]]:
                conv.note("tools[].parameters(translated_to_upstream_schema)")
    if conv.chat.get("response_format", {}).get("type") == "json_schema":
        requested_schema = conv.chat["response_format"]["json_schema"]["schema"]
        if normalized.get("generationConfig", {}).get("responseSchema") != requested_schema:
            conv.note("text.format.schema(translated_to_upstream_schema)")

    for content in normalized.get("contents", []):
        if not isinstance(content, dict) or content.get("role") not in ("user", "model"):
            raise ResponsesError(400, "Unrepresentable content role for Antigravity", code="invalid_request")

    api_request = {"model": normalized.pop("model"), "request": normalized}
    return api_request, info


def _split_claude_tool_declarations(gemini: Dict[str, Any], info: Dict[str, Any]) -> None:
    """Upstream _ensure_empty_tool_schema_for_claude keeps only declarations[0] of every tools
    entry, and convert_openai_tools_to_gemini puts all functions into ONE entry, so a Claude
    request with N function tools reaches Antigravity with only the first tool (chat path bug,
    observed 2026-10-28). Emit one entry per declaration (the shape anthropic2gemini already
    uses) so nothing is lost on the Responses path."""
    tools = gemini.get("tools")
    if not isinstance(tools, list):
        return
    split: List[Dict[str, Any]] = []
    for t in tools:
        decls = t.get("functionDeclarations") if isinstance(t, dict) else None
        if isinstance(decls, list) and len(decls) > 1 and len(t) == 1:
            split.extend({"functionDeclarations": [d]} for d in decls)
            info["tool_entries_split"] = info.get("tool_entries_split", 0) + len(decls)
        else:
            split.append(t)
    gemini["tools"] = split


def _claude_thinking_post(normalized: Dict[str, Any], conv: ConvertedRequest, info: Dict[str, Any]) -> None:
    """Responses-only Claude adjustments (flag RESPONSES_CLAUDE_THINKING_WITH_TOOLS, default on).

    Upstream _prepare_claude_history drops thinkingConfig whenever the history contains a
    functionCall. Empirically (2026-10-28, claude-opus-5-5-high / claude-opus-4-6-thinking)
    Antigravity accepts thinking + tool history both with and without the original signed
    thinking block, so we restore the profile's thinkingConfig. A skip-sentinel signature on
    a functionCall part next to a real thinking block makes Antigravity reject the block
    ("Invalid `signature` in `thinking` block"), and Claude does not need the sentinel at all,
    so sentinels are removed from Claude functionCall parts here.
    """
    profile = resolve_profile(normalized.get("model", ""))
    gc = normalized.setdefault("generationConfig", {})
    if profile.thinking != "none":
        wanted: Dict[str, Any] = {}
        _apply_thinking(profile, wanted, True)
        if "thinkingConfig" not in gc and "thinkingConfig" in wanted:
            gc["thinkingConfig"] = wanted["thinkingConfig"]
            info["thinking_restored"] = True
        elif "thinkingConfig" in gc:
            gc["thinkingConfig"] = {**gc["thinkingConfig"], "includeThoughts": True}
        gc.setdefault("maxOutputTokens", MAX_OUTPUT_TOKENS)
        gc.setdefault("topK", 64)
    for c in normalized.get("contents", []) or []:
        for p in c.get("parts", []) or []:
            if isinstance(p, dict) and "functionCall" in p and p.get("thoughtSignature") == SKIP_THOUGHT_SIGNATURE_VALIDATOR:
                p.pop("thoughtSignature", None)
