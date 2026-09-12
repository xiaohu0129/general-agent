"""U6：LLM/Embedding 适配硬化。

- llm.temperature 配置装配与请求体透传（非流式 _generate / 流式 _astream）
- SSE 单个坏分片跳过告警、不中断流
- 共享 httpx.AsyncClient/Client 注入复用（不归适配层关闭），per-request timeout
"""
from __future__ import annotations

import json

import httpx
from langchain_core.messages import HumanMessage

from general_agent.app import create_app
from general_agent.config import get_settings
from general_agent.embedding import EmbeddingClient
from general_agent.llm import OpenAICompatibleModel

USAGE = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def _sse_bytes(content: str = "ok") -> bytes:
    base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": 1, "model": "stub"}
    parts = [
        _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _sse({**base, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}),
    ]
    parts += [
        _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        _sse({**base, "choices": [], "usage": USAGE}),
        b"data: [DONE]\n\n",
    ]
    return b"".join(parts)


def _sse_transport(captured: dict | None = None, *, body: bytes | None = None) -> httpx.MockTransport:
    payload = body if body is not None else _sse_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
        return httpx.Response(200, stream=httpx.ByteStream(payload))

    return httpx.MockTransport(handler)


def _nonstream_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": USAGE,
            },
        )

    return httpx.MockTransport(handler)


# ---------------- 6.1 temperature ----------------

def test_create_app_wires_llm_temperature(monkeypatch):
    monkeypatch.setattr(get_settings().llm, "temperature", 0.7)
    app = create_app()
    assert app.state.model.temperature == 0.7


def test_nonstream_request_body_carries_temperature():
    captured: dict = {}
    model = OpenAICompatibleModel(
        base_url="http://stub", temperature=0.7, transport=_nonstream_transport(captured)
    )
    model.invoke([HumanMessage(content="hi")])
    assert captured["body"]["temperature"] == 0.7


async def test_stream_request_body_carries_temperature():
    captured: dict = {}
    model = OpenAICompatibleModel(
        base_url="http://stub", temperature=0.7, transport=_sse_transport(captured)
    )
    chunks = [c async for c in model.astream([HumanMessage(content="hi")])]
    assert captured["body"]["temperature"] == 0.7
    assert captured["body"]["stream"] is True
    assert "".join(c.content for c in chunks) == "ok"


# ---------------- 6.2 坏 SSE 分片 ----------------

async def test_stream_skips_bad_sse_chunk_and_continues():
    base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": 1, "model": "stub"}
    body = b"".join(
        [
            _sse({**base, "choices": [{"index": 0, "delta": {"content": "前段"}, "finish_reason": None}]}),
            'data: {"choices": [坏 json\n\n'.encode("utf-8"),
            _sse({**base, "choices": [{"index": 0, "delta": {"content": "后段"}, "finish_reason": None}]}),
            _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _sse({**base, "choices": [], "usage": USAGE}),
            b"data: [DONE]\n\n",
        ]
    )
    model = OpenAICompatibleModel(base_url="http://stub", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=httpx.ByteStream(body))
    ))
    chunks = [c async for c in model.astream([HumanMessage(content="q")])]
    content = "".join(c.content for c in chunks)
    assert "前段" in content and "后段" in content
    usage = [c.usage_metadata for c in chunks if c.usage_metadata]
    assert usage and usage[-1]["total_tokens"] == 11


async def test_stream_skips_non_object_sse_chunks_and_continues():
    base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": 1, "model": "stub"}
    body = b"".join(
        [
            _sse({**base, "choices": [{"index": 0, "delta": {"content": "前段"}, "finish_reason": None}]}),
            b"data: [1,2]\n\n",
            b'data: "x"\n\n',
            b"data: 123\n\n",
            b"data: null\n\n",
            _sse({**base, "choices": [{"index": 0, "delta": {"content": "后段"}, "finish_reason": None}]}),
            _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            _sse({**base, "choices": [], "usage": USAGE}),
            b"data: [DONE]\n\n",
        ]
    )
    model = OpenAICompatibleModel(base_url="http://stub", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=httpx.ByteStream(body))
    ))
    chunks = [c async for c in model.astream([HumanMessage(content="q")])]
    content = "".join(c.content for c in chunks)
    assert content == "前段后段"
    usage = [c.usage_metadata for c in chunks if c.usage_metadata]
    assert usage and usage[-1]["total_tokens"] == 11


# ---------------- 6.3 共享 httpx client ----------------

async def test_embedding_reuses_injected_client_with_per_request_timeout():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        emb = EmbeddingClient("http://emb", client=shared)
        await emb.embed_texts(["a"])
        await emb.embed_texts(["b"])
        assert len(seen) == 2
        assert shared.is_closed is False
        assert seen[0].extensions["timeout"]["read"] == 30.0
    finally:
        await shared.aclose()


async def test_llm_stream_reuses_injected_async_client_with_per_request_timeout():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(_sse_bytes()))

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        model = OpenAICompatibleModel(base_url="http://stub", client=shared)
        for _ in range(2):
            chunks = [c async for c in model.astream([HumanMessage(content="q")])]
            assert "".join(c.content for c in chunks) == "ok"
        assert len(seen) == 2
        assert shared.is_closed is False
        assert seen[0].extensions["timeout"]["read"] == 60.0
    finally:
        await shared.aclose()


def test_llm_generate_reuses_injected_sync_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "stub",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}
                ],
                "usage": USAGE,
            },
        )

    shared = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        model = OpenAICompatibleModel(base_url="http://stub", sync_client=shared)
        model.invoke([HumanMessage(content="q")])
        model.invoke([HumanMessage(content="q")])
        assert shared.is_closed is False
    finally:
        shared.close()


def test_transport_injection_still_creates_per_call_clients():
    captured: dict = {}
    model = OpenAICompatibleModel(
        base_url="http://stub", transport=_nonstream_transport(captured)
    )
    assert model.invoke([HumanMessage(content="q")]).content == "ok"
