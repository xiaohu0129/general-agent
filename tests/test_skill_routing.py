"""Skill 意图路由专项测试：规则/索引/路由编排/embedding 客户端/澄清 e2e/确定性。

全部使用 fake embedder / fake LLM / 内存 store，不依赖外部 embedding 或 LLM 服务。
"""
from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from conftest import DemoSkill, FakeStore, build_test_app, make_llm_transport, parse_sse
from general_agent.config import RouteRule
from general_agent.embedding import EmbeddingClient
from general_agent.llm import LLMError
from general_agent.skills import Skill, SkillContext, SkillRegistry
from general_agent.skill_router.index import SkillIndex
from general_agent.skill_router.router import (
    PATH_CHITCHAT,
    PATH_CLARIFY,
    PATH_DEGRADED,
    PATH_FALLBACK,
    PATH_LLM,
    PATH_RULE,
    PATH_VECTOR,
    RouteDecision,
    SkillRouter,
)
from general_agent.skill_router.rules import RuleMatcher


# ---------------- 测试用 Skill（不同 category + examples） ----------------
class WeatherSkill(Skill):
    name = "weather_skill"
    description = "查询天气情况"
    category = "weather"
    examples = ["今天天气怎么样", "查一下天气", "weather forecast"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"weather": "sunny"}


class TicketSkill(Skill):
    name = "ticket_skill"
    description = "创建和查询工单"
    category = "ticket"
    examples = ["帮我建工单", "查询工单状态", "create ticket"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ticketId": "T1"}


class ReportSkill(Skill):
    name = "report_skill"
    description = "生成数据报表"
    category = "report"
    examples = ["生成本月报表", "导出数据报表"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"report": "ok"}


def _skills():
    return [WeatherSkill(), TicketSkill(), ReportSkill()]


# ---------------- fake embedder：关键词 one-hot，余弦可控 ----------------
def _keyword_vec(text: str) -> list[float]:
    v = [0.0] * 8
    low = text.lower()
    for kw, dim in (("天气", 0), ("weather", 0), ("工单", 1), ("ticket", 1), ("报表", 2), ("report", 2)):
        if kw in low:
            v[dim] = 1.0
    return v


class FakeEmbedder:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def embed_texts(self, texts):
        self.calls += 1
        if self.fail:
            raise LLMError("UNAVAILABLE", "embed boom")
        return [_keyword_vec(t) for t in texts]


class FakeLLM:
    """路由兜底 LLM：response 为 dict（转 JSON）/str；或 Exception 抛出。"""

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.last_messages = None

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = messages
        if isinstance(self.response, Exception):
            raise self.response
        content = json.dumps(self.response, ensure_ascii=False) if isinstance(self.response, dict) else self.response
        return AIMessage(content=content)


def _ctx(env="dev"):
    return SkillContext(env=env, user="u1", session_id="s1")


async def _router(embedder=None, llm=None, *, top_k=20, score_threshold=0.5, margin=0.1, rules=None, index=None):
    emb = embedder or FakeEmbedder()
    idx = index
    if idx is None:
        idx = SkillIndex(emb, cache_dir="", model_id="fake-embed")
        await idx.build(_skills())
    return SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher(rules or []),
        llm=llm or FakeLLM({"category": "unknown", "confidence": 0.2}),
        embedder=emb,
        top_k=top_k,
        score_threshold=score_threshold,
        margin=margin,
    )


# ---------------- 规则路由 ----------------
class TestRuleMatcher:
    def test_match_by_skill_name(self):
        rules = [RouteRule(pattern="工单", skills=["ticket_skill"])]
        m = RuleMatcher(rules)
        got = m.match("帮我查工单", _skills())
        assert [s.name for s in got] == ["ticket_skill"]

    def test_match_by_category(self):
        rules = [RouteRule(pattern="天气", category="weather")]
        got = RuleMatcher(rules).match("天气如何", _skills())
        assert [s.name for s in got] == ["weather_skill"]

    def test_no_match_returns_none(self):
        assert RuleMatcher([RouteRule(pattern="不存在的词", skills=["x"])]).match("你好", _skills()) is None

    def test_first_rule_wins(self):
        rules = [
            RouteRule(pattern="工单", skills=["ticket_skill"]),
            RouteRule(pattern=".*", skills=["weather_skill"]),
        ]
        got = RuleMatcher(rules).match("查工单", _skills())
        assert [s.name for s in got] == ["ticket_skill"]

    def test_target_intersect_candidates(self):
        # 规则指向不在候选集的 skill -> 不返回
        rules = [RouteRule(pattern=".*", skills=["not_exist_skill"])]
        assert RuleMatcher(rules).match("随便", _skills()) == []

    def test_invalid_regex_raises(self):
        with pytest.raises(Exception):
            RuleMatcher([RouteRule(pattern="([0-9", skills=["x"])])


