"""
[local patch] Gemini (Antigravity) response -> OpenAI Responses output items / SSE events.

Direct mapping with per-stream state (no chat-chunk intermediate, no global LRU):
part order is preserved (thought -> reasoning item, text -> message item,
functionCall -> function_call / custom_tool_call item), finishReason is mapped to
completed / incomplete / failed, and a stream that ends without finishReason is
reported as failed instead of pretending to complete.
"""
import codecs
import copy
import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.responses import encrypted as enc
from src.responses.request import ConvertedRequest, _arguments_digest
from src.responses.upstream_api import (
    SKIP_THOUGHT_SIGNATURE_VALIDATOR,
    SYNTHETIC_TOOL_NAME,
    _convert_usage_metadata,
    _reverse_transform_args,
    is_internal_placeholder_text,
    is_skip_thought_signature_placeholder,
)

CONTENT_FILTER_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}


# Metadata is an allowlisted extension, never an alternate plaintext carrier for
# signed thinking or replay secrets. Apply the same boundary at every depth.
_OMIT_METADATA = object()
_PRIVATE_METADATA_FIELDS = {
    "thought", "thoughttext", "thoughtsignature", "thoughtsignaturedelta",
    "thinking", "thinkingtext", "reasoning", "reasoningtext", "redactedthinking",
    "signature", "signaturedelta", "encryptedcontent", "encrypteddata",
    "state", "privatestate", "replaystate", "statekey", "responsesstatekey",
    "accesstoken", "refreshtoken", "apikey", "authorization", "password", "clientsecret",
}
_PRIVATE_METADATA_BLOCKS = {"thinking", "reasoning", "redactedthinking"}


def _metadata_field(key: Any) -> str:
    return "".join(char for char in str(key).lower() if char.isalnum())


def _public_metadata(value: Any) -> Tuple[Any, bool]:
    """Return public structure and whether any private field/block was omitted."""
    if isinstance(value, dict):
        if any(_metadata_field(key) == "thought" and item is True for key, item in value.items()):
            return _OMIT_METADATA, True
        if _metadata_field(value.get("type", "")) in _PRIVATE_METADATA_BLOCKS:
            return _OMIT_METADATA, True
        public, omitted = {}, False
        for key, item in value.items():
            if _metadata_field(key) in _PRIVATE_METADATA_FIELDS:
                omitted = True
                continue
            cleaned, private = _public_metadata(item)
            omitted = omitted or private
            if cleaned is not _OMIT_METADATA:
                public[key] = cleaned
        return public, omitted
    if isinstance(value, list):
        public, omitted = [], False
        for item in value:
            cleaned, private = _public_metadata(item)
            omitted = omitted or private
            if cleaned is not _OMIT_METADATA:
                public.append(cleaned)
        return public, omitted
    return copy.deepcopy(value), False


