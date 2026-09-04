"""POST /chat：用户提问 SSE 流式入口（M7 producer/consumer 解耦 + 心跳 + eventSeq）。

M6 治理依赖 governance_dep（鉴权/env 白名单/限流）解析出 Identity。
M7：订阅 broker -> spawn producer（run_turn -> broker.distribute）-> consumer 轮询队列；
仅转发当前 turnId 的事件，turn_end/error 收尾 SSE。producer 为独立 task，不随 SSE 断开取消。
"""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .. import events, observability
from ..agent import build_agent
from ..broker import Broker
from ..config import get_settings
from ..logging_setup import get_logger
from ..runner import run_turn
from ..security import GovernanceError, Identity, audit, governance_dep
from ..session import SessionStore, session_key
from ..skills import SkillContext

router = APIRouter(tags=["chat"])
logger = get_logger(__name__)


class ChatRequest(BaseModel):
    message: str
    sessionId: str | None = None


def _derive_trace_id(request: Request) -> str:
    tid = observability.current_trace_id()
    if tid:
        return tid
    return request.headers.get("x-trace-id") or uuid4().hex


async def _produce(
    app_state,
    broker: Broker,
    session_id: str,
    *,
    agent,
    message_store,
    chat_sessions,
    create_session: bool,
    session_title: str,
    service: str,
    env: str,
    user: str,
    turn_id: str,
    trace_id: str,
    user_message: str,
    max_tool_rounds: int,
    direct_reply: str | None = None,
) -> None:
    """Producer：消费 run_turn 事件流 -> broker.distribute（分配 eventSeq + 入 ring + fan-out）。

    不随 SSE 断开取消：即使前端断开，轮次仍跑完并落库，事件入 ring 供 GET /stream 续传。
    任何异常转 error 事件分发，保证前端能收到完整语义（turn_start -> error）。
    引用持有由调用方（chat）同步加入 app.state.inflight，这里只在结束时移除。
    """
    task = asyncio.current_task()
    # 同一会话串行化：用户中途停止后立即再发消息时，后轮必须等前轮 producer 跑完落库后
    # 才载入历史，否则两轮并发会导致 agent_message 行交错（user1,user2,asst2,asst1）、
    # 工具消息跨轮次配对等错乱。注册表按引用计数回收，避免"释放与唤醒之间误删锁"的竞态。
    try:
        async with app_state.turn_locks.lock(session_id):
            # 会话行延后到此处（而非请求入口）创建：producer 不随断开取消，
            # 保证"建会话"与"跑轮次"原子且必定完成，避免极早断开留下空孤儿会话。
            if create_session and chat_sessions is not None:
                await chat_sessions.create(user, service, env, session_title, session_id=session_id)
            async for ev in run_turn(
                agent=agent,
                message_store=message_store,
                service=service,
                env=env,
                user=user,
                session_id=session_id,
                turn_id=turn_id,
                trace_id=trace_id,
                user_message=user_message,
                max_tool_rounds=max_tool_rounds,
                direct_reply=direct_reply,
            ):
                await broker.distribute(session_id, ev)
    except Exception as exc:
        logger.exception("producer_error", turnId=turn_id, sessionId=session_id)
        code = getattr(exc, "code", None) or "internal_error"
        await broker.distribute(
            session_id, events.error(turn_id, trace_id, str(exc) or "internal_error", code=code)
        )
    finally:
        app_state.inflight.discard(task)
        # Web 会话：轮次结束后刷新 updated_at（best-effort，失败不影响主流程）
        try:
            if chat_sessions is not None and get_settings().security.auth_mode == "session":
                await chat_sessions.touch(session_id)
        except Exception:
            logger.warning("touch_session_failed", sessionId=session_id)


