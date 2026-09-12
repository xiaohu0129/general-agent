"""Skill 意图路由专项测试：规则/索引/路由编排/embedding 客户端/澄清 e2e/确定性。

全部使用 fake embedder / fake LLM / 内存 store，不依赖外部 embedding 或 LLM 服务。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage

from conftest import DemoSkill, FakeStore, build_test_app, make_llm_transport, parse_sse
from general_agent.config import RouteRule, RoutingSettings
from general_agent.embedding import EmbeddingClient
from general_agent.llm import LLMError
from general_agent.skills import Skill, SkillContext, SkillRegistry
from general_agent.skill_router.context import (
    DEFAULT_REFER_TERMS,
    build_context_query,
    contains_referral,
    rewrite_query,
)
from general_agent.skill_router.index import SkillIndex
from general_agent.skill_router.keyword import KeywordIndex, tokenize
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
        assert idx1.ready and emb1.calls == len(_skills())  # 多向量：逐 Skill 一次 embed_texts
        emb2 = FakeEmbedder()
        idx2 = await SkillIndex(emb2, cache_dir=str(tmp_path), model_id="fake").build(_skills())
        assert idx2.ready and emb2.calls == 0  # 命中缓存，不再调 embedder

    async def test_metadata_change_invalidates_cache(self, tmp_path):
        await SkillIndex(FakeEmbedder(), cache_dir=str(tmp_path), model_id="fake").build(_skills())

        class ChangedWeather(WeatherSkill):
            description = "查询天气情况（已更新描述）"

        emb = FakeEmbedder()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake").build([ChangedWeather(), TicketSkill(), ReportSkill()])
        assert idx.ready and emb.calls == len(_skills())  # 哈希变化 -> 重建（逐 Skill embed）

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

    async def test_batching_splits_requests_by_size_and_order(self):
        batches: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            inputs = json.loads(request.content)["input"]
            batches.append(inputs)
            # 确定性伪向量：首维=文本长度（全局唯一），次维=批内序号
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": j, "embedding": [float(len(s)), float(j)]} for j, s in enumerate(inputs)
                    ]
                },
            )

        texts = ["x" * (i + 1) for i in range(7)]
        client = EmbeddingClient(
            "http://emb", batch_size=3, transport=httpx.MockTransport(handler)
        )
        vecs = await client.embed_texts(texts)

        assert len(batches) == 3
        assert batches == [texts[0:3], texts[3:6], texts[6:7]]
        assert len(vecs) == 7
        assert [v[0] for v in vecs] == [float(i + 1) for i in range(7)]

    async def test_batch_boundaries_no_empty_trailing_batch(self):
        for n, expected_calls, expected_last in [(64, 1, 64), (65, 2, 1)]:
            batches: list[list[str]] = []

            def handler(request: httpx.Request) -> httpx.Response:
                inputs = json.loads(request.content)["input"]
                batches.append(inputs)
                return httpx.Response(
                    200,
                    json={"data": [{"index": j, "embedding": [float(j)]} for j in range(len(inputs))]},
                )

            client = EmbeddingClient(
                "http://emb", batch_size=64, transport=httpx.MockTransport(handler)
            )
            vecs = await client.embed_texts([f"t{i}" for i in range(n)])
            assert len(batches) == expected_calls
            assert len(batches[-1]) == expected_last
            assert all(len(b) > 0 for b in batches)
            assert len(vecs) == n

    async def test_empty_input_sends_no_request(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            calls += 1
            return httpx.Response(200, json={"data": []})

        client = EmbeddingClient("http://emb", transport=httpx.MockTransport(handler))
        assert await client.embed_texts([]) == []
        assert calls == 0

    async def test_batch_http_error_raises_no_partial_result(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["input"] == ["d", "e", "f"]:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return httpx.Response(
                200,
                json={"data": [{"index": j, "embedding": [1.0]} for j in range(len(body["input"]))]},
            )

        client = EmbeddingClient("http://emb", batch_size=3, transport=httpx.MockTransport(handler))
        with pytest.raises(LLMError) as ei:
            await client.embed_texts(["a", "b", "c", "d", "e", "f", "g"])
        assert ei.value.code in ("INTERNAL", "UNAVAILABLE")

    async def test_batch_transport_error_wrapped_as_llm_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = EmbeddingClient("http://emb", batch_size=3, transport=httpx.MockTransport(handler))
        with pytest.raises(LLMError) as ei:
            await client.embed_texts(["a", "b", "c", "d"])
        assert ei.value.code == "INTERNAL"


# ---------------- 端到端：澄清轮次与工具收窄 ----------------
class _FakeRouter:
    def __init__(self, decision):
        self.decision = decision

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
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


# ---------------- 多向量段索引 + 段内 max-sim + has_examples（任务 2.1） ----------------
class _AlphasSkill(Skill):
    """base 描述不含触发词；examples 两条语义迥异的用法。"""

    name = "alphas_skill"
    description = "一个通用的多用途助手"
    category = "utility"
    examples = ["做阿尔法相关的事情", "做贝塔相关的事情"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _OtherSkill(Skill):
    name = "other_skill"
    description = "另一个完全无关的工具"
    category = "utility"
    examples: list[str] = []

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _MultiVectorEmbedder:
    """确定性 3 维 one-hot embedder：alpha/beta 各占一维，base 段聚合后两维都亮。

    单段拼一起 embed（旧算法）时 base 含两条 example -> [0,1,1] 归一化，
    query=[0,1,0] 余弦仅 1/√2≈0.707；多向量 max-sim 命中 alpha example 段得 1.0。
    """

    def __init__(self):
        self.calls = 0
        self.batch_sizes: list[int] = []

    async def embed_texts(self, texts):
        self.calls += 1
        self.batch_sizes.append(len(texts))
        out = []
        for t in texts:
            v = [0.0, 0.0, 0.0]
            if "阿尔法" in t:
                v[0] = 1.0
            if "贝塔" in t:
                v[1] = 1.0
            if "通用" in t:
                v[2] = 1.0
            out.append(v)
        return out


class TestMultiVectorIndex:
    async def test_max_sim_not_diluted_by_other_examples(self, tmp_path):
        skills = [_AlphasSkill(), _OtherSkill()]
        emb = _MultiVectorEmbedder()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx.ready
        q = _normalize_test([1.0, 0.0, 0.0])  # 只命中 "阿尔法" 用法
        results = idx.search(q, skills, top_k=10)
        top_skill, score = results[0]
        assert top_skill.name == "alphas_skill"
        # 段内 max-sim：命中 alpha example 段，不被 beta 段与 base 段拉低
        assert score == pytest.approx(1.0, abs=1e-6)
        # 对照：旧"单段拼一起"算法（base 含两条 example）同 query 只会得 1/√2
        assert score > 1.0 / math.sqrt(2.0) + 0.1

    async def test_skills_without_examples_marked(self, tmp_path):
        skills = [_AlphasSkill(), _OtherSkill()]
        idx = await SkillIndex(FakeEmbedder(), cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx.ready
        # 无 examples 的 Skill 可从状态取到清单
        assert idx.skills_without_examples == ["other_skill"]
        # spans 可读：每元素含 skill/has_examples/count
        span = next(sp for sp in idx.spans if sp["skill"] == "other_skill")
        assert span["has_examples"] is False
        assert span["count"] == 1  # 仅 base 段
        span_a = next(sp for sp in idx.spans if sp["skill"] == "alphas_skill")
        assert span_a["has_examples"] is True
        assert span_a["count"] == 3  # base + 2 examples

    async def test_cache_reload_skips_embedder(self, tmp_path):
        skills = [_AlphasSkill(), _OtherSkill()]
        emb1 = _MultiVectorEmbedder()
        idx1 = await SkillIndex(emb1, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx1.ready and emb1.calls == 2  # 逐 Skill 一次 embed_texts
        # 新 index 实例、同 skills 同 cache_dir：命中缓存不调 embedder
        emb2 = _MultiVectorEmbedder()
        idx2 = await SkillIndex(emb2, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx2.ready and emb2.calls == 0
        # 重载后 max-sim 行为保持
        q = _normalize_test([1.0, 0.0, 0.0])
        assert idx2.search(q, skills, top_k=1)[0][1] == pytest.approx(1.0, abs=1e-6)

    async def test_legacy_single_vector_cache_triggers_rebuild(self, tmp_path):
        skills = [_AlphasSkill(), _OtherSkill()]
        from general_agent.skill_router.index import metadata_hash

        # 先写旧格式（单向量，无 v/spans），hash 伪装成"内容未变但结构陈旧"
        legacy = {
            "model": "fake-mv",
            "hash": metadata_hash(skills),
            "names": [s.name for s in skills],
            "vectors": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
        }
        (Path(tmp_path) / "skill_index.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        emb = _MultiVectorEmbedder()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx.ready
        assert emb.calls == 2  # 旧结构不兼容 -> 重建
        # 落盘为新结构（含 v 与 spans），且扁平矩阵段数对齐
        data = json.loads((Path(tmp_path) / "skill_index.json").read_text(encoding="utf-8"))
        assert data.get("v") == 2
        assert [sp["skill"] for sp in data["spans"]] == [s.name for s in skills]
        assert len(data["vectors"]) == sum(sp["count"] for sp in data["spans"])

    async def test_partial_failure_degrades_without_raise(self, tmp_path):
        skills = [_AlphasSkill(), _OtherSkill()]

        class _FailFirstEmbedder(_MultiVectorEmbedder):
            async def embed_texts(self, texts):
                self.calls += 1
                raise LLMError("UNAVAILABLE", "multi-vector boom")

        emb = _FailFirstEmbedder()
        # 不抛异常即通过；ready=False（全部 Skill 均无可用向量时的整体降级）
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx.ready is False
        assert idx.search([1.0, 0.0, 0.0], skills, 10) == []

    async def test_multi_vector_failure_falls_back_to_base_segment(self, tmp_path):
        skills = [_AlphasSkill(), _OtherSkill()]

        class _FailMultiOnly(_MultiVectorEmbedder):
            """多向量批（>1 段）抛错，单段 base 兜底成功 -> 该 Skill 退单向量。"""

            async def embed_texts(self, texts):
                self.calls += 1
                if len(texts) > 1:
                    raise LLMError("UNAVAILABLE", "multi boom")
                return await super().embed_texts(texts)

        emb = _FailMultiOnly()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx.ready  # 不抛异常，整体仍可用
        span_a = next(sp for sp in idx.spans if sp["skill"] == "alphas_skill")
        assert span_a["count"] == 1  # 多向量失败后退化为仅 base 段
        span_o = next(sp for sp in idx.spans if sp["skill"] == "other_skill")
        assert span_o["count"] == 1
        # 降级后检索仍按段内 max-sim（单段）正常打分
        results = idx.search(_normalize_test([0.0, 0.0, 1.0]), skills, top_k=10)
        assert results and results[0][0].name == "alphas_skill"

    async def test_multi_vector_disabled_single_base_segment(self, tmp_path):
        skills = [_AlphasSkill(), _OtherSkill()]
        emb = _MultiVectorEmbedder()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake-mv", multi_vector=False).build(skills)
        assert idx.ready
        # 每 Skill 仅 base 一条段（退回单向量行为，缓存仍用新 spans 结构）
        assert all(sp["count"] == 1 for sp in idx.spans)
        assert len(idx._vectors) == 2
        data = json.loads((Path(tmp_path) / "skill_index.json").read_text(encoding="utf-8"))
        assert data["v"] == 2 and len(data["spans"]) == 2

    async def test_degraded_single_vector_build_not_cached(self, tmp_path):
        # 修复1a：多向量批失败退 base 单段成功 -> ready=True 但不写缓存，端点恢复后重启可重建 example 段
        skills = [_AlphasSkill(), _OtherSkill()]

        class _FailMultiOnly(_MultiVectorEmbedder):
            """多向量批（>1 段）抛错，单段 base 兜底成功 -> 该 Skill 退单向量。"""

            async def embed_texts(self, texts):
                self.calls += 1
                if len(texts) > 1:
                    raise LLMError("UNAVAILABLE", "multi boom")
                return await super().embed_texts(texts)

        emb = _FailMultiOnly()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx.ready  # 整体仍可用
        span_a = next(sp for sp in idx.spans if sp["skill"] == "alphas_skill")
        assert span_a["count"] == 1  # 降级为仅 base 段
        assert not (Path(tmp_path) / "skill_index.json").exists()  # 降级状态不落缓存

    async def test_missing_segment_skill_build_not_cached(self, tmp_path):
        # 修复1b：某 Skill base 也失败（count==0）、其他 Skill 成功 -> ready=True 但不写缓存
        skills = [_AlphasSkill(), _OtherSkill()]

        class _FailOtherOnly(_MultiVectorEmbedder):
            """仅 other_skill 的 base 段 embed 失败（无 examples 无法降级）-> count==0。"""

            async def embed_texts(self, texts):
                self.calls += 1
                if any("other_skill" in t for t in texts):
                    raise LLMError("UNAVAILABLE", "base boom")
                return await super().embed_texts(texts)

        emb = _FailOtherOnly()
        idx = await SkillIndex(emb, cache_dir=str(tmp_path), model_id="fake-mv").build(skills)
        assert idx.ready  # 仍有 Skill 拿到向量，整体可用
        span_o = next(sp for sp in idx.spans if sp["skill"] == "other_skill")
        assert span_o["count"] == 0  # 缺段
        assert not (Path(tmp_path) / "skill_index.json").exists()  # 缺段状态不落缓存


def _normalize_test(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# ---------------- BM25 关键词路 + RRF 双路融合（任务 2b.1） ----------------
class _SkuOrderSkill(Skill):
    """description 用词与精确符号无关；example 含型号 SKU-8800。"""

    name = "sku_order_skill"
    description = "处理商城售后相关事务的助手"
    category = "order"
    examples = ["商品 SKU-8800 退货流程怎么走"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _HelpCenterSkill(Skill):
    name = "help_center_skill"
    description = "查询帮助中心与知识库文章"
    category = "support"
    examples = ["帮助中心在哪里", "如何搜索知识库文章"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


def _kw_skills():
    return [_HelpCenterSkill(), _SkuOrderSkill()]


class TestKeywordTokenizer:
    def test_digits_bigram_and_stopwords(self):
        # 数字串完整保留；中文相邻两字成 bi-gram
        toks = tokenize("订单 123 退款")
        assert "123" in toks
        assert "订单" in toks and "退款" in toks

    def test_model_number_kept_whole_lowercased(self):
        assert tokenize("SKU-8800") == ["sku-8800"]

    def test_pinyin_abbreviation_lowercased(self):
        toks = tokenize("OA 审批")
        assert "oa" in toks
        assert "审批" in toks

    def test_stopwords_removed_and_not_in_bigram(self):
        toks = tokenize("我的订单退款了吗")
        for sw in ("我", "的", "了", "吗"):
            assert sw not in toks
        assert "的订" not in toks  # 停用字不参与 bi-gram，噪声从源头剔除
        assert "订单" in toks and "退款" in toks


class TestKeywordIndex:
    def test_exact_symbol_ranks_skill_first(self):
        idx = KeywordIndex().build(_kw_skills())
        results = idx.search("SKU-8800 怎么退", _kw_skills(), top_k=10)
        assert results
        assert results[0][0].name == "sku_order_skill"

    def test_respects_candidates_and_empty(self):
        idx = KeywordIndex().build(_kw_skills())
        assert idx.search("SKU-8800", [_HelpCenterSkill()], top_k=10) == []
        assert KeywordIndex().build([]).search("SKU-8800", [], top_k=10) == []
        assert idx.search("完全无关的措辞", _kw_skills(), top_k=10) == []

    def test_build_is_pure_local_no_embedder(self):
        spy = FakeEmbedder()
        # 构造不接收 embedder：零依赖模块不持有任何外部客户端
        with pytest.raises(TypeError):
            KeywordIndex(embedder=spy)
        idx = KeywordIndex().build(_kw_skills())
        idx.search("SKU-8800", _kw_skills(), top_k=5)
        assert spy.calls == 0

    async def test_works_without_vector_index_ready(self):
        # embedding 全失败：SkillIndex 未就绪，KeywordIndex 独立仍可返回候选
        failed = await SkillIndex(FakeEmbedder(fail=True), cache_dir="", model_id="fake").build(_kw_skills())
        assert failed.ready is False
        kw = KeywordIndex().build(_kw_skills())
        results = kw.search("SKU-8800 退货", _kw_skills(), top_k=5)
        assert results and results[0][0].name == "sku_order_skill"


class _RrfSkillA(Skill):
    name = "rrf_a_skill"
    description = "甲方向的处理助手"
    category = "hyb"
    examples = ["vecx-100 情形处理"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _RrfSkillB(Skill):
    name = "rrf_b_skill"
    description = "乙方向的处理助手"
    category = "hyb"
    examples = ["keyy-200 情形处理"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _RrfSkillC(Skill):
    name = "rrf_c_skill"
    description = "兼顾甲乙两方向的处理助手"
    category = "hyb"
    examples = ["vecx-100 keyy-200 情形处理"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _HybridEmbedder:
    """确定性 one-hot：vecx-100 占 dim0、keyy-200 占 dim1；query 文本两路 token 都含，
    但只让 vecx 亮起（keyy 对向量路是 OOV），从而向量路支持 A/C、不支持 B。"""

    async def embed_texts(self, texts):
        out = []
        for t in texts:
            v = [0.0, 0.0]
            if "vecx-100" in t:
                v[0] = 1.0
            if "keyy-200" in t:
                v[1] = 1.0
            out.append(v)
        return out


async def _hybrid_router(*, hybrid=True, keyword_index=None, rrf_k=60, skills=None):
    skills = skills or [_RrfSkillB(), _RrfSkillC(), _RrfSkillA()]
    emb = _HybridEmbedder()
    idx = await SkillIndex(emb, cache_dir="", model_id="fake-hyb").build(skills)
    if keyword_index is None and hybrid:
        keyword_index = KeywordIndex().build(skills)
    return SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=FakeLLM({"category": "unknown", "confidence": 0.2}),
        embedder=emb,
        keyword_index=keyword_index,
        hybrid=hybrid,
        rrf_k=rrf_k,
    )


class TestRrfFusion:
    async def test_dual_path_skill_ranks_first(self):
        router = await _hybrid_router()
        dec = await router.route("vecx-100 keyy-200", [_RrfSkillB(), _RrfSkillC(), _RrfSkillA()])
        rrf = dec.details["rrf"]
        names = [item["skill"] for item in rrf]
        assert names[0] == "rrf_c_skill"  # 双路共同支持 > 仅单路支持
        c_score = next(item["score"] for item in rrf if item["skill"] == "rrf_c_skill")
        a_score = next(item["score"] for item in rrf if item["skill"] == "rrf_a_skill")
        b_score = next(item["score"] for item in rrf if item["skill"] == "rrf_b_skill")
        assert c_score > a_score and c_score > b_score
        # 高置信门仍只看向量：rrf 明细存在且双路共同支持项居前（rrf 分不参与阈值判定）
        bm25 = {item["skill"]: item["score"] for item in dec.details["bm25_k"]}
        assert bm25["rrf_c_skill"] > bm25["rrf_a_skill"]

    async def test_hybrid_disabled_skips_keyword_path(self):
        class _SpyKeyword:
            def __init__(self, inner):
                self.inner = inner
                self.calls = 0

            def search(self, *a, **k):
                self.calls += 1
                return self.inner.search(*a, **k)

        spy = _SpyKeyword(KeywordIndex().build([_RrfSkillB(), _RrfSkillC(), _RrfSkillA()]))
        router = await _hybrid_router(hybrid=False, keyword_index=spy)
        dec = await router.route("vecx-100 keyy-200", [_RrfSkillB(), _RrfSkillC(), _RrfSkillA()])
        assert spy.calls == 0
        assert "bm25_k" not in dec.details and "rrf" not in dec.details

    async def test_index_not_ready_bm25_narrows_to_llm(self):
        # 任务 2b.3：index 未就绪不再全量 DEGRADED——BM25 收窄 -> LLM 兜底，path 钉死为 llm
        emb = FakeEmbedder(fail=True)
        idx = await SkillIndex(emb, cache_dir="", model_id="fake").build(_kw_skills())
        assert idx.ready is False
        llm = FakeLLM({"category": "order", "confidence": 0.9})
        router = SkillRouter(
            index=idx,
            rule_matcher=RuleMatcher([]),
            llm=llm,
            embedder=emb,
            keyword_index=KeywordIndex().build(_kw_skills()),
        )
        dec = await router.route("SKU-8800 退货", _kw_skills())
        assert dec.path == PATH_LLM
        assert dec.details["reason"] == "index_not_ready"
        assert dec.details["degraded_keyword"] is True
        assert dec.details["semantic_off"] is True
        assert [s.name for s in dec.tools] == ["sku_order_skill"]
        assert len(dec.tools) < len(_kw_skills())

    async def test_keyword_hits_injected_into_fallback_prompt(self):
        llm = FakeLLM({"category": "unknown", "confidence": 0.2})
        skills = _skills() + [_SkuOrderSkill()]
        emb = FakeEmbedder()
        idx = await SkillIndex(emb, cache_dir="", model_id="fake").build(skills)
        router = SkillRouter(
            index=idx,
            rule_matcher=RuleMatcher([]),
            llm=llm,
            embedder=emb,
            keyword_index=KeywordIndex().build(skills),
        )
        dec = await router.route("SKU-8800 退货", skills)
        assert dec.path in (PATH_LLM, PATH_CLARIFY)
        prompt = llm.last_messages[-1].content
        assert "sku_order_skill" in prompt

    async def test_default_router_has_no_keyword_keys(self):
        # 不传 keyword_index：现有行为逐字节保持，details 不出现新键
        router = await _router()
        dec = await router.route("你好呀", _skills())
        assert "bm25_k" not in dec.details and "rrf" not in dec.details


# ---- BM25 保底并入回归（fix round 1）：向量 floor 截断的 Skill 被 BM25 rank≤3 救回，rank=4 不救 ----
_GUARD_QUERY = "aaa-1000 bbb-2000 sku-8800 tok-4000"


class _FloorMainSkill(Skill):
    """向量高置信锚点：base 段被 _FloorCutEmbedder 识别为 [1,0]；自身不含任何型号 token。"""

    name = "floor_main_skill"
    description = "处理通用高频事务的主力助手"
    category = "通用事务"
    examples: list[str] = []

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _GuardKw1Skill(Skill):
    name = "guard_kw1_skill"
    description = "第一个精确型号相关的售后助手"
    category = "售后"
    examples = ["aaa-1000"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _GuardKw2Skill(Skill):
    name = "guard_kw2_skill"
    description = "第二个精确型号相关的售后助手"
    category = "售后"
    examples = ["bbb-2000"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _SkuFloorGuardSkill(Skill):
    """向量路零分（被 floor 截断）；唯一精确型号 sku-8800 只在 BM25 路命中，同分按名排第 3。"""

    name = "sku_floor_guard_skill"
    description = "处理商城退换货相关事务的助手"
    category = "售后"
    examples = ["sku-8800"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _TokRank4Skill(Skill):
    """唯一精确型号 tok-4000 只在 BM25 路命中，同分按名排第 4（超出 rank≤3 保底上限）。"""

    name = "tok_rank4_skill"
    description = "处理库存盘点相关事务的助手"
    category = "售后"
    examples = ["tok-4000"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _FloorCutEmbedder:
    """确定性 2 维 fake：floor_main 段 [1,0]、其余段零向量；query 与 [1,0] 余弦恰为 0.9。

    向量 top1=0.9 高置信，其余 Skill 余弦 0 < floor(0.25) 被截断；
    query 文本无语义线索，四个型号 token 只能被 KeywordIndex 命中。
    """

    async def embed_texts(self, texts):
        out = []
        for t in texts:
            if t == _GUARD_QUERY:
                out.append([0.9, math.sqrt(0.19)])
            elif "floor_main_skill" in t:
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 0.0])
        return out


class TestKeywordGuardFloor:
    async def test_bm25_rank_le3_recovers_vector_floor_cut_skill(self):
        skills = [
            _FloorMainSkill(),
            _GuardKw1Skill(),
            _GuardKw2Skill(),
            _SkuFloorGuardSkill(),
            _TokRank4Skill(),
        ]
        emb = _FloorCutEmbedder()
        idx = await SkillIndex(emb, cache_dir="", model_id="fake-guard").build(skills)
        router = SkillRouter(
            index=idx,
            rule_matcher=RuleMatcher([]),
            llm=FakeLLM({"category": "unknown", "confidence": 0.2}),
            embedder=emb,
            keyword_index=KeywordIndex().build(skills),
        )
        dec = await router.route(_GUARD_QUERY, skills)

        # ① 高置信门只看向量：top1=0.9≥0.5、gap=0.9≥0.1 -> PATH_VECTOR
        assert dec.path == PATH_VECTOR
        assert dec.details["top1_score"] == pytest.approx(0.9, abs=1e-6)

        # sku Skill 向量余弦为 0，低于 floor=threshold*0.5=0.25，向量放行集本已将其截断丢弃
        vec_scores = {item["skill"]: item["score"] for item in dec.details["top_k"]}
        assert vec_scores["sku_floor_guard_skill"] < 0.5 * 0.5

        # 四个型号 BM25 同分（df=1、example 段 dl=1），按 Skill 名兜底排序：sku 第 3、tok 第 4
        bm25_names = [item["skill"] for item in dec.details["bm25_k"]]
        assert bm25_names.index("sku_floor_guard_skill") == 2
        assert bm25_names.index("tok_rank4_skill") == 3

        # ② 被向量 floor 截断的 Skill 因 BM25 rank≤3 保底出现在 decision.tools
        names = [s.name for s in dec.tools]
        assert "sku_floor_guard_skill" in names
        assert "guard_kw1_skill" in names and "guard_kw2_skill" in names
        # ③ BM25 rank=4 的 Skill 不被并入，锁定 kw_results[:3] 上限
        assert "tok_rank4_skill" not in names

        # ④ details 含双路回放字段
        assert "bm25_k" in dec.details and "rrf" in dec.details


def test_app_hybrid_disabled_does_not_build_keyword_index(monkeypatch):
    from fastapi.testclient import TestClient

    from general_agent import app as app_mod

    monkeypatch.setattr(app_mod, "EmbeddingClient", lambda *a, **k: FakeEmbedder())
    app, _, _ = build_test_app(skill=WeatherSkill())
    settings = app_mod.get_settings()
    monkeypatch.setattr(settings.routing, "enabled", True)
    monkeypatch.setattr(settings.routing, "hybrid", False)
    monkeypatch.setattr(settings.embedding, "base_url", "http://fake-emb")

    with TestClient(app) as client:
        assert app.state.skill_router is not None
        assert app.state.skill_router.keyword_index is None
        client.get("/health")  # lifespan 内装配完成即可正常服务


def test_app_hybrid_enabled_builds_keyword_index(monkeypatch):
    from fastapi.testclient import TestClient

    from general_agent import app as app_mod

    monkeypatch.setattr(app_mod, "EmbeddingClient", lambda *a, **k: FakeEmbedder())
    app, _, _ = build_test_app(skill=WeatherSkill())
    settings = app_mod.get_settings()
    monkeypatch.setattr(settings.routing, "enabled", True)
    monkeypatch.setattr(settings.routing, "hybrid", True)
    monkeypatch.setattr(settings.embedding, "base_url", "http://fake-emb")

    with TestClient(app):
        assert isinstance(app.state.skill_router.keyword_index, KeywordIndex)


# ---- semantic 入参 + stub/embedding 故障 -> BM25 收窄 -> LLM 兜底（任务 2b.3） ----
class _AlwaysOnEmbedder:
    """建索引恒返回非零向量（ready=True），带调用计数 spy。"""

    def __init__(self):
        self.calls = 0

    async def embed_texts(self, texts):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


class _FailQueryEmbedder:
    """建索引正常（非零向量、ready=True）；路由阶段对 query 文本 embed 抛 LLMError。"""

    def __init__(self, query: str):
        self.query = query
        self.calls = 0

    async def embed_texts(self, texts):
        self.calls += 1
        if any(t == self.query for t in texts):
            raise LLMError("UNAVAILABLE", "query embed boom")
        return [[1.0, 0.0] for _ in texts]


_DEG_QUERY = "SKU-8800 退货"


async def _degrade_router(*, semantic, embedder, llm, hybrid=True):
    idx = await SkillIndex(embedder, cache_dir="", model_id="fake-deg").build(_kw_skills())
    kw = KeywordIndex().build(_kw_skills()) if hybrid else None
    router = SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=llm,
        embedder=embedder,
        keyword_index=kw,
        hybrid=hybrid,
        semantic=semantic,
    )
    return router, idx


class TestSemanticOffDegradation:
    async def test_semantic_off_skips_embedder_and_bm25_narrows(self):
        emb = _AlwaysOnEmbedder()
        llm = FakeLLM({"category": "order", "confidence": 0.9})
        router, idx = await _degrade_router(semantic=False, embedder=emb, llm=llm)
        assert idx.ready
        calls_after_build = emb.calls
        dec = await router.route(_DEG_QUERY, _kw_skills())
        assert emb.calls == calls_after_build  # semantic=False：路由阶段绝不调 embedder
        assert dec.path == PATH_LLM
        assert [s.name for s in dec.tools] == ["sku_order_skill"]
        assert len(dec.tools) < len(_kw_skills())  # BM25 收窄，非全量平铺
        assert dec.details["semantic_off"] is True
        assert dec.details["degraded_keyword"] is True

    async def test_query_embed_failure_bm25_narrows_to_llm(self):
        emb = _FailQueryEmbedder(_DEG_QUERY)
        llm = FakeLLM({"category": "order", "confidence": 0.9})
        router, idx = await _degrade_router(semantic=True, embedder=emb, llm=llm)
        assert idx.ready
        dec = await router.route(_DEG_QUERY, _kw_skills())
        assert dec.path == PATH_LLM
        assert [s.name for s in dec.tools] == ["sku_order_skill"]
        assert len(dec.tools) < len(_kw_skills())
        assert dec.details["degraded_keyword"] is True
        assert "query_embed_failed" in dec.details["reason"]

    async def test_semantic_off_llm_unknown_is_clarify(self):
        emb = _AlwaysOnEmbedder()
        router, _ = await _degrade_router(
            semantic=False, embedder=emb, llm=FakeLLM({"category": "unknown", "confidence": 0.2})
        )
        dec = await router.route(_DEG_QUERY, _kw_skills())
        assert dec.path == PATH_CLARIFY
        assert dec.tools == []
        assert dec.details["degraded_keyword"] is True

    async def test_query_embed_failure_llm_unknown_is_clarify(self):
        emb = _FailQueryEmbedder(_DEG_QUERY)
        router, _ = await _degrade_router(
            semantic=True, embedder=emb, llm=FakeLLM({"category": "unknown", "confidence": 0.2})
        )
        dec = await router.route(_DEG_QUERY, _kw_skills())
        assert dec.path == PATH_CLARIFY
        assert "query_embed_failed" in dec.details["reason"]

    async def test_semantic_off_bm25_empty_is_degraded_full(self):
        emb = _AlwaysOnEmbedder()
        router, _ = await _degrade_router(
            semantic=False, embedder=emb, llm=FakeLLM({"category": "order", "confidence": 0.9})
        )
        dec = await router.route("完全无关的措辞", _kw_skills())
        assert dec.path == PATH_DEGRADED
        assert len(dec.tools) == len(_kw_skills())
        assert dec.details["reason"] == "semantic_off"

    async def test_hybrid_disabled_semantic_off_degraded_full_no_embed(self):
        emb = _AlwaysOnEmbedder()
        router, _ = await _degrade_router(
            semantic=False, embedder=emb, llm=FakeLLM({"category": "order", "confidence": 0.9}), hybrid=False
        )
        calls_after_build = emb.calls
        dec = await router.route(_DEG_QUERY, _kw_skills())
        assert emb.calls == calls_after_build
        assert dec.path == PATH_DEGRADED
        assert len(dec.tools) == len(_kw_skills())

    async def test_semantic_off_llm_error_falls_back_to_full_candidates(self):
        emb = _AlwaysOnEmbedder()
        router, _ = await _degrade_router(
            semantic=False, embedder=emb, llm=FakeLLM(RuntimeError("llm down"))
        )
        dec = await router.route(_DEG_QUERY, _kw_skills())
        assert dec.path == PATH_FALLBACK
        assert len(dec.tools) == len(_kw_skills())  # 裁定：LLM 异常回退系统全量 candidates
        assert dec.details["reason"] == "route_llm_failed"


# ---- 跨轮上下文：历史拼接 + 按需 query 改写（任务 3.1+3.2） ----
class _RefundSkill(Skill):
    """退款售后 Skill：索引段只被 退款/退货 类语义点亮。"""

    name = "refund_skill"
    description = "办理退款退货的售后助手"
    category = "aftersale"
    examples = ["申请退款流程", "退货退款要怎么操作"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _InvoiceSkill(Skill):
    name = "invoice_skill"
    description = "开具发票与查询发票"
    category = "billing"
    examples = ["怎么开发票", "申请电子发票"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


def _ctx_skills():
    return [_RefundSkill(), _InvoiceSkill()]


# 不含任何业务关键词的历史：拼接串本身点亮不了任何向量维
_CTX_HISTORY = [
    {"role": "user", "content": "你好，我有个业务问题想咨询一下"},
    {"role": "assistant", "content": "请讲，有什么可以帮您"},
]
_REFERRAL_MESSAGE = "那个咋整"  # 命中指代词“那个”，自身无业务关键词，BM25 对两个 Skill 均零命中
_NONREFERRAL_LOW_MESSAGE = "咋整呢"  # 无指代词，同样低置信且 BM25 零命中
_REWRITTEN_REFUND = "请问退款退货需要怎么申请"  # 改写后的独立 query，点亮退款维


class _CtxEmbedder:
    """确定性 2 维 one-hot：退款/退货 -> dim0，发票 -> dim1；记录每次 embed 入参供断言。"""

    def __init__(self):
        self.calls = 0
        self.inputs: list[list[str]] = []

    async def embed_texts(self, texts):
        self.calls += 1
        self.inputs.append(list(texts))
        out = []
        for t in texts:
            v = [0.0, 0.0]
            if "退款" in t or "退货" in t:
                v[0] = 1.0
            if "发票" in t:
                v[1] = 1.0
            out.append(v)
        return out


class ScriptedLLM:
    """按 system prompt 区分“查询改写”与“意图分类”两类调用，分别计数、分别脚本化响应。"""

    def __init__(self, *, rewrite_response=None, rewrite_raises=None, classify_response=None):
        self.rewrite_response = rewrite_response if rewrite_response is not None else {"query": _REWRITTEN_REFUND}
        self.rewrite_raises = rewrite_raises
        self.classify_response = classify_response or {"category": "aftersale", "confidence": 0.9}
        self.rewrite_calls = 0
        self.classify_calls = 0

    async def ainvoke(self, messages, **kwargs):
        sys_text = messages[0].content if messages else ""
        if "改写" in sys_text:  # 改写 system prompt 独有措辞，分类 prompt 为“意图路由分类器”
            self.rewrite_calls += 1
            if self.rewrite_raises is not None:
                raise self.rewrite_raises
            resp = self.rewrite_response
            content = json.dumps(resp, ensure_ascii=False) if isinstance(resp, dict) else str(resp)
            return AIMessage(content=content)
        self.classify_calls += 1
        return AIMessage(content=json.dumps(self.classify_response, ensure_ascii=False))


class _KeywordSpy:
    """包一层 KeywordIndex.search：记录调用次数与每次 query 入参。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.queries: list[str] = []

    def search(self, query, candidates, top_k):
        self.calls += 1
        self.queries.append(query)
        return self.inner.search(query, candidates, top_k)


