"""M7 长任务稳定性事件中枢（Broker）。

对应综合设计 §4.3/§4.4、M7 设计 §1.2：
- eventSeq：单调递增，作为 SSE id 行，支持断线 Last-Event-ID 续传。
- ring buffer：每会话保留最近 N 条事件，供续传重放。
- fan-out：一个会话多个订阅者（POST /chat + GET /stream）共享同一事件流。
- notification：异步任务完成通知，与 turn_* 事件多路复用。

背压策略（D2）：订阅者队列 put_nowait，QueueFull 时丢弃该订阅者（ring 全量保留），
避免慢消费者阻塞 producer。多实例扩展（Redis seq/ring/Pub/Sub）见 M7 设计 §1.4，接口不变。
"""
from __future__ import annotations

import asyncio
from collections import deque

from . import events
from .logging_setup import get_logger

logger = get_logger(__name__)


class Broker:
    def __init__(self, ring_size: int = 256, sub_queue_size: int = 1024) -> None:
        self._ring_size = ring_size
        self._sub_queue_size = sub_queue_size
        self._seq: dict[str, int] = {}
        self._ring: dict[str, deque] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._drops = 0

    def next_seq(self, session_id: str) -> int:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        return seq

    def replay(self, session_id: str, after_seq: int) -> list[dict]:
        """返回 eventSeq > after_seq 的 ring 事件，供断线续传重放。"""
        ring = self._ring.get(session_id, ())
        return [ev for ev in ring if int(ev.get("id", 0)) > after_seq]

    async def distribute(self, session_id: str, raw_event: dict) -> dict:
        """分配 eventSeq -> 入 ring -> fan-out 订阅者（返回带 seq 的事件）。"""
        seq = self.next_seq(session_id)
        ev = events.with_seq(raw_event, seq)
        ring = self._ring.setdefault(session_id, deque(maxlen=self._ring_size))
        ring.append(ev)
        for q in list(self._subs.get(session_id, ())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                self._drops += 1
                logger.warning("broker_sub_queue_full", sessionId=session_id, eventSeq=seq)
        return ev

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._sub_queue_size)
        self._subs.setdefault(session_id, set()).add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(session_id)
        if subs and q in subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(session_id, None)

    def active_subscribers(self, session_id: str) -> int:
        return len(self._subs.get(session_id, ()))

    async def publish_notification(
        self,
        session_id: str,
        task_id: str,
        status: str,
        message: str | None = None,
        trace_id: str = "",
    ) -> dict:
        """发布异步任务通知：notification 事件与 turn_* 事件多路复用同一通道。"""
        ev = events.notification(task_id, status, message=message, trace_id=trace_id)
        out = await self.distribute(session_id, ev)
        logger.info(
            "notification_published",
            sessionId=session_id,
            taskId=task_id,
            status=status,
            eventSeq=out["id"],
            deliveredTo=self.active_subscribers(session_id),
        )
        return out

    @property
    def drops(self) -> int:
        return self._drops
