"""GET /stream：持久 SSE 通道（M7 续传 + 通知多路复用）。

断线重连携 Last-Event-ID -> replay ring(seq>lastEventId) -> 转 live 事件（去重 replay 过的 seq）；
本通道下发所有会话事件（turn_* + notification），与 POST /chat 的单轮过滤互补。
M6 治理依赖 governance_dep（鉴权/env/限流）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from .. import events
from ..broker import Broker
from ..config import get_settings
from ..logging_setup import get_logger
from ..security import GovernanceError, Identity, governance_dep

router = APIRouter(tags=["stream"])
logger = get_logger(__name__)


@router.get("/stream")
async def stream(
    request: Request,
    sessionId: str,
    identity: Identity = Depends(governance_dep),
    lastEventId: int | None = None,
):
    broker: Broker = request.app.state.broker
    settings = get_settings()

    # session 模式：订阅前做会话归属校验，越权/不存在 -> 404
    if settings.security.auth_mode == "session":
        owned = await request.app.state.chat_sessions.get_owned(sessionId, identity.user)
        if owned is None:
            raise GovernanceError(404, "SESSION_NOT_FOUND", "会话不存在")

    heartbeat = settings.broker.heartbeat_interval

    # Last-Event-ID 优先取查询参数，否则取标准头（浏览器 EventSource 自动携带）
    last = lastEventId
    if last is None:
        header_val = request.headers.get("last-event-id")
        if header_val:
            try:
                last = int(header_val)
            except ValueError:
                raise GovernanceError(400, "VALIDATION", "malformed Last-Event-ID header")
        else:
            last = 0

    queue = await broker.subscribe(sessionId)

    async def event_stream():
        # replay 段最大 seq；live 事件 seq 单调递增，seq <= 该上界即重放重复（有界，无需 set）
        max_replayed_seq = 0
        try:
            # 先 replay ring（seq > last），补断线 gap
            for ev in broker.replay(sessionId, last):
                max_replayed_seq = max(max_replayed_seq, int(ev.get("id", 0)))
                yield ev
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield events.heartbeat()
                    continue
                if int(ev.get("id", 0)) <= max_replayed_seq:
                    continue
                yield ev
        finally:
            broker.unsubscribe(sessionId, queue)

    return EventSourceResponse(event_stream())
