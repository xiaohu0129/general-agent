"""Agent 运行核心：载入历史 -> 组装消息 -> 调用图 -> 流式事件 -> 持久化。

C2：无状态图（无 checkpointer），每轮从 MySQL 载入历史（限流+裁剪）。
max_tool_rounds 映射为 recursion_limit，GraphRecursionError -> turn_end{max_tool_rounds}；
上下文治理：B2 修复，按 token 预算裁剪，保留 tool_call/tool_message 配对。
M8：run_turn/load_history/agent_graph/append_messages 各建 span，turn.duration metric。
M5：工具异常不杀轮次——ToolNode handle_tool_errors 将异常转为 status=error 的 ToolMessage 回给 LLM。
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from . import events, observability
from .config import get_settings
from .logging_setup import get_logger
from .message_store import MessageStore

logger = get_logger(__name__)

_LC_TO_ROLE = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}
# DB 行数硬上限，防止极端历史一次性载入过多；token 裁剪在 Python 侧做
_HISTORY_ROW_LIMIT = 500


def _row_to_message(row: dict) -> BaseMessage:
    role = row["role"]
    content = row["content"] or ""
    if role == "assistant":
        tcs = row.get("tool_calls") or []
        tool_calls = [
            {
                "id": t.get("id", ""),
                "name": t["name"],
                "args": json.loads(t["arguments"]) if isinstance(t.get("arguments"), str) else (t.get("arguments") or {}),
                "type": "tool_call",
            }
            for t in tcs
        ]
        return AIMessage(content=content, tool_calls=tool_calls)
    if role == "tool":
        return ToolMessage(content=content, tool_call_id=row.get("tool_call_id") or "")
    return HumanMessage(content=content)


def _message_to_row(m: BaseMessage) -> dict:
    row: dict[str, Any] = {
        "role": _LC_TO_ROLE.get(m.type, m.type),
        "content": m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False),
    }
    if isinstance(m, AIMessage) and m.tool_calls:
        row["tool_calls"] = [
            {"id": tc["id"], "name": tc["name"], "arguments": json.dumps(tc["args"], ensure_ascii=False)}
            for tc in m.tool_calls
        ]
    if isinstance(m, ToolMessage):
        row["tool_call_id"] = m.tool_call_id
    return row


def _est_tokens(m: BaseMessage) -> int:
    """估算 token：len(content)//4（约 4 char/token，CJK 偏高估），含 tool_calls args。"""
    c = m.content
    s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    if isinstance(m, AIMessage) and m.tool_calls:
        for tc in m.tool_calls:
            s += json.dumps(tc.get("args", {}), ensure_ascii=False)
    return len(s) // 4 + 1


