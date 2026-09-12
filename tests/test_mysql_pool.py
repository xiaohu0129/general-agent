"""U7：MySQL 连接池保活 pool_recycle（不依赖真实 MySQL）。

monkeypatch aiomysql.create_pool 与 init_schema，全程不触达真实库；
每个用例前后复位模块全局 _pool，避免状态串扰。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import general_agent.mysql_client as mysql_client
from general_agent.config import get_settings


@pytest.fixture
def isolated_pool(monkeypatch):
    """mock create_pool/init_schema，复位 _pool 与 mock 调用记录。"""
    mysql_client._pool = None
    fake_pool = object()
    create_pool = AsyncMock(return_value=fake_pool)
    init_schema = AsyncMock()
    monkeypatch.setattr(mysql_client.aiomysql, "create_pool", create_pool)
    monkeypatch.setattr(mysql_client, "init_schema", init_schema)
    yield create_pool, init_schema, fake_pool
    mysql_client._pool = None


async def test_pool_recycle_default_1800(isolated_pool):
    create_pool, init_schema, fake_pool = isolated_pool

    pool = await mysql_client.get_mysql()

    assert pool is fake_pool
    assert create_pool.await_args.kwargs["pool_recycle"] == 1800
    init_schema.assert_awaited_once_with(fake_pool)


async def test_pool_recycle_config_override(isolated_pool, monkeypatch):
    create_pool, _, _ = isolated_pool
    monkeypatch.setattr(get_settings().mysql, "pool_recycle", 60, raising=False)

    await mysql_client.get_mysql()

    assert create_pool.await_args.kwargs["pool_recycle"] == 60
