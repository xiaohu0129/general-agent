"""注册/登录/登出/当前用户（Cookie+Session）。

- 密码仅以 PBKDF2 哈希存储；登录成功下发 HttpOnly + SameSite=Lax cookie；
- 用户不存在与密码错误统一返回 401 INVALID_CREDENTIALS（防用户枚举）；
- 登录失败按 "IP+用户名" 与 "用户名" 双维度限流（各 5 次/10 分钟，任一命中即锁）；
- 注册按客户端 IP 限流（security.register_rate，超限 429 RATE_LIMIT）。
- /auth/* 不走 governance_dep（未登录），/auth/me 自行校验 cookie。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .. import auth as auth_mod
from .. import observability
from ..config import get_settings
from ..logging_setup import get_logger
from ..security import (
    GovernanceError,
    _clear_session_cookie,
    _set_session_cookie,
    audit,
)
from ..user_store import UserExistsError, UserStore

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)


class AuthRequest(BaseModel):
    username: str
    password: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _user_store(request: Request) -> UserStore:
    return request.app.state.user_store


@router.post("/register")
async def register(body: AuthRequest, request: Request, response: Response) -> dict:
    settings = get_settings()
    # 按客户端 IP 限流（独立于对话令牌桶）；取令牌在凭据校验/建用户之前，超限不建用户、不发 cookie
    ip = _client_ip(request)
    limiter = getattr(request.app.state, "register_limiter", None)
    if limiter is not None and not limiter.allow(ip):
        observability.record_rate_limit_hit(settings.security.web.env)
        audit("register_rate_limited", actor=ip, env=settings.security.web.env, resource=ip)
        raise GovernanceError(429, "RATE_LIMIT", "rate limit exceeded")
    err = auth_mod.validate_credentials(body.username, body.password)
    if err:
        raise GovernanceError(400, "VALIDATION", err)
    store = _user_store(request)
    try:
        uid = await store.create_user(body.username, auth_mod.hash_password(body.password))
    except UserExistsError:
        raise GovernanceError(409, "USER_EXISTS", "用户名已存在")

    sessions = request.app.state.login_sessions
    sess = sessions.create(uid, body.username)
    _set_session_cookie(response, settings, sess.token)
    audit("register", actor=uid, env=settings.security.web.env, resource=body.username)
    logger.info("user_registered", uid=uid, username=body.username)
    return {"uid": uid, "username": body.username}


@router.post("/login")
async def login(body: AuthRequest, request: Request, response: Response) -> dict:
    settings = get_settings()
    ip = _client_ip(request)
    sessions = request.app.state.login_sessions

    if sessions.login_locked(ip, body.username):
        raise GovernanceError(429, "LOGIN_LOCKED", "失败次数过多，请稍后再试")

    if auth_mod.validate_credentials(body.username, body.password):
        # 格式不合法直接按凭据错误处理（不泄露具体规则）
        sessions.record_login_fail(ip, body.username)
        raise GovernanceError(401, "INVALID_CREDENTIALS", "用户名或密码错误")

    store = _user_store(request)
    user = await store.get_by_username(body.username)
    if user is None:
        # 用户不存在也执行一次等价 PBKDF2（结果忽略），使两种失败路径耗时不可区分
        auth_mod.verify_password(body.password, auth_mod._DUMMY_HASH)
        password_ok = False
    else:
        password_ok = auth_mod.verify_password(body.password, user["password_hash"])
    if user is None or not password_ok:
        sessions.record_login_fail(ip, body.username)
        audit("login_failed", actor=body.username, env=settings.security.web.env, resource=ip)
        raise GovernanceError(401, "INVALID_CREDENTIALS", "用户名或密码错误")

    sessions.clear_login_fails(ip, body.username)
    sess = sessions.create(user["uid"], user["username"])
    _set_session_cookie(response, settings, sess.token)
    audit("login_ok", actor=user["uid"], env=settings.security.web.env, resource=ip)
    logger.info("user_login", uid=user["uid"], username=user["username"])
    return {"uid": user["uid"], "username": user["username"]}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    settings = get_settings()
    token = request.cookies.get(settings.security.session.cookie_name)
    if token:
        request.app.state.login_sessions.revoke(token)
    _clear_session_cookie(response, settings)
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    settings = get_settings()
    token = request.cookies.get(settings.security.session.cookie_name)
    sess = request.app.state.login_sessions.get(token)
    if sess is None:
        raise GovernanceError(401, "UNAUTHORIZED", "not authenticated")
    return {"uid": sess.uid, "username": sess.username}
