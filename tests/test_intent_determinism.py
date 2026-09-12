"""11.2 确定性与回归：相同输入两轮路由工具集/path 一致；routing.enabled=false 与无 Skill 情况既有测试通过。"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from general_agent.skill_router.index import SkillIndex
from general_agent.skill_router.keyword import KeywordIndex
from general_agent.skill_router.router import SkillRouter
from general_agent.skill_router.rules import RuleMatcher
from general_agent.skills import Skill, SkillContext


class _ASkill(Skill):
    name = "d_a_skill"
    description = "查询订单状态"
    category = "order"
    examples = ["查订单", "订单到哪了"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": 1}


class _BSkill(Skill):
    name = "d_b_skill"
    description = "办理退款"
    category = "refund"
    examples = ["我要退款"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": 2}


class _Emb:
    async def embed_texts(self, texts):
        out = []
        for t in texts:
            v = [0.0, 0.0]
            if "订单" in t:
                v[0] = 1.0
            if "退款" in t:
                v[1] = 1.0
            out.append(v)
        return out


class _LLM:
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=json.dumps({"category": "unknown", "confidence": 0.1}))


async def _mk_router():
    skills = [_ASkill(), _BSkill()]
    emb = _Emb()
    idx = SkillIndex(emb, cache_dir="", model_id="det-embed")
    await idx.build(skills)
    return SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=_LLM(),
        embedder=emb,
        keyword_index=KeywordIndex().build(skills),
        hybrid=True,
    )


@pytest.mark.asyncio
async def test_deterministic_two_rounds_same_result():
    """相同输入两轮路由：工具集与 path 完全一致。"""
    router = await _mk_router()
    candidates = [_ASkill(), _BSkill()]
    d1 = await router.route("查订单", candidates)
    d2 = await router.route("查订单", candidates)
    assert d1.path == d2.path
    assert [s.name for s in d1.tools] == [s.name for s in d2.tools]

    # 低置信路径（改写关闭的确定性）：两轮也一致
    d3 = await router.route("嗯嗯啊啊", candidates)
    d4 = await router.route("嗯嗯啊啊", candidates)
    assert d3.path == d4.path
    assert [s.name for s in d3.tools] == [s.name for s in d4.tools]


def test_routing_disabled_falls_back_to_all_tools():
    """routing.enabled=false：无 router -> 全量工具（既有行为，回归保护）。"""
    from conftest import build_test_app, make_llm_transport, parse_sse
    from fastapi.testclient import TestClient

    app, _, _ = build_test_app(llm_transport=make_llm_transport())
    assert app.state.skill_router is None or app.state.skill_router is None
    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "调用工具"}) as r:
        evs = parse_sse("".join(ch for ch in r.iter_text()))
    # 全量工具（DemoSkill）可被调用
    assert any(e[0] == "tool_end" for e in evs)


def test_no_skills_chitchat_path():
    """无 Skill：路由返回 chitchat 纯对话（既有行为，回归保护）。"""
    from conftest import build_test_app, parse_sse
    from fastapi.testclient import TestClient

    app, _, _ = build_test_app()

    class _EmptyRegistry:
        def list_allowed(self, ctx):
            return []

    app.state.skill_registry = _EmptyRegistry()

    class _FakeRouter:
        async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
            from general_agent.skill_router.router import PATH_CHITCHAT, RouteDecision

            assert candidates == []
            return RouteDecision(PATH_CHITCHAT, [], details={})

    app.state.skill_router = _FakeRouter()
    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "你好"}) as r:
        evs = parse_sse("".join(ch for ch in r.iter_text()))
    assert any(e[0] == "turn_end" for e in evs)
    assert not any(e[0] in ("tool_start", "tool_end", "clarify") for e in evs)
