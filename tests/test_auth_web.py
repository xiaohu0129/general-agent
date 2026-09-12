"""Web 认证（Cookie+Session）+ 多会话管理测试。

不依赖外部服务：FakeUserStore/FakeChatSessionStore 内存替身 + stub LLM + FakeStore 消息。
auth_mode 在测试内切换为 session（conftest 默认 disabled），结束后恢复。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from general_agent.auth import LoginSessionStore, hash_password, verify_password
from general_agent.config import get_settings
from general_agent.user_store import UserExistsError

from conftest import build_test_app, parse_sse


class FakeUserStore:
    def __init__(self):
        self.users: dict[str, dict] = {}

    async def create_user(self, username, password_hash):
        if username in self.users:
            raise UserExistsError(username)
        uid = uuid.uuid4().hex
        self.users[username] = {"uid": uid, "username": username, "password_hash": password_hash}
        return uid

    async def get_by_username(self, username):
        return self.users.get(username)


class FakeChatSessions:
    def __init__(self):
        self.sessions: dict[str, dict] = {}

    async def create(self, uid, service, env, title, session_id=None):
        sid = session_id or uuid.uuid4().hex
        self.sessions[sid] = {
            "session_id": sid, "uid": uid, "service": service, "env": env, "title": title
        }
        return {"sessionId": sid, "title": title, "createdAt": None, "updatedAt": None}

    async def list_for_user(self, uid, limit=50):
        return [
            {"sessionId": s["session_id"], "title": s["title"], "createdAt": None, "updatedAt": None}
            for s in self.sessions.values()
            if s["uid"] == uid
        ]

    async def get_owned(self, session_id, uid):
        s = self.sessions.get(session_id)
        return s if (s and s["uid"] == uid) else None

    async def get_owned_scoped(self, session_id, service, env, uid):
        s = self.sessions.get(session_id)
        if s and s["uid"] == uid and s["service"] == service and s["env"] == env:
            return s
        return None

    async def claim_if_absent(self, session_id, service, env, uid, title):
        existing = self.sessions.get(session_id)
        if existing is not None:
            if (
                existing["uid"] == uid
                and existing["service"] == service
                and existing["env"] == env
            ):
                return existing
            return None
        row = {
            "session_id": session_id, "uid": uid, "service": service, "env": env,
            "title": (title or "新会话")[:128],
        }
        self.sessions[session_id] = row
        return row

    async def rename(self, session_id, uid, title):
        s = self.sessions.get(session_id)
        if s and s["uid"] == uid and title.strip():
            s["title"] = title
            return True
        return False

    async def delete(self, session_id, uid):
        s = self.sessions.get(session_id)
        if s and s["uid"] == uid:
            del self.sessions[session_id]
            return True
        return False

    async def touch(self, session_id):
        pass


@pytest.fixture
def web_app():
    settings = get_settings()
    saved = settings.security.auth_mode
    settings.security.auth_mode = "session"
    app, store, broker = build_test_app()
    app.state.user_store = FakeUserStore()
    app.state.chat_sessions = FakeChatSessions()
    try:
        yield app
    finally:
        settings.security.auth_mode = saved


def _client(app):
    return TestClient(app)


def _register(client, username="alice", password="password123"):
    return client.post("/auth/register", json={"username": username, "password": password})


def test_password_hash_roundtrip():
    h = hash_password("secret123")
    assert h.startswith("pbkdf2$")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "garbage")


def test_register_sets_cookie_and_me(web_app):
    with _client(web_app) as c:
        r = _register(c)
        assert r.status_code == 200
        assert r.json()["username"] == "alice"
        assert "ga_session" in r.cookies
        me = c.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == "alice"


def test_duplicate_register_conflict(web_app):
    with _client(web_app) as c:
        assert _register(c).status_code == 200
        r = _register(c)
        assert r.status_code == 409
        assert r.json()["code"] == "USER_EXISTS"


def test_register_validation(web_app):
    with _client(web_app) as c:
        r = c.post("/auth/register", json={"username": "a", "password": "short"})
        assert r.status_code == 400


def test_unauthorized_blocked(web_app):
    with _client(web_app) as c:
        assert c.get("/auth/me").status_code == 401
        assert c.get("/sessions").status_code == 401
        assert c.post("/chat", json={"message": "hi"}).status_code == 401


def test_login_wrong_and_right(web_app):
    with _client(web_app) as c:
        _register(c, "bob", "password123")
    with _client(web_app) as c:
        r = c.post("/auth/login", json={"username": "bob", "password": "nope1234"})
        assert r.status_code == 401
        assert r.json()["code"] == "INVALID_CREDENTIALS"
        r = c.post("/auth/login", json={"username": "ghost", "password": "password123"})
        assert r.status_code == 401  # 用户不存在同样 401（防枚举）
        r = c.post("/auth/login", json={"username": "bob", "password": "password123"})
        assert r.status_code == 200
        assert c.get("/auth/me").status_code == 200


def test_login_nonexistent_user_runs_dummy_pbkdf2(web_app, monkeypatch):
    """防用户枚举时序差：用户不存在也必须执行一次等价 PBKDF2（dummy hash）。"""
    from general_agent import auth as auth_mod

    called_with: list[str] = []
    real_verify = auth_mod.verify_password

    def spy(password, stored):
        # spy 必须包真实函数，不能让真实登录路径的密码校验失效
        called_with.append(stored)
        return real_verify(password, stored)

    monkeypatch.setattr(auth_mod, "verify_password", spy)
    with _client(web_app) as c:
        _register(c, "bob", "password123")
        called_with.clear()
        r = c.post("/auth/login", json={"username": "ghost", "password": "password123"})
        assert r.status_code == 401
        assert r.json()["code"] == "INVALID_CREDENTIALS"
        assert auth_mod._DUMMY_HASH in called_with  # 不存在路径执行了 dummy PBKDF2
        # spy 包原函数：真实校验路径仍然有效
        ok = c.post("/auth/login", json={"username": "bob", "password": "password123"})
        assert ok.status_code == 200
        bad = c.post("/auth/login", json={"username": "bob", "password": "wrongpass"})
        assert bad.status_code == 401


def test_logout_revokes_session(web_app):
    with _client(web_app) as c:
        _register(c)
        assert c.get("/auth/me").status_code == 200
        assert c.post("/auth/logout").status_code == 200
        assert c.get("/auth/me").status_code == 401


def test_chat_creates_session_and_history_flow(web_app):
    with _client(web_app) as c:
        _register(c)
        resp = c.post("/chat", json={"message": "你好，帮我查一下"})
        assert resp.status_code == 200
        evs = parse_sse(resp.text)
        names = [e[0] for e in evs]
        assert "turn_start" in names
        assert "turn_end" in names
        start_data = next(d for n, d, _ in evs if n == "turn_start")
        sid = start_data["sessionId"]
        assert sid

        lst = c.get("/sessions").json()["sessions"]
        assert len(lst) == 1
        assert lst[0]["sessionId"] == sid
        assert "你好" in lst[0]["title"]

        msgs = c.get(f"/sessions/{sid}/messages").json()["messages"]
        roles = [m["role"] for m in msgs]
        assert "user" in roles and "assistant" in roles

        assert c.patch(f"/sessions/{sid}", json={"title": "改个名字"}).status_code == 200
        assert c.get("/sessions").json()["sessions"][0]["title"] == "改个名字"

        assert c.delete(f"/sessions/{sid}").status_code == 200
        assert c.get("/sessions").json()["sessions"] == []


def test_chat_with_owned_session_id(web_app):
    with _client(web_app) as c:
        _register(c)
        created = c.post("/sessions", json={"title": "手工会话"}).json()
        sid = created["sessionId"]
        resp = c.post("/chat", json={"message": "继续", "sessionId": sid})
        assert resp.status_code == 200
        evs = parse_sse(resp.text)
        start_data = next(d for n, d, _ in evs if n == "turn_start")
        assert start_data["sessionId"] == sid


def test_chat_unknown_session_404(web_app):
    with _client(web_app) as c:
        _register(c)
        r = c.post("/chat", json={"message": "hi", "sessionId": "deadbeef"})
        assert r.status_code == 404


def test_cross_user_access_denied(web_app):
    with _client(web_app) as a:
        _register(a, "alice", "password123")
        resp = a.post("/chat", json={"message": "alice 的秘密对话"})
        sid = next(d for n, d, _ in parse_sse(resp.text) if n == "turn_start")["sessionId"]
    with _client(web_app) as b:
        _register(b, "carol", "password123")
        assert b.get(f"/sessions/{sid}/messages").status_code == 404
        assert b.patch(f"/sessions/{sid}", json={"title": "x"}).status_code == 404
        assert b.delete(f"/sessions/{sid}").status_code == 404
        assert b.get("/sessions").json()["sessions"] == []


# ---------------- 登录失败双维度锁定（ip|username + username） ----------------
def test_login_lock_username_dimension_across_ips():
    """同一用户名在 5 个不同 IP 各失败 1 次后，第 6 个新 IP 也被锁（防分布式撞库）。"""
    store = LoginSessionStore()
    t = 1000.0
    for i in range(5):
        store.record_login_fail(f"10.0.0.{i + 1}", "bob", now=t)
    assert store.login_locked("10.9.9.9", "bob", now=t) is True
    # 其他用户名不受牵连
    assert store.login_locked("10.9.9.9", "carol", now=t) is False
    # 任一参与撞库的 IP 也被锁
    assert store.login_locked("10.0.0.1", "bob", now=t) is True


def test_login_lock_ip_dimension_still_works():
    """原 ip|username 维度行为不变：同 IP 同用户名 5 次失败后锁定。"""
    store = LoginSessionStore()
    for i in range(5):
        store.record_login_fail("10.0.0.1", "bob", now=1000.0 + i)
    assert store.login_locked("10.0.0.1", "bob", now=1005.0) is True


def test_clear_login_fails_clears_both_dimensions():
    """登录成功清两维：username 维度清空后新 IP 可再登录。"""
    store = LoginSessionStore()
    t = 1000.0
    for i in range(5):
        store.record_login_fail(f"10.0.0.{i + 1}", "bob", now=t)
    assert store.login_locked("10.9.9.9", "bob", now=t) is True
    store.clear_login_fails("10.0.0.1", "bob")
    assert store.login_locked("10.9.9.9", "bob", now=t) is False
    assert store.login_locked("10.0.0.1", "bob", now=t) is False


def test_login_lock_username_window_expires():
    """新维度窗口过期自动解锁。"""
    store = LoginSessionStore()
    for i in range(5):
        store.record_login_fail(f"10.0.0.{i + 1}", "bob", now=1000.0)
    assert store.login_locked("10.9.9.9", "bob", now=1600.5) is False
    assert "bob" not in store._login_user_fails


def test_purge_expired_clears_both_fail_dicts():
    """_purge_expired 同时清扫两个失败计数 dict。"""
    store = LoginSessionStore()
    for i in range(3):
        store.record_login_fail(f"10.0.0.{i + 1}", "bob", now=1000.0)
    store.create("u1", "x", now=2000.0)  # 超窗 600s，触发清扫
    assert store._login_fails == {}
    assert store._login_user_fails == {}


# ---------------- 注册按 IP 限流 ----------------
def test_register_rate_settings_defaults():
    from general_agent.config import SecuritySettings

    rr = SecuritySettings().register_rate
    assert rr.enabled is True
    assert rr.rps == pytest.approx(10 / 60)
    assert rr.burst == 5


def test_register_rate_limited_by_ip(web_app):
    """同 IP 超过突发上限：429 RATE_LIMIT，不建用户、不发 cookie。"""
    from general_agent.security import TokenBucket

    web_app.state.register_limiter = TokenBucket(rate=0.0, capacity=2)
    with _client(web_app) as c:
        assert _register(c, "u1").status_code == 200
        assert _register(c, "u2").status_code == 200
        r = _register(c, "u3")
        assert r.status_code == 429
        assert r.json()["code"] == "RATE_LIMIT"
        assert "ga_session" not in r.cookies
    assert len(web_app.state.user_store.users) == 2


def test_register_rate_limit_disabled(web_app):
    """register_limiter 为 None（enabled=False）时不限流。"""
    web_app.state.register_limiter = None
    with _client(web_app) as c:
        for i in range(7):
            assert _register(c, f"user{i}").status_code == 200
    assert len(web_app.state.user_store.users) == 7
