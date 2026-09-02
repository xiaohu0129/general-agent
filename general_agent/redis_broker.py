"""Redis 多实例事件中枢（长任务稳定性，Broker 的多实例实现）。

用 Redis 实现跨实例共享的事件中枢：
- eventSeq：Redis INCR 发号，全局单调；
- ring buffer：Redis List + LTRIM 环形保留；
- 跨实例通知：Redis Pub/Sub（频道 general:agent:notify:{sessionId}）；
- fan-out：本地订阅者仍为进程内 asyncio.Queue，收到 Pub/Sub 后本地分发。

与内存 Broker 接口一致，可作 drop-in 替换。
"""
from __future__ import annotations

import asyncio
import json
from collections import deque

from . import events
from .logging_setup import get_logger
from .redis_client import get_redis

logger = get_logger(__name__)

REDIS_NS = "general:agent"


class RedisBroker:
    """Redis 多实例事件中枢。"""

    def __init__(self, ring_size: int = 256, sub_queue_size: int = 1024) -> None:
        self._ring_size = ring_size
        self._sub_queue_size = sub_queue_size
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._drops = 0
        self._pubsub: asyncio.Task | None = None

    # ---------- seq ----------
    async def next_seq(self, session_id: str) -> int:
        r = get_redis()
        return await r.incr(f"{REDIS_NS}:seq:{session_id}")

    # ---------- ring ----------
    async def replay(self, session_id: str, after_seq: int) -> list[dict]:
        r = get_redis()
        key = f"{REDIS_NS}:ring:{session_id}"
        items = await r.lrange(key, 0, -1)
        out = []
        for raw in items:
            try:
                ev = json.loads(raw)
                if int(ev.get("id", 0)) > after_seq:
                    out.append(ev)
            except Exception:
                pass
        return out

    async def _push_ring(self, session_id: str, ev: dict) -> None:
        r = get_redis()
        key = f"{REDIS_NS}:ring:{session_id}"
        pipe = r.pipeline()
        pipe.rpush(key, json.dumps(ev, ensure_ascii=False))
        pipe.ltrim(key, -self._ring_size, -1)
        await pipe.execute()

    # ---------- distribute ----------
    async def distribute(self, session_id: str, raw_event: dict) -> dict:
        seq = await self.next_seq(session_id)
        ev = events.with_seq(raw_event, seq)
        await self._push_ring(session_id, ev)
        for q in list(self._subs.get(session_id, ())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                self._drops += 1
                logger.warning("redis_broker_sub_queue_full", sessionId=session_id, eventSeq=seq)
        return ev

    # ---------- subscribers ----------
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

    # ---------- notification ----------
    async def publish_notification(
        self, session_id: str, task_id: str, status: str,
        message: str | None = None, trace_id: str = "",
    ) -> dict:
        ev = events.notification(task_id, status, message=message, trace_id=trace_id)
        # 补 sessionId，供 Pub/Sub 接收方路由
        ev["sessionId"] = session_id
        out = await self.distribute(session_id, ev)
        # 再发 Pub/Sub，通知其他实例分发到其本地订阅者
        try:
            r = get_redis()
            notify_key = f"{REDIS_NS}:notify:{session_id}"
            await r.publish(notify_key, json.dumps(ev, ensure_ascii=False))
        except Exception:
            logger.exception("redis_pubsub_publish_failed", sessionId=session_id)
        logger.info(
            "redis_broker_notify_published",
            sessionId=session_id, taskId=task_id, status=status,
            eventSeq=int(out["id"]),
            deliveredTo=self.active_subscribers(session_id),
        )
        return out

    # ---------- cross-instance listener ----------
    async def _fanout_local(self, session_id: str, ev: dict) -> None:
        """本地 fan-out：分发给本进程订阅者（ring 已由 distribute 写入）。"""
        for q in list(self._subs.get(session_id, ())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                self._drops += 1

    async def start_listener(self) -> None:
        """启动 Redis Pub/Sub 监听，把跨实例 notification 分发到本地订阅者。"""
        if self._pubsub is not None:
            return
        self._pubsub = asyncio.ensure_future(self._listen_loop())

    async def _listen_loop(self) -> None:
        r = get_redis()
        pubsub = r.pubsub()
        try:
            await pubsub.psubscribe(f"{REDIS_NS}:notify:*")
            async for msg in pubsub.listen():
                if msg["type"] != "pmessage":
                    continue
                try:
                    ev = json.loads(msg["data"])
                    session_id = ev.get("sessionId", "")
                    if not session_id:
                        continue
                    # 本地 fan-out：本实例 distribute 已处理过，此处主要服务其他实例发来的事件
                    await self._fanout_local(session_id, ev)
                except Exception:
                    logger.exception("redis_broker_listener_error")
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.punsubscribe()
            await pubsub.aclose()

    async def stop_listener(self) -> None:
        if self._pubsub is not None:
            self._pubsub.cancel()
            self._pubsub = None