@router.post("/chat")
async def chat(req: ChatRequest, request: Request, identity: Identity = Depends(governance_dep)):
    service, env, user = identity.service, identity.env, identity.user
    settings = get_settings()

    # session 模式（Web 登录）：多会话——传了 sessionId 做归属校验；没传则预生成 ID，
    # 会话行由 producer 在锁内创建（见 _produce），避免请求极早断开留下空孤儿会话。
    chat_sessions = request.app.state.chat_sessions if settings.security.auth_mode == "session" else None
    create_session = False
    session_title = ""
    if settings.security.auth_mode == "session":
        if req.sessionId:
            owned = await chat_sessions.get_owned(req.sessionId, user)
            if owned is None:
                raise GovernanceError(404, "SESSION_NOT_FOUND", "会话不存在")
            session_id = req.sessionId
        else:
            session_id = uuid4().hex
            create_session = True
            session_title = (
                req.message.strip().splitlines()[0][:20] if req.message.strip() else "新会话"
            )
    else:
        session_id = req.sessionId or session_key(service, env, user)

    turn_id = uuid4().hex
    trace_id = _derive_trace_id(request)
    observability.bind_request_context(env=env, user=user, session_id=session_id, turn_id=turn_id)
    logger.info("chat_turn_start", turnId=turn_id, sessionId=session_id, service=service, env=env, user=user)

    heartbeat = settings.broker.heartbeat_interval

    # 会话 owner 落 Redis（best-effort）
    if settings.redis.nodes or settings.redis.url:
        try:
            await SessionStore().save_session(session_id, service=service, env=env, user=user)
        except Exception as exc:
            logger.warning("save_session_failed", error=str(exc), sessionId=session_id)

    # 按 env 过滤 Skill -> Skill 意图路由（规则/向量/LLM 兜底/澄清）收窄工具集 -> 每请求重建 agent
    ctx = SkillContext(
        env=env,
        user=user,
        session_id=session_id,
        services=dict(request.app.state.services),
    )
    candidates = request.app.state.skill_registry.list_allowed(ctx)
    decision = None
    skill_router = getattr(request.app.state, "skill_router", None)
    if skill_router is not None:
        tracer = observability.get_tracer()
        with tracer.start_as_current_span("intent_route") as span:
            decision = await skill_router.route(req.message, candidates)
            span.set_attribute("route_path", decision.path)
            span.set_attribute("route_candidate_count", len(candidates))
            span.set_attribute("route_tool_count", len(decision.tools))
            span.set_attribute("route_tools", ",".join(s.name for s in decision.tools))
            for k, v in decision.details.items():
                if isinstance(v, (str, int, float, bool)):
                    span.set_attribute(f"route.{k}", v)
        observability.record_intent_route(
            decision.path, category=str(decision.details.get("llm_category") or "")
        )
        audit(
            "intent_route",
            actor=user,
            env=env,
            resource=decision.path,
            trace_id=trace_id,
            tools=[s.name for s in decision.tools],
            **{k: v for k, v in decision.details.items() if isinstance(v, (str, int, float, bool))},
        )
        tools = [s.to_tool(ctx) for s in decision.tools]
    else:
        tools = [s.to_tool(ctx) for s in candidates]
    agent = build_agent(request.app.state.model, tools, system_prompt=settings.agent.system_prompt)
    direct_reply = decision.clarify_text if decision is not None else None

    broker: Broker = request.app.state.broker
    # 先订阅再 spawn producer，确保 turn_start 不丢
    queue = await broker.subscribe(session_id)
    producer = asyncio.create_task(
        _produce(
            request.app.state,
            broker,
            session_id,
            agent=agent,
            message_store=request.app.state.message_store,
            chat_sessions=chat_sessions,
            create_session=create_session,
            session_title=session_title,
            service=service,
            env=env,
            user=user,
            turn_id=turn_id,
            trace_id=trace_id,
            user_message=req.message,
            max_tool_rounds=request.app.state.max_tool_rounds,
            direct_reply=direct_reply,
        )
    )
    # 同步持有 task 引用，防止 chat 返回后 producer 被 GC（asyncio 官方建议）；
    # _produce 结束时从 inflight 移除。
    request.app.state.inflight.add(producer)

    async def event_stream():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield events.heartbeat()
                    continue
                # 仅转发当前轮次事件，过滤 notification 等其他轮次
                data = json.loads(ev.get("data") or "{}")
                if data.get("turnId") != turn_id:
                    continue
                yield ev
                if ev.get("event") in ("turn_end", "error"):
                    break
        finally:
            broker.unsubscribe(session_id, queue)
            # producer 仍会跑完，事件入 ring 供 GET /stream 续传

    return EventSourceResponse(event_stream())
