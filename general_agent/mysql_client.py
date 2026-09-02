"""MySQL 异步连接池（aiomysql）；agent 消息表 DDL 初始化。"""
from __future__ import annotations

import asyncio

import aiomysql

from .config import get_settings
from .logging_setup import get_logger

logger = get_logger(__name__)

_pool: aiomysql.Pool | None = None
_pool_lock = asyncio.Lock()

SCHEMA_DDL = """CREATE TABLE IF NOT EXISTS agent_message (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  service VARCHAR(64) NOT NULL,
  env VARCHAR(32) NOT NULL,
  user_id VARCHAR(128) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  turn_id VARCHAR(64) NOT NULL,
  role VARCHAR(16) NOT NULL,
  content MEDIUMTEXT NOT NULL,
  tool_calls JSON NULL,
  tool_call_id VARCHAR(64) NULL,
  content_ref VARCHAR(512) NULL,
  content_size BIGINT NULL,
  content_kind VARCHAR(32) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_session (service, env, user_id, session_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_user (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  uid VARCHAR(32) NOT NULL UNIQUE,
  username VARCHAR(64) NOT NULL UNIQUE,
  password_hash VARCHAR(256) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_chat_session (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id VARCHAR(64) NOT NULL UNIQUE,
  uid VARCHAR(32) NOT NULL,
  service VARCHAR(64) NOT NULL,
  env VARCHAR(32) NOT NULL,
  title VARCHAR(128) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_uid (uid, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


async def get_mysql() -> aiomysql.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:  # double-check under lock
                cfg = get_settings().mysql
                _pool = await aiomysql.create_pool(
                    host=cfg.host,
                    port=cfg.port,
                    user=cfg.user,
                    password=cfg.password,
                    db=cfg.database,
                    autocommit=True,
                    maxsize=cfg.pool_size,
                    charset="utf8mb4",
                )
                await init_schema(_pool)
    return _pool


async def init_schema(pool: aiomysql.Pool | None = None) -> None:
    pool = pool or await get_mysql()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for ddl in SCHEMA_DDL.split(";"):
                ddl = ddl.strip()
                if ddl:
                    await cur.execute(ddl)
    logger.info("agent_schema_ready")


async def close_mysql() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None