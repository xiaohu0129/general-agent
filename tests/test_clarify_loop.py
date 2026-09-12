"""澄清文本闭环（prev_clarify）+ clarify_meta 持久化链路测试（tasks 4.1/4.2）。

覆盖：
- router：prev_clarify 收窄子集裁决（text 成功 / repeat 再澄清 / 空子集 / chitchat 保持 / 规则逃生门不受限）
- chat 层：路由前从末条 assistant 澄清行 meta 构造 prev_clarify、history 截取规则、context_turns=0 不载历史
- 持久化：chat clarify_meta 装配落库（options=[] 默认 + 手构带前缀 options 原样透传）、run_turn 直连落 meta
纯内存/fake，不依赖 MySQL/网络。
"""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from conftest import FakeStore, build_test_app, parse_sse
from general_agent.skill_router.index import SkillIndex
from general_agent.skill_router.router import (
    PATH_CHITCHAT,
    PATH_CLARIFY,
    PATH_LLM,
    PATH_RULE,
    PATH_VECTOR,
    RouteDecision,
    SkillRouter,
)
from general_agent.skill_router.rules import RuleMatcher
from general_agent.config import RouteRule
from general_agent.runner import run_turn
from general_agent.skills import Skill, SkillContext


# ---------------- 测试域 Skill ----------------
class _OrderSkill(Skill):
    name = "order_skill"
    description = "查询和处理订单"
    category = "order"
    examples = ["查我的订单", "订单到哪了"]

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
    return [_OrderSkill(), _RefundSkill(), _InvoiceSkill()]


class _ClarifyEmbedder:
    """3 维 one-hot：退款/退货 dim0、订单 dim1、发票 dim2；无业务词 -> 零向量低置信。"""

    async def embed_texts(self, texts):
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
        self.last_prompt = None

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        self.last_prompt = messages[-1].content
        return AIMessage(content=json.dumps(self.response, ensure_ascii=False))


async def _router(llm=None, rules=None):
    skills = _skills()
    emb = _ClarifyEmbedder()
    idx = await SkillIndex(emb, cache_dir="", model_id="fake-clarify").build(skills)
    router = SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher(rules or []),
        llm=llm or _ScriptLLM({"category": "unknown", "confidence": 0.2}),
        embedder=emb,
        hybrid=False,
    )
    return router, skills


_PREV = {"categories": ["order", "refund"], "turn_id": "Tn", "options": []}