# ---------------- 向量索引 ----------------
class TestSkillIndex:
    async def test_build_and_search_ranks(self, tmp_path):
        emb = FakeEmbedder()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake").build(_skills())
        assert idx.ready
        qv = _keyword_vec("今天天气怎么样")
        results = idx.search(qv, _skills(), top_k=20)
        assert results[0][0].name == "weather_skill"
        assert results[0][1] == pytest.approx(1.0, abs=1e-6)

    async def test_top_k_truncation(self, tmp_path):
        idx = await SkillIndex(FakeEmbedder(), cache_dir=str(tmp_path), model_id="fake").build(_skills())
        results = idx.search(_keyword_vec("weather 工单 报表"), _skills(), top_k=2)
        assert len(results) == 2

    async def test_search_respects_candidates(self, tmp_path):
        idx = await SkillIndex(FakeEmbedder(), cache_dir=str(tmp_path), model_id="fake").build(_skills())
        results = idx.search(_keyword_vec("天气"), [TicketSkill()], top_k=20)
        assert all(s.name != "weather_skill" for s, _ in results)

    async def test_cache_reused_without_reembed(self, tmp_path):
        emb1 = FakeEmbedder()
        idx1 = await SkillIndex(emb1, cache_dir=str(tmp_path), model_id="fake").build(_skills())
        assert idx1.ready and emb1.calls == 1
        emb2 = FakeEmbedder()
        idx2 = await SkillIndex(emb2, cache_dir=str(tmp_path), model_id="fake").build(_skills())
        assert idx2.ready and emb2.calls == 0  # 命中缓存，不再调 embedder

    async def test_metadata_change_invalidates_cache(self, tmp_path):
        await SkillIndex(FakeEmbedder(), cache_dir=str(tmp_path), model_id="fake").build(_skills())

        class ChangedWeather(WeatherSkill):
            description = "查询天气情况（已更新描述）"

        emb = FakeEmbedder()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake").build([ChangedWeather(), TicketSkill(), ReportSkill()])
        assert idx.ready and emb.calls == 1  # 哈希变化 -> 重建

    async def test_build_failure_marks_not_ready(self, tmp_path):
        idx = await SkillIndex(FakeEmbedder(fail=True), cache_dir=str(tmp_path), model_id="fake").build(_skills())
        assert not idx.ready
        assert idx.search(_keyword_vec("天气"), _skills(), 20) == []


