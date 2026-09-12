"""U3：全鉴权模式会话归属统一到 MySQL owner（claim_if_absent/get_owned_scoped）。

conftest 默认 disabled 模式 + x-* 头；内存 FakeChatSessions 承载 owner 行。
方法级验证 claim 原子语义（fake 模拟 INSERT IGNORE），API 级验证四种入口情形中的
越权拒绝、首次认领、升级前历史会话认领与 /stream 统一归属校验。
"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from conftest import (
    FakeChatSessions,
    DemoSkill,
    build_test_app,
    make_llm_transport,
    parse_sse,
    run_server,
)
from general_agent.skill_router.router import PATH_CLARIFY, RouteDecision


# ---------------- 方法级（3.1）：内存 fake 的 claim/scoped 语义 ----------------
async def test_claim_first_succeeds_and_is_idempotent():
    sessions = FakeChatSessions()
    row = await sessions.claim_if_absent("sid1", "default", "dev", "alice", "第一个标题")
    assert row is not None
    assert row["session_id"] == "sid1"
    assert row["uid"] == "alice"
    assert row["service"] == "default"
    assert row["env"] == "dev"
    assert row["title"] == "第一个标题"

    # 幂等：同身份重复 claim 返回同一行，不新建、不改标题
    again = await sessions.claim_if_absent("sid1", "default", "dev", "alice", "别的标题")
    assert again is row
    assert again["title"] == "第一个标题"
    assert len(sessions.sessions) == 1


async def test_claim_second_identity_returns_none_and_keeps_owner():
    sessions = FakeChatSessions()
    assert await sessions.claim_if_absent("sid1", "default", "dev", "alice", "t") is not None
    # 第二身份（user 维度不同）claim 同一 id -> None
    assert await sessions.claim_if_absent("sid1", "default", "dev", "bob", "t") is None
    # service/env 维度不同同样被拒
    assert await sessions.claim_if_absent("sid1", "other", "dev", "alice", "t") is None
    assert await sessions.claim_if_absent("sid1", "default", "prod", "alice", "t") is None
    owner = sessions.sessions["sid1"]
    assert owner["uid"] == "alice" and owner["service"] == "default" and owner["env"] == "dev"
    assert len(sessions.sessions) == 1


async def test_get_owned_scoped_matches_four_tuple():
    sessions = FakeChatSessions()
    await sessions.claim_if_absent("sid1", "default", "dev", "alice", "t")
    hit = await sessions.get_owned_scoped("sid1", "default", "dev", "alice")
    assert hit is not None and hit["uid"] == "alice"
    assert await sessions.get_owned_scoped("sid1", "default", "dev", "bob") is None
    assert await sessions.get_owned_scoped("sid1", "other", "dev", "alice") is None
    assert await sessions.get_owned_scoped("sid1", "default", "prod", "alice") is None
    assert await sessions.get_owned_scoped("missing", "default", "dev", "alice") is None


# ---------------- API 级（3.3） ----------------
class _SpyTransport:
    """记录 LLM HTTP 调用次数，委托给内嵌 stub transport。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        return self.inner.handle_request(request)

    async def handle_async_request(self, request):
        self.calls += 1
        return await self.inner.handle_async_request(request)

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)

    async def __aenter__(self):
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self.inner.__aexit__(*exc)


def _post_chat(app, payload, headers):
    client = TestClient(app)
    with client.stream("POST", "/chat", json=payload, headers=headers) as r:
        status = r.status_code
        text = "".join(r.iter_text())
    return status, parse_sse(text)


def test_explicit_sid_first_writer_runs_turn():
    """情形 3：api 模式 + 显式新 sessionId -> claim 成功，正常执行轮次。"""
    app, store, broker = build_test_app()
    sid = "explicit-sid-1"
    status, evs = _post_chat(
        app, {"message": "请调用工具执行一个演示任务", "sessionId": sid},
        {"x-service": "default", "x-env": "dev", "x-user": "alice"},
    )
    assert status == 200
    names = [e for e, _, _ in evs]
    assert names[0] == "turn_start"
    assert names[-1] == "turn_end"
    start = next(d for e, d, _ in evs if e == "turn_start")
    assert start["sessionId"] == sid
    owner = app.state.chat_sessions.sessions[sid]
    assert owner["uid"] == "alice"
    assert any(r["user_id"] == "alice" and r["session_id"] == sid for r in store.rows)


