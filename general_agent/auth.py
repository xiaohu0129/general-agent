"""Web 登录认证：密码哈希（PBKDF2）+ Cookie/Session 登录态 + 登录失败限流。

认证模式 auth_mode=session 时启用（见 config.security）：
- 密码：PBKDF2-HMAC-SHA256，随机 salt，标准库实现，不落明文；
- 登录态：服务端保存（内存 TTL dict；Redis 版预留），浏览器仅持不透明 token（HttpOnly cookie）；
- 即时吊销：登出/踢下线删服务端记录即生效，无 JWT 式滞后窗口；
- token 仅经 cookie 传输、secrets 生成、常量时间比较，XSS 不可读、不可猜测。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from .logging_setup import get_logger

logger = get_logger(__name__)

# ---------------- 密码哈希 ----------------
_PBKDF2_ALGO = "pbkdf2"
_PBKDF2_ROUNDS = 200_000
_SALT_BYTES = 16

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u4e00-\u9fa5]{2,32}$")
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 64


def validate_credentials(username: str, password: str) -> str | None:
    """返回错误消息；合法返回 None。"""
    if not USERNAME_RE.match(username or ""):
        return "用户名需为 2-32 位字母、数字、下划线或中文"
    if not (PASSWORD_MIN_LEN <= len(password or "") <= PASSWORD_MAX_LEN):
        return f"密码长度需为 {PASSWORD_MIN_LEN}-{PASSWORD_MAX_LEN} 位"
    return None


def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{_PBKDF2_ALGO}${rounds}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验；stored 格式非法时返回 False（不抛异常）。"""
    try:
        algo, rounds_s, salt_hex, hash_hex = stored.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_s)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------- 登录会话 ----------------
@dataclass
class LoginSession:
    token: str
    uid: str
    username: str
    created_at: float
    expires_at: float


class LoginSessionStore:
    """内存登录态：token -> LoginSession，懒过期 + 滑动续期。

    多实例部署可替换为 Redis 实现（key general:agent:loginsession:<token>，TTL=ttl），
    接口保持 create/get/touch/revoke/revoke_all 一致。
    """

    def __init__(self, ttl_seconds: float = 7 * 24 * 3600) -> None:
        self.ttl = ttl_seconds
        self._sessions: dict[str, LoginSession] = {}
        # 登录失败限流：key -> (失败次数, 首次失败时间)
        self._login_fails: dict[str, tuple[int, float]] = {}
        self._max_fails = 5
        self._lock_window = 600.0  # 10 分钟窗口

    def _purge_expired(self, now: float) -> None:
        expired = [t for t, s in self._sessions.items() if s.expires_at <= now]
        for t in expired:
            self._sessions.pop(t, None)
        # 顺带清理超出锁定窗口的登录失败计数，防批量撞库时键无界增长
        stale = [k for k, (_, first) in self._login_fails.items() if now - first > self._lock_window]
        for k in stale:
            self._login_fails.pop(k, None)

    def create(self, uid: str, username: str, *, now: float | None = None) -> LoginSession:
        now = now if now is not None else time.time()
        self._purge_expired(now)
        token = secrets.token_urlsafe(32)
        sess = LoginSession(
            token=token, uid=uid, username=username, created_at=now, expires_at=now + self.ttl
        )
        self._sessions[token] = sess
        return sess

    def get(self, token: str | None, *, now: float | None = None) -> LoginSession | None:
        if not token:
            return None
        now = now if now is not None else time.time()
        sess = self._sessions.get(token)
        if sess is None:
            return None
        if sess.expires_at <= now:
            self._sessions.pop(token, None)
            return None
        # 常量时间比较 token，防时序侧信道
        if not hmac.compare_digest(sess.token, token):
            return None
        return sess

    def touch(self, sess: LoginSession, *, now: float | None = None) -> bool:
        """滑动续期：剩余寿命不足一半则重置；返回是否续期（用于重写 cookie Max-Age）。"""
        now = now if now is not None else time.time()
        if sess.expires_at - now < self.ttl / 2:
            sess.expires_at = now + self.ttl
            return True
        return False

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)

    def revoke_all(self, uid: str) -> None:
        """踢下线：删除该用户全部登录态。"""
        for t in [t for t, s in self._sessions.items() if s.uid == uid]:
            self._sessions.pop(t, None)

    # ---------------- 登录失败限流 ----------------
    def _fail_key(self, ip: str, username: str) -> str:
        return f"{ip}|{username}"

    def login_locked(self, ip: str, username: str, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        self._purge_expired(now)
        rec = self._login_fails.get(self._fail_key(ip, username))
        if rec is None:
            return False
        count, first = rec
        if now - first > self._lock_window:
            self._login_fails.pop(self._fail_key(ip, username), None)
            return False
        return count >= self._max_fails

    def record_login_fail(self, ip: str, username: str, *, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        key = self._fail_key(ip, username)
        count, first = self._login_fails.get(key, (0, now))
        if now - first > self._lock_window:
            count, first = 0, now
        self._login_fails[key] = (count + 1, first)

    def clear_login_fails(self, ip: str, username: str) -> None:
        self._login_fails.pop(self._fail_key(ip, username), None)
