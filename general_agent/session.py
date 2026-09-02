"""会话与上下文：四级 ID + service/env/user 隔离 + Redis 瞬态（SessionStore）。

四级 ID（设计文档 §4.2）：
- sessionId：会话级，稳定，绑定 service+env+user；消息历史按此作用域从 MySQL 载入（M4）。
- turnId：每轮提问唯一。
- eventSeq：通道级，单调递增，SSE 断线续传依据（M7 使用）。
- taskId：异步任务句柄，完成通知靠它关联回发起点。

记忆模型（C2 已定）：无状态图（无 checkpointer），消息历史存 MySQL（message_store，
身份 service+env+user+session_id 作用域隔离）。SessionStore 只持瞬态/会话元数据
（eventSeq、活跃通道、task↔session），不重复存消息。
"""
from __future__ import annotations

import time
from uuid import uuid4

from .logging_setup import get_logger

logger = get_logger(__name__)

KEY_PREFIX = "general:agent"


def new_turn_id() -> str:
    return uuid4().hex


def new_task_id() -> str:
    return uuid4().hex


def session_key(service: str, env: str, user: str) -> str:
    """隔离维度 service+env+user -> 稳定命名键。"""
    return f"{service}:{env}:{user}"


def _k(*parts: str) -> str:
    return ":".join((KEY_PREFIX, *parts))


class SessionStore:
    """Redis 持久化会话元数据。可注入 client 以便测试，默认用 get_redis()。"""

    def __init__(self, client=None) -> None:
        self._client = client

    async def _redis(self):
        if self._client is not None:
            return self._client
        from .redis_client import get_redis

        return get_redis()

    async def next_event_seq(self, session_id: str) -> int:
        r = await self._redis()
        return await r.incr(_k("seq", session_id))

    async def save_session(
        self, session_id: str, *, service: str, env: str, user: str, ttl: int = 86400
    ) -> None:
        r = await self._redis()
        key = _k("session", session_id)
        await r.hset(
            key,
            mapping={
                "sessionId": session_id,
                "service": service,
                "env": env,
                "user": user,
                "createdAt": str(int(time.time())),
            },
        )
        await r.expire(key, ttl)

    async def get_session(self, session_id: str) -> dict | None:
        r = await self._redis()
        data = await r.hgetall(_k("session", session_id))
        return data or None

    async def register_channel(self, session_id: str, channel: str) -> None:
        r = await self._redis()
        await r.sadd(_k("channels", session_id), channel)

    async def unregister_channel(self, session_id: str, channel: str) -> None:
        r = await self._redis()
        await r.srem(_k("channels", session_id), channel)

    async def active_channels(self, session_id: str) -> list[str]:
        r = await self._redis()
        return list(await r.smembers(_k("channels", session_id)))

    async def bind_task(self, task_id: str, session_id: str, user: str, ttl: int = 604800) -> None:
        r = await self._redis()
        key = _k("task", task_id)
        await r.hset(
            key, mapping={"sessionId": session_id, "user": user, "createdAt": str(int(time.time()))}
        )
        await r.expire(key, ttl)

    async def lookup_task(self, task_id: str) -> dict | None:
        r = await self._redis()
        data = await r.hgetall(_k("task", task_id))
        return data or None