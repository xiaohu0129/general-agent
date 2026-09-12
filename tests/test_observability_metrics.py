"""可观测指标增补（8.1/8.2）：误杀/分数分布/改写/缺参/澄清结果维度。

全部用 FakeCounter/FakeHistogram（monkeypatch observability 模块级 instrument 变量），
断言 metric 名、标签与值；不依赖外部服务、不拉真 OTel。
防双计：record_intent_route(path="clarify") 不再自动累加 clarify counter；
首轮澄清（无 clarify_outcome）不触发 record_clarify。
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from conftest import build_test_app, make_llm_transport, parse_sse
from general_agent import observability as obs
from general_agent.skill_router.router import (
    PATH_CLARIFY,
    PATH_LLM,
    PATH_OPTION,
    PATH_VECTOR,
    RouteDecision,
)
from general_agent.skills import Skill, SkillContext


class FakeCounter:
    """记录 add 值与标签的假 counter。"""

    def __init__(self):
        self.records: list[tuple[int, dict]] = []

    def add(self, value, attrs):
        self.records.append((value, dict(attrs)))


class FakeHistogram:
    """记录 record 值与标签的假 histogram。"""

    def __init__(self):
        self.records: list[tuple[float, dict]] = []

    def record(self, value, attrs):
        self.records.append((value, dict(attrs)))


# ---------------- 直调函数级断言 ----------------
class TestRecordIntentMiss:
    def test_counter_name_and_labels(self):
        c = FakeCounter()
        obs._intent_miss = c
        try:
            obs.record_intent_miss("vector", retrieval="vector")
        finally:
            obs._intent_miss = None
        assert c.records == [(1, {"env": "dev", "path": "vector", "retrieval": "vector"})]

    def test_option_and_keyword_labels(self):
        c = FakeCounter()
        obs._intent_miss = c
        try:
            obs.record_intent_miss("option", retrieval="keyword")
            obs.record_intent_miss("llm", retrieval="keyword")
        finally:
            obs._intent_miss = None
        paths = {r[1]["path"] for r in c.records}
        assert paths == {"option", "llm"}
        assert all(r[1]["retrieval"] == "keyword" for r in c.records)
        assert all(set(r[1]) == {"env", "path", "retrieval"} for r in c.records)


class TestRecordIntentScore:
    def test_histograms_with_route_label(self):
        score, gap = FakeHistogram(), FakeHistogram()
        obs._intent_score, obs._intent_score_gap = score, gap
        try:
            obs.record_intent_score(0.91, 0.12, "vector")
            obs.record_intent_score(8.3, None, "bm25")
            obs.record_intent_score(0.03, None, "rrf")
        finally:
            obs._intent_score, obs._intent_score_gap = None, None
        routes = [r[1]["route"] for r in score.records]
        assert routes == ["vector", "bm25", "rrf"]
        # gap 仅在提供时记录
        gap_routes = [r[1]["route"] for r in gap.records]
        assert gap_routes == ["vector"]
        assert gap.records[0] == (0.12, {"env": "dev", "route": "vector"})
        assert all(set(r[1]) == {"env", "route"} for r in score.records + gap.records)

    def test_lazy_guard_no_instrument_no_error(self):
        # instrument 未初始化（观测关闭）时静默返回
        obs.record_intent_score(0.9, 0.1, "vector")
        obs.record_intent_miss("vector", retrieval="vector")


class TestRecordIntentRewrite:
    def test_counter_labels_success_failed(self):
        c = FakeCounter()
        obs._intent_rewrite = c
        try:
            obs.record_intent_rewrite("success")
            obs.record_intent_rewrite("failed")
        finally:
            obs._intent_rewrite = None
        assert c.records == [
            (1, {"env": "dev", "result": "success"}),
            (1, {"env": "dev", "result": "failed"}),
        ]

    def test_invalid_result_raises(self):
        with pytest.raises(ValueError):
            obs.record_intent_rewrite("ok")


class TestRecordMissingArgs:
    def test_counter_labels_env_tool(self):
        c = FakeCounter()
        obs._missing_args = c
        try:
            obs.record_missing_args("book_trip")
        finally:
            obs._missing_args = None
        assert c.records == [(1, {"env": "dev", "tool": "book_trip"})]
        assert set(c.records[0][1]) == {"env", "tool"}


class TestRecordClarify:
    def test_counter_reuses_name_with_result_label(self):
        c = FakeCounter()
        obs._intent_clarify = c
        try:
            obs.record_clarify("option")
            obs.record_clarify("text")
            obs.record_clarify("repeat")
        finally:
            obs._intent_clarify = None
        assert c.records == [
            (1, {"env": "dev", "result": "option"}),
            (1, {"env": "dev", "result": "text"}),
            (1, {"env": "dev", "result": "repeat"}),
        ]

    def test_invalid_result_raises(self):
        with pytest.raises(ValueError):
            obs.record_clarify("first_round")


class TestRecordIntentRoute:
    def test_path_option_counts_into_route_dimension(self):
        c = FakeCounter()
        obs._intent_route = c
        try:
            obs.record_intent_route("option")
        finally:
            obs._intent_route = None
        assert c.records == [(1, {"env": "dev", "path": "option", "category": "none"})]

    def test_clarify_path_no_longer_auto_increments_clarify_counter(self):
        # 防双计：移除 observability.py 内嵌 if path=="clarify" 分支
        route, clarify = FakeCounter(), FakeCounter()
        obs._intent_route, obs._intent_clarify = route, clarify
        try:
            obs.record_intent_route(PATH_CLARIFY)
        finally:
            obs._intent_route, obs._intent_clarify = None, None
        assert route.records and route.records[0][1]["path"] == "clarify"
        assert clarify.records == []  # MUST NOT 自动累加

    def test_repeat_clarify_counted_once_via_record_clarify(self):
        # repeat 轮澄清计数仅经 record_clarify(result="repeat") +1 一次
        route, clarify = FakeCounter(), FakeCounter()
        obs._intent_route, obs._intent_clarify = route, clarify
        try:
            obs.record_intent_route(PATH_CLARIFY)
            obs.record_clarify("repeat")
        finally:
            obs._intent_route, obs._intent_clarify = None, None
        repeat_adds = [r for r in clarify.records if r[1].get("result") == "repeat"]
        assert repeat_adds == [(1, {"env": "dev", "result": "repeat"})]


# ---------------- chat 层接线（澄清结果 / 分数 / 改写） ----------------
class _FakeRouter:
    def __init__(self, decision):
        self.decision = decision

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
        return self.decision


class _SkillA(Skill):
    name = "skill_a"
    description = "工具 A"

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


def _post(app):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "帮我"}) as r:
        text = "".join(chunk for chunk in r.iter_text())
    return parse_sse(text)


def _spy(monkeypatch, name):
    calls = []
    monkeypatch.setattr(obs, name, lambda *a, **k: calls.append((a, k)))
    return calls


class TestChatWiring:
    def test_clarify_outcome_option_triggers_record_clarify(self, monkeypatch):
        clarify = _spy(monkeypatch, "record_clarify")
        app, _, _ = build_test_app(skill=_SkillA())
        app.state.skill_router = _FakeRouter(
            RouteDecision(PATH_OPTION, [_SkillA()], details={"clarify_outcome": "option"})
        )
        _post(app)
        assert clarify == [(("option",), {})]

    def test_clarify_outcome_text_and_repeat_triggers_record_clarify(self, monkeypatch):
        for outcome, path, tools in (
            ("text", PATH_VECTOR, [_SkillA()]),
            ("repeat", PATH_CLARIFY, []),
        ):
            clarify = _spy(monkeypatch, "record_clarify")
            app, _, _ = build_test_app(skill=_SkillA())
            app.state.skill_router = _FakeRouter(
                RouteDecision(
                    path,
                    tools,
                    clarify_text="再说明下？" if path == PATH_CLARIFY else None,
                    details={"clarify_outcome": outcome},
                )
            )
            _post(app)
            assert clarify == [((outcome,), {})]

    def test_first_round_clarify_without_outcome_no_record_clarify(self, monkeypatch):
        # 首轮澄清（无 clarify_outcome）不触发 record_clarify
        clarify = _spy(monkeypatch, "record_clarify")
        app, _, _ = build_test_app(skill=_SkillA())
        app.state.skill_router = _FakeRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="想做什么？", details={"categories": ["order"]})
        )
        _post(app)
        assert clarify == []

    def test_non_clarify_no_record_clarify(self, monkeypatch):
        clarify = _spy(monkeypatch, "record_clarify")
        app, _, _ = build_test_app(skill=_SkillA())
        app.state.skill_router = _FakeRouter(RouteDecision(PATH_VECTOR, [_SkillA()], details={}))
        _post(app)
        assert clarify == []

    def test_score_histograms_vector_bm25_rrf(self, monkeypatch):
        calls = []
        monkeypatch.setattr(obs, "record_intent_score", lambda *a, **k: calls.append((a, k)))
        app, _, _ = build_test_app(skill=_SkillA())
        app.state.skill_router = _FakeRouter(
            RouteDecision(
                PATH_LLM,
                [_SkillA()],
                details={
                    "top1_score": 0.92,
                    "score_gap": 0.15,
                    "bm25_k": [{"skill": "skill_a", "score": 8.3}],
                    "rrf": [{"skill": "skill_a", "score": 0.03}],
                },
            )
        )
        _post(app)
        routes = [c[0][2] for c in calls]
        assert routes.count("vector") == 1 and routes.count("bm25") == 1 and routes.count("rrf") == 1
        vec = next(c for c in calls if c[0][2] == "vector")
        assert vec[0][0] == pytest.approx(0.92) and vec[0][1] == pytest.approx(0.15)
        bm = next(c for c in calls if c[0][2] == "bm25")
        assert bm[0][0] == pytest.approx(8.3)

    def test_semantic_off_skips_vector_score(self, monkeypatch):
        calls = []
        monkeypatch.setattr(obs, "record_intent_score", lambda *a, **k: calls.append((a, k)))
        app, _, _ = build_test_app(skill=_SkillA())
        app.state.skill_router = _FakeRouter(
            RouteDecision(
                PATH_LLM,
                [_SkillA()],
                details={"semantic_off": True, "bm25_k": [{"skill": "skill_a", "score": 7.1}]},
            )
        )
        _post(app)
        routes = [c[0][2] for c in calls]
        assert "vector" not in routes
        assert "bm25" in routes

    def test_rewrite_counters_from_details(self, monkeypatch):
        for key, result in (("query_rewritten", "success"), ("query_rewrite_failed", "failed")):
            calls = []
            monkeypatch.setattr(obs, "record_intent_rewrite", lambda *a, **k: calls.append((a, k)))
            app, _, _ = build_test_app(skill=_SkillA())
            app.state.skill_router = _FakeRouter(
                RouteDecision(PATH_VECTOR, [_SkillA()], details={key: True})
            )
            _post(app)
            assert calls == [((), {"result": result})]

    def test_no_rewrite_no_counter(self, monkeypatch):
        calls = []
        monkeypatch.setattr(obs, "record_intent_rewrite", lambda *a, **k: calls.append((a, k)))
        app, _, _ = build_test_app(skill=_SkillA())
        app.state.skill_router = _FakeRouter(RouteDecision(PATH_VECTOR, [_SkillA()], details={}))
        _post(app)
        assert calls == []


# ---------------- runner 层接线（缺参计数） ----------------
class _BookSkill(Skill):
    name = "book_skill"
    description = "预订工具"

    class _Args:
        pass

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class TestMissingArgsWiring:
    def test_missing_args_records_counter(self, monkeypatch):
        from pydantic import BaseModel, Field

        class Args(BaseModel):
            city: str = Field(description="城市")

        class BookSkill(Skill):
            name = "book_skill"
            description = "预订工具"
            args_schema = Args

            async def run(self, ctx: SkillContext, **kwargs):
                return {"ok": True}

        calls = []
        monkeypatch.setattr(obs, "record_missing_args", lambda *a, **k: calls.append((a, k)))
        app, store, _ = build_test_app(
            skill=BookSkill(), llm_transport=make_llm_transport(tool_name="book_skill", args={})
        )
        from fastapi.testclient import TestClient

        client = TestClient(app)
        with client.stream("POST", "/chat", json={"message": "帮我订"}) as r:
            text = "".join(chunk for chunk in r.iter_text())
        evs = parse_sse(text)
        tool_end = next(d for e, d, _ in evs if e == "tool_end")
        assert tool_end["status"] == "error" and tool_end["errorCode"] == "MISSING_ARGS"
        assert calls == [(("book_skill",), {})]

    def test_tool_error_path_no_missing_args(self, monkeypatch):
        # 业务异常 re-raise 走 on_tool_error，不记缺参
        calls = []
        monkeypatch.setattr(obs, "record_missing_args", lambda *a, **k: calls.append((a, k)))

        class BadSkill(Skill):
            name = "bad_skill"
            description = "会抛错的工具"

            async def run(self, ctx: SkillContext, **kwargs):
                raise RuntimeError("boom")

        app, _, _ = build_test_app(
            skill=BadSkill(), llm_transport=make_llm_transport(tool_name="bad_skill")
        )
        from fastapi.testclient import TestClient

        client = TestClient(app)
        with client.stream("POST", "/chat", json={"message": "跑一下"}) as r:
            text = "".join(chunk for chunk in r.iter_text())
        evs = parse_sse(text)
        tool_end = next(d for e, d, _ in evs if e == "tool_end")
        assert tool_end["status"] == "error"
        assert calls == []
