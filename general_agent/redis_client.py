"""Redis 客户端：按配置构建集群或单点异步客户端，进程内单例。"""
from __future__ import annotations

import redis.asyncio as aioredis
from redis.asyncio.cluster import RedisCluster
from redis.cluster import ClusterNode

from .config import RedisSettings, get_settings

_client: aioredis.Redis | RedisCluster | None = None


def _build_client(cfg: RedisSettings):
    """根据配置构建异步 Redis 客户端（惰性，不立即连接）。"""
    password = cfg.password or None
    if cfg.nodes:
        startup = []
        for item in cfg.nodes.split(","):
            item = item.strip()
            if not item:
                continue
            host, _, port = item.partition(":")
            startup.append(ClusterNode(host=host, port=int(port) if port else 6379))
        return RedisCluster(
            startup_nodes=startup,
            ssl=cfg.ssl,
            password=password,
            decode_responses=True,
        )
    return aioredis.from_url(
        cfg.url or "redis://localhost:6379",
        ssl=cfg.ssl,
        password=password,
        decode_responses=True,
    )


def get_redis():
    """返回进程内单例 Redis 客户端。"""
    global _client
    if _client is None:
        _client = _build_client(get_settings().redis)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None