"""结构化澄清选项产出（4.4）+ 选项回传确定性收窄（4.5）测试。

覆盖：
- 4.4：澄清分支 decision.clarify_options/details["clarify_options"] 产出
  [{label,value}]，value 带 category:/skill: 前缀且名均在 env 候选集内；
  可混两种粒度；数量 ≤ clarify_option_max；开关关闭时不产出；repeat 路径同样产出。
- 4.5：携带 clarify_selection 命中上轮 options 且经候选集合法 -> path="option"
  确定性收窄（不调 embedder/路由 LLM/规则），details 标注；非法/缺失回落文本闭环；
  chat e2e：label 落 user 行、value 不进 content、update_clarify_selected 回写一次。
纯内存/fake，不依赖网络。
"""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from conftest import FakeStore, build_test_app, parse_sse
from general_agent.skill_router.index import SkillIndex
from general_agent.skill_router.keyword import KeywordIndex
from general_agent.skill_router.router import (
    PATH_CLARIFY,
    PATH_OPTION,
    RouteDecision,
    SkillRouter,
)
from general_agent.skill_router.rules import RuleMatcher
from general_agent.config import RouteRule
from general_agent.skills import Skill, SkillContext


# ---------------- 测试域 Skill ----------------
class _OrderQuerySkill(Skill):
    name = "order_query"
    description = "查询订单物流状态"
    category = "order"
    examples = ["查我的订单", "订单到哪了"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _OrderCancelSkill(Skill):
    name = "order_cancel"
    description = "取消订单"
    category = "order"
    examples = ["怎么取消订单"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _RefundSkill(Skill):
    name = "refund_skill"
    description = "办理退款退货售后"
    category = "refund"
    examples = ["申请退款", "退货退款怎么操作"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _InvoiceSkill(Skill):
    name = "invoice_skill"
    description = "开具发票"
    category = "billing"
    examples = ["怎么开发票"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


def _skills():
    return [_OrderQuerySkill(), _OrderCancelSkill(), _RefundSkill(), _InvoiceSkill()]


class _CountingEmbedder:
    """3 维 one-hot：退款 dim0、订单 dim1、发票 dim2；无业务词 -> 零向量低置信。"""

    def __init__(self):
        self.calls = 0

    async def embed_texts(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0, 0.0, 0.0]
            if "退款" in t or "退货" in t:
                v[0] = 1.0
            if "订单" in t:
                v[1] = 1.0
            if "发票" in t:
                v[2] = 1.0
            out.append(v)
        return out


class _ScriptLLM:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=json.dumps(self.response, ensure_ascii=False))


class _CountingRuleMatcher(RuleMatcher):
    def __init__(self, rules=None):
        super().__init__(rules or [])
        self.calls = 0

    def match(self, message, candidates):
        self.calls += 1
        return super().match(message, candidates)


async def _router(llm=None, *, rules=None, hybrid=False, clarify_options=True,
                  clarify_option_max=4, embedder=None, semantic=True):
    skills = _skills()
    emb = embedder or _CountingEmbedder()
    idx = await SkillIndex(emb, cache_dir="", model_id="fake-options").build(skills)
    emb.calls = 0  # 仅统计 route 期调用，不计索引构建
    keyword_index = KeywordIndex().build(skills) if hybrid else None
    rule_matcher = _CountingRuleMatcher(rules or [])
    router = SkillRouter(
        index=idx,
        rule_matcher=rule_matcher,
        llm=llm or _ScriptLLM({"category": "unknown", "confidence": 0.2}),
        embedder=emb,
        hybrid=hybrid,
        keyword_index=keyword_index,
        semantic=semantic,
        clarify_options=clarify_options,
        clarify_option_max=clarify_option_max,
    )
    return router, skills, emb, rule_matcher


def _assert_legal_options(options, skills):
    legal_categories = {s.category for s in skills if s.category}
    legal_skills = {s.name for s in skills}
    values = [o["value"] for o in options]
    assert len(values) == len(set(values))  # value 去重
    for o in options:
        assert set(o) == {"label", "value"}
        assert isinstance(o["label"], str) and o["label"].strip()
        prefix, _, name = o["value"].partition(":")
        assert prefix in ("category", "skill")
        assert name
        if prefix == "category":
            assert name in legal_categories
            assert o["label"] == name
        else:
            assert name in legal_skills


# ---------------- 4.4 结构化选项产出 ----------------
class TestClarifyOptionsProduction:
    async def test_first_clarify_produces_prefixed_options(self):
        # LLM 给候选 categories + 点名 skills（低置信，进澄清分支）
        llm = _ScriptLLM({
            "category": "unknown",
            "confidence": 0.2,
            "categories": ["refund", "billing"],
            "skills": ["invoice_skill"],
            "clarify_question": "具体想办什么？",
        })
        router, skills, _, _ = await _router(llm=llm)
        dec = await router.route("帮我处理下", skills)
        assert dec.path == PATH_CLARIFY
        options = dec.clarify_options
        assert options is not None and options == dec.details["clarify_options"]
        _assert_legal_options(options, skills)
        values = [o["value"] for o in options]
        # 先 category 后 skill，固定顺序
        assert values[:2] == ["category:refund", "category:billing"]
        assert "skill:invoice_skill" in values
        # skill label 取短描述
        inv = next(o for o in options if o["value"] == "skill:invoice_skill")
        assert inv["label"] == "开具发票"
        # categories 中集外名不得出现
        assert "category:不存在" not in values

    async def test_llm_illegal_category_and_skill_names_filtered(self):
        llm = _ScriptLLM({
            "category": "unknown",
            "confidence": 0.2,
            "categories": ["refund", "ghost_dir"],
            "skills": ["refund_skill", "ghost_skill"],
        })
        router, skills, _, _ = await _router(llm=llm)
        dec = await router.route("随便吧", skills)
        values = [o["value"] for o in dec.clarify_options]
        assert "category:ghost_dir" not in values
        assert "skill:ghost_skill" not in values
        assert "category:refund" in values
        assert "skill:refund_skill" in values

    async def test_empty_llm_categories_backfilled_from_retrieval(self):
        # LLM 不给 categories/skills：无语义（不走向量）+ BM25 命中 refund_skill 收窄，
        # 来源 A 由本轮融合候选所属域补（refund），来源 B 补融合 Skill（refund_skill）
        router, skills, _, _ = await _router(hybrid=True, semantic=False)
        dec = await router.route("我要退款", skills)
        assert dec.path == PATH_CLARIFY
        assert dec.details.get("degraded_keyword") is True
        options = dec.clarify_options
        assert options is not None
        values = [o["value"] for o in options]
        _assert_legal_options(options, skills)
        assert values == ["category:refund", "skill:refund_skill"]

    async def test_options_count_capped_by_max(self):
        llm = _ScriptLLM({
            "category": "unknown",
            "confidence": 0.2,
            "categories": ["refund", "billing", "order"],
            "skills": ["invoice_skill", "refund_skill", "order_query"],
        })
        router, skills, _, _ = await _router(llm=llm, clarify_option_max=3)
        dec = await router.route("帮个忙", skills)
        assert len(dec.clarify_options) <= 3
        # 截断保序：先 category
        assert dec.clarify_options[0]["value"] == "category:refund"

    async def test_mixed_prefixes_in_one_card(self):
        llm = _ScriptLLM({
            "category": "unknown",
            "confidence": 0.2,
            "categories": ["refund"],
            "skills": ["refund_skill"],
        })
        router, skills, _, _ = await _router(llm=llm)
        dec = await router.route("帮个忙", skills)
        prefixes = {o["value"].split(":", 1)[0] for o in dec.clarify_options}
        assert prefixes == {"category", "skill"}

    async def test_repeat_clarify_produces_options_with_history_on_top(self):
        # 文本闭环 repeat 路径：历史 categories 合法者置顶作为来源 A
        router, skills, _, _ = await _router(
            llm=_ScriptLLM({"category": "unknown", "confidence": 0.2})
        )
        prev = {
            "categories": ["order", "refund"],
            "options": [
                {"label": "订单", "value": "category:order"},
                {"label": "退款", "value": "category:refund"},
            ],
            "turn_id": "Tn",
        }
        dec = await router.route("随便吧", skills, prev_clarify=prev)
        assert dec.path == PATH_CLARIFY
        assert dec.details["clarify_outcome"] == "repeat"
        options = dec.clarify_options
        assert options is not None
        _assert_legal_options(options, skills)
        values = [o["value"] for o in options]
        assert values[:2] == ["category:order", "category:refund"]

    async def test_clarify_options_disabled_falls_back_to_plain_text(self):
        router, skills, _, _ = await _router(clarify_options=False)
        dec = await router.route("随便吧", skills)
        assert dec.path == PATH_CLARIFY
        assert dec.clarify_text  # 纯文本澄清行为不变
        assert dec.clarify_options is None
        assert not dec.details.get("clarify_options")

    async def test_chitchat_and_non_clarify_have_no_options(self):
        router, skills, _, _ = await _router(
            llm=_ScriptLLM({"category": "chitchat", "confidence": 0.95})
        )
        dec = await router.route("哈哈谢谢", skills)
        assert dec.clarify_options is None
        assert "clarify_options" not in dec.details


# ---------------- 4.5 选项回传确定性收窄 ----------------
_PREV_WITH_OPTIONS = {
    "categories": ["order", "refund"],
    "options": [
        {"label": "订单", "value": "category:order"},
        {"label": "退款", "value": "category:refund"},
        {"label": "退款办理", "value": "skill:refund_skill"},
    ],
    "turn_id": "Tn",
}


class TestOptionSelectionNarrowing:
    async def test_category_selection_narrows_without_embedder_llm_rules(self):
        llm = _ScriptLLM({"category": "unknown", "confidence": 0.2})
        router, skills, emb, rules = await _router(llm=llm)
        dec = await router.route(
            "退款", skills,
            prev_clarify=dict(_PREV_WITH_OPTIONS),
            selection="category:refund",
        )
        assert dec.path == PATH_OPTION
        assert [s.name for s in dec.tools] == ["refund_skill"]
        assert dec.details["clarify_outcome"] == "option"
        assert dec.details["clarify_selection"] == "category:refund"
        assert emb.calls == 0
        assert llm.calls == 0
        assert rules.calls == 0

    async def test_skill_selection_narrows_to_single_skill(self):
        router, skills, _, _ = await _router()
        dec = await router.route(
            "退款办理", skills,
            prev_clarify=dict(_PREV_WITH_OPTIONS),
            selection="skill:refund_skill",
        )
        assert dec.path == PATH_OPTION
        assert [s.name for s in dec.tools] == ["refund_skill"]

    async def test_category_selection_expands_to_whole_category(self):
        router, skills, _, _ = await _router()
        dec = await router.route(
            "订单", skills,
            prev_clarify=dict(_PREV_WITH_OPTIONS),
            selection="category:order",
        )
        assert dec.path == PATH_OPTION
        assert {s.name for s in dec.tools} == {"order_query", "order_cancel"}

    async def test_selection_takes_priority_over_rule_escape_hatch(self):
        # 点选优先于规则逃生门：即使消息文本命中规则，也按 selection 确定性收窄
        router, skills, emb, rules = await _router(
            rules=[RouteRule(pattern="发票", skills=["invoice_skill"])]
        )
        dec = await router.route(
            "发票", skills,
            prev_clarify=dict(_PREV_WITH_OPTIONS),
            selection="category:refund",
        )
        assert dec.path == PATH_OPTION
        assert [s.name for s in dec.tools] == ["refund_skill"]
        assert rules.calls == 0

    async def test_empty_value_ignored_falls_back_to_text_loop(self):
        llm = _ScriptLLM({"category": "refund", "confidence": 0.9})
        router, skills, emb, rules = await _router(llm=llm)
        dec = await router.route(
            "我要退款", skills,
            prev_clarify=dict(_PREV_WITH_OPTIONS),
            selection="",
        )
        assert dec.path != PATH_OPTION
        assert dec.details.get("clarify_selection_ignored") is True
        assert emb.calls >= 1  # 回落正常裁决

    async def test_bad_prefix_ignored(self):
        router, skills, _, _ = await _router(
            llm=_ScriptLLM({"category": "unknown", "confidence": 0.2})
        )
        dec = await router.route(
            "退款", skills,
            prev_clarify=dict(_PREV_WITH_OPTIONS),
            selection="foo:refund",
        )
        assert dec.path != PATH_OPTION
        assert dec.details.get("clarify_selection_ignored") is True

    async def test_value_not_in_prev_options_ignored(self):
        router, skills, _, _ = await _router(
            llm=_ScriptLLM({"category": "unknown", "confidence": 0.2})
        )
        dec = await router.route(
            "开发票", skills,
            prev_clarify=dict(_PREV_WITH_OPTIONS),
            selection="category:billing",
        )
        assert dec.path != PATH_OPTION
        assert dec.details.get("clarify_selection_ignored") is True
        # 回落裁决：billing 不在历史候选方向，repeat 澄清不暴露 invoice 工具
        assert dec.tools == []

    async def test_name_outside_env_candidates_ignored(self):
        # value 在上轮 options，但当前 env 候选集已无该 Skill -> 忽略回落
        prev = {
            "categories": ["refund"],
            "options": [{"label": "幽灵", "value": "skill:ghost_skill"}],
            "turn_id": "Tn",
        }
        router, skills, _, _ = await _router(
            llm=_ScriptLLM({"category": "unknown", "confidence": 0.2})
        )
        dec = await router.route(
            "幽灵", skills,
            prev_clarify=prev,
            selection="skill:ghost_skill",
        )
        assert dec.path != PATH_OPTION
        assert dec.details.get("clarify_selection_ignored") is True
        assert all(s.name != "ghost_skill" for s in dec.tools)

    async def test_selection_without_prev_clarify_ignored(self):
        # 点了旧卡片：携带 selection 但无 prev_clarify -> 忽略，不报错
        router, skills, _, _ = await _router(
            llm=_ScriptLLM({"category": "unknown", "confidence": 0.2})
        )
        dec = await router.route(
            "退款", skills, selection="category:refund"
        )
        assert dec.path != PATH_OPTION
        assert dec.details.get("clarify_selection_ignored") is True

    async def test_hand_typed_message_without_selection_unchanged(self):
        router, skills, emb, _ = await _router()
        dec = await router.route(
            "我要退款", skills, prev_clarify=dict(_PREV_WITH_OPTIONS)
        )
        assert dec.path != PATH_OPTION
        assert "clarify_selection_ignored" not in dec.details
        assert emb.calls >= 1


# ---------------- chat 层：ChatRequest + 回写 e2e ----------------
_SVC, _ENV, _USER = "default", "dev", "anonymous"
_SID = "default:dev:anonymous"


def _seed(store, turn_id, role, content, meta=None):
    store._seq += 1
    store.rows.append(
        {
            "id": store._seq,
            "service": _SVC,
            "env": _ENV,
            "user_id": _USER,
            "session_id": _SID,
            "turn_id": turn_id,
            "role": role,
            "content": content,
            "tool_calls": None,
            "tool_call_id": None,
            "meta": meta,
            "content_ref": None,
            "content_size": None,
            "content_kind": None,
        }
    )


class _OptionRouter:
    """确定性 option 决策的替身路由：记录 selection 入参。"""

    def __init__(self, decision):
        self.decision = decision
        self.last_selection = object()

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
        self.last_selection = selection
        return self.decision


class _SpyStore(FakeStore):
    """包装 update_clarify_selected 记录调用参数。"""

    def __init__(self):
        super().__init__()
        self.selected_calls = []

    async def update_clarify_selected(self, service, env, user_id, session_id, turn_id, selected):
        self.selected_calls.append((service, env, user_id, session_id, turn_id, selected))
        return await super().update_clarify_selected(
            service, env, user_id, session_id, turn_id, selected
        )


def _post(app, payload):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json=payload) as r:
        text = "".join(chunk for chunk in r.iter_text())
    return parse_sse(text)


class TestChatOptionSelection:
    def test_option_selection_persists_label_and_writes_back_selected(self):
        from conftest import DemoSkill

        app, store, _ = build_test_app(store=_SpyStore(), skill=DemoSkill())
        clarify_meta = {
            "kind": "clarify",
            "categories": ["order", "refund"],
            "options": [{"label": "退款", "value": "category:refund"}],
        }
        _seed(store, "Tn", "assistant", "你想查订单还是退款？", meta=clarify_meta)

        decision = RouteDecision(
            PATH_OPTION,
            [DemoSkill()],
            details={
                "clarify_outcome": "option",
                "clarify_selection": "category:refund",
            },
        )
        rec = _OptionRouter(decision)
        app.state.skill_router = rec
        _post(app, {"message": "退款", "clarify_selection": {"value": "category:refund"}})

        # selection 透传到 route
        assert rec.last_selection == "category:refund"
        # 回写一次，turn_id/selected 正确
        assert store.selected_calls == [
            (_SVC, _ENV, _USER, _SID, "Tn", "category:refund")
        ]
        # label 落 user 行 content；带前缀 value MUST NOT 出现在任何 content
        user_rows = [r for r in store.rows if r["role"] == "user"]
        assert user_rows[-1]["content"] == "退款"
        assert all("category:refund" not in (r["content"] or "") for r in store.rows)
        # 上轮澄清行 meta.selected 被回写、options 不被覆盖
        clarify_row = next(r for r in store.rows if r["turn_id"] == "Tn")
        assert clarify_row["meta"]["selected"] == "category:refund"
        assert clarify_row["meta"]["options"] == clarify_meta["options"]

    def test_invalid_selection_does_not_write_back(self):
        from conftest import DemoSkill

        app, store, _ = build_test_app(store=_SpyStore(), skill=DemoSkill())
        _seed(
            store, "Tn", "assistant", "请选择",
            meta={"kind": "clarify", "categories": ["refund"], "options": []},
        )
        decision = RouteDecision(
            PATH_CLARIFY, [],
            clarify_text="再说清楚点？",
            details={
                "categories": ["refund"],
                "clarify_outcome": "repeat",
                "clarify_selection_ignored": True,
            },
        )
        rec = _OptionRouter(decision)
        app.state.skill_router = rec
        _post(app, {"message": "嗯嗯", "clarify_selection": {"value": "foo:bar"}})
        assert store.selected_calls == []
        assert all("foo:bar" not in (r["content"] or "") for r in store.rows)

    def test_no_selection_field_hand_typed_unchanged(self):
        from conftest import DemoSkill

        app, store, _ = build_test_app(store=_SpyStore(), skill=DemoSkill())
        _seed(
            store, "Tn", "assistant", "请选择",
            meta={"kind": "clarify", "categories": ["refund"], "options": []},
        )
        decision = RouteDecision(
            "vector", [DemoSkill()], details={"clarify_outcome": "text"}
        )
        rec = _OptionRouter(decision)
        app.state.skill_router = rec
        _post(app, {"message": "那个退款的吧"})
        assert rec.last_selection is None
        assert store.selected_calls == []
