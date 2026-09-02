"""产物下载端点测试：本人可下载、越权 404、未登录 401。"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from general_agent.blob_store import LocalBlobStore
from general_agent.config import get_settings
from general_agent.message_store import MessageStore

from conftest import build_test_app
from test_auth_web import FakeChatSessions, FakeUserStore


class _Cursor:
    def __init__(self, row):
        self.row = row

    async def execute(self, *a, **k):
        pass

    async def fetchone(self):
        return self.row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, row):
        self.row = row

    def cursor(self, *a, **k):
        return _Cursor(self.row)


class ArtifactPool:
    def __init__(self, row):
        self.row = row

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self.row)


@pytest.fixture
def web_app():
    settings = get_settings()
    saved = settings.security.auth_mode
    settings.security.auth_mode = "session"
    app, _, _ = build_test_app()
    app.state.user_store = FakeUserStore()
    app.state.chat_sessions = FakeChatSessions()
    try:
        yield app
    finally:
        settings.security.auth_mode = saved


def _register(c, username="alice"):
    return c.post("/auth/register", json={"username": username, "password": "password123"})


async def _seed_artifact(app, uid, sid, tmp_path, body=b'{"report":1}'):
    blob = LocalBlobStore(root=tmp_path)
    ref = await blob.put((uid, sid, "t1"), body, ext=".json")
    pool = ArtifactPool({"content_ref": ref, "content_kind": "json"})
    app.state.message_store = MessageStore(pool=pool, blob_store=blob)
    app.state.chat_sessions.sessions[sid] = {
        "session_id": sid, "uid": uid, "service": "web", "env": "dev", "title": "t"
    }


def test_download_own_artifact(web_app, tmp_path):
    import asyncio

    with TestClient(web_app) as c:
        _register(c)
        uid = c.get("/auth/me").json()["uid"]
        sid = "sid-art"
        asyncio.get_event_loop().run_until_complete(_seed_artifact(web_app, uid, sid, tmp_path))
        r = c.get(f"/sessions/{sid}/artifacts/7")
        assert r.status_code == 200
        assert r.content == b'{"report":1}'
        assert "application/json" in r.headers["content-type"]


def test_download_cross_user_404(web_app, tmp_path):
    import asyncio

    with TestClient(web_app) as a:
        _register(a, "alice")
        uid = a.get("/auth/me").json()["uid"]
        sid = "sid-art"
        asyncio.get_event_loop().run_until_complete(_seed_artifact(web_app, uid, sid, tmp_path))
    with TestClient(web_app) as b:
        _register(b, "carol")
        r = b.get(f"/sessions/{sid}/artifacts/7")
        assert r.status_code == 404


def test_download_unauthorized(web_app):
    with TestClient(web_app) as c:
        r = c.get("/sessions/whatever/artifacts/1")
        assert r.status_code == 401
