"""lifespan shutdown 资源关闭：未配置/未初始化 Redis 时不报错且调用 close_redis。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from general_agent import redis_client

from conftest import build_test_app


async def test_close_redis_noop_without_client():
    redis_client._client = None
    await redis_client.close_redis()


def test_lifespan_shutdown_closes_redis_without_config():
    from fastapi.testclient import TestClient

    app, _, _ = build_test_app()
    redis_client._client = None
    fake_close = AsyncMock()
    with patch("general_agent.redis_client.close_redis", fake_close):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    fake_close.assert_awaited_once()


def test_lifespan_shutdown_closes_shared_http_clients():
    from fastapi.testclient import TestClient

    app, _, _ = build_test_app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.http_client.is_closed is False
        assert app.state.sync_http_client.is_closed is False
    assert app.state.http_client.is_closed is True
    assert app.state.sync_http_client.is_closed is True
