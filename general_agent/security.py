"""M6 治理依赖：鉴权 + env 白名单 + 限流 + 审计。

入口 governance_dep（auth -> env 白名单 -> 限流）解析出 Identity，供 /chat、/stream。
service_auth_dep（api_key 校验）供 /internal/notify；/health 免鉴权。
详见 docs/00-AGENT综合设计.md §五（治理层）与 docs/01-AGENT实现设计.md。
"""
from __future__ import annotations

import hmac
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response

from .auth import LoginSessionStore
from .config import get_settings
from .logging_setup import get_logger

audit_logger = get_logger("audit")


class GovernanceError(HTTPException):
    """治理错误，detail 为 {"code":..,"message":..}，由 app 统一 handler 转 JSON。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})
        self.code = code


@dataclass
class Identity:
    service: str
    env: str
    user: str

    @property
    def rate_key(self) -> str:
        return f"{self.user}:{self.env}"


def resolve_identity(request: Request) -> Identity:
    return Identity(
        service=request.headers.get("x-service", "default"),
        env=request.headers.get("x-env", "dev"),
        user=request.headers.get("x-user", "anonymous"),
    )


def _set_session_cookie(response: Response, settings, token: str) -> None:
    """写登录 cookie：HttpOnly（JS 不可读，防 XSS 窃取）+ SameSite=Lax（防 CSRF）。"""
    s = settings.security.session
    response.set_cookie(
        key=s.cookie_name,
        value=token,
        max_age=int(settings.security.session.ttl_hours * 3600),
        path="/",
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
    )


def _clear_session_cookie(response: Response, settings) -> None:
    response.delete_cookie(
        key=settings.security.session.cookie_name, path="/", httponly=True,
        samesite="lax", secure=settings.security.session.cookie_secure,
    )


def _web_session_identity(request: Request, response: Response, settings) -> Identity:
    """auth_mode=session：从 HttpOnly cookie 解析登录态 -> Identity（user=uid）。"""
    store: LoginSessionStore = request.app.state.login_sessions
    token = request.cookies.get(settings.security.session.cookie_name)
    sess = store.get(token)
    if sess is None:
        raise GovernanceError(401, "UNAUTHORIZED", "not authenticated")
    # 滑动续期：剩余寿命不足一半则重写 cookie
    if store.touch(sess):
        _set_session_cookie(response, settings, sess.token)
    request.state.username = sess.username
    return Identity(
        service=settings.security.web.service,
        env=settings.security.web.env,
        user=sess.uid,
    )


# ---------------- 审计 ----------------
def audit(action: str, *, actor: str, env: str, resource: str = "", trace_id: str = "", **extra) -> None:
    audit_logger.info(
        "audit",
        action=action,
        actor=actor,
        env=env,
        resource=resource,
        traceId=trace_id,
        **extra,
    )


# ---------------- 令牌桶 ----------------
class TokenBucket:
    """单机内存令牌桶，按 key 独立计数；多实例可替换为 Redis（Lua 原子脚本）。"""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate
        self.capacity = capacity
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        tokens, last = self._state.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens >= cost:
            tokens -= cost
            self._state[key] = (tokens, now)
            return True
        self._state[key] = (tokens, now)
        return False


# ---------------- 鉴权 ----------------
def _api_key_valid(token: str, keys) -> bool:
    """Constant-time API key check to avoid timing side-channels."""
    if not token:
        return False
    valid = False
    for k in keys:
        valid |= hmac.compare_digest(token, k)
    return valid


def _check_auth(settings, request: Request) -> None:
    mode = settings.security.auth_mode
    if mode == "disabled":
        return
    if mode == "jwt":
        # 预留未实现：不得静默放行（fails open 会导致误配暴露服务）
        raise GovernanceError(500, "CONFIG", "auth_mode=jwt 尚未实现，请使用 session/api_key/disabled")
    if mode == "api_key":
        token = request.headers.get("x-api-key", "")
        if not _api_key_valid(token, settings.security.api_keys):
            raise GovernanceError(401, "AUTH", "invalid or missing api key")


def _check_env(settings, env: str) -> None:
    allowed = settings.security.allowed_envs
    if allowed and env not in allowed:
        raise GovernanceError(400, "VALIDATION", f"env '{env}' not allowed")


def _check_rate_limit(settings, request: Request, identity: Identity) -> None:
    rl = settings.security.rate_limit
    if not rl.enabled:
        return
    limiter: TokenBucket = request.app.state.rate_limiter
    if not limiter.allow(identity.rate_key):
        from . import observability

        observability.record_rate_limit_hit(identity.env)
        audit("rate_limited", actor=identity.user, env=identity.env, resource=identity.rate_key)
        raise GovernanceError(429, "RATE_LIMIT", "rate limit exceeded")


# ---------------- 依赖 ----------------
def governance_dep(request: Request, response: Response) -> Identity:
    """统一治理：鉴权 -> env 白名单 -> 限流，通过则返回 Identity。

    auth_mode=session：Cookie+Session 登录态（Web 终端用户），Identity.user=uid；
    其余模式：x-service/x-env/x-user 头（API 调用方），行为与旧版一致。
    """
    settings = get_settings()
    mode = settings.security.auth_mode
    if mode == "session":
        identity = _web_session_identity(request, response, settings)
    else:
        identity = resolve_identity(request)
        _check_auth(settings, request)
    _check_env(settings, identity.env)
    _check_rate_limit(settings, request, identity)
    if mode not in ("disabled", "session"):
        audit("auth_ok", actor=identity.user, env=identity.env, resource=identity.service)
    return identity


def service_auth_dep(request: Request) -> None:
    """服务间鉴权（/internal/notify）：校验 api_key。"""
    settings = get_settings()
    mode = settings.security.auth_mode
    if mode == "disabled":
        return
    token = request.headers.get("x-api-key", "")
    if not _api_key_valid(token, settings.security.api_keys):
        raise GovernanceError(401, "AUTH", "invalid or missing service api key")
