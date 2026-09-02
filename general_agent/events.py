"""面向 UI 的 SSE 事件构造。

事件经 sse-starlette 直接 yield 为 dict（{"event":..,"data":..}）。
M2/M4：turn_* / tool_*；M7：notification / with_seq / eventSeq 续传 / heartbeat。
"""
from __future__ import annotations

import json


FINISH_REASON_STOP = "stop"
FINISH_REASON_MAX_TOOL_ROUNDS = "max_tool_rounds"


def _sse(event: str, **fields) -> dict:
    return {"event": event, "data": json.dumps(fields, ensure_ascii=False)}


def turn_start(turn_id: str, trace_id: str, session_id: str | None = None) -> dict:
    fields = {"turnId": turn_id, "traceId": trace_id}
    if session_id is not None:
        fields["sessionId"] = session_id
    return _sse("turn_start", **fields)


def turn_delta(turn_id: str, trace_id: str, content: str) -> dict:
    return _sse("turn_delta", turnId=turn_id, traceId=trace_id, content=content)


def turn_end(turn_id: str, trace_id: str, finish_reason: str = FINISH_REASON_STOP) -> dict:
    return _sse("turn_end", turnId=turn_id, traceId=trace_id, finishReason=finish_reason)


def tool_start(turn_id: str, trace_id: str, tool_call_id: str, tool_name: str, args: dict) -> dict:
    return _sse(
        "tool_start",
        turnId=turn_id,
        traceId=trace_id,
        toolCallId=tool_call_id,
        toolName=tool_name,
        args=args,
    )


def tool_end(
    turn_id: str,
    trace_id: str,
    tool_call_id: str,
    status: str,
    result: object = None,
    error: str | None = None,
    error_code: str | None = None,
) -> dict:
    return _sse(
        "tool_end",
        turnId=turn_id,
        traceId=trace_id,
        toolCallId=tool_call_id,
        status=status,
        result=result,
        error=error,
        errorCode=error_code,
    )


def error(turn_id: str, trace_id: str, message: str, code: str | None = None) -> dict:
    return _sse("error", turnId=turn_id, traceId=trace_id, message=message, code=code)


def notification(
    task_id: str,
    status: str,
    message: str | None = None,
    trace_id: str = "",
) -> dict:
    """异步任务通知事件，与 turn_* 事件多路复用，不带 turnId。"""
    fields: dict = {"taskId": task_id, "status": status, "traceId": trace_id}
    if message is not None:
        fields["message"] = message
    return _sse("notification", **fields)


def with_seq(raw_event: dict, seq: int) -> dict:
    """为事件打 eventSeq：设置 SSE id 行 + 写入 data.eventSeq，供断线续传。"""
    ev = dict(raw_event)
    ev["id"] = str(seq)
    data = json.loads(ev.get("data") or "{}")
    data["eventSeq"] = seq
    ev["data"] = json.dumps(data, ensure_ascii=False)
    return ev


def heartbeat() -> dict:
    """SSE 注释行心跳，浏览器忽略但刷新网关 idle 计时。"""
    return {"comment": "heartbeat"}
