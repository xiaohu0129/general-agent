"""删除会话级联清理外置 blob 产物测试。"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from general_agent.blob_store import LocalBlobStore
from general_agent.config import get_settings

from conftest import build_test_app
from test_auth_web import FakeChatSessions, FakeUserStore


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


def test_delete_session_removes_blob(web_app, tmp_path):
    blob = LocalBlobStore(root=tmp_path)
    web_app.state.blob_store = blob
    with TestClient(web_app) as c:
        c.post("/auth/register", json={"username": "alice", "password": "password123"})
        uid = c.get("/auth/me").json()["uid"]
        sid = "sid-cascade"
        web_app.state.chat_sessions.sessions[sid] = {
            "session_id": sid, "uid": uid, "service": "web", "env": "dev", "title": "t"
        }
        ref = asyncio.get_event_loop().run_until_complete(
            blob.put((uid, sid, "t1"), b'{"big":1}', ext=".json")
        )
        path = blob.local_path(ref)
        assert path.exists()
        # 在消息存储里登记该外置消息
        web_app.state.message_store.rows.append(
            {"id": 1, "role": "tool", "content": "head", "tool_calls": None,
             "tool_call_id": "c1", "content_ref": ref, "content_size": 9, "content_kind": "json"}
        )
        r = c.delete(f"/sessions/{sid}")
        assert r.status_code == 200
        assert not path.exists()  # blob 文件已级联清理