# ---------------- router 文本闭环语义 ----------------
class TestPrevClarifyNarrowing:
    async def test_answer_within_candidates_narrows_and_outcome_text(self):
        # 当前回答指向"退款" -> 子集内向量高置信 -> 只放退款工具，path 非 clarify
        router, skills = await _router()
        dec = await router.route("我要退款", skills, prev_clarify=dict(_PREV))
        assert dec.path == PATH_VECTOR
        assert [s.name for s in dec.tools] == ["refund_skill"]
        assert "invoice_skill" not in [s.name for s in dec.tools]
        assert dec.details["clarify_outcome"] == "text"

    async def test_llm_tier_success_within_subset_outcome_text(self):
        # 零向量低置信 -> LLM 在子集内选域 refund -> path=llm 收窄成功
        llm = _ScriptLLM({"category": "refund", "confidence": 0.9})
        router, skills = await _router(llm=llm)
        dec = await router.route("呃那个东西", skills, prev_clarify=dict(_PREV))
        assert dec.path == PATH_LLM
        assert [s.name for s in dec.tools] == ["refund_skill"]
        assert dec.details["clarify_outcome"] == "text"
        # LLM 裁决范围仅限子集：prompt 不含集外 billing/invoice
        assert "invoice_skill" not in llm.last_prompt
        assert "billing" not in llm.last_prompt

    async def test_still_unknown_repeats_clarify_with_history_categories(self):
        # 仍无法对应 -> 再次澄清，details 带历史候选方向
        router, skills = await _router(
            llm=_ScriptLLM({"category": "unknown", "confidence": 0.2, "clarify_question": "具体是哪件事？"})
        )
        dec = await router.route("随便吧", skills, prev_clarify=dict(_PREV))
        assert dec.path == PATH_CLARIFY
        assert dec.tools == []
        assert dec.details["clarify_outcome"] == "repeat"
        assert dec.details["clarify_history_categories"] == ["order", "refund"]
        # repeat 澄清 details["categories"] 至少含历史方向
        cats = dec.details["categories"]
        assert "order" in cats and "refund" in cats

    async def test_empty_subset_repeats_clarify_without_calling_llm(self):
        # 历史候选方向与当前全集交集为空 -> 直接 repeat 澄清，不调用 LLM/embedding 裁决
        llm = _ScriptLLM({"category": "refund", "confidence": 0.9})
        router, skills = await _router(llm=llm)
        prev = {"categories": ["nonexistent_dir"], "turn_id": "T9", "options": []}
        dec = await router.route("我要退款", skills, prev_clarify=prev)
        assert dec.path == PATH_CLARIFY
        assert dec.details["clarify_outcome"] == "repeat"
        assert dec.details["clarify_history_categories"] == ["nonexistent_dir"]
        assert dec.details["categories"] == ["nonexistent_dir"]
        assert llm.calls == 0

    async def test_chitchat_stays_chitchat_without_outcome(self):
        router, skills = await _router(
            llm=_ScriptLLM({"category": "chitchat", "confidence": 0.95})
        )
        dec = await router.route("哈哈谢谢", skills, prev_clarify=dict(_PREV))
        assert dec.path == PATH_CHITCHAT
        assert dec.tools == []
        assert "clarify_outcome" not in dec.details

    async def test_rule_escape_hatch_still_uses_full_candidates(self):
        # 规则路对当前消息原文 + 全量 candidates 生效，不受 prev_clarify 限制，且不打 outcome
        router, skills = await _router(
            rules=[RouteRule(pattern="发票", skills=["invoice_skill"])]
        )
        dec = await router.route("我要开发票", skills, prev_clarify=dict(_PREV))
        assert dec.path == PATH_RULE
        assert [s.name for s in dec.tools] == ["invoice_skill"]
        assert "clarify_outcome" not in dec.details

    async def test_no_prev_clarify_behavior_unchanged(self):
        router, skills = await _router(
            llm=_ScriptLLM({"category": "unknown", "confidence": 0.2})
        )
        dec = await router.route("随便吧", skills)
        assert dec.path == PATH_CLARIFY
        assert "clarify_outcome" not in dec.details
        assert "clarify_history_categories" not in dec.details


# ---------------- chat 层：历史加载 + prev_clarify 构造 ----------------
_SVC, _ENV, _USER = "default", "dev", "anonymous"
_SID = "default:dev:anonymous"


class _RecordingRouter:
    def __init__(self, decision):
        self.decision = decision
        self.last_history = object()
        self.last_prev = object()

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
        self.last_history = history
        self.last_prev = prev_clarify
        return self.decision


def _seed(store, turn_id, role, content, meta=None):
    # 同步测试内直接构造 FakeStore 行（TestClient 自带 portal，不能嵌套事件循环）
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


def _post(app):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "继续说"}) as r:
        text = "".join(chunk for chunk in r.iter_text())
    return parse_sse(text)


