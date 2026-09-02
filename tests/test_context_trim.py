"""上下文裁剪测试（B2 修复）：token 预算裁剪 + 保 tool 配对不孤儿。"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from general_agent.runner import _drop_unanswered_tool_calls, _est_tokens, _trim_history


def test_trim_keeps_all_within_budget():
    msgs = [HumanMessage(content="hello"), AIMessage(content="hi")]
    assert _trim_history(msgs, 1000) == msgs


def test_trim_drops_oldest_when_over_budget():
    # 每条约 3 token，4 条 12 -> 预算 8 丢最旧 1-2 条
    msgs = [
        HumanMessage(content="aaaaaaaa"),  # 3 token
        AIMessage(content="bbbbbbbb"),
        HumanMessage(content="cccccccc"),
        AIMessage(content="dddddddd"),
    ]
    trimmed = _trim_history(msgs, 8)
    assert trimmed[-1] is msgs[-1]
    assert len(trimmed) < len(msgs)


def test_trim_preserves_tool_pair_no_orphan_tool():
    # asst(tool_calls) + tool 若被裁到只剩 ToolMessage，应跳过该孤儿 ToolMessage
    msgs = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "f", "args": {}, "type": "tool_call"}]),
        ToolMessage(content="result", tool_call_id="c1"),
        AIMessage(content="final answer"),
    ]
    # 预算极小：要么不含 tool，要么含 tool 必有对应 tool_call
    trimmed = _trim_history(msgs, 5)
    assert not any(isinstance(m, ToolMessage) for m in trimmed) or _has_matching_call(trimmed)
    # 若含 ToolMessage，其前必有 asst(tool_calls)
    for i, m in enumerate(trimmed):
        if isinstance(m, ToolMessage):
            assert any(
                isinstance(trimmed[j], AIMessage) and trimmed[j].tool_calls for j in range(i)
            )


def test_trim_full_pair_kept_when_budget_allows():
    msgs = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "f", "args": {}, "type": "tool_call"}]),
        ToolMessage(content="result", tool_call_id="c1"),
        AIMessage(content="final"),
    ]
    trimmed = _trim_history(msgs, 1000)
    assert trimmed == msgs  # 预算充足全保留


def test_trim_empty():
    assert _trim_history([], 100) == []


def test_est_tokens_includes_tool_args():
    m = AIMessage(content="x", tool_calls=[{"id": "c1", "name": "f", "args": {"a": "bbbbbbb"}, "type": "tool_call"}])
    assert _est_tokens(m) > _est_tokens(AIMessage(content="x"))


def _has_matching_call(trimmed):
    ids = {tc["id"] for m in trimmed if isinstance(m, AIMessage) and m.tool_calls for tc in m.tool_calls}
    return all(isinstance(m, ToolMessage) and m.tool_call_id in ids for m in trimmed if isinstance(m, ToolMessage))


def test_drop_unanswered_tool_calls_max_rounds():
    # max_tool_rounds 场景：末尾 AIMessage(tool_calls) 无对应 ToolMessage，应被剔除
    msgs = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "f", "args": {}, "type": "tool_call"}]),
        ToolMessage(content="r1", tool_call_id="c1"),
        AIMessage(content="", tool_calls=[{"id": "c2", "name": "f", "args": {}, "type": "tool_call"}]),
    ]
    out = _drop_unanswered_tool_calls(msgs)
    # c2 无 ToolMessage -> 该条空 AIMessage 被丢弃；c1 配对保留
    assert len(out) == 3
    assert not any(
        isinstance(m, AIMessage)
        and m.tool_calls
        and any(tc.get("id") == "c2" for tc in m.tool_calls)
        for m in out
    )


def test_drop_unanswered_keeps_answered_pair():
    msgs = [
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "f", "args": {}, "type": "tool_call"}]),
        ToolMessage(content="r1", tool_call_id="c1"),
        AIMessage(content="最终回答"),
    ]
    out = _drop_unanswered_tool_calls(msgs)
    assert out == msgs


def test_drop_unanswered_strips_only_orphan_call():
    # 同一 AIMessage 含两个 tool_call，仅一个有 ToolMessage：剔除孤儿、保留配对
    msgs = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c1", "name": "f", "args": {}, "type": "tool_call"},
                {"id": "c2", "name": "g", "args": {}, "type": "tool_call"},
            ],
        ),
        ToolMessage(content="r1", tool_call_id="c1"),
    ]
    out = _drop_unanswered_tool_calls(msgs)
    ai = [m for m in out if isinstance(m, AIMessage)]
    assert len(ai) == 1
    assert [tc["id"] for tc in ai[0].tool_calls] == ["c1"]