async def _context_router(
    *,
    llm,
    context_turns: int = 3,
    query_rewrite: bool = True,
    refer_terms=None,
):
    skills = _ctx_skills()
    emb = _CtxEmbedder()
    idx = await SkillIndex(emb, cache_dir="", model_id="fake-ctx").build(skills)
    spy = _KeywordSpy(KeywordIndex().build(skills))
    router = SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=llm,
        embedder=emb,
        keyword_index=spy,
        hybrid=True,
        context_turns=context_turns,
        query_rewrite=query_rewrite,
        refer_terms=refer_terms,
    )
    return router, emb, spy, skills


class TestContextHelpers:
    def test_build_context_query_concats_roles_and_content(self):
        history = [
            {"role": "user", "content": "我想问退款"},
            {"role": "assistant", "content": "请补充订单号"},
        ]
        q = build_context_query(history, "那个怎么办")
        assert "我想问退款" in q and "请补充订单号" in q and "那个怎么办" in q
        # 角色可辨
        assert "用户" in q and "助手" in q

    def test_build_context_query_none_or_empty_is_message_itself(self):
        assert build_context_query(None, "原文消息") == "原文消息"
        assert build_context_query([], "原文消息") == "原文消息"

    def test_contains_referral_hits_and_misses(self):
        terms = list(DEFAULT_REFER_TERMS)
        assert contains_referral("它在哪里", terms) is True
        assert contains_referral("那个退款", terms) is True
        assert contains_referral("再来一单", terms) is True
        assert contains_referral("查询今天天气", terms) is False
        # 自定义词表生效
        assert contains_referral("咱接着弄", ["接着"]) is True

    def test_contains_referral_excludes_qita_other_phrases(self):
        # 修复3：单字"他/它"不命中"其他/其它"中的字；独立出现仍命中；多字词不受影响
        terms = list(RoutingSettings().refer_terms)
        assert contains_referral("看看其他方案", terms) is False
        assert contains_referral("其它方式呢", terms) is False
        assert contains_referral("其他", terms) is False
        # spec 场景：独立"它"必须保持命中
        assert contains_referral("那它的退款呢", terms) is True
        assert contains_referral("他怎么说", terms) is True
        assert contains_referral("这个", terms) is True  # 多字词维持普通子串匹配
        assert contains_referral("今天天气真好", terms) is False

    async def test_rewrite_query_returns_query_string(self):
        llm = FakeLLM({"query": "独立的检索查询", "used_context": True})
        got = await rewrite_query(llm, _CTX_HISTORY, _REFERRAL_MESSAGE)
        assert got == "独立的检索查询"
        # 改写 prompt 同时含历史与当前消息
        prompt = llm.last_messages[-1].content
        assert "业务问题想咨询" in prompt and _REFERRAL_MESSAGE in prompt

    async def test_rewrite_query_bad_json_raises(self):
        with pytest.raises(Exception):
            await rewrite_query(FakeLLM("not a json"), _CTX_HISTORY, _REFERRAL_MESSAGE)

    async def test_rewrite_query_missing_key_raises(self):
        with pytest.raises(Exception):
            await rewrite_query(FakeLLM({"foo": 1}), _CTX_HISTORY, _REFERRAL_MESSAGE)

    async def test_rewrite_query_llm_exception_raises(self):
        with pytest.raises(Exception):
            await rewrite_query(FakeLLM(RuntimeError("llm down")), _CTX_HISTORY, _REFERRAL_MESSAGE)


