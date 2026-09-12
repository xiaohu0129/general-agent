"""轮次接线与 health（9.1 尾项 / 9.2 / 9.3）。

9.1：缺参 MISSING_ARGS 两轮 e2e（第一轮回流引导，第二轮用户补齐后重试成功）；
     chat 层路由前 load 历史（limit=context_turns*2+1）与 runner 全量加载并存。
9.2：/health routing 状态补充 multi_vector/hybrid/semantic/clarify_options/
     no_examples_skills；stub/降级模式 mode 标注正确（rule+keyword / rule-only）。
9.3：list/dict 型路由 details 序列化进 intent_route span 与审计（不被标量过滤丢弃）。
"""
from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from conftest import build_test_app, make_llm_transport, parse_sse
from general_agent import observability as obs
from general_agent.skills import Skill, SkillContext

_SVC, _ENV, _USER = "default", "dev", "anonymous"


# ---------------- 9.1 缺参两轮 e2e ----------------
class _TripArgs(json.JSONEncoder):
    pass


def test_missing_args_two_turn_e2e():
    """第一轮缺参回流引导 + 模型追问，第二轮用户补齐重试成功（半槽位填充闭环）。"""
    from pydantic import BaseModel, Field

    class BookArgs(BaseModel):
        from_city: str = Field(description="出发城市")
        to_city: str = Field(description="到达城市")

    run_log: list[dict] = []

    class BookTripSkill(Skill):
        name = "book_trip"
        description = "预订火车票"
        args_schema = BookArgs

        async def run(self, ctx: SkillContext, *, from_city: str = "", to_city: str = "") -> dict:
            run_log.append({"from": from_city, "to": to_city})
            return {"ok": True, "from": from_city, "to": to_city}

    # 状态机 transport（按末条消息分派）：
    # - last=tool 且 content 含 MISSING_ARGS：缺参引导回流 -> 模型定向追问
    # - last=tool（正常结果）：终答
    # - last=user 且为第二轮补齐（"上海"）：带全参重试
    # - last=user（第一轮）：缺参调用（只给 from_city）
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}

        def gen():
            base = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "stub"}
            yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n".encode()
            if last.get("role") == "tool":
                if "MISSING_ARGS" in (last.get("content") or ""):
                    c = "请问到达城市是哪里？"
                else:
                    c = "已为你预订成功。"
                yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'content': c}, 'finish_reason': None}]})}\n\n".encode()
                yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n".encode()
            elif "上海" in (last.get("content") or ""):
                # 第二轮：用户补齐到达城市，模型带全参重试
                tc = {"index": 0, "id": "call_2", "type": "function", "function": {"name": "book_trip", "arguments": json.dumps({"from_city": "北京", "to_city": "上海"})}}
                yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'tool_calls': [tc]}, 'finish_reason': None}]})}\n\n".encode()
                yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n".encode()
            else:
                # 第一轮：模型只给一个参数（缺 to_city）
                tc = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "book_trip", "arguments": json.dumps({"from_city": "北京"})}}
                yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'tool_calls': [tc]}, 'finish_reason': None}]})}\n\n".encode()
                yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n".encode()
            yield f"data: {json.dumps({**base, 'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}})}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, stream=httpx.ByteStream(b"".join(gen())))

    app, store, _ = build_test_app(skill=BookTripSkill(), llm_transport=httpx.MockTransport(handler))
    from fastapi.testclient import TestClient

    client = TestClient(app)

    # 第一轮：缺参 -> MISSING_ARGS 回流 -> 模型追问
    with client.stream("POST", "/chat", json={"message": "帮我订北京出发的火车票"}) as r:
        evs1 = parse_sse("".join(chunk for chunk in r.iter_text()))
    tool_ends1 = [d for e, d, _ in evs1 if e == "tool_end"]
    assert tool_ends1 and tool_ends1[0]["status"] == "error"
    assert tool_ends1[0]["errorCode"] == "MISSING_ARGS"
    assert run_log == []  # 业务未执行
    assert any("到达城市" in (d.get("content") or "") for e, d, _ in evs1 if e == "turn_delta")

    # 第二轮：用户补齐 -> 历史含第一轮追问 -> 全参重试成功
    with client.stream("POST", "/chat", json={"message": "上海"}) as r:
        evs2 = parse_sse("".join(chunk for chunk in r.iter_text()))
    tool_ends2 = [d for e, d, _ in evs2 if e == "tool_end"]
    assert tool_ends2 and tool_ends2[-1]["status"] == "success"
    assert run_log == [{"from": "北京", "to": "上海"}]
    assert any("预订成功" in (d.get("content") or "") for e, d, _ in evs2 if e == "turn_delta")


