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
import time
from collections import deque
from collections.abc import Callable

from . import events
from .logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_RING_SIZE = 2048
DEFAULT_SESSION_TTL = 86400.0
# 懒扫描成本阈值：维护会话数达到该值后才允许扫描；两次扫描间至少隔
# max(阈值, 上次扫描后留存会话数) 次入口操作，把 O(n) 扫描摊销为每事件 O(1)；
# 会话数低于阈值时内存本身有界。
SWEEP_MIN_SESSIONS = 64


class Broker:
    def __init__(
        self,
        ring_size: int = DEFAULT_RING_SIZE,
        sub_queue_size: int = 1024,
        session_ttl: float = DEFAULT_SESSION_TTL,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self._ring_size = ring_size
        self._sub_queue_size = sub_queue_size
        self._session_ttl = session_ttl
        self._time = time_func or time.monotonic
        self._seq: dict[str, int] = {}
        self._ring: dict[str, deque] = {}
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._last_active: dict[str, float] = {}
        self._ops_since_sweep = 0
        self._sweep_interval = SWEEP_MIN_SESSIONS
        self._drops = 0

    @property
    def ring_size(self) -> int:
        return self._ring_size

    @property
    def session_ttl(self) -> float:
        return self._session_ttl

    def sweep(self, now: float | None = None) -> int:
        """清除"无订阅者且空闲超过 session_ttl"的会话状态，返回回收会话数。"""
        now = self._time() if now is None else now
        reclaimed = 0
        for sid in list(set(self._last_active) | set(self._subs)):
            if self._subs.get(sid):  # 有活跃订阅者 MUST NOT 回收
                continue
            last = self._last_active.get(sid)
            if last is not None and now - last > self._session_ttl:
                self._seq.pop(sid, None)
                self._ring.pop(sid, None)
                self._last_active.pop(sid, None)
                self._subs.pop(sid, None)
                reclaimed += 1
        return reclaimed

    def _tracked_sessions(self) -> int:
        """维护会话数的 O(1) 上界近似（仅用于门控，不用于回收判定）。

        真实并集规模 ∈ [max, 2·max]：最坏只在阈值附近多触发一次 O(n) 扫描，
        扫描本身的 TTL+订阅判定不变，故对正确性零影响；避免每事件构造并集。
        """
        return max(len(self._last_active), len(self._subs))

    def _maybe_sweep(self) -> None:
        """distribute/subscribe 入口懒扫描：达阈值且距上次扫描满一个动态间隔才扫。

        门控全程 O(1)（仅两次 len）；间隔 = max(阈值, 上次扫描后留存会话数)，
        使 O(n) 扫描摊销为每事件 O(1)；会话数低于阈值时内存本身有界，不扫描。
        """
        self._ops_since_sweep += 1
        if (
            self._tracked_sessions() >= SWEEP_MIN_SESSIONS
            and self._ops_since_sweep >= self._sweep_interval
        ):
            self.sweep()
            self._ops_since_sweep = 0
            self._sweep_interval = max(SWEEP_MIN_SESSIONS, self._tracked_sessions())

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
        self._maybe_sweep()
        seq = self.next_seq(session_id)
        ev = events.with_seq(raw_event, seq)
        ring = self._ring.setdefault(session_id, deque(maxlen=self._ring_size))
        ring.append(ev)
        self._last_active[session_id] = self._time()
        for q in list(self._subs.get(session_id, ())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                self._drops += 1
                logger.warning("broker_sub_queue_full", sessionId=session_id, eventSeq=seq)
        return ev

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        self._maybe_sweep()
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