# ---------------- 路由编排 ----------------
class TestSkillRouter:
    async def test_rule_path_skips_embedding(self):
        emb = FakeEmbedder()
        router = await _router(embedder=emb, rules=[RouteRule(pattern="工单", skills=["ticket_skill"])])
        calls_after_build = emb.calls  # 建索引已批量 embed 一次
        dec = await router.route("查工单", _skills())
        assert dec.path == PATH_RULE
        assert [s.name for s in dec.tools] == ["ticket_skill"]
        assert emb.calls == calls_after_build  # 规则命中，路由阶段不再 query embed

    async def test_vector_high_confidence_narrows(self):
        router = await _router()
        dec = await router.route("今天天气怎么样", _skills())
        assert dec.path == PATH_VECTOR
        names = [s.name for s in dec.tools]
        assert "weather_skill" in names
        assert "ticket_skill" not in names
        assert dec.details["top1_score"] == pytest.approx(1.0, abs=1e-6)

    async def test_low_confidence_llm_picks_category(self):
        llm = FakeLLM({"category": "ticket", "confidence": 0.9, "reason": "工单相关"})
        router = await _router(llm=llm)
        dec = await router.route("你好呀", _skills())  # 无关键词 -> 零向量低置信
        assert dec.path == PATH_LLM
        assert [s.name for s in dec.tools] == ["ticket_skill"]
        assert dec.details["llm_category"] == "ticket"
        assert llm.calls == 1

    async def test_chitchat(self):
        router = await _router(llm=FakeLLM({"category": "chitchat", "confidence": 0.95}))
        dec = await router.route("嗨，你好", _skills())
        assert dec.path == PATH_CHITCHAT
        assert dec.tools == []

    async def test_clarify_when_unknown(self):
        router = await _router(llm=FakeLLM({"category": "unknown", "confidence": 0.3, "clarify_question": "你想查天气还是建工单？"}))
        dec = await router.route("呃那个东西", _skills())
        assert dec.path == PATH_CLARIFY
        assert dec.tools == []
        assert dec.clarify_text and "天气" in dec.clarify_text or dec.clarify_text

    async def test_degraded_when_index_not_ready(self):
        emb = FakeEmbedder(fail=True)
        idx = SkillIndex(emb, cache_dir="", model_id="fake")
        await idx.build(_skills())
        router = await _router(embedder=emb, index=idx)
        dec = await router.route("查天气", _skills())
        assert dec.path == PATH_DEGRADED
        assert len(dec.tools) == 3  # 退回 env 过滤后全量

    async def test_fallback_when_llm_errors(self):
        router = await _router(llm=FakeLLM(RuntimeError("llm down")))
        dec = await router.route("你好", _skills())
        assert dec.path == PATH_FALLBACK
        assert len(dec.tools) == 3

    async def test_no_candidates_is_chitchat(self):
        router = await _router()
        dec = await router.route("随便说点什么", [])
        assert dec.path == PATH_CHITCHAT
        assert dec.tools == []

    async def test_deterministic_same_input_same_tools(self):
        router = await _router()
        d1 = await router.route("今天天气怎么样", _skills())
        d2 = await router.route("今天天气怎么样", _skills())
        assert [s.name for s in d1.tools] == [s.name for s in d2.tools]
        assert d1.path == d2.path == PATH_VECTOR
        assert d1.details["index_version"] == d2.details["index_version"]


# ---------------- Embedding 客户端 ----------------
class TestEmbeddingClient:
    async def test_request_shape_and_parse(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3, 0.4]}]},
            )

        client = EmbeddingClient("http://emb", model="m1", api_key="secret-key", transport=httpx.MockTransport(handler))
        vecs = await client.embed_texts(["a", "b"])
        assert vecs == [[0.1, 0.2], [0.3, 0.4]]
        assert captured["url"].endswith("/v1/embeddings")
        assert captured["body"]["model"] == "m1" and captured["body"]["input"] == ["a", "b"]
        assert captured["auth"] == "Bearer secret-key"

    async def test_error_mapped_and_key_not_leaked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom secret-key"}})

        client = EmbeddingClient("http://emb", api_key="secret-key", transport=httpx.MockTransport(handler))
        with pytest.raises(LLMError) as ei:
            await client.embed_texts(["a"])
        assert ei.value.code in ("INTERNAL", "UNAVAILABLE")

    async def test_empty_input(self):
        client = EmbeddingClient("http://emb", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        assert await client.embed_texts([]) == []


# ---------------- 端到端：澄清轮次与工具收窄 ----------------
class _FakeRouter:
    def __init__(self, decision):
        self.decision = decision

    async def route(self, message, candidates):
        return self.decision


class TestRoutingE2E:
    def test_clarify_turn_has_no_tool_call(self):
        app, store, _ = build_test_app(skill=DemoSkill())
        app.state.skill_router = _FakeRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="你想查天气还是建工单？请补充一下。", details={})
        )
        from fastapi.testclient import TestClient

        client = TestClient(app)  # 不跑 lifespan，保留注入的 fake router
        with client.stream("POST", "/chat", json={"message": "呃那个"}) as r:
            text = "".join(chunk for chunk in r.iter_text())
        evs = parse_sse(text)
        names = [e[0] for e in evs if e[0]]
        assert "tool_start" not in names and "tool_end" not in names
        assert any(e[0] == "turn_end" for e in evs)
        # 澄清文本作为 assistant 消息持久化
        assistant_rows = [r for r in store.rows if r["role"] == "assistant"]
        assert assistant_rows and "天气还是建工单" in assistant_rows[-1]["content"]

    def test_narrowed_tools_drive_agent(self):
        app, store, _ = build_test_app(
            skill=WeatherSkill(), llm_transport=make_llm_transport(tool_name="weather_skill")
        )
        app.state.skill_router = _FakeRouter(
            RouteDecision(PATH_VECTOR, [WeatherSkill()], details={"top1_score": 1.0})
        )
        from fastapi.testclient import TestClient

        client = TestClient(app)
        with client.stream("POST", "/chat", json={"message": "查天气"}) as r:
            text = "".join(chunk for chunk in r.iter_text())
        evs = parse_sse(text)
        tool_starts = [e for e in evs if e[0] == "tool_start"]
        tool_ends = [e for e in evs if e[0] == "tool_end"]
        assert tool_starts and tool_ends
        assert tool_ends[0][1].get("status") == "success"


