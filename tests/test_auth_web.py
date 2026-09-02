"""Web 认证（Cookie+Session）+ 多会话管理测试。

不依赖外部服务：FakeUserStore/FakeChatSessionStore 内存替身 + stub LLM + FakeStore 消息。
auth_mode 在测试内切换为 session（conftest 默认 disabled），结束后恢复。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from general_agent.auth import hash_password, verify_password
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

    async def get_by_uid(self, uid):
        return next((u for u in self.users.values() if u["uid"] == uid), None)


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