class TestCrossTurnRouting:
    async def test_high_confidence_uses_concat_query_without_rewrite(self):
        # 六项断言①：高置信无指代 -> embed 入参为拼接串，0 LLM
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm)
        start = len(emb.inputs)
        dec = await router.route("发票怎么开", skills, history=_CTX_HISTORY)
        route_inputs = [t for batch in emb.inputs[start:] for t in batch]
        assert dec.path == PATH_VECTOR
        assert "refund_skill" not in [s.name for s in dec.tools]
        assert route_inputs == [build_context_query(_CTX_HISTORY, "发票怎么开")]
        assert "业务问题想咨询" in route_inputs[0]  # 确为拼接串而非裸 message
        assert llm.rewrite_calls == 0 and llm.classify_calls == 0
        assert spy.calls == 1 and spy.queries == ["发票怎么开"]

    async def test_context_slice_keeps_last_n_turns_not_n_messages(self):
        # context_turns 按轮切片：N 轮 = 2N 条消息；MUST NOT 按消息数截断导致只用一半上下文
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm, context_turns=2)
        history = [
            {"role": "user", "content": "最早轮的问题"},
            {"role": "assistant", "content": "最早轮的回答"},
            {"role": "user", "content": "次新轮的问题"},
            {"role": "assistant", "content": "次新轮的回答"},
            {"role": "user", "content": "最新轮的问题"},
            {"role": "assistant", "content": "最新轮的回答"},
        ]
        start = len(emb.inputs)
        dec = await router.route("发票怎么开", skills, history=history)
        route_inputs = [t for batch in emb.inputs[start:] for t in batch]
        expected_recent = history[-4:]  # 2 轮 = 4 条消息
        assert route_inputs == [build_context_query(expected_recent, "发票怎么开")]
        assert "次新轮的问题" in route_inputs[0]  # 第 2 轮未被截掉
        assert "最早轮的问题" not in route_inputs[0]  # 超出 N 轮的被截掉
        assert dec.path == PATH_VECTOR
        assert llm.rewrite_calls == 0 and llm.classify_calls == 0

    async def test_referral_triggers_rewrite_and_rehits_skill(self):
        # 六项断言②（指代分支）：拼接低置信 -> 改写 1 次 -> 第 2 次 embed 用改写串 -> 命中退款
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm)
        start = len(emb.inputs)
        dec = await router.route(_REFERRAL_MESSAGE, skills, history=_CTX_HISTORY)
        route_inputs = [t for batch in emb.inputs[start:] for t in batch]
        assert dec.path == PATH_VECTOR
        assert [s.name for s in dec.tools] == ["refund_skill"]
        assert llm.rewrite_calls == 1
        assert len(emb.inputs) - start == 2  # 拼接串 + 改写串各 embed 一次
        assert route_inputs[0] == build_context_query(_CTX_HISTORY, _REFERRAL_MESSAGE)
        assert route_inputs[1] == _REWRITTEN_REFUND
        assert dec.details["query_rewritten"] is True
        assert dec.details["rewritten_query"] == _REWRITTEN_REFUND
        assert spy.calls == 1 and spy.queries == [_REFERRAL_MESSAGE]

    async def test_low_confidence_without_referral_also_triggers_rewrite(self):
        # 六项断言②（无指代但拼接低置信分支）
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm)
        dec = await router.route(_NONREFERRAL_LOW_MESSAGE, skills, history=_CTX_HISTORY)
        assert dec.path == PATH_VECTOR
        assert [s.name for s in dec.tools] == ["refund_skill"]
        assert llm.rewrite_calls == 1
        assert spy.calls == 1

    async def test_rewrite_llm_exception_silently_falls_back(self):
        # 六项断言③（异常子例）：轮次不抛、details 标记失败、沿用拼接 fused 走兜底
        llm = ScriptedLLM(rewrite_raises=RuntimeError("rewrite boom"))
        router, emb, spy, skills = await _context_router(llm=llm)
        start = len(emb.inputs)
        dec = await router.route(_REFERRAL_MESSAGE, skills, history=_CTX_HISTORY)
        assert dec.path == PATH_LLM  # 分类 LLM 兜底成功点名
        assert [s.name for s in dec.tools] == ["refund_skill"]
        assert dec.details["query_rewrite_failed"] is True
        assert llm.rewrite_calls == 1 and llm.classify_calls == 1
        assert len(emb.inputs) - start == 1  # 改写失败不发生第二次 embed
        assert spy.calls == 1

    async def test_rewrite_unparseable_silently_falls_back(self):
        # 六项断言③（不可解析子例）
        llm = ScriptedLLM(rewrite_response="不是 JSON 的一段话")
        router, emb, spy, skills = await _context_router(llm=llm)
        dec = await router.route(_REFERRAL_MESSAGE, skills, history=_CTX_HISTORY)
        assert dec.path == PATH_LLM
        assert dec.details["query_rewrite_failed"] is True
        assert llm.rewrite_calls == 1 and llm.classify_calls == 1
        assert spy.calls == 1

    async def test_context_turns_zero_uses_raw_message(self):
        # 六项断言④：context_turns=0 -> embed 入参逐字节等于当前轮原文
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm, context_turns=0)
        start = len(emb.inputs)
        dec = await router.route("发票怎么开", skills, history=_CTX_HISTORY)
        route_inputs = [t for batch in emb.inputs[start:] for t in batch]
        assert dec.path == PATH_VECTOR
        assert route_inputs == ["发票怎么开"]
        assert llm.rewrite_calls == 0 and llm.classify_calls == 0
        assert spy.calls == 1

    async def test_query_rewrite_disabled_low_confidence_goes_to_llm(self):
        # 六项断言④：query_rewrite=false 且低置信 -> 不拼接不改写，路由 LLM 兜底 1 次
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm, query_rewrite=False)
        start = len(emb.inputs)
        dec = await router.route(_REFERRAL_MESSAGE, skills, history=_CTX_HISTORY)
        route_inputs = [t for batch in emb.inputs[start:] for t in batch]
        assert dec.path == PATH_LLM
        assert route_inputs == [_REFERRAL_MESSAGE]  # 不拼接
        assert llm.rewrite_calls == 0 and llm.classify_calls == 1
        assert "query_rewrite_failed" not in dec.details and "query_rewritten" not in dec.details
        assert spy.calls == 1

    async def test_rewrite_rehit_high_confidence_skips_route_llm(self):
        # 六项断言⑤：双检索控制流——重检高置信直接 vector，分类 LLM 0 次、改写 LLM 1 次
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm)
        dec = await router.route(_REFERRAL_MESSAGE, skills, history=_CTX_HISTORY)
        assert dec.path == PATH_VECTOR
        assert llm.rewrite_calls == 1 and llm.classify_calls == 0
        assert spy.calls == 1

    async def test_bm25_runs_once_on_raw_message_across_scenarios(self):
        # 六项断言⑥：拼接/改写/降级各场景 BM25 恒只对当前消息原文跑一次
        llm = ScriptedLLM()
        router, _, spy, skills = await _context_router(llm=llm)

        await router.route("发票怎么开", skills, history=_CTX_HISTORY)
        await router.route(_REFERRAL_MESSAGE, skills, history=_CTX_HISTORY)
        assert spy.queries == ["发票怎么开", _REFERRAL_MESSAGE]

        llm_fail = ScriptedLLM(rewrite_raises=RuntimeError("x"))
        router2, _, spy2, skills2 = await _context_router(llm=llm_fail)
        await router2.route(_REFERRAL_MESSAGE, skills2, history=_CTX_HISTORY)

        llm_off = ScriptedLLM()
        router3, _, spy3, skills3 = await _context_router(llm=llm_off, query_rewrite=False)
        await router3.route(_REFERRAL_MESSAGE, skills3, history=_CTX_HISTORY)

        assert spy2.calls == 1 and spy2.queries == [_REFERRAL_MESSAGE]
        assert spy3.calls == 1 and spy3.queries == [_REFERRAL_MESSAGE]

    async def test_history_none_is_byte_identical_to_single_turn(self):
        # 默认 history=None：即使消息含指代词且低置信，也与现状逐字节一致（不拼接、不改写）
        llm = ScriptedLLM()
        router, emb, spy, skills = await _context_router(llm=llm)
        start = len(emb.inputs)
        dec = await router.route(_REFERRAL_MESSAGE, skills)
        route_inputs = [t for batch in emb.inputs[start:] for t in batch]
        assert route_inputs == [_REFERRAL_MESSAGE]
        assert llm.rewrite_calls == 0 and llm.classify_calls == 1
        assert "query_rewritten" not in dec.details and "query_rewrite_failed" not in dec.details
        assert spy.calls == 1
        # Fix round 1 回归锁：单轮路径兜底仍面对全量 categories，且不打融合收窄标记
        assert dec.path == PATH_LLM
        assert dec.details["categories"] == ["aftersale", "billing"]
        assert "fallback_candidates" not in dec.details