def _rid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def map_usage(usage_metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    u = _convert_usage_metadata(usage_metadata or {})
    if not u:
        return None
    return {
        "input_tokens": u["prompt_tokens"],
        "input_tokens_details": {"cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
        "output_tokens": u["completion_tokens"],
        "output_tokens_details": {"reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)},
        "total_tokens": u["total_tokens"],
    }


class ResponsesMapper:
    def __init__(self, conv: ConvertedRequest, account_ref: Callable[[], Optional[str]] = lambda: None,
                 response_id: Optional[str] = None, warnings_ref: Callable[[], List[str]] = lambda: []):
        self.conv = conv
        self.account_ref = account_ref
        self.warnings_ref = warnings_ref
        self.id = response_id or _rid("resp")
        self.created_at = int(time.time())
        self.seq = 0
        self.output: List[Dict[str, Any]] = []
        self.cur: Optional[Dict[str, Any]] = None       # open item state
        self.usage_metadata: Optional[Dict[str, Any]] = None
        self.finish_reason: Optional[str] = None
        self.model_version: Optional[str] = None
        self.error: Optional[Dict[str, Any]] = None
        self.finished = False
        self.upstream_metadata: Dict[str, Any] = {}
        self._line_buffer = ""
        self._utf8 = codecs.getincrementaldecoder("utf-8")()
        self._event_data: List[str] = []
        self.stats = {"reasoning": 0, "reasoning_signed": 0, "message": 0, "function_call": 0, "custom_tool_call": 0}

    # ------------------------------------------------------------ events
    def _ev(self, etype: str, **payload) -> Dict[str, Any]:
        ev = {"type": etype, "sequence_number": self.seq, **payload}
        self.seq += 1
        return ev

    def response_object(self, status: str) -> Dict[str, Any]:
        e = self.conv.echo
        obj = {
            "id": self.id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "model": self.conv.requested_model,
            "output": list(self.output),
            "usage": None,
        }
        obj.update(e)
        warnings = list(dict.fromkeys(self.conv.ignored + self.warnings_ref()))
        if warnings:
            obj["x_agy_ignored_params"] = warnings
        if self.upstream_metadata:
            obj["x_agy_upstream"] = copy.deepcopy(self.upstream_metadata)
        return obj

    def start_events(self) -> List[Dict[str, Any]]:
        r = self.response_object("in_progress")
        return [self._ev("response.created", response=r), self._ev("response.in_progress", response=r)]

    # ------------------------------------------------------------ item lifecycle
    def _open(self, kind: str) -> List[Dict[str, Any]]:
        evs: List[Dict[str, Any]] = []
        idx = len(self.output)
        if kind == "reasoning":
            item = {"type": "reasoning", "id": _rid("rs"), "summary": []}
            if self.conv.include_encrypted:
                item["encrypted_content"] = None
            self.cur = {"kind": kind, "index": idx, "item": item, "text": "", "sig": None,
                        "visible": self.conv.summary_enabled or self.conv.include_encrypted, "part_open": False}
            self.output.append(item)
            if self.cur["visible"]:
                evs.append(self._ev("response.output_item.added", output_index=idx, item=dict(item)))
        elif kind == "message":
            item = {"type": "message", "id": _rid("msg"), "status": "in_progress", "role": "assistant", "content": []}
            self.cur = {"kind": kind, "index": idx, "item": item, "text": ""}
            self.output.append(item)
            evs.append(self._ev("response.output_item.added", output_index=idx, item=dict(item, content=[])))
            evs.append(self._ev("response.content_part.added", item_id=item["id"], output_index=idx,
                                content_index=0, part={"type": "output_text", "text": "", "annotations": []}))
        return evs

    def _close(self, status: str = "completed") -> List[Dict[str, Any]]:
        evs: List[Dict[str, Any]] = []
        c = self.cur
        if not c:
            return evs
        self.cur = None
        idx, item = c["index"], c["item"]
        if c["kind"] == "reasoning":
            text = c["text"]
            self.stats["reasoning"] += 1
            if self.conv.summary_enabled and text:
                item["summary"] = [{"type": "summary_text", "text": text}]
                if c["part_open"]:
                    evs.append(self._ev("response.reasoning_summary_text.done", item_id=item["id"],
                                        output_index=idx, summary_index=0, text=text))
                    evs.append(self._ev("response.reasoning_summary_part.done", item_id=item["id"], output_index=idx,
                                        summary_index=0, part={"type": "summary_text", "text": text}))
            sig = c["sig"]
            if self.conv.include_encrypted:
                if sig and sig != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
                    try:
                        item["encrypted_content"] = enc.encode(self.conv.real_model, self.account_ref(), sig, text)
                        self.stats["reasoning_signed"] += 1
                    except (enc.StateKeyError, ValueError):
                        item["encrypted_content"] = None
                        self.fail("Unable to preserve authenticated reasoning state")
                else:
                    item["encrypted_content"] = None
            if not c["visible"]:
                # summary=none and no encrypted_content requested: not part of the output
                self.output.pop(idx)
                return evs
            evs.append(self._ev("response.output_item.done", output_index=idx, item=item))
        elif c["kind"] == "message":
            text = c["text"]
            part = {"type": "output_text", "text": text, "annotations": []}
            item["content"] = [part]
            item["status"] = status
            self.stats["message"] += 1
            evs.append(self._ev("response.output_text.done", item_id=item["id"], output_index=idx,
                                content_index=0, text=text, logprobs=[]))
            evs.append(self._ev("response.content_part.done", item_id=item["id"], output_index=idx,
                                content_index=0, part=part))
            evs.append(self._ev("response.output_item.done", output_index=idx, item=item))
        return evs

    # ------------------------------------------------------------ parts
    def _reasoning_text(self, text: str, sig: Optional[str]) -> List[Dict[str, Any]]:
        evs: List[Dict[str, Any]] = []
        if not self.cur or self.cur["kind"] != "reasoning":
            if not text and not sig:
                return evs
            evs += self._close()
            evs += self._open("reasoning")
        c = self.cur
        if text:
            c["text"] += text
            if self.conv.summary_enabled:
                item = c["item"]
                if not c["part_open"]:
                    c["part_open"] = True
                    evs.append(self._ev("response.reasoning_summary_part.added", item_id=item["id"],
                                        output_index=c["index"], summary_index=0,
                                        part={"type": "summary_text", "text": ""}))
                evs.append(self._ev("response.reasoning_summary_text.delta", item_id=item["id"],
                                    output_index=c["index"], summary_index=0, delta=text))
        if sig:
            c["sig"] = sig
            if self.conv.is_claude:
                # Claude: the signature closes the thinking block
                evs += self._close()
        return evs

    def _message_text(self, text: str) -> List[Dict[str, Any]]:
        evs: List[Dict[str, Any]] = []
        if not self.cur or self.cur["kind"] != "message":
            evs += self._close()
            evs += self._open("message")
        c = self.cur
        c["text"] += text
        evs.append(self._ev("response.output_text.delta", item_id=c["item"]["id"], output_index=c["index"],
                            content_index=0, delta=text, logprobs=[]))
        return evs

    def _function_call(self, fc: Dict[str, Any], signature: Optional[str] = None) -> List[Dict[str, Any]]:
        evs = self._close()
        upstream_name = fc.get("name", "nameless_function")
        args = _reverse_transform_args(fc.get("args", {}))
        if not isinstance(args, dict) or not isinstance(upstream_name, str) or not upstream_name:
            self.fail("Malformed upstream function-call arguments")
            return evs
        if self.conv.use_anti_truncation and upstream_name == SYNTHETIC_TOOL_NAME:
            content = args.get("content") if isinstance(args, dict) else None
            return evs + (self._message_text(content) if isinstance(content, str) and content else [])
        if self.conv.tool_map and upstream_name not in self.conv.tool_map:
            self.fail("Upstream requested an undeclared function")
            return evs
        call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:24]}"
        if not isinstance(call_id, str):
            self.fail("Malformed upstream function-call identifier")
            return evs
        if signature and signature != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
            try:
                state = enc.encode(self.conv.real_model, self.account_ref(), signature, "",
                                   call_id=call_id, function_name=upstream_name,
                                   arguments_hash=_arguments_digest(args))
                call_id = enc.CALL_ID_PREFIX + state[len(enc.PREFIX):]
            except (enc.StateKeyError, ValueError):
                self.fail("Unable to preserve authenticated function-call state")
                return evs
        info = self.conv.tool_map.get(upstream_name)
        idx = len(self.output)
        if info is not None and info.kind == "custom":
            raw = args.get("input")
            if not isinstance(raw, str):
                self.fail("Upstream custom tool did not return an input string")
                return evs
            item = {"type": "custom_tool_call", "id": _rid("ctc"), "status": "in_progress", "call_id": call_id,
                    "name": info.name, "input": ""}
            if info.namespace:
                item["namespace"] = info.namespace
            self.output.append(item)
            evs.append(self._ev("response.output_item.added", output_index=idx, item=dict(item)))
            evs.append(self._ev("response.custom_tool_call_input.delta", item_id=item["id"], output_index=idx, delta=raw))
            evs.append(self._ev("response.custom_tool_call_input.done", item_id=item["id"], output_index=idx, input=raw))
            item["input"] = raw
            item["status"] = "completed"
            self.stats["custom_tool_call"] += 1
        else:
            name = info.name if info else upstream_name
            arguments = json.dumps(args)
            item = {"type": "function_call", "id": _rid("fc"), "status": "in_progress", "call_id": call_id,
                    "name": name, "arguments": ""}
            if info is not None and info.namespace:
                item["namespace"] = info.namespace
            self.output.append(item)
            evs.append(self._ev("response.output_item.added", output_index=idx, item=dict(item)))
            evs.append(self._ev("response.function_call_arguments.delta", item_id=item["id"], output_index=idx, delta=arguments))
            evs.append(self._ev("response.function_call_arguments.done", item_id=item["id"], output_index=idx,
                                arguments=arguments, name=name))
            item["arguments"] = arguments
            item["status"] = "completed"
            self.stats["function_call"] += 1
        evs.append(self._ev("response.output_item.done", output_index=idx, item=item))
        return evs

    @staticmethod
    def _inline_markdown(part: Dict[str, Any]) -> Optional[str]:
        # parity with the chat converter (markdown rendering of non-text parts)
        if "executableCode" in part:
            ec = part["executableCode"]
            return f"\n```{ec.get('language', 'python').lower()}\n{ec.get('code', '')}\n```\n"
        if "codeExecutionResult" in part:
            r = part["codeExecutionResult"]
            if r.get("output"):
                return f"\n```{'output' if r.get('outcome') == 'OUTCOME_OK' else 'error'}\n{r['output']}\n```\n"
            return None
        if "inlineData" in part:
            d = part["inlineData"]
            return f"![gemini-generated-content](data:{d.get('mimeType', 'image/png')};base64,{d.get('data', '')})"
        return None

    # ------------------------------------------------------------ input
    def feed_gemini(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Feed one parsed Gemini response object (stream chunk or full non-stream body)."""
        evs: List[Dict[str, Any]] = []
        if self.finished or self.error:
            return evs
        if "error" in data and "candidates" not in data and "response" not in data:
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            self.error = {"code": "server_error", "message": f"upstream error: {msg}"[:2000]}
            return evs
        r = data.get("response", data)
        if not isinstance(r, dict):
            self.fail("Malformed upstream response object")
            return evs
        for key in ("modelVersion", "responseId", "promptFeedback", "usageMetadata"):
            if key in r:
                public, omitted = _public_metadata(r[key])
                if public is not _OMIT_METADATA:
                    self.upstream_metadata[key] = public
                if omitted:
                    self.conv.note("upstream.metadata_private_fields(omitted)")
        candidates = r.get("candidates") or []
        if not isinstance(candidates, list):
            self.fail("Malformed upstream candidates")
            return evs
        if len(candidates) > 1:
            self.fail("Multiple upstream candidates cannot be represented by this Responses endpoint")
            return evs
        for candidate in candidates:
            if not isinstance(candidate, dict):
                self.fail("Malformed upstream candidate")
                return evs
            for key in ("groundingMetadata", "citationMetadata", "urlContextMetadata", "safetyRatings", "avgLogprobs", "logprobsResult", "finishMessage"):
                if key in candidate:
                    public, omitted = _public_metadata(candidate[key])
                    if public is not _OMIT_METADATA:
                        self.upstream_metadata[key] = public
                    if omitted:
                        self.conv.note("upstream.metadata_private_fields(omitted)")
        if r.get("modelVersion") is not None and not isinstance(r["modelVersion"], str):
            self.fail("Malformed upstream model version")
            return evs
        if r.get("usageMetadata") is not None and not isinstance(r["usageMetadata"], dict):
            self.fail("Malformed upstream usage metadata")
            return evs
        if r.get("modelVersion") and not self.model_version:
            self.model_version = r["modelVersion"]
        for cand in candidates:
            content = cand.get("content") or {}
            if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
                self.fail("Malformed upstream content parts")
                return evs
            for part in content.get("parts", []):
                if self.error:
                    return evs
                if not isinstance(part, dict):
                    self.fail("Malformed upstream content part")
                    return evs
                if "text" in part and not isinstance(part["text"], str):
                    self.fail("Malformed upstream text part")
                    continue
                if part.get("thoughtSignature") is not None and not isinstance(part["thoughtSignature"], str):
                    self.fail("Malformed upstream thought signature")
                    continue
                if "functionCall" in part:
                    if not isinstance(part["functionCall"], dict):
                        self.fail("Malformed upstream function call")
                        continue
                    evs += self._function_call(part["functionCall"], part.get("thoughtSignature"))
                elif part.get("thought") is True:
                    if is_skip_thought_signature_placeholder(part):
                        continue
                    evs += self._reasoning_text(part.get("text") or "", part.get("thoughtSignature"))
                elif "text" in part:
                    text = part.get("text") or ""
                    if not isinstance(text, str):
                        self.fail("Malformed upstream text part")
                        continue
                    if not text or is_skip_thought_signature_placeholder(part):
                        continue
                    if part.get("thoughtSignature") and part["thoughtSignature"] != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
                        self.conv.note("upstream.text_signature(not_replayable)")
                    evs += self._message_text(text)
                else:
                    public, omitted = _public_metadata(part)
                    if omitted:
                        self.conv.note("upstream.multimodal_private_fields(not_replayed)")
                    md = self._inline_markdown(public) if isinstance(public, dict) else None
                    if md:
                        self.conv.note("upstream.multimodal(rendered_as_markdown)")
                        self.upstream_metadata.setdefault("multimodal_parts", []).append(public)
                        evs += self._message_text(md)
                    elif set(part) == {"thoughtSignature"}:
                        self.conv.note("upstream.text_signature(not_replayable)")
                    elif part:
                        self.fail("Unsupported upstream content part")
            if cand.get("finishReason"):
                self.finish_reason = cand["finishReason"]
        if r.get("usageMetadata"):
            self.usage_metadata = r["usageMetadata"]
        return evs

    def _consume_event(self) -> List[Dict[str, Any]]:
        if not self._event_data:
            return []
        payload = "\n".join(self._event_data)
        self._event_data = []
        if self.error:
            return []
        if payload == "[DONE]":
            return []
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            self.fail("Malformed upstream SSE JSON")
            return []
        if not isinstance(data, dict):
            self.fail("Malformed upstream SSE object")
            return []
        return self.feed_gemini(data)

    def feed_sse_line(self, line: Any) -> List[Dict[str, Any]]:
        """Parse complete SSE lines or fragmented UTF-8 chunks without losing data."""
        if self.error or self.finished:
            return []
        try:
            text = self._utf8.decode(bytes(line)) if isinstance(line, (bytes, bytearray)) else str(line)
        except UnicodeError:
            self.fail("Invalid upstream SSE UTF-8")
            return []
        if len(self._line_buffer) + len(text) > 8 * 1024 * 1024:
            self.fail("Upstream SSE frame exceeds supported size")
            return []
        # The shared upstream also yields already-split data lines. Flush an
        # unfinished previous line explicitly before accepting another data line.
        if self._line_buffer and text.startswith("data:"):
            self._event_data.append(self._line_buffer[5:].lstrip(" ") if self._line_buffer.startswith("data:") else self._line_buffer)
            self._line_buffer = ""
            prior = self._consume_event()
        else:
            prior = []
        if self.error:
            return prior
        self._line_buffer += text
        evs: List[Dict[str, Any]] = prior
        while "\n" in self._line_buffer:
            raw, self._line_buffer = self._line_buffer.split("\n", 1)
            raw = raw.rstrip("\r")
            if not raw:
                evs += self._consume_event()
            elif raw.startswith("data:"):
                self._event_data.append(raw[5:].lstrip(" "))
            # event:, id:, retry: and comment lines do not carry model data.
        if self.error:
            return evs
        # Complete single-line JSON is safe to accept immediately; an incomplete
        # JSON prefix remains buffered until the next chunk or finish().
        if self._line_buffer.startswith("data:") and not self._event_data:
            payload = self._line_buffer[5:].lstrip(" ")
            try:
                complete = payload == "[DONE]" or isinstance(json.loads(payload), dict)
            except (ValueError, TypeError):
                complete = False
            if complete:
                self._line_buffer = ""
                self._event_data.append(payload)
                evs += self._consume_event()
        return evs

    def fail(self, message: str, code: str = "server_error") -> None:
        if not self.error:
            self.error = {"code": code, "message": message[:2000]}

    # ------------------------------------------------------------ end
    def final_status(self) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """(status, incomplete_details, error)"""
        if self.error:
            return "failed", None, self.error
        fr = self.finish_reason
        if fr is None:
            return "failed", None, {"code": "server_error",
                                    "message": "upstream stream ended without finishReason (truncated)"}
        if fr == "STOP":
            return "completed", None, None
        if fr == "MAX_TOKENS":
            return "incomplete", {"reason": "max_output_tokens"}, None
        if fr in CONTENT_FILTER_REASONS:
            return "incomplete", {"reason": "content_filter"}, None
        if fr == "OTHER" and (self.stats["function_call"] or self.stats["custom_tool_call"]):
            # Antigravity reports Claude's tool_use stop as OTHER (non-stream endpoint); with a
            # complete tool call this is a normal end of turn, not a failure.
            return "completed", None, None
        return "failed", None, {"code": "server_error", "message": f"upstream finishReason={fr}"}

    def finish(self) -> List[Dict[str, Any]]:
        if self.finished:
            return []
        try:
            tail = self._utf8.decode(b"", final=True)
            if tail:
                self._line_buffer += tail
        except UnicodeError:
            self.fail("Truncated upstream SSE UTF-8")
        if self._line_buffer:
            if self._line_buffer.startswith("data:"):
                self._event_data.append(self._line_buffer[5:].lstrip(" "))
            elif self._line_buffer.strip() and not self._line_buffer.startswith(":"):
                self.fail("Truncated upstream SSE frame")
            self._line_buffer = ""
        trailing = self._consume_event()
        status, incomplete, error = self.final_status()
        evs = trailing + self._close("incomplete" if status != "completed" else "completed")
        status, incomplete, error = self.final_status()
        self.finished = True
        resp = self.response_object(status)
        resp["incomplete_details"] = incomplete
        resp["error"] = error
        resp["usage"] = map_usage(self.usage_metadata)
        self.final = resp
        etype = {"completed": "response.completed", "incomplete": "response.incomplete",
                 "failed": "response.failed"}[status]
        evs.append(self._ev(etype, response=resp))
        return evs

    def final_with_output_text(self) -> Dict[str, Any]:
        resp = dict(self.final)
        resp["output_text"] = "".join(
            p.get("text", "") for it in resp["output"] if it.get("type") == "message"
            for p in it.get("content", []) if p.get("type") == "output_text")
        return resp


def sse(ev: Dict[str, Any]) -> bytes:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")
