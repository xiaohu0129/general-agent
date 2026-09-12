"""11.1 端到端（真实 SkillRouter 全链路 + stub chat + 确定性 fake embedding + 多域 DemoSkill）。

覆盖清单（tasks.md 11.1）：
① 指代消解经改写命中（FakeLLM 改写 + 语义 embedder）
② 澄清闭环不重复反问（prev_clarify 文本闭环）
③ 澄清产出 options 并下发 clarify 事件（SSE）
④ 选项回传确定性收窄（断言未走向量/LLM）
⑤ 非法 value 回落文本闭环
⑥ 断线重放与历史回放含 options
⑦ 缺参两轮追问补齐（在 test_health_and_serialization 已覆盖，此处省）
⑧ 多用法 Skill 多向量召回
⑨ 收窄路径误杀上报（真实路由）
⑩ 含订单号/型号消息经 BM25 关键词路命中
⑪ stub 无语义模式下 BM25 收窄且不发生向量伪高置信
⑫ BM25 降级轮 LLM 兜底成功记 path=llm 且误杀以 retrieval=keyword 上报
"""
from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage

from conftest import build_test_app, make_llm_transport, parse_sse
from general_agent.skill_router.index import SkillIndex
from general_agent.skill_router.keyword import KeywordIndex
from general_agent.skill_router.router import SkillRouter
from general_agent.skill_router.rules import RuleMatcher
from general_agent.skills import Skill, SkillContext