def _maybe_json(value):
    """ToolMessage.content 若为 JSON 字符串则解析为 dict，供 SSE 结构化 result；否则原样返回。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _drop_unanswered_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """剔除无对应 ToolMessage 的 tool_calls（孤儿工具调用）。

    触发场景：达到 max_tool_rounds 时 GraphRecursionError 在最后一次工具执行前抛出，
    此时已捕获一条 AIMessage(tool_calls=[...]) 但没有后续 ToolMessage；若落库，下次载入
    历史会把"assistant tool_calls 后直接跟 human"发给 OpenAI，触发 400 且会话永久不可用。
    处理：仅保留有匹配 ToolMessage 的 tool_call；清洗后既无内容也无工具调用的 AIMessage 丢弃。
    """
    answered_ids = {
        m.tool_call_id for m in messages if isinstance(m, ToolMessage) and m.tool_call_id
    }
    out: list[BaseMessage] = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            kept = [tc for tc in m.tool_calls if tc.get("id") in answered_ids]
            if not kept and not (m.content and str(m.content).strip()):
                continue
            if len(kept) != len(m.tool_calls):
                m = AIMessage(content=m.content, tool_calls=kept)
        out.append(m)
    return out


def _trim_history(messages: list[BaseMessage], max_tokens: int) -> list[BaseMessage]:
    """按 token 预算保留最近消息，并避免 tool_call/tool_message 孤儿（保留配对，丢弃孤立的 ToolMessage）。

    自 best_cut 起若首条是 ToolMessage（其前导 assistant tool_call 已被裁掉），则向后跳过，
    避免把无对应 tool_call 的 ToolMessage 喂给 LLM 导致工具协议错乱。
    """
    if not messages:
        return messages
    total = 0
    best_cut = len(messages)
    i = len(messages) - 1
    while i >= 0:
        t = _est_tokens(messages[i])
        if total + t > max_tokens:
            break
        total += t
        best_cut = i
        i -= 1
    # 跳过开头的孤儿 ToolMessage（前导 AIMessage(tool_calls) 已被裁剪）
    while best_cut < len(messages) and isinstance(messages[best_cut], ToolMessage):
        best_cut += 1
    return messages[best_cut:]


async def run_turn(
    *,
    agent,
    message_store: MessageStore,
    service: str,
    env: str,
    user: str,
    session_id: str,
    turn_id: str,
    trace_id: str,
    user_message: str,
    max_tool_rounds: int = 8,
) -> AsyncIterator[dict]:
    tracer = observability.get_tracer()
    start = time.monotonic()
    observability.bind_request_context(env=env, user=user, session_id=session_id, turn_id=turn_id)

    with tracer.start_as_current_span("run_turn") as turn_span:
        turn_span.set_attribute("turn_id", turn_id)
        turn_span.set_attribute("session_id", session_id)
        turn_span.set_attribute("env", env)
        turn_span.set_attribute("user", user)

        # turn_start 先于任何可能失败的 I/O（load_history）：前端立即开气泡，
        # 即便后续历史加载/持久化失败也能收到 turn_start -> error 的完整语义。
        yield events.turn_start(turn_id, trace_id, session_id)

        with tracer.start_as_current_span("load_history"):
            rows = await message_store.load_messages(
                service, env, user, session_id, limit=_HISTORY_ROW_LIMIT
            )
        history = [_row_to_message(r) for r in rows]
        # 剔除上一轮因 max_tool_rounds 等原因未落 ToolMessage 的孤儿 tool_calls（否则 OpenAI 400）
        history = _drop_unanswered_tool_calls(history)
        history = _trim_history(history, get_settings().agent.max_context_tokens)
        await message_store.append_message(service, env, user, session_id, turn_id, "user", user_message)
        history.append(HumanMessage(content=user_message))

        recursion_limit = max_tool_rounds * 2 + 1  # 每轮约 2 步（agent+tools）
        new_messages: list[BaseMessage] = []
        persisted_tool_ids: set[str] = set()
        finish = events.FINISH_REASON_STOP
        try:
            with tracer.start_as_current_span("agent_graph"):
                async for ev in agent.astream_events(
                    {"messages": history}, version="v2", config={"recursion_limit": recursion_limit}
                ):
                    name = ev.get("event", "")
                    data = ev.get("data", {}) or {}
                    if name == "on_chat_model_stream":
                        chunk = data.get("chunk")
                        text = getattr(chunk, "content", "") if chunk is not None else ""
                        if isinstance(text, str) and text:
                            yield events.turn_delta(turn_id, trace_id, text)
                    elif name == "on_chat_model_end":
                        out = data.get("output")
                        if isinstance(out, AIMessage):
                            new_messages.append(out)
                    elif name == "on_tool_start":
                        inp = data.get("input")
                        args = inp if isinstance(inp, dict) else ({"input": str(inp)} if inp is not None else {})
                        yield events.tool_start(turn_id, trace_id, ev.get("run_id", ""), ev.get("name", ""), args)
                    elif name == "on_tool_end":
                        out = data.get("output")
                        result = _maybe_json(getattr(out, "content", out)) if out is not None else None
                        yield events.tool_end(turn_id, trace_id, ev.get("run_id", ""), "success", result=result)
                        if isinstance(out, ToolMessage) and out.tool_call_id not in persisted_tool_ids:
                            persisted_tool_ids.add(out.tool_call_id)
                            new_messages.append(out)
                    elif name == "on_tool_error":
                        err = data.get("error")
                        code = getattr(err, "code", None)
                        yield events.tool_end(
                            turn_id, trace_id, ev.get("run_id", ""), "error",
                            error=str(err) or "tool error", error_code=code,
                        )
                    elif name == "on_chain_end" and ev.get("name") == "tools":
                        # 兜底：某些 ToolMessage 仅在 on_chain_end(tools) 出现，补齐/去重
                        out = data.get("output")
                        msgs = out.get("messages") if isinstance(out, dict) else None
                        if isinstance(msgs, list):
                            for m in msgs:
                                if isinstance(m, ToolMessage) and m.tool_call_id not in persisted_tool_ids:
                                    persisted_tool_ids.add(m.tool_call_id)
                                    new_messages.append(m)
        except GraphRecursionError:
            finish = events.FINISH_REASON_MAX_TOOL_ROUNDS
            logger.warning(
                "max_tool_rounds_reached", sessionId=session_id, turnId=turn_id, max_tool_rounds=max_tool_rounds
            )

        with tracer.start_as_current_span("append_messages"):
            # 持久化前清洗：max_tool_rounds 中断可能留下未执行的 tool_calls（无 ToolMessage），
            # 不落库以免污染历史导致后续轮次 OpenAI 400
            for m in _drop_unanswered_tool_calls(new_messages):
                row = _message_to_row(m)
                await message_store.append_message(
                    service,
                    env,
                    user,
                    session_id,
                    turn_id,
                    row["role"],
                    row["content"],
                    tool_calls=row.get("tool_calls"),
                    tool_call_id=row.get("tool_call_id"),
                )

        observability.record_turn((time.monotonic() - start) * 1000, finish)
        yield events.turn_end(turn_id, trace_id, finish)
