"""OpenAI 兼容 stub LLM 服务：实现 /v1/chat/completions（非流式 + SSE 流式）。

按规则模拟 agent：最后一条为 tool 结果 -> 给出最终回答；
用户消息含触发词（"工具/调用/create/任务"）-> 调用请求中声明的第一个工具；否则普通回复。
供 general-agent 在真实 LLM 端点就绪前端到端联调。
运行: python -m general_agent.stub_llm  (默认 :9094)
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="openai-compatible llm stub")

_USAGE = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
_TRIGGERS = ("工具", "调用", "create", "任务", "task")

# ---------------- 确定性 stub embedding（哈希向量，无语义、仅供无外部依赖测试） ----------------
_EMBED_DIM = 256
_TOKEN_RE = re.compile(r"[0-9a-z]+|[\u4e00-\u9fff]", re.IGNORECASE)


def _stub_embedding(text: str) -> list[float]:
    """基于 token 哈希的确定性向量：相同文本同向量；共享 token 的文本余弦相近。"""
    vec = [0.0] * _EMBED_DIM
    tokens = _TOKEN_RE.findall(text.lower())
    for tok in tokens:
        digest = hashlib.md5(tok.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % _EMBED_DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class EmbeddingsBody(BaseModel):
    model: str | None = None
    input: str | list[str]


@app.post("/v1/embeddings")
async def embeddings(body: EmbeddingsBody):
    texts = [body.input] if isinstance(body.input, str) else body.input
    data = [
        {"object": "embedding", "index": i, "embedding": _stub_embedding(t)}
        for i, t in enumerate(texts)
    ]
    return JSONResponse(
        {
            "object": "list",
            "data": data,
            "model": body.model or "stub-embedding",
            "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": 0},
        }
    )


def _tool_name(body: dict) -> str:
    tools = body.get("tools") or []
    if tools:
        return (tools[0].get("function") or {}).get("name", "") or "demo_skill"
    return "demo_skill"


def _decide(body: dict) -> dict:
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    tool_name = _tool_name(body)
    if last.get("role") == "tool":
        content = last.get("content", "")
        text = f"工具已执行完成（{str(content)[:48]}）。"
        return {"content": text, "tool_calls": [], "finish_reason": "stop"}
    text = last.get("content", "") or ""
    if any(k in text for k in _TRIGGERS):
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps({"query": text}, ensure_ascii=False)},
                }
            ],
            "finish_reason": "tool_calls",
        }
    return {
        "content": "我是通用 Agent stub，可以通过注册的 Skill（工具）帮你完成任务。",
        "tool_calls": [],
        "finish_reason": "stop",
    }


def _completion(body: dict, result: dict) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": result["content"]}
    if result["tool_calls"]:
        message["tool_calls"] = result["tool_calls"]
    return {
        "id": f"chatcmpl-{uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model") or "stub",
        "choices": [{"index": 0, "message": message, "finish_reason": result["finish_reason"]}],
        "usage": _USAGE,
    }


class ChatBody(BaseModel):
    model: str | None = None
    messages: list[dict]
    tools: list | None = None
    tool_choice: Any = None
    stream: bool = False


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatBody):
    result = _decide(body.model_dump())
    if not body.stream:
        return JSONResponse(_completion(body.model_dump(), result))

    def gen():
        base = {
            "id": f"chatcmpl-{uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": body.model or "stub",
        }
        first = dict(base)
        first["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"

        if result["tool_calls"]:
            tc = result["tool_calls"][0]
            chunk = dict(base)
            chunk["choices"] = [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        elif result["content"]:
            chunk = dict(base)
            chunk["choices"] = [
                {"index": 0, "delta": {"content": result["content"]}, "finish_reason": None}
            ]
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        final = dict(base)
        final["choices"] = [{"index": 0, "delta": {}, "finish_reason": result["finish_reason"]}]
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        # usage chunk（stream_options.include_usage）：choices 为空
        usage_chunk = dict(base)
        usage_chunk["choices"] = []
        usage_chunk["usage"] = _USAGE
        yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9094, log_config=None)