def test_other_identity_post_404_no_history_read_no_write_no_model():
    """跨身份 POST 已归属会话：404，不写消息、不调模型。"""
    spy = _SpyTransport(make_llm_transport())
    app, store, broker = build_test_app(llm_transport=spy)
    sid = "explicit-sid-2"
    ha = {"x-service": "default", "x-env": "dev", "x-user": "alice"}
    status, _ = _post_chat(
        app, {"message": "请调用工具执行一个演示任务", "sessionId": sid}, ha
    )
    assert status == 200
    assert spy.calls >= 1  # alice 的轮次确实调了模型

    # bob（user 维度不同）
    before = spy.calls
    client = TestClient(app)
    r = client.post(
        "/chat", json={"message": "窃听", "sessionId": sid},
        headers={"x-service": "default", "x-env": "dev", "x-user": "bob"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "SESSION_NOT_FOUND"
    assert spy.calls == before  # 越权请求零模型调用
    assert not [r for r in store.rows if r["user_id"] == "bob"]  # 无任何消息写入

    # alice 换 service/env 维度同样被拒
    for headers in (
        {"x-service": "other", "x-env": "dev", "x-user": "alice"},
        {"x-service": "default", "x-env": "prod", "x-user": "alice"},
    ):
        r2 = client.post("/chat", json={"message": "x", "sessionId": sid}, headers=headers)
        assert r2.status_code == 404
    assert spy.calls == before
    assert not [r for r in store.rows if r["session_id"] == sid and r["user_id"] != "alice"]


def test_stream_ownership_check_all_modes():
    """GET /stream：非 owner 404（建流前），owner 可订阅。"""
    app, store, broker = build_test_app()
    sid = "stream-sid-1"
    ha = {"x-service": "default", "x-env": "dev", "x-user": "alice"}
    status, _ = _post_chat(
        app, {"message": "请调用工具执行一个演示任务", "sessionId": sid}, ha
    )
    assert status == 200

    client = TestClient(app)
    assert client.get("/stream", params={"sessionId": sid},
                      headers={"x-user": "bob"}).status_code == 404
    assert client.get("/stream", params={"sessionId": sid},
                      headers={"x-service": "other", "x-user": "alice"}).status_code == 404
    assert client.get("/stream", params={"sessionId": sid},
                      headers={"x-env": "prod", "x-user": "alice"}).status_code == 404
    assert client.get("/stream", params={"sessionId": "never-existed"},
                      headers=ha).status_code == 404

    # owner：真实服务器可订阅（收到心跳即说明建流成功）
    with run_server(app) as base:
        with httpx.Client(base_url=base, timeout=5) as c:
            with c.stream("GET", "/stream", params={"sessionId": sid, "lastEventId": 0},
                          headers=ha) as s:
                assert s.status_code == 200
                saw_heartbeat = False
                for line in s.iter_lines():
                    if line.startswith(":"):
                        saw_heartbeat = True
                        break
                assert saw_heartbeat


def test_implicit_session_key_claimed_and_stable():
    """情形 4：无 sessionId -> session_key(service,env,user) 稳定 id 并被身份认领。"""
    app, store, broker = build_test_app()
    headers = {"x-service": "svc", "x-env": "staging", "x-user": "carol"}
    status, evs = _post_chat(app, {"message": "请调用工具执行一个演示任务"}, headers)
    assert status == 200
    sid = next(d for e, d, _ in evs if e == "turn_start")["sessionId"]
    assert sid == "svc:staging:carol"
    owner = app.state.chat_sessions.sessions[sid]
    assert (owner["service"], owner["env"], owner["uid"]) == ("svc", "staging", "carol")

    # 第二次 POST 同身份：幂等 claim，继续同一会话
    status, evs = _post_chat(app, {"message": "再来一轮"}, headers)
    assert status == 200
    assert next(d for e, d, _ in evs if e == "turn_start")["sessionId"] == sid
    assert len(app.state.chat_sessions.sessions) == 1


class _RecordingRouter:
    def __init__(self, decision):
        self.decision = decision
        self.last_history = None

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
        self.last_history = history
        return self.decision


def _seed_legacy_rows(store, sid):
    """升级前历史：只有消息行（service/env/user 与原身份一致），无 owner 行。"""
    for i, (turn_id, role, content) in enumerate((
        ("t1", "user", "升级前的旧问题"),
        ("t1", "assistant", "升级前的旧回答"),
    )):
        store._seq += 1
        store.rows.append({
            "id": store._seq,
            "service": "default", "env": "dev", "user_id": "alice",
            "session_id": sid, "turn_id": turn_id, "role": role, "content": content,
            "tool_calls": None, "tool_call_id": None, "meta": None,
            "content_ref": None, "content_size": None, "content_kind": None,
        })


def test_legacy_unowned_session_claimed_with_history_loaded():
    """升级认领：预置消息行但无 owner 行，原身份 POST 后认领成功且历史可载入。"""
    app, store, broker = build_test_app(skill=DemoSkill())
    sid = "legacy-sid-1"
    _seed_legacy_rows(store, sid)
    assert sid not in app.state.chat_sessions.sessions

    rec = _RecordingRouter(
        RouteDecision(PATH_CLARIFY, [], clarify_text="好的", details={"categories": ["x"]})
    )
    app.state.skill_router = rec

    status, evs = _post_chat(
        app, {"message": "继续", "sessionId": sid},
        {"x-service": "default", "x-env": "dev", "x-user": "alice"},
    )
    assert status == 200
    assert [e for e, _, _ in evs][-1] == "turn_end"

    owner = app.state.chat_sessions.sessions[sid]
    assert (owner["service"], owner["env"], owner["uid"]) == ("default", "dev", "alice")
    # claim 先于路由历史读取：路由确实拿到了升级前历史
    assert rec.last_history is not None
    assert any("升级前的旧问题" in (h["content"] or "") for h in rec.last_history)

    # 认领后他人不可再用
    client = TestClient(app)
    r = client.post("/chat", json={"message": "x", "sessionId": sid},
                    headers={"x-service": "default", "x-env": "dev", "x-user": "bob"})
    assert r.status_code == 404
