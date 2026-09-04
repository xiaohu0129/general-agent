"""FastAPI 应用工厂。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__, observability
from .api import auth_routes, chat, health, notify, sessions, stream
from .auth import LoginSessionStore
from .blob_store import LocalBlobStore
from .broker import Broker
from .chat_session_store import ChatSessionStore
from .config import get_settings
from .embedding import EmbeddingClient
from .llm import OpenAICompatibleModel
from .logging_setup import configure_logging, get_logger
from .message_store import MessageStore
from .security import GovernanceError, TokenBucket
from .skill_router import SkillRouter
from .skill_router.index import SkillIndex
from .skill_router.rules import RuleMatcher
from .skills import build_registry
from .turn_lock import TurnLockRegistry
from .user_store import UserStore

logger = get_logger(__name__)


async def _setup_skill_router(app: FastAPI, settings) -> None:
    """启动时构建 Skill 向量索引并装配 SkillRouter；routing.enabled=false 时不装配。"""
    app.state.skill_router = None
    app.state.routing_status = {"enabled": False, "index_ready": False, "mode": "off", "embedding_model": ""}
    if not settings.routing.enabled:
        return
    registry = app.state.skill_registry
    all_skills = registry.list_all()
    emb_cfg = settings.embedding
    embedder = EmbeddingClient(
        emb_cfg.base_url,
        model=emb_cfg.model,
        api_key=emb_cfg.api_key,
        timeout=emb_cfg.timeout,
    )
    # embedding 指向 stub（base_url 留空）时检索无语义，仅规则可用 -> 路由自动降级
    is_stub = not emb_cfg.base_url
    index = SkillIndex(embedder, cache_dir=emb_cfg.cache_dir, model_id=emb_cfg.model)
    await index.build(all_skills)
    mode = "rule+vector" if (index.ready and not is_stub) else ("rule-only" if index.ready else "degraded")
    if is_stub or not index.ready:
        logger.warning(
            "skill_router_degraded",
            reason="embedding_endpoint_is_stub" if is_stub else "index_build_failed",
            hint="配置 embedding.base_url 为真实端点后向量检索才生效，当前仅规则/全量兜底",
        )
    router = SkillRouter(
        index=index,
        rule_matcher=RuleMatcher(settings.routing.rules),
        llm=app.state.model,
        embedder=embedder,
        top_k=settings.routing.top_k,
        score_threshold=settings.routing.score_threshold,
        margin=settings.routing.margin,
    )
    app.state.skill_router = router
    app.state.routing_status = {
        "enabled": True,
        "index_ready": bool(index.ready),
        "mode": mode,
        "embedding_model": emb_cfg.model,
    }


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log.level, settings.log.json_output, settings.log.redact_secrets)

    if settings.security.auth_mode == "session" and "*" in settings.security.cors_origins:
        raise RuntimeError(
            "security.cors_origins 不能为 ['*']：session 模式需携带 cookie，必须配置明确白名单"
        )

    # 初始化 providers + propagator + httpx 出站 instrumentation（不阻塞，失败仅告警）
    observability.setup_observability(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _setup_skill_router(app, settings)
        yield
        observability.shutdown_observability()
        # release the lazily-created MySQL pool, if any
        from .mysql_client import close_mysql

        await close_mysql()

    app = FastAPI(title="general-agent", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 治理错误统一 JSON 响应 -> {"code":..,"message":..}
    @app.exception_handler(GovernanceError)
    async def _governance_exc_handler(request: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    # LLM：未配置端点时指向本地 stub（:9094），生产改 llm.base_url
    llm_cfg = settings.llm
    base_url = llm_cfg.base_url or "http://localhost:9094"
    app.state.model = OpenAICompatibleModel(
        base_url=base_url,
        model=llm_cfg.model,
        api_key=llm_cfg.api_key,
        timeout=llm_cfg.timeout,
    )
    # Skill 注册表，按请求 env 重建 agent；业务方在此注册自己的 Skill
    app.state.skill_registry = build_registry()
    # 业务服务注入点：Skill 内通过 ctx.services[key] 取用；业务方在此放入自己的客户端
    app.state.services: dict = {}
    # 大产物外置存储（Tier2）：超大工具结果/胖结果写本地 blob 目录，MySQL 行只存引用
    app.state.blob_store = LocalBlobStore(root=settings.artifacts.dir)
    app.state.message_store = MessageStore(
        blob_store=app.state.blob_store,
        inline_threshold=settings.artifacts.inline_threshold,
        head_chars=settings.artifacts.head_chars,
    )
    app.state.max_tool_rounds = settings.agent.max_tool_rounds
    # Web 登录态 + 用户/对话会话存储
    app.state.login_sessions = LoginSessionStore(ttl_seconds=settings.security.session.ttl_hours * 3600)
    app.state.user_store = UserStore()
    app.state.chat_sessions = ChatSessionStore()
    # 事件中枢 Broker
    app.state.broker = Broker(
        ring_size=settings.broker.ring_size,
        sub_queue_size=settings.broker.sub_queue_size,
    )
    # 内存限流器
    app.state.rate_limiter = TokenBucket(
        rate=settings.security.rate_limit.rps,
        capacity=settings.security.rate_limit.burst,
    )
    # producer 任务引用集，防 GC + 便于观测
    app.state.inflight = set()
    # 按 session_id 的轮次锁注册表：串行化同一会话的 producer，防止中断后重叠轮次导致历史交错
    app.state.turn_locks = TurnLockRegistry()
    # Skill 意图路由（lifespan 启动时构建索引并装配；未跑 lifespan/关闭时为 None -> 全量工具）
    app.state.skill_router = None
    app.state.routing_status = {
        "enabled": settings.routing.enabled,
        "index_ready": False,
        "mode": "off",
        "embedding_model": settings.embedding.model,
    }

    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(chat.router)
    app.include_router(stream.router)
    app.include_router(sessions.router)
    app.include_router(notify.router)

    # FastAPI 入站 instrumentation（根 server span + 入站上下文提取），须在 app 构建后调用
    observability.instrument_app(app)
    return app


app = create_app()