# ---------------- 9.2 health routing 状态 ----------------
def test_health_routing_disabled_shape():
    app, _, _ = build_test_app()
    from fastapi.testclient import TestClient

    data = TestClient(app).get("/health").json()
    r = data["routing"]
    assert r["enabled"] is False
    assert r["mode"] == "off"


def test_health_routing_new_fields_and_stub_mode(monkeypatch):
    from general_agent import app as app_mod

    class FakeEmbedder:
        async def embed_texts(self, texts):
            return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(app_mod, "EmbeddingClient", lambda *a, **k: FakeEmbedder())
    app, _, _ = build_test_app()
    settings = app_mod.get_settings()
    monkeypatch.setattr(settings.routing, "enabled", True)
    # stub：base_url 留空 -> 无语义，mode=rule+keyword（hybrid 开）
    monkeypatch.setattr(settings.embedding, "base_url", "")
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        data = client.get("/health").json()
    r = data["routing"]
    assert r["enabled"] is True
    assert r["multi_vector"] is True
    assert r["hybrid"] is True
    assert r["semantic"] is False  # stub 无语义
    assert r["clarify_options"] is True
    assert isinstance(r["no_examples_skills"], list)
    assert r["mode"] == "rule+keyword"  # stub 且 BM25 可用


def test_health_routing_real_embedding_mode(monkeypatch):
    from general_agent import app as app_mod

    class FakeEmbedder:
        async def embed_texts(self, texts):
            return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(app_mod, "EmbeddingClient", lambda *a, **k: FakeEmbedder())
    app, _, _ = build_test_app()
    settings = app_mod.get_settings()
    monkeypatch.setattr(settings.routing, "enabled", True)
    monkeypatch.setattr(settings.embedding, "base_url", "http://fake-emb")
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        data = client.get("/health").json()
    r = data["routing"]
    assert r["semantic"] is True
    assert r["mode"] == "rule+vector"


def test_health_routing_hybrid_off_mode(monkeypatch):
    from general_agent import app as app_mod

    class FakeEmbedder:
        async def embed_texts(self, texts):
            return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(app_mod, "EmbeddingClient", lambda *a, **k: FakeEmbedder())
    app, _, _ = build_test_app()
    settings = app_mod.get_settings()
    monkeypatch.setattr(settings.routing, "enabled", True)
    monkeypatch.setattr(settings.embedding, "base_url", "")
    monkeypatch.setattr(settings.routing, "hybrid", False)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        data = client.get("/health").json()
    r = data["routing"]
    assert r["hybrid"] is False
    assert r["mode"] == "rule-only"  # stub 且 BM25 关 -> 仅规则


# ---------------- 10.1 health 聚合 status ----------------
def test_health_status_ok_when_no_redis_and_routing_off():
    app, _, _ = build_test_app()
    from fastapi.testclient import TestClient

    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["redis"] == "not_configured"
    assert data["routing"]["mode"] == "off"
    assert data["status"] == "ok"


@pytest.mark.parametrize("mode", ["off", "rule-only", "rule+keyword", "rule+vector"])
def test_health_status_ok_for_non_degraded_routing_modes(mode):
    app, _, _ = build_test_app()
    app.state.routing_status = {"enabled": mode != "off", "mode": mode}
    from fastapi.testclient import TestClient

    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["routing"]["mode"] == mode
    assert data["status"] == "ok"  # 主动 stub（rule+keyword 等）不算降级


