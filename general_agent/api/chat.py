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
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .. import events, observability
from ..agent import build_agent
from ..broker import Broker
from ..config import get_settings
from ..logging_setup import get_logger
from ..runner import run_turn
from ..security import GovernanceError, Identity, audit, governance_dep
from ..skills import SkillContext

router = APIRouter(tags=["chat"])
logger = get_logger(__name__)


def session_key(service: str, env: str, user: str) -> str:
    """无 sessionId 时的稳定隐式 ID（隔离维度 service+env+user）。"""
    return f"{service}:{env}:{user}"


def _first_line_title(message: str) -> str:
    return message.strip().splitlines()[0][:20] if message.strip() else "新会话"


class ClarifySelection(BaseModel):
    value: str = Field(max_length=200)


class ChatRequest(BaseModel):
    message: str
    sessionId: str | None = None
    clarify_selection: ClarifySelection | None = None


def _derive_trace_id(request: Request) -> str:
    tid = observability.current_trace_id()
    if tid:
        return tid
    return request.headers.get("x-trace-id") or uuid4().hex


async def _load_routing_context(message_store, settings, service, env, user, session_id):
    """路由前载历史（升序）：拼接 history 与澄清闭环 prev_clarify 相互独立。

    - history：最近 context_turns*2+1 行截取 user/assistant 文本行（跨轮拼接用），
      context_turns=0 或无 store 时不读 -> None；
    - prev_clarify：末条 assistant 行为澄清轮时据其 meta 构造（limit=1 收窄读取，
      与 context_turns 解耦——关闭拼接不应连带关闭澄清闭环）；无 store -> None。
    """
    if message_store is None:
        return None, None
    n = settings.routing.context_turns
    history = None
    if n > 0:
        rows = await message_store.load_messages(
            service, env, user, session_id, limit=n * 2 + 1
        )
        history = [
            {"role": r.get("role"), "content": r.get("content") or ""}
            for r in rows
            if r.get("role") in ("user", "assistant")
        ]
    prev_clarify = None
    last = await message_store.load_messages(service, env, user, session_id, limit=1)
    if last and last[-1].get("role") == "assistant":
        meta = last[-1].get("meta")
        if isinstance(meta, dict) and meta.get("kind") == "clarify":
            prev_clarify = {
                "categories": meta.get("categories"),
                "options": meta.get("options") or [],
                "turn_id": last[-1].get("turn_id"),
            }
    return history, prev_clarify


def _derive_retrieval(details: dict) -> str:
    """从路由 details 推导检索路标签：降级（BM25 收窄/语义关闭）记 keyword，否则 vector。"""
    if "degraded_keyword" in details or details.get("semantic_off"):
        return "keyword"
    return "vector"


