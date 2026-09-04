"""OpenAI 兼容 embedding 客户端：POST {base_url}/v1/embeddings。

用于 Skill 向量路由（Tool RAG）：对 Skill 描述/示例与用户消息计算向量。
base_url 留空时指向本地 stub（:9094，stub_llm 提供 /v1/embeddings 确定性向量）。
API Key 仅存内存，不落日志；错误分类复用 llm 模块的 LLMError/_classify_error。
"""
from __future__ import annotations

from typing import Any

import httpx

from .llm import LLMError, _STATUS_TO_CODE  # 复用错误分类

_STUB_BASE = "http://localhost:9094"


class EmbeddingClient:
    def __init__(
        self,
        base_url: str = "",
        *,
        model: str = "doubao-embedding-vision",
        api_key: str = "",
        timeout: float = 30.0,
        transport: Any = None,
    ) -> None:
        self.base_url = (base_url or _STUB_BASE).rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    def _endpoint(self) -> str:
        return f"{self.base_url}/v1/embeddings"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self.model, "input": texts}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                r = await client.post(self._endpoint(), json=body, headers=self._headers())
                if r.is_error:
                    raise self._http_error(r)
                data = r.json()
        except LLMError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not code:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                code = _STATUS_TO_CODE.get(status, "INTERNAL") if status else (
                    "TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "INTERNAL"
                )
            raise LLMError(code, str(exc)) from exc
        items = data.get("data") or []
        # OpenAI 兼容响应按 index 排序，防御乱序
        items = sorted(items, key=lambda d: d.get("index", 0))
        vectors = [item.get("embedding") for item in items]
        if len(vectors) != len(texts) or any(v is None for v in vectors):
            raise LLMError("INTERNAL", "embedding response missing vectors")
        return vectors

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
