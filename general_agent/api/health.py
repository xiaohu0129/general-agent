"""健康检查：服务存活 + 配置可读 + Redis 连通性探测 + OTel 状态。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import __version__, observability
from ..config import get_settings
from ..logging_setup import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    redis_status = "not_configured"
    if settings.redis.nodes or settings.redis.url:
        redis_status = await _ping_redis()
    return {
        "status": "ok",
        "service": "general-agent",
        "version": __version__,
        "llm": {
            "base_url": settings.llm.base_url or "http://localhost:9094(stub)",
            "model": settings.llm.model,
        },
        "redis": redis_status,
        "observability": {
            "enabled": settings.observability.enabled,
            "otlp_endpoint": settings.observability.otlp.endpoint or "",
            "initialized": observability.is_enabled(),
        },
    }


async def _ping_redis() -> str:
    try:
        from ..redis_client import get_redis

        client = get_redis()
        await client.ping()
        return "ok"
    except Exception as exc:
        logger.warning("redis_ping_failed", error=str(exc))
        return "error"