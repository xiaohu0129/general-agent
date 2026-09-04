"""OpenAI 兼容 HTTP -> langchain BaseChatModel 适配层。

对接任意 OpenAI 兼容端点（POST {base_url}/v1/chat/completions）：
_generate 调非流式接口，_astream 调 SSE 流式接口（data: {chunk}，[DONE] 结束）。
支持 bind_tools + tool_calls（function calling）。
每次调用建 llm_call span（model 属性）+ 记录 llm metric（duration/tokens/errors）。
本地联调可使用 stub：python -m general_agent.stub_llm（默认 :9094）。
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from . import observability
from .logging_setup import get_logger

logger = get_logger(__name__)

_ROLE_MAP = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}

_STATUS_TO_CODE = {
    429: "RATE_LIMIT",
    401: "AUTH",
    403: "AUTH",
    504: "TIMEOUT",
    503: "UNAVAILABLE",
    400: "CONTENT_FILTER",
}


class LLMError(Exception):
    """LLM 服务错误，携带 error_code。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"llm error: {code} {message}")


def _classify_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return code
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return _STATUS_TO_CODE.get(status, "INTERNAL")
    if isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT"
    return "INTERNAL"


def _content_to_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


class OpenAICompatibleModel(BaseChatModel):
    base_url: str = "http://localhost:9094"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    timeout: float = 60.0
    temperature: float = 0.0  # 默认 0：执行/路由 LLM 确定性优先
    transport: Any = None  # 注入 httpx transport（测试用 MockTransport）

    _bound_tools: list[dict] = PrivateAttr(default_factory=list)
    _bound_tool_choice: Any = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "openai-compatible"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        clone = self.model_copy()
        clone._bound_tools = [self._format_tool(t) for t in tools]
        clone._bound_tool_choice = tool_choice
        return clone

    @staticmethod
    def _format_tool(t) -> dict:
        if isinstance(t, BaseTool):
            schema = t.args_schema.model_json_schema() if t.args_schema else {"type": "object", "properties": {}}
            return {
                "type": "function",
                "function": {"name": t.name, "description": t.description or "", "parameters": schema},
            }
        if isinstance(t, dict):
            return t
        name = getattr(t, "name", getattr(t, "__name__", "tool"))
        desc = getattr(t, "description", "") or ""
        schema = getattr(t, "args_schema", None)
        schema = schema.model_json_schema() if schema else {"type": "object", "properties": {}}
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}

    def _build_body(self, messages, *, stream: bool, **kwargs) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [self._msg_to_dict(m) for m in messages],
            "stream": stream,
            "temperature": self.temperature,
        }
        if self._bound_tools:
            body["tools"] = self._bound_tools
            body["tool_choice"] = self._bound_tool_choice or "auto"
        if stream:
            # 让流式响应末尾携带 usage
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def _msg_to_dict(m: BaseMessage) -> dict:
        role = _ROLE_MAP.get(m.type, m.type)
        d: dict[str, Any] = {"role": role, "content": _content_to_str(m.content)}
        if isinstance(m, ToolMessage):
            d["tool_call_id"] = m.tool_call_id
        if isinstance(m, AIMessage) and m.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"], ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ]
        return d

    @staticmethod
    def _parse_message(data: dict) -> AIMessage:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments", "{}")
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                args = {}
            tool_calls.append(
                {"id": tc.get("id", ""), "name": fn.get("name", ""), "args": args, "type": "tool_call"}
            )
        ai = AIMessage(content=msg.get("content") or "", tool_calls=tool_calls)
        usage = data.get("usage") or {}
        if usage:
            ai.usage_metadata = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
        ai.response_metadata = {
            "finish_reason": choice.get("finish_reason") or "stop",
            "model": data.get("model", ""),
        }
        return ai

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/v1/chat/completions"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        body = self._build_body(messages, stream=False, **kwargs)
        tracer = observability.get_tracer()
        start = time.monotonic()
        with tracer.start_as_current_span("llm_call") as span:
            span.set_attribute("model", self.model)
            try:
                with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                    r = client.post(self._endpoint(), json=body, headers=self._headers())
                    if r.is_error:
                        raise self._http_error(r)
                    data = r.json()
                ai = self._parse_message(data)
                usage = ai.usage_metadata or {}
                observability.record_llm(
                    (time.monotonic() - start) * 1000,
                    model=self.model or data.get("model", ""),
                    prompt_tokens=usage.get("input_tokens", 0),
                    completion_tokens=usage.get("output_tokens", 0),
                )
                return ChatResult(generations=[ChatGeneration(message=ai)])
            except Exception as exc:
                code = _classify_error(exc)
                observability.record_span_error(span, code, str(exc))
                observability.record_llm(
                    (time.monotonic() - start) * 1000, model=self.model, error_code=code
                )
                raise

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        body = self._build_body(messages, stream=True, **kwargs)
        tracer = observability.get_tracer()
        start = time.monotonic()
        model_name = self.model
        prompt_tokens = 0
        completion_tokens = 0
        with tracer.start_as_current_span("llm_call") as span:
            span.set_attribute("model", self.model)
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                    async with client.stream("POST", self._endpoint(), json=body, headers=self._headers()) as r:
                        if r.is_error:
                            await r.aread()
                            raise self._http_error(r)
                        async for line in r.aiter_lines():
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            for chunk in self._emit_chunk(json.loads(payload)):
                                um = getattr(chunk.message, "usage_metadata", None)
                                if um:
                                    prompt_tokens = um.get("input_tokens", 0)
                                    completion_tokens = um.get("output_tokens", 0)
                                rm = getattr(chunk.message, "response_metadata", None) or {}
                                if rm.get("model"):
                                    model_name = rm["model"]
                                yield chunk
                observability.record_llm(
                    (time.monotonic() - start) * 1000,
                    model=model_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            except Exception as exc:
                code = _classify_error(exc)
                observability.record_span_error(span, code, str(exc))
                observability.record_llm(
                    (time.monotonic() - start) * 1000, model=model_name, error_code=code
                )
                raise

    def _emit_chunk(self, data: dict):
        # 末尾 usage chunk：choices 为空，仅携带 usage
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        if not choices:
            if usage:
                chunk = AIMessageChunk(content="")
                chunk.usage_metadata = {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                yield ChatGenerationChunk(message=chunk)
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        content = delta.get("content")
        tool_call_chunks = []
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_call_chunks.append(
                {
                    "name": fn.get("name", ""),
                    "args": fn.get("arguments", ""),
                    "id": tc.get("id", ""),
                    "index": tc.get("index", 0),
                    "type": "tool_call_chunk",
                }
            )
        message = AIMessageChunk(content=content or "", tool_call_chunks=tool_call_chunks)
        meta: dict[str, Any] = {}
        if finish_reason:
            meta["finish_reason"] = finish_reason
        if data.get("model"):
            meta["model"] = data["model"]
        if meta:
            message.response_metadata = meta
        yield ChatGenerationChunk(message=message)

    @staticmethod
    def _http_error(r: httpx.Response) -> LLMError:
        try:
            err = r.json().get("error") or {}
            message = err.get("message") or r.text[:200]
            code = str(err.get("code") or "").upper() or _STATUS_TO_CODE.get(r.status_code, "INTERNAL")
        except Exception:
            message = r.text[:200]
            code = _STATUS_TO_CODE.get(r.status_code, "INTERNAL")
        return LLMError(code, message)