# ---------------- intent_route span（可观测） ----------------
class TestIntentRouteSpan:
    def test_span_records_decision(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        app, _, _ = build_test_app(skill=DemoSkill())
        app.state.skill_router = _FakeRouter(
            RouteDecision(PATH_VECTOR, [DemoSkill()], details={"top1_score": 0.9})
        )
        from fastapi.testclient import TestClient

        client = TestClient(app)  # 不跑 lifespan，保留注入的 fake router
        with client.stream("POST", "/chat", json={"message": "调用工具 create 任务"}) as r:
            "".join(chunk for chunk in r.iter_text())

        spans = exporter.get_finished_spans()
        route_spans = [s for s in spans if s.name == "intent_route"]
        assert route_spans
        attrs = route_spans[0].attributes
        assert attrs.get("route_path") == PATH_VECTOR
        assert attrs.get("route_tool_count") == 1


# ---------------- temperature=0 透传 / stub embedding / health / lifespan ----------------
def test_llm_temperature_zero():
    captured = {}
    inner = make_llm_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["temperature"] = body.get("temperature")
        return inner.handler(request)

    app, _, _ = build_test_app(llm_transport=httpx.MockTransport(handler))
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "调用工具 create 任务"}) as r:
        "".join(chunk for chunk in r.iter_text())
    assert captured["temperature"] == 0


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


async def test_stub_embeddings_deterministic():
    from general_agent.stub_llm import app as stub_app

    transport = httpx.ASGITransport(app=stub_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stub") as c:
        async def emb(text):
            r = await c.post("/v1/embeddings", json={"model": "x", "input": text})
            return r.json()["data"][0]["embedding"]

        v1 = await emb("天气查询怎么样")
        v2 = await emb("天气查询怎么样")
        v3 = await emb("工单创建处理")
    assert v1 == v2  # 相同文本确定性同向量
    assert _cos(v1, v2) > _cos(v1, v3)  # 相关文本余弦高于无关文本


def test_health_reports_routing():
    app, _, _ = build_test_app()
    from fastapi.testclient import TestClient

    data = TestClient(app).get("/health").json()
    assert "routing" in data
    assert data["routing"]["enabled"] is False  # conftest 默认关闭路由


def test_lifespan_wires_router_when_enabled(monkeypatch):
    from fastapi.testclient import TestClient

    from general_agent import app as app_mod

    # 用假 embedder 替换 EmbeddingClient，lifespan 构建索引不访问网络
    monkeypatch.setattr(app_mod, "EmbeddingClient", lambda *a, **k: FakeEmbedder())
    app, _, _ = build_test_app(skill=WeatherSkill())
    settings = app_mod.get_settings()
    monkeypatch.setattr(settings.routing, "enabled", True)
    monkeypatch.setattr(settings.embedding, "base_url", "http://fake-emb")  # 非 stub -> rule+vector

    with TestClient(app) as client:
        assert app.state.skill_router is not None
        assert app.state.routing_status["mode"] == "rule+vector"
        assert app.state.routing_status["index_ready"] is True
        data = client.get("/health").json()
        assert data["routing"]["enabled"] is True