def test_health_status_degraded_when_redis_ping_error(monkeypatch):
    from general_agent import config as config_mod
    from general_agent.api import health as health_mod

    app, _, _ = build_test_app()
    settings = config_mod.get_settings()
    monkeypatch.setattr(settings.redis, "url", "redis://unreachable:6379")

    async def _ping_error():
        return "error"

    monkeypatch.setattr(health_mod, "_ping_redis", _ping_error)

    from fastapi.testclient import TestClient

    resp = TestClient(app).get("/health")
    assert resp.status_code == 200  # 降级也绝不 5xx
    data = resp.json()
    assert data["redis"] == "error"
    assert data["status"] == "degraded"


def test_health_status_degraded_when_routing_mode_degraded():
    app, _, _ = build_test_app()
    app.state.routing_status = {"enabled": True, "mode": "degraded"}
    from fastapi.testclient import TestClient

    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["routing"]["mode"] == "degraded"
    assert data["status"] == "degraded"


def test_health_endpoint_200_when_probe_raises(monkeypatch):
    from general_agent import config as config_mod
    from general_agent.api import health as health_mod

    app, _, _ = build_test_app()
    settings = config_mod.get_settings()
    monkeypatch.setattr(settings.redis, "nodes", "localhost:6379")

    async def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(health_mod, "_ping_redis", _boom)

    from fastapi.testclient import TestClient

    resp = TestClient(app).get("/health")  # 探测自身异常 MUST NOT 拖垮端点
    assert resp.status_code == 200
    assert resp.json()["redis"] == "error"
    assert resp.json()["status"] == "degraded"


# ---------------- 9.3 details 可回放序列化 ----------------
class _FakeRouter:
    def __init__(self, decision):
        self.decision = decision

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
        return self.decision


class _RefundSkill(Skill):
    name = "refund_skill"
    description = "办理退款"
    category = "refund"

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


def _routing_decision_with_rich_details():
    from general_agent.skill_router.router import PATH_LLM, RouteDecision

    return RouteDecision(
        PATH_LLM,
        [_RefundSkill()],
        clarify_options=[{"label": "退款", "value": "category:refund"}],
        details={
            "top_k": [{"skill": "refund_skill", "score": 0.81}, {"skill": "order_skill", "score": 0.77}],
            "bm25_k": [{"skill": "refund_skill", "score": 8.3}],
            "rrf": [{"skill": "refund_skill", "score": 0.031}],
            "categories": ["refund", "order"],
            "clarify_options": [{"label": "退款", "value": "category:refund"}],
            "llm_category": "refund",
        },
    )


def test_intent_route_span_serializes_list_details(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(obs, "get_tracer", lambda: provider.get_tracer("general.agent"))

    app, _, _ = build_test_app(skill=_RefundSkill())
    app.state.skill_router = _FakeRouter(_routing_decision_with_rich_details())
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "退款怎么办"}) as r:
        "".join(chunk for chunk in r.iter_text())

    spans = [s for s in exporter.get_finished_spans() if s.name == "intent_route"]
    assert spans
    attrs = spans[-1].attributes
    # top-k 工具名+分数（含 BM25/RRF 两路）序列化进 span，不再被标量过滤丢弃
    assert "refund_skill" in attrs.get("route.top_k", "")
    assert "0.81" in attrs.get("route.top_k", "")
    assert "refund_skill" in attrs.get("route.bm25_k", "")
    assert "refund_skill" in attrs.get("route.rrf", "")
    assert "refund" in attrs.get("route.categories", "")
    assert "category:refund" in attrs.get("route.clarify_options", "")


def test_intent_route_audit_serializes_list_details(monkeypatch, capsys):
    app, _, _ = build_test_app(skill=_RefundSkill())
    app.state.skill_router = _FakeRouter(_routing_decision_with_rich_details())
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "退款怎么办"}) as r:
        "".join(chunk for chunk in r.iter_text())

    # 审计日志（structlog PrintLoggerFactory 直写 stdout）含 top-k 工具名+分数与澄清选项
    text = capsys.readouterr().out
    audit_lines = [ln for ln in text.splitlines() if '"intent_route"' in ln and '"event": "audit"' in ln]
    assert audit_lines
    audit_text = "\n".join(audit_lines)
    assert "refund_skill" in audit_text and "0.81" in audit_text
    assert "category:refund" in audit_text
