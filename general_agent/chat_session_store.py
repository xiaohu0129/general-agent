"""对话会话持久化（MySQL agent_chat_session 表）：多会话 CRUD + 归属校验。

一个用户（uid）拥有多个对话会话；session_id 即 agent_message.session_id。
所有查询带 uid 条件，越权访问在 SQL WHERE 层即被挡（查不到 -> 404）。
"""
from __future__ import annotations

from uuid import uuid4

import aiomysql

from .logging_setup import get_logger

logger = get_logger(__name__)

TABLE = "agent_chat_session"
MESSAGE_TABLE = "agent_message"
TITLE_MAX_LEN = 128


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _row_to_dict(row: dict) -> dict:
    return {
        "sessionId": row["session_id"],
        "title": row["title"],
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


class ChatSessionStore:
    """可注入 pool 以便测试，默认用 get_mysql()。"""

    def __init__(self, pool=None) -> None:
        self._pool = pool

    async def _pool_obj(self):
        if self._pool is not None:
            return self._pool
        from .mysql_client import get_mysql

        return await get_mysql()

    async def create(self, uid: str, service: str, env: str, title: str, session_id: str | None = None) -> dict:
        session_id = session_id or uuid4().hex
        title = (title or "新会话")[:TITLE_MAX_LEN]
        pool = await self._pool_obj()
        sql = (
            f"INSERT INTO {TABLE} (session_id, uid, service, env, title) "
            "VALUES (%s, %s, %s, %s, %s)"
        )
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (session_id, uid, service, env, title))
                await cur.execute(
                    f"SELECT session_id, title, created_at, updated_at FROM {TABLE} WHERE session_id=%s",
                    (session_id,),
                )
                row = await cur.fetchone()
        logger.info("chat_session_created", sessionId=session_id, uid=uid)
        return _row_to_dict(row)

    async def list_for_user(self, uid: str, limit: int = 50) -> list[dict]:
        pool = await self._pool_obj()
        sql = (
            f"SELECT session_id, title, created_at, updated_at FROM {TABLE} "
            "WHERE uid=%s ORDER BY updated_at DESC LIMIT %s"
        )
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (uid, limit))
                rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def get_owned(self, session_id: str, uid: str) -> dict | None:
        """归属校验：返回会话行；不属于该用户或不存在返回 None。"""
        pool = await self._pool_obj()
        sql = (
            f"SELECT session_id, uid, service, env, title, created_at, updated_at "
            f"FROM {TABLE} WHERE session_id=%s AND uid=%s"
        )
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (session_id, uid))
                return await cur.fetchone()

    async def rename(self, session_id: str, uid: str, title: str) -> bool:
        title = (title or "")[:TITLE_MAX_LEN]
        if not title.strip():
            return False
        pool = await self._pool_obj()
        sql = f"UPDATE {TABLE} SET title=%s, updated_at=NOW(3) WHERE session_id=%s AND uid=%s"
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (title, session_id, uid))
                return cur.rowcount > 0

    async def delete(self, session_id: str, uid: str) -> bool:
        """删除会话行及其全部消息（同事务语义，autocommit 下顺序执行）。"""
        pool = await self._pool_obj()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {MESSAGE_TABLE} WHERE session_id=%s AND user_id=%s",
                    (session_id, uid),
                )
                await cur.execute(
                    f"DELETE FROM {TABLE} WHERE session_id=%s AND uid=%s", (session_id, uid)
                )
                return cur.rowcount > 0

    async def touch(self, session_id: str) -> None:
        """有新消息时刷新 updated_at（best-effort）。"""
        pool = await self._pool_obj()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE {TABLE} SET updated_at=NOW(3) WHERE session_id=%s", (session_id,)
                )