class TestChatPrevClarifyConstruction:
    def test_loads_recent_history_and_builds_prev_clarify(self):
        app, store, _ = build_test_app()
        # 3 行旧历史（被 limit=7 截掉）：t0 一对 + t1 user
        _seed(store, "t0", "user", "更早的问题")
        _seed(store, "t0", "assistant", "更早的回答")
        _seed(store, "t1", "user", "问题1")
        # 最近 7 行：t1 asst、t2 user/tool/asst、t3 user/asst、Tn 澄清 asst（其中 1 条 tool）
        _seed(store, "t1", "assistant", "回答1")
        _seed(store, "t2", "user", "问题2")
        _seed(store, "t2", "tool", "工具结果2")
        _seed(store, "t2", "assistant", "回答2")
        _seed(store, "t3", "user", "问题3")
        _seed(store, "t3", "assistant", "回答3")
        clarify_meta = {
            "kind": "clarify",
            "categories": ["order", "refund"],
            "options": [{"label": "退款", "value": "category:refund"}],
        }
        _seed(store, "Tn", "assistant", "你想查订单还是退款？", meta=clarify_meta)

        rec = _RecordingRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="再确认一下", details={"categories": ["order"]})
        )
        app.state.skill_router = rec
        _post(app)

        # context_turns=3 -> 载最近 7 行；升序；tool 行不入 history
        assert rec.last_history is not None
        assert len(rec.last_history) == 6  # 7 行中 1 条 tool 被跳过
        assert all(h["role"] in ("user", "assistant") for h in rec.last_history)
        assert rec.last_history[0]["content"] == "回答1"
        assert rec.last_history[-1] == {"role": "assistant", "content": "你想查订单还是退款？"}
        # 末条 assistant 为澄清行 -> prev_clarify 含 categories/options/turn_id
        assert rec.last_prev == {
            "categories": ["order", "refund"],
            "options": [{"label": "退款", "value": "category:refund"}],
            "turn_id": "Tn",
        }

    def test_load_limit_is_context_turns_times_two_plus_one(self):
        app, store, _ = build_test_app()
        _seed(store, "t1", "user", "你好")

        class _LimitStore(FakeStore):
            def __init__(self, inner):
                self._inner = inner
                self.load_limits = []

            async def load_messages(self, service, env, user_id, session_id, limit=None):
                self.load_limits.append(limit)
                return await self._inner.load_messages(service, env, user_id, session_id, limit=limit)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        limit_store = _LimitStore(store)
        app.state.message_store = limit_store
        rec = _RecordingRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="?", details={"categories": ["order"]})
        )
        app.state.skill_router = rec
        _post(app)
        # 路由前那次 load 的 limit=7（3*2+1）；runner 喂图的全量 load（limit=None）行为不变
        assert 7 in limit_store.load_limits
        assert limit_store.load_limits[0] == 7

    def test_last_assistant_without_clarify_meta_prev_is_none(self):
        app, store, _ = build_test_app()
        _seed(store, "t1", "user", "你好")
        _seed(store, "t1", "assistant", "你好，有什么可以帮你")  # meta=None
        rec = _RecordingRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="?", details={"categories": ["order"]})
        )
        app.state.skill_router = rec
        _post(app)
        assert rec.last_prev is None
        assert rec.last_history is not None and len(rec.last_history) == 2

    def test_earlier_clarify_ignored_when_last_assistant_is_normal(self):
        app, store, _ = build_test_app()
        _seed(store, "t1", "assistant", "请选择方向", meta={"kind": "clarify", "categories": ["order"], "options": []})
        _seed(store, "t2", "user", "查订单")
        _seed(store, "t2", "assistant", "订单已发货")  # 末条非澄清
        rec = _RecordingRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="?", details={"categories": ["order"]})
        )
        app.state.skill_router = rec
        _post(app)
        assert rec.last_prev is None

    def test_context_turns_zero_skips_history_load(self, monkeypatch):
        from general_agent.config import get_settings

        app, store, _ = build_test_app()
        _seed(store, "t1", "user", "你好")

        class _CountingStore(FakeStore):
            def __init__(self, inner):
                self._inner = inner
                self.load_limits = []

            async def load_messages(self, service, env, user_id, session_id, limit=None):
                self.load_limits.append(limit)
                return await self._inner.load_messages(
                    service, env, user_id, session_id, limit=limit
                )

            def __getattr__(self, name):
                return getattr(self._inner, name)

        counting = _CountingStore(store)
        app.state.message_store = counting

        monkeypatch.setattr(get_settings().routing, "context_turns", 0)
        rec = _RecordingRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="?", details={"categories": ["order"]})
        )
        app.state.skill_router = rec
        _post(app)
        assert rec.last_history is None  # 拼接历史不读
        assert rec.last_prev is None  # 末条是 user 行（非澄清 assistant 行）-> prev 仍 None
        # 路由前仅澄清行的收窄读取（limit=1，与 context_turns 解耦）；runner 全量 load（limit=500）
        assert counting.load_limits.count(1) == 1
        assert counting.load_limits[-1] == 500

    def test_context_turns_zero_still_builds_prev_clarify_from_last_row(self, monkeypatch):
        # 解耦：context_turns=0 只关跨轮拼接，澄清闭环（prev_clarify 构造）照常
        from general_agent.config import get_settings

        app, store, _ = build_test_app()
        clarify_meta = {
            "kind": "clarify",
            "categories": ["order", "refund"],
            "options": [{"label": "退款", "value": "category:refund"}],
        }
        _seed(store, "Tn", "assistant", "你想查订单还是退款？", meta=clarify_meta)

        class _LimitProbeStore(FakeStore):
            def __init__(self, inner):
                self._inner = inner
                self.load_limits = []

            async def load_messages(self, service, env, user_id, session_id, limit=None):
                self.load_limits.append(limit)
                return await self._inner.load_messages(
                    service, env, user_id, session_id, limit=limit
                )

            def __getattr__(self, name):
                return getattr(self._inner, name)

        probe = _LimitProbeStore(store)
        app.state.message_store = probe

        monkeypatch.setattr(get_settings().routing, "context_turns", 0)
        rec = _RecordingRouter(
            RouteDecision(PATH_CLARIFY, [], clarify_text="?", details={"categories": ["order"]})
        )
        app.state.skill_router = rec
        _post(app)

        assert rec.last_prev == {
            "categories": ["order", "refund"],
            "options": [{"label": "退款", "value": "category:refund"}],
            "turn_id": "Tn",
        }
        assert rec.last_history is None  # 拼接历史仍关闭
        assert 1 in probe.load_limits  # 澄清行以收窄读取（limit=1）获得