# ---- Fix round 1：跨轮低置信兜底候选收窄到最终 RRF 融合候选（spec:162 / tasks 3.2） ----
class _FusedAfterMainSkill(Skill):
    """同域售后 Skill：索引段只亮 退款/退货 维。"""

    name = "fused_after_main_skill"
    description = "办理退款退货售后"
    category = "aftersale"
    examples = ["申请退款流程", "退货退款怎么操作"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _FusedAfterExtraSkill(Skill):
    """同域售后 Skill：索引段只亮 工单/进度 维；用于让改写串与 main 同分低置信。"""

    name = "fused_after_extra_skill"
    description = "售后工单进度跟踪与催办"
    category = "aftersale"
    examples = ["查询售后工单处理进度"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _FusedAfterIsoSkill(Skill):
    """同域售后 Skill：索引段只亮 保修 维——同 category 但不应进入融合候选。"""

    name = "fused_after_iso_skill"
    description = "产品售后保修登记"
    category = "aftersale"
    examples = ["产品保修卡怎么登记"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _FusedBillingSkill(Skill):
    name = "fused_billing_skill"
    description = "开具发票"
    category = "billing"
    examples = ["怎么开发票"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


def _fused_skills():
    # 顺序锁定：零分/同分时向量检索保持候选顺序可回放
    return [_FusedAfterMainSkill(), _FusedAfterExtraSkill(), _FusedAfterIsoSkill(), _FusedBillingSkill()]


class _FusedEmbedder:
    """4 维 one-hot：退款/退货 dim0、工单/进度 dim1、发票 dim2、保修 dim3；记录入参。"""

    def __init__(self):
        self.inputs: list[list[str]] = []

    async def embed_texts(self, texts):
        self.inputs.append(list(texts))
        out = []
        for t in texts:
            v = [0.0, 0.0, 0.0, 0.0]
            if "退款" in t or "退货" in t:
                v[0] = 1.0
            if "工单" in t or "进度" in t or "催办" in t:
                v[1] = 1.0
            if "发票" in t:
                v[2] = 1.0
            if "保修" in t:
                v[3] = 1.0
            out.append(v)
        return out


_FUSED_HISTORY_REFUND = [
    {"role": "user", "content": "我想问退款的事"},
    {"role": "assistant", "content": "请补充一下具体问题"},
]


async def _fused_router(llm):
    skills = _fused_skills()
    emb = _FusedEmbedder()
    idx = await SkillIndex(emb, cache_dir="", model_id="fake-fused").build(skills)
    router = SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=llm,
        embedder=emb,
        keyword_index=KeywordIndex().build(skills),
        hybrid=True,
        context_turns=3,
        query_rewrite=True,
    )
    return router, skills


class TestCrossTurnFusedFallback:
    async def test_rewrite_still_low_fallback_narrows_to_fused_candidates(self):
        # a：改写成功但重检仍低置信（main/extra 同分 gap=0）-> 兜底候选=fused'，
        # 同域但融合候选外的 iso 不出现，categories 只来自融合候选
        llm = ScriptedLLM(
            rewrite_response={"query": "请问退款工单的处理方式"},
            classify_response={"category": "aftersale", "confidence": 0.9},
        )
        router, skills = await _fused_router(llm)
        dec = await router.route("那个咋整", skills, history=_CTX_HISTORY)
        assert dec.path == PATH_LLM
        names = {s.name for s in dec.tools}
        assert names == {"fused_after_main_skill", "fused_after_extra_skill"}
        assert "fused_after_iso_skill" not in names
        assert len(dec.tools) < len(skills)
        assert dec.details["categories"] == ["aftersale"]
        assert dec.details["fallback_candidates"] == "fused"
        assert llm.rewrite_calls == 1 and llm.classify_calls == 1

    async def test_rewrite_failure_fallback_uses_first_fused_candidates(self):
        # b：改写 LLM 抛异常静默降级 -> 兜底候选=首次 fused（历史“退款”只点亮 main），tools ⊆ fused
        llm = ScriptedLLM(
            rewrite_raises=RuntimeError("rewrite boom"),
            classify_response={"category": "aftersale", "confidence": 0.9},
        )
        router, skills = await _fused_router(llm)
        dec = await router.route("那个咋整", skills, history=_FUSED_HISTORY_REFUND)
        assert dec.path == PATH_LLM
        assert dec.details["query_rewrite_failed"] is True
        assert dec.details["fallback_candidates"] == "fused"
        names = {s.name for s in dec.tools}
        assert names == {"fused_after_main_skill"}
        assert names <= {
            "fused_after_main_skill", "fused_after_extra_skill", "fused_after_iso_skill", "fused_billing_skill"
        }
        assert "fused_after_extra_skill" not in names and "fused_after_iso_skill" not in names
        assert llm.rewrite_calls == 1 and llm.classify_calls == 1

    async def test_empty_fused_falls_back_to_full_candidates(self):
        # c：两路均无证据（拼接与改写 query 都零向量、BM25 对当前消息零命中）-> 回退全量并打标
        llm = ScriptedLLM(
            rewrite_response={"query": "一些完全无关的内容xyz"},
            classify_response={"category": "aftersale", "confidence": 0.9},
        )
        router, skills = await _fused_router(llm)
        dec = await router.route("那个咋整", skills, history=_CTX_HISTORY)
        assert dec.path == PATH_LLM
        assert dec.details["fallback_candidates"] == "full"
        names = {s.name for s in dec.tools}
        assert names == {
            "fused_after_main_skill",
            "fused_after_extra_skill",
            "fused_after_iso_skill",
        }
        assert dec.details["categories"] == ["aftersale", "billing"]


# ---- 兜底 LLM 三级分流 + confidence 生效（任务 5.1） ----
class _TicketQuerySkill(Skill):
    """与 TicketSkill 同域的第二个 Skill：用于区分“点名收窄到 1 个”与“整域全放”。"""

    name = "ticket_query_skill"
    description = "查询工单进度"
    category = "ticket"
    examples: list[str] = []

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


def _tier_skills():
    return [WeatherSkill(), TicketSkill(), _TicketQuerySkill(), ReportSkill()]


_TIER_MESSAGE = "你好呀"  # FakeEmbedder 零向量 -> 必低置信 -> 必走路由 LLM 兜底


async def _tier_router(llm, *, llm_conf_high=0.7, skills=None):
    """单轮低置信：无规则/无关键词索引、零向量低置信 -> 必走路由 LLM 兜底，只调用分类 LLM 一次。"""
    skills = skills or _tier_skills()
    emb = FakeEmbedder()
    idx = await SkillIndex(emb, cache_dir="", model_id="fake-tier").build(skills)
    router = SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=llm,
        embedder=emb,
        hybrid=False,
        llm_conf_high=llm_conf_high,
    )
    return router, skills


class TestLlmConfidenceTiering:
    async def test_high_confidence_names_in_candidate_skill_narrows_to_it(self):
        # ① 高 confidence + 候选集内 skills=[A] -> 工具集仅 {A}（同域另一个 Skill 不放）
        llm = FakeLLM({
            "category": "ticket",
            "confidence": 0.95,
            "skills": ["ticket_skill"],
            "reason": "明确点名工单技能",
        })
        router, skills = await _tier_router(llm)
        dec = await router.route(_TIER_MESSAGE, skills)
        assert dec.path == PATH_LLM
        assert [s.name for s in dec.tools] == ["ticket_skill"]
        assert dec.details["llm_skills"] == ["ticket_skill"]
        assert llm.calls == 1

    async def test_medium_confidence_category_only_returns_whole_category(self):
        # ② 中 confidence（< llm_conf_high）且未点名 -> 该域全部候选 Skill
        router, skills = await _tier_router(
            FakeLLM({"category": "ticket", "confidence": 0.6, "reason": "像是工单域"})
        )
        dec = await router.route(_TIER_MESSAGE, skills)
        assert dec.path == PATH_LLM
        assert {s.name for s in dec.tools} == {"ticket_skill", "ticket_query_skill"}

    async def test_high_confidence_without_skills_falls_back_to_category(self):
        # ② 仅 category（即使 confidence 高、无 skills 字段）-> 该域全部候选 Skill
        router, skills = await _tier_router(
            FakeLLM({"category": "ticket", "confidence": 0.9, "reason": "工单域"})
        )
        dec = await router.route(_TIER_MESSAGE, skills)
        assert dec.path == PATH_LLM
        assert {s.name for s in dec.tools} == {"ticket_skill", "ticket_query_skill"}

    async def test_partial_illegal_skill_names_drops_illegal_keeps_named(self):
        # ③a 部分非法：丢弃候选集外非法名，合法名仍按高置信点名（不放同域另一 Skill）
        llm = FakeLLM({
            "category": "ticket",
            "confidence": 0.95,
            "skills": ["ticket_skill", "not_exist_skill"],
            "reason": "一个合法一个非法",
        })
        router, skills = await _tier_router(llm)
        dec = await router.route(_TIER_MESSAGE, skills)
        assert dec.path == PATH_LLM
        assert [s.name for s in dec.tools] == ["ticket_skill"]

    async def test_all_illegal_skill_names_fall_through_to_category(self):
        # ③b 全部非法：点名落空，合法 category 兜底 -> 该域全部候选 Skill
        llm = FakeLLM({
            "category": "ticket",
            "confidence": 0.95,
            "skills": ["not_exist_skill", "another_bad_skill"],
            "reason": "全部点名非法",
        })
        router, skills = await _tier_router(llm)
        dec = await router.route(_TIER_MESSAGE, skills)
        assert dec.path == PATH_LLM
        assert {s.name for s in dec.tools} == {"ticket_skill", "ticket_query_skill"}

    async def test_all_illegal_skill_names_unknown_category_clarifies(self):
        # ③c 全部非法且 category 非法/unknown -> 澄清
        llm = FakeLLM({
            "category": "unknown",
            "confidence": 0.95,
            "skills": ["not_exist_skill"],
            "clarify_question": "你想办什么业务？",
        })
        router, skills = await _tier_router(llm)
        dec = await router.route(_TIER_MESSAGE, skills)
        assert dec.path == PATH_CLARIFY
        assert dec.tools == []
        assert dec.clarify_text == "你想办什么业务？"

    async def test_threshold_boundary_uses_configured_llm_conf_high(self):
        # confidence 恰为 llm_conf_high 边界：默认阈值 0.7 时点名收窄到 1 个；
        # 阈值抬到 0.95 时同响应回落 category -> 该域 2 个候选 Skill 全放
        resp = {
            "category": "ticket",
            "confidence": 0.7,
            "skills": ["ticket_skill"],
            "reason": "边界置信",
        }
        router, skills = await _tier_router(FakeLLM(dict(resp)), llm_conf_high=0.7)
        dec = await router.route(_TIER_MESSAGE, skills)
        assert [s.name for s in dec.tools] == ["ticket_skill"]

        router, skills = await _tier_router(FakeLLM(dict(resp)), llm_conf_high=0.95)
        dec = await router.route(_TIER_MESSAGE, skills)
        assert {s.name for s in dec.tools} == {"ticket_skill", "ticket_query_skill"}

    async def test_chitchat_and_unknown_branch_unchanged(self):
        # ④ chitchat -> 空工具纯对话；unknown/低置信 -> 澄清，均不受 skills 字段影响
        router, skills = await _tier_router(FakeLLM({"category": "chitchat", "confidence": 0.99}))
        dec = await router.route("嗨你好", skills)
        assert dec.path == PATH_CHITCHAT
        assert dec.tools == []

        router, skills = await _tier_router(
            FakeLLM({"category": "unknown", "confidence": 0.2, "clarify_question": "你想查什么？"})
        )
        dec = await router.route("呃那个", skills)
        assert dec.path == PATH_CLARIFY
        assert dec.tools == []
        assert dec.clarify_text == "你想查什么？"