# ---------------- 多域 DemoSkill ----------------
class _OrderSkill(Skill):
    name = "e_order_skill"
    description = "查询订单的物流状态"
    category = "order"
    examples = ["查一下我的订单到哪了", "订单 123 发货了吗"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": "order"}


class _RefundSkill(Skill):
    name = "e_refund_skill"
    description = "办理退款退货"
    category = "refund"
    examples = ["这个东西想退款", "申请退货退款"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": "refund"}


class _InvoiceSkill(Skill):
    name = "e_invoice_skill"
    description = "开具发票"
    category = "invoice"
    examples = ["帮我开发票", "开一张增值税发票"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": "invoice"}


class _SkuSkill(Skill):
    name = "e_sku_skill"
    description = "查询 SKU 型号库存"
    category = "sku"
    examples = ["查 SKU-8800 的库存", "这个型号还有货吗"]

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": "sku"}


_DOMAINS = [_OrderSkill, _RefundSkill, _InvoiceSkill, _SkuSkill]


class _SemanticEmbedder:
    """确定性语义向量：按域关键词命中维度（订单/退款/发票/SKU 型号各一维）。"""

    DIMS = [
        ("订单", 0), ("order", 0),
        ("退款", 1), ("退货", 1), ("refund", 1),
        ("发票", 2), ("invoice", 2),
        ("sku", 3), ("型号", 3),
    ]

    def __init__(self):
        self.calls = 0

    async def embed_texts(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0, 0.0, 0.0, 0.0]
            for kw, dim in self.DIMS:
                if kw in t.lower():
                    v[dim] = 1.0
            out.append(v)
        return out


class _RewriteLLM:
    """改写 LLM：把指代消息结合历史改写为语义独立的退款查询。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=json.dumps({"query": "申请退款"}, ensure_ascii=False))


class _RefundLLM:
    """兜底 LLM：低置信 unknown -> 澄清（候选 category 给 order/refund）。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=json.dumps(
            {"category": "unknown", "confidence": 0.1, "categories": ["order", "refund"]},
            ensure_ascii=False,
        ))


class _OrderLLM:
    """兜底 LLM：高置信点名 order 域（BM25 降级轮用）。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage(content=json.dumps(
            {"category": "order", "confidence": 0.9, "skills": ["e_order_skill"]},
            ensure_ascii=False,
        ))


async def _build_router(embedder, llm, *, skills=None, semantic=True, hybrid=True, **kw):
    skills = skills or [C() for C in _DOMAINS]
    idx = SkillIndex(embedder, cache_dir="", model_id="e2e-embed")
    await idx.build(skills)
    return SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=llm,
        embedder=embedder,
        keyword_index=KeywordIndex().build(skills) if hybrid else None,
        hybrid=hybrid,
        semantic=semantic,
        **kw,
    )


def _mount(app, router):
    app.state.skill_router = router


def _post(app, message, **extra):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": message, **extra}) as r:
        return parse_sse("".join(chunk for chunk in r.iter_text()))


@pytest.mark.asyncio
async def test_e2e_referential_rewrite_hits_refund():
    """① 指代消解：'那它的退款呢' 经改写（订单退款）后命中退款 Skill，跳过路由 LLM。"""
    emb = _SemanticEmbedder()
    rewrite = _RewriteLLM()
    fallback = _OrderLLM()
    router = await _build_router(emb, rewrite)
    # 用同一 LLM 会先改写后（若走到）兜底；此处断言改写后高置信直接 vector，不进兜底
    dec = await router.route(
        "那它的退款呢",
        [C() for C in _DOMAINS],
        history=[{"role": "user", "content": "查订单 123 的状态"}],
    )
    assert dec.path == "vector"
    assert [s.name for s in dec.tools] == ["e_refund_skill"]
    assert dec.details.get("query_rewritten") is True
    assert rewrite.calls == 1


@pytest.mark.asyncio
async def test_e2e_clarify_loop_no_repeat_ask():
    """② 澄清闭环：上轮澄清 order/refund，本轮回答指向退款 -> 在候选内收窄、不再澄清。"""
    emb = _SemanticEmbedder()
    router = await _build_router(emb, _RefundLLM())
    candidates = [C() for C in _DOMAINS]
    prev = {"categories": ["order", "refund"], "options": [], "turn_id": "t-clarify"}
    dec = await router.route("退款", candidates, prev_clarify=prev)
    assert dec.path != "clarify"
    assert {s.category for s in dec.tools} <= {"refund"}
    assert dec.details.get("clarify_outcome") == "text"


@pytest.mark.asyncio
async def test_e2e_clarify_options_and_selection_narrowing():
    """③④ 澄清产出 options（带前缀、候选内）+ 点选回传确定性收窄（0 向量/0 LLM）。"""
    emb = _SemanticEmbedder()
    router = await _build_router(emb, _RefundLLM())
    candidates = [C() for C in _DOMAINS]

    first = await router.route("帮我处理一下", candidates)
    assert first.path == "clarify"
    assert first.clarify_options, "澄清必须产出结构化选项"
    values = {o["value"] for o in first.clarify_options}
    assert all(v.startswith(("category:", "skill:")) for v in values)
    assert values <= {
        "category:order", "category:refund", "category:invoice", "category:sku",
        "skill:e_order_skill", "skill:e_refund_skill", "skill:e_invoice_skill", "skill:e_sku_skill",
    }

    # 点选退款域选项 -> path=option，跳过向量与 LLM
    emb2 = _SemanticEmbedder()
    llm2 = _RefundLLM()
    router2 = await _build_router(emb2, llm2)
    baseline = emb2.calls  # 索引构建期 embed 计数为基线，选项收窄轮 MUST NOT 增量
    prev = {"categories": ["order", "refund"], "options": first.clarify_options, "turn_id": "t-1"}
    dec = await router2.route(
        "退款",
        candidates,
        prev_clarify=prev,
        selection="category:refund",
    )
    assert dec.path == "option"
    assert {s.category for s in dec.tools} == {"refund"}
    assert dec.details.get("clarify_outcome") == "option"
    # ④ 关键断言：选项收窄跳过向量检索（embed 无增量）与路由 LLM（0 次调用）
    assert emb2.calls == baseline
    assert llm2.calls == 0


@pytest.mark.asyncio
async def test_e2e_invalid_selection_falls_back_to_text_loop():
    """⑤ 非法 value（不在 options）-> 忽略标记，回落文本闭环裁决。"""
    emb = _SemanticEmbedder()
    router = await _build_router(emb, _RefundLLM())
    candidates = [C() for C in _DOMAINS]
    prev = {"categories": ["order", "refund"], "options": [{"label": "退款", "value": "category:refund"}], "turn_id": "t-1"}
    dec = await router.route(
        "退款",
        candidates,
        prev_clarify=prev,
        selection="category:invoice",  # 不在 options 内
    )
    assert dec.path != "option"
    assert dec.details.get("clarify_selection_ignored") is True
    # 文本闭环在候选方向内裁决成功
    assert dec.details.get("clarify_outcome") == "text"


@pytest.mark.asyncio
async def test_e2e_bm25_exact_symbol_hit():
    """⑩ 含型号（SKU-8800）消息经 BM25 关键词路命中（向量 OOV 场景）。"""
    emb = _SemanticEmbedder()
    router = await _build_router(emb, _RefundLLM())
    candidates = [C() for C in _DOMAINS]
    dec = await router.route("SKU-8800 还有货吗", candidates)
    # 向量路命中 sku 维（"sku" 关键词），高置信直接收窄；BM25 亦应命中同域
    assert "e_sku_skill" in {s.name for s in dec.tools}
    bm25_names = {item["skill"] for item in dec.details.get("bm25_k", [])}
    assert "e_sku_skill" in bm25_names


@pytest.mark.asyncio
async def test_e2e_stub_mode_bm25_no_fake_high_confidence():
    """⑪ semantic=false（stub）：不发生向量伪高置信，BM25 收窄 -> LLM 兜底。"""
    emb = _SemanticEmbedder()
    llm = _OrderLLM()
    router = await _build_router(emb, llm, semantic=False)
    candidates = [C() for C in _DOMAINS]
    dec = await router.route("订单 123 发货了吗", candidates)
    assert dec.details.get("semantic_off") is True
    assert dec.details.get("degraded_keyword") is True
    # BM25 收窄后 LLM 兜底成功点名 -> path=llm（非 degraded）
    assert dec.path == "llm"
    assert "e_order_skill" in {s.name for s in dec.tools}
    # 向量路被跳过：embed 未被调用（构建后 query 不 embed）
    # （SkillIndex 构建用了 emb；semantic=False 下 route 不调 embed_texts——以 path/details 为准）


@pytest.mark.asyncio
async def test_e2e_bm25_degraded_miss_reported_with_keyword_retrieval(monkeypatch):
    """⑫ BM25 降级轮 LLM 兜底成功（path=llm）+ 模型调用集外工具 -> 误杀 retrieval=keyword。"""
    import general_agent.observability as obs

    emb = _SemanticEmbedder()
    llm = _OrderLLM()
    router = await _build_router(emb, llm, semantic=False)

    app, store, _ = build_test_app(
        skill=_OrderSkill(),
        llm_transport=make_llm_transport(tool_name="e_invoice_skill", args={"query": "x"}),
    )
    _mount(app, router)

    calls = []
    monkeypatch.setattr(obs, "record_intent_miss", lambda p, **k: calls.append((p, k)))
    evs = _post(app, "订单 123 发货了吗")
    assert any(e[0] == "turn_end" for e in evs)
    # LLM 兜底成功点名 order（path=llm），模型越界调 invoice -> 误杀照计、retrieval=keyword
    assert calls and calls[0][0] == "llm" and calls[0][1]["retrieval"] == "keyword"


@pytest.mark.asyncio
async def test_e2e_multi_usage_skill_multivector_recall():
    """⑧ 多用法 Skill：两条语义迥异 example，只匹配其一仍高分召回。"""
    class _MultiSkill(Skill):
        name = "e_multi_skill"
        description = "多功能助手"
        category = "misc"
        examples = ["做阿尔法相关的事", "查贝塔数据"]

        async def run(self, ctx: SkillContext, **kwargs):
            return {"ok": True}

    class _AlphaEmbedder:
        async def embed_texts(self, texts):
            return [[1.0 if "阿尔法" in t else 0.0] for t in texts]

    emb = _AlphaEmbedder()
    skills = [_MultiSkill(), _RefundSkill()]
    idx = SkillIndex(emb, cache_dir="", model_id="alpha")
    await idx.build(skills)
    router = SkillRouter(
        index=idx,
        rule_matcher=RuleMatcher([]),
        llm=_RefundLLM(),
        embedder=emb,
        keyword_index=None,
        hybrid=False,
    )
    dec = await router.route("帮我做阿尔法任务", skills)
    assert dec.path == "vector"
    assert "e_multi_skill" in {s.name for s in dec.tools}


@pytest.mark.asyncio
async def test_e2e_clarify_sse_and_history_replay_with_options():
    """③⑥ SSE：澄清轮下发 clarify 事件（真实路由器产 options）+ 历史回放透出 options。"""
    emb = _SemanticEmbedder()
    router = await _build_router(emb, _RefundLLM())
    app, store, _ = build_test_app(skill=_OrderSkill())
    _mount(app, router)

    evs = _post(app, "帮我处理一下")
    clarify_events = [d for e, d, _ in evs if e == "clarify"]
    assert clarify_events, "澄清轮必须下发 clarify 事件"
    opts = clarify_events[0]["options"]
    assert opts and all(o["value"].startswith(("category:", "skill:")) for o in opts)
    assert not any(e[0] in ("tool_start", "tool_end") for e in evs)
    assert evs[-1][0] == "turn_end" and evs[-1][1]["finishReason"] == "stop"

    # 历史回放：澄清 assistant 行 meta 透出 options（FakeStore DTO）
    msgs = [
        m for m in (await store.load_web_messages("default", "dev", "anonymous", "default:dev:anonymous"))["messages"]
        if m["role"] == "assistant"
    ]
    assert msgs and msgs[-1]["options"] == opts


@pytest.mark.asyncio
async def test_e2e_selection_writeback_and_value_not_in_content():
    """④ 补：真实 HTTP 点选 -> label 落库、value 不进任何 content、meta.selected 回写。"""
    emb = _SemanticEmbedder()
    router = await _build_router(emb, _RefundLLM())
    app, store, _ = build_test_app(
        skill=_RefundSkill(),
        llm_transport=make_llm_transport(tool_name="e_refund_skill", args={"query": "x"}),
    )
    _mount(app, router)

    # 第一轮：澄清落库（带 options）
    _post(app, "帮我处理一下")
    rows = store.rows
    clarify_row = next(r for r in rows if r["role"] == "assistant" and (r.get("meta") or {}).get("kind") == "clarify")
    opt_refund = next(o for o in clarify_row["meta"]["options"] if "refund" in o["value"])
    assert opt_refund["value"].startswith(("category:", "skill:"))

    # 第二轮：点选（message=label, clarify_selection=value）
    evs = _post(app, opt_refund["label"], clarify_selection={"value": opt_refund["value"]})
    assert any(e[0] == "tool_end" for e in evs)  # 收窄后模型调用退款工具
    # label 作为 user content 落库；带前缀 value 不出现在任何 content
    assert any(r["role"] == "user" and r["content"] == opt_refund["label"] for r in store.rows)
    assert all(opt_refund["value"] not in (r["content"] or "") for r in store.rows)
    # 上轮澄清行 meta.selected 被回写
    assert clarify_row["meta"].get("selected") == opt_refund["value"]
    assert clarify_row["meta"].get("options")  # 未被覆盖