def _serialize_details(details: dict) -> dict:
    """details 展平为可回放标量：list/dict 序列化为紧凑字符串，标量原样保留。

    span attributes 与审计日志仅接受标量；top_k/BM25/RRF/categories/clarify_options
    等结构化检索结果是回放关键信息，MUST NOT 被标量过滤静默丢弃。
    """
    out: dict = {}
    for k, v in details.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, dict)):
            try:
                out[k] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                out[k] = str(v)
        else:
            out[k] = str(v)
    return out


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
    clarify_meta: dict | None = None,
    route_path: str | None = None,
    recommended_tools: list[str] | None = None,
    retrieval: str | None = None,
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
                clarify_meta=clarify_meta,
                route_path=route_path,
                recommended_tools=recommended_tools,
                retrieval=retrieval,
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
    # 消息体边界先于归属 claim/历史读取/路由/LLM/broker/落库：空消息或超长在此即拒，
    # 不得产生 owner 行等任何副作用（strip 长度语义 pydantic Field 无法表达，显式判断）
    stripped_message = req.message.strip()
    if not 1 <= len(stripped_message) <= 8000:
        raise GovernanceError(400, "VALIDATION", "message 去除首尾空白后长度须在 1~8000 字符之间")
    settings = get_settings()
    auth_mode = settings.security.auth_mode

    # 会话归属四情形（owner 权威为 MySQL agent_chat_session，不依赖可选 Redis）：
    # 1. session 模式 + 无 sessionId：预生成 ID，会话行仍由 producer 在 turn 锁内创建；
    # 2. session 模式 + 有 sessionId：get_owned(uid)，未知/越权 -> 404（不做无主认领）；
    # 3/4. api_key/disabled ± sessionId：进入路由前 claim（INSERT IGNORE 先到先得），
    #    无 sid 用 session_key 隐式稳定 id（升级前历史会话由原身份认领）；已属他人 -> 404。
    chat_sessions = request.app.state.chat_sessions
    create_session = False
    session_title = ""
    if auth_mode == "session":
        if req.sessionId:
            owned = await chat_sessions.get_owned(req.sessionId, user)
            if owned is None:
                raise GovernanceError(404, "SESSION_NOT_FOUND", "会话不存在")
            session_id = req.sessionId
        else:
            session_id = uuid4().hex
            create_session = True
            session_title = _first_line_title(req.message)
    else:
        session_id = req.sessionId or session_key(service, env, user)
        # claim 必须先于任何历史读取/模型调用：越权请求在此即 404，不留读/写痕迹
        owned = await chat_sessions.claim_if_absent(
            session_id, service, env, user, _first_line_title(req.message)
        )
        if owned is None:
            raise GovernanceError(404, "SESSION_NOT_FOUND", "会话不存在")

    turn_id = uuid4().hex
    trace_id = _derive_trace_id(request)
    observability.bind_request_context(env=env, user=user, session_id=session_id, turn_id=turn_id)
    logger.info("chat_turn_start", turnId=turn_id, sessionId=session_id, service=service, env=env, user=user)

    heartbeat = settings.broker.heartbeat_interval

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
    routing_history = None
    prev_clarify = None
    route_path: str | None = None
    recommended_tools: list[str] | None = None
    retrieval: str | None = None
    if skill_router is not None:
        routing_history, prev_clarify = await _load_routing_context(
            request.app.state.message_store, settings, service, env, user, session_id
        )
        tracer = observability.get_tracer()
        with tracer.start_as_current_span("intent_route") as span:
            decision = await skill_router.route(
                req.message,
                candidates,
                history=routing_history,
                prev_clarify=prev_clarify,
                selection=(req.clarify_selection.value if req.clarify_selection else None),
            )
            span.set_attribute("route_path", decision.path)
            span.set_attribute("route_candidate_count", len(candidates))
            span.set_attribute("route_tool_count", len(decision.tools))
            span.set_attribute("route_tools", ",".join(s.name for s in decision.tools))
            for k, v in _serialize_details(decision.details).items():
                span.set_attribute(f"route.{k}", v)
        observability.record_intent_route(
            decision.path, category=str(decision.details.get("llm_category") or "")
        )
        # 澄清闭环结局（仅上一轮澄清产生的 option/text/repeat；首轮澄清 details 无该键）
        clarify_outcome = decision.details.get("clarify_outcome")
        if clarify_outcome:
            observability.record_clarify(clarify_outcome)
        # 检索分数分布：vector 必记（有 top1），bm25/rrf 各自在 details 存在时记
        details = decision.details
        if details.get("top1_score") is not None and not details.get("semantic_off"):
            observability.record_intent_score(
                details["top1_score"], details.get("score_gap"), "vector"
            )
        bm25_k = details.get("bm25_k")
        if isinstance(bm25_k, list) and bm25_k:
            top = bm25_k[0]
            if isinstance(top, dict) and "score" in top:
                observability.record_intent_score(top["score"], None, "bm25")
        rrf = details.get("rrf") or details.get("rrf_rewritten")
        if isinstance(rrf, list) and rrf:
            top = rrf[0]
            if isinstance(top, dict) and "score" in top:
                observability.record_intent_score(top["score"], None, "rrf")
        # query 改写触发计数（成功/失败）
        if details.get("query_rewritten"):
            observability.record_intent_rewrite(result="success")
        if details.get("query_rewrite_failed"):
            observability.record_intent_rewrite(result="failed")
        audit(
            "intent_route",
            actor=user,
            env=env,
            resource=decision.path,
            trace_id=trace_id,
            tools=[s.name for s in decision.tools],
            recommended_tools=",".join(s.name for s in decision.tools),
            **_serialize_details(decision.details),
        )
        tools = [s.to_tool(ctx, arg_guard=settings.routing.arg_guard) for s in decision.tools]
        # 误杀比对装配：收窄路径带 path/推荐集/检索路，degraded/fallback 全量不上报
        if decision.path in ("rule", "vector", "llm", "option"):
            route_path = decision.path
            recommended_tools = [s.name for s in decision.tools]
            retrieval = _derive_retrieval(decision.details)
    else:
        tools = [s.to_tool(ctx, arg_guard=settings.routing.arg_guard) for s in candidates]
    agent = build_agent(request.app.state.model, tools, system_prompt=settings.agent.system_prompt)
    direct_reply = decision.clarify_text if decision is not None else None
    clarify_meta = None
    if decision is not None and decision.path == "clarify":
        categories = decision.details.get("categories")
        if not categories:
            categories = sorted({s.category for s in candidates if s.category})
        clarify_meta = {
            "kind": "clarify",
            "categories": categories,
            "options": decision.clarify_options
            if decision.clarify_options is not None
            else (decision.details.get("clarify_options") or []),
        }

    # 选项确定性收窄成功：把所选项回写到上一轮澄清行 meta.selected（best-effort，不杀轮次）
    if decision is not None and decision.path == "option" and prev_clarify:
        message_store = request.app.state.message_store
        if message_store is not None:
            try:
                await message_store.update_clarify_selected(
                    service,
                    env,
                    user,
                    session_id,
                    prev_clarify["turn_id"],
                    decision.details.get("clarify_selection"),
                )
            except Exception as exc:
                logger.warning("update_clarify_selected_failed", error=str(exc), sessionId=session_id)

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
            clarify_meta=clarify_meta,
            route_path=route_path,
            recommended_tools=recommended_tools,
            retrieval=retrieval,
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