# ---------------- clarify_meta 落库链路 ----------------
class TestClarifyMetaPersistence:
    def test_clarify_turn_persists_meta_with_empty_options(self):
        app, store, _ = build_test_app()
        app.state.skill_router = _RecordingRouter(
            RouteDecision(
                PATH_CLARIFY,
                [],
                clarify_text="你想查订单还是退款？",
                details={"categories": ["order", "refund"]},
            )
        )
        _post(app)
        assistant_rows = [r for r in store.rows if r["role"] == "assistant"]
        user_rows = [r for r in store.rows if r["role"] == "user"]
        assert assistant_rows[-1]["meta"] == {
            "kind": "clarify",
            "categories": ["order", "refund"],
            "options": [],
        }
        assert user_rows[-1]["meta"] is None  # user 行不写 meta

    def test_handbuilt_prefixed_options_persisted_verbatim(self):
        # 手构带前缀 options 经 chat -> run_turn 落库，原样持久化（下一批结构化产出的管道前置）
        options = [{"label": "退款", "value": "category:refund"}]
        app, store, _ = build_test_app()
        app.state.skill_router = _RecordingRouter(
            RouteDecision(
                PATH_CLARIFY,
                [],
                clarify_text="请选择",
                details={"categories": ["refund"], "clarify_options": options},
            )
        )
        _post(app)
        meta = [r for r in store.rows if r["role"] == "assistant"][-1]["meta"]
        assert meta["options"] == options
        assert meta["options"][0]["value"] == "category:refund"

    def test_non_clarify_turn_persists_no_meta(self):
        from conftest import DemoSkill

        app, store, _ = build_test_app(skill=DemoSkill())
        app.state.skill_router = _RecordingRouter(
            RouteDecision(PATH_VECTOR, [DemoSkill()], details={"top1_score": 1.0})
        )
        _post(app)
        assistant_rows = [r for r in store.rows if r["role"] == "assistant"]
        assert assistant_rows and all(r["meta"] is None for r in assistant_rows)


async def test_run_turn_clarify_meta_persisted_directly():
    # 直连 run_turn：clarify_meta 写 assistant 行、user 行无 meta
    store = FakeStore()
    meta = {"kind": "clarify", "categories": ["refund"], "options": []}
    events_seen = []
    async for ev in run_turn(
        agent=None,
        message_store=store,
        service=_SVC,
        env=_ENV,
        user=_USER,
        session_id=_SID,
        turn_id="Tn",
        trace_id="tr1",
        user_message="我要退款",
        direct_reply="请补充订单号",
        clarify_meta=meta,
    ):
        events_seen.append(ev.get("event"))
    assert "turn_end" in events_seen
    rows = await store.load_messages(_SVC, _ENV, _USER, _SID)
    assert rows[0]["role"] == "user" and rows[0]["meta"] is None
    assert rows[1]["role"] == "assistant" and rows[1]["meta"] == meta
