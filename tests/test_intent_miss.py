"""误杀比对（7.1/7.2）：收窄路径下模型实际调用推荐集外工具 -> 计误杀。

事实基础（探针核实）：runner 只把 decision.tools 绑定进 ReAct 图，推荐集外工具
调用是未绑定 tool_call——不产生 on_tool_start/on_tool_end，只在 on_chat_model_end
的 AIMessage.tool_calls 里可见（LangGraph tools 节点产出 invalid_tool 错误
ToolMessage）。因此误杀检测挂 on_chat_model_end 的 tool_calls 名比对。
纯内存/fake meter，不依赖外部服务。
"""
from __future__ import annotations

import json

from conftest import build_test_app, make_llm_transport, parse_sse
from general_agent.skill_router.router import (
    PATH_DEGRADED,
    PATH_FALLBACK,
    PATH_LLM,
    PATH_OPTION,
    PATH_VECTOR,
    RouteDecision,
)
from general_agent.skills import Skill, SkillContext


# ---------------- 测试域 Skill ----------------
class _OrderSkill(Skill):
    name = "order_skill"
    description = "查询订单物流状态"
    category = "order"

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _RefundSkill(Skill):
    name = "refund_skill"
    description = "办理退款退货"
    category = "refund"

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _TicketSkill(Skill):
    name = "ticket_skill"
    description = "创建查询工单"
    category = "ticket"

    async def run(self, ctx: SkillContext, **kwargs):
        return {"ok": True}


class _FakeRouter:
    def __init__(self, decision):
        self.decision = decision

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
        return self.decision


class _MissSpy:
    """记录 record_intent_miss 调用参数（monkeypatch observability 模块级函数）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, path, *, retrieval=None):
        self.calls.append((path, retrieval))


def _make_transport(tool_name: str):
    # 模型第一轮点名工具（无论推荐集是否包含），工具结果回喂后给终答
    return make_llm_transport(tool_name=tool_name, args={"query": "do"})


def _post(app):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    with client.stream("POST", "/chat", json={"message": "帮我处理"}) as r:
        text = "".join(chunk for chunk in r.iter_text())
    return parse_sse(text)


def _run_with_miss_spy(app, monkeypatch, decision, skill):
    """装配 fake router + miss spy，跑一轮 chat，返回 (events, spy.calls)。"""
    import general_agent.observability as obs

    spy = _MissSpy()
    monkeypatch.setattr(obs, "record_intent_miss", spy)
    app.state.skill_router = _FakeRouter(decision)
    evs = _post(app)
    return evs, spy.calls


class TestIntentMiss:
    def test_vector_path_out_of_set_call_counts_miss(self, monkeypatch):
        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=_make_transport("ticket_skill"))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(PATH_VECTOR, [_OrderSkill()], details={"top1_score": 0.9}),
            _OrderSkill(),
        )
        assert any(e[0] == "turn_end" for e in evs)
        assert calls == [("vector", "vector")]

    def test_option_path_out_of_set_call_counts_miss(self, monkeypatch):
        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=_make_transport("ghost_tool"))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(
                PATH_OPTION,
                [_RefundSkill()],
                details={"clarify_outcome": "option", "clarify_selection": "category:refund"},
            ),
            _RefundSkill(),
        )
        assert any(e[0] == "turn_end" for e in evs)
        assert calls == [("option", "vector")]

    def test_in_set_call_no_miss(self, monkeypatch):
        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=_make_transport("order_skill"))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(PATH_VECTOR, [_OrderSkill(), _RefundSkill()], details={}),
            _OrderSkill(),
        )
        assert any(e[0] == "tool_end" for e in evs)
        assert calls == []

    def test_degraded_full_path_no_miss(self, monkeypatch):
        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=_make_transport("ticket_skill"))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(PATH_DEGRADED, [_OrderSkill()], details={"semantic_off": True}),
            _OrderSkill(),
        )
        assert calls == []

    def test_fallback_full_path_no_miss(self, monkeypatch):
        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=_make_transport("ticket_skill"))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(PATH_FALLBACK, [_OrderSkill()], details={"reason": "route_llm_failed"}),
            _OrderSkill(),
        )
        assert calls == []

    def test_bm25_degraded_llm_narrow_counts_miss_with_keyword_retrieval(self, monkeypatch):
        # BM25 收窄 -> LLM 兜底成功（path=llm）轮次，模型调用集外工具照计误杀，retrieval=keyword
        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=_make_transport("ticket_skill"))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(
                PATH_LLM,
                [_OrderSkill(), _RefundSkill()],
                details={"degraded_keyword": True, "semantic_off": True, "llm_category": "order"},
            ),
            _OrderSkill(),
        )
        assert any(e[0] == "turn_end" for e in evs)
        assert calls == [("llm", "keyword")]

    def test_no_tool_call_no_miss(self, monkeypatch):
        # 徒手作答（不调工具）-> 不上报
        import httpx

        def handler(request):
            payload = (
                'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{"content":"我直接回答。"}}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                "data: [DONE]\n\n"
            ).encode()
            return httpx.Response(200, stream=httpx.ByteStream(payload))

        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=httpx.MockTransport(handler))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(PATH_VECTOR, [_OrderSkill()], details={}),
            _OrderSkill(),
        )
        assert not any(e[0] == "tool_start" for e in evs)
        assert any(e[0] == "turn_end" for e in evs)
        assert calls == []

    def test_multiple_out_of_set_calls_count_each(self, monkeypatch):
        # 同一轮 AIMessage 内多次越界调用（含同名）各计一次
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            last = body["messages"][-1] if body.get("messages") else {}

            def gen():
                base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": 1, "model": "stub"}
                yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n".encode()
                if last.get("role") == "tool":
                    done = f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'content': 'done'}, 'finish_reason': None}]})}\n\n".encode()
                    yield done
                    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n".encode()
                else:
                    for i, nm in enumerate(["ticket_skill", "ticket_skill", "ghost_tool"]):
                        tc = {"index": 0, "id": f"call_{i}", "type": "function", "function": {"name": nm, "arguments": "{}"}}
                        yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'tool_calls': [tc]}, 'finish_reason': None}]})}\n\n".encode()
                    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n".encode()
                yield f"data: {json.dumps({**base, 'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}})}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return httpx.Response(200, stream=httpx.ByteStream(b"".join(gen())))

        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=httpx.MockTransport(handler))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(PATH_VECTOR, [_OrderSkill()], details={}),
            _OrderSkill(),
        )
        assert len(calls) == 3
        assert all(c == ("vector", "vector") for c in calls)

    def test_turn_span_annotates_missed_tool(self, monkeypatch):
        # span 标注 route.missed_tool（InMemory exporter 断言）
        # 注意：不 set 全局 TracerProvider（OTel 禁止二次覆盖，会污染其他 span 测试），
        # 改为 monkeypatch observability.get_tracer 指向本地 provider
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        import general_agent.observability as obs

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(obs, "get_tracer", lambda: provider.get_tracer("general.agent"))

        app, store, _ = build_test_app(skill=_OrderSkill(), llm_transport=_make_transport("ticket_skill"))
        evs, calls = _run_with_miss_spy(
            app,
            monkeypatch,
            RouteDecision(PATH_VECTOR, [_OrderSkill()], details={}),
            _OrderSkill(),
        )
        assert calls == [("vector", "vector")]
        turn_spans = [s for s in exporter.get_finished_spans() if s.name == "run_turn"]
        assert turn_spans
        attrs = turn_spans[-1].attributes
        assert attrs.get("route.missed_tool") == "ticket_skill"
