"""用户持久化（MySQL agent_user 表）：注册/查询。密码仅存 PBKDF2 哈希。"""
from __future__ import annotations

from uuid import uuid4

import aiomysql

from .logging_setup import get_logger

logger = get_logger(__name__)

TABLE = "agent_user"


class UserExistsError(Exception):
    """用户名冲突（注册）。"""


class UserStore:
    """可注入 pool 以便测试，默认用 get_mysql()。"""

    def __init__(self, pool=None) -> None:
        self._pool = pool

    async def _pool_obj(self):
        if self._pool is not None:
            return self._pool
        from .mysql_client import get_mysql

        return await get_mysql()

    async def create_user(self, username: str, password_hash: str) -> str:
        """创建用户，返回 uid；用户名冲突抛 UserExistsError。"""
        uid = uuid4().hex
        pool = await self._pool_obj()
        sql = f"INSERT INTO {TABLE} (uid, username, password_hash) VALUES (%s, %s, %s)"
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, (uid, username, password_hash))
        except Exception as exc:
            # 1062 = Duplicate entry（MySQL）
            if getattr(exc, "args", [None])[0] == 1062 or "Duplicate" in str(exc):
                raise UserExistsError(username) from exc
            raise
        logger.info("user_created", uid=uid, username=username)
        return uid

    async def get_by_username(self, username: str) -> dict | None:
        pool = await self._pool_obj()
        sql = f"SELECT uid, username, password_hash FROM {TABLE} WHERE username=%s"
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (username,))
                return await cur.fetchone()

    async def get_by_uid(self, uid: str) -> dict | None:
        pool = await self._pool_obj()
        sql = f"SELECT uid, username, password_hash FROM {TABLE} WHERE uid=%s"
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (uid,))
                return await cur.fetchone()
