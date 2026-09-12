"""U8：POST /chat 与 POST /internal/notify 入参边界约束（400 VALIDATION，先于一切副作用）。"""
from __future__ import annotations

import httpx

from conftest import FakeStore, build_test_app, client_for, make_llm_transport

_CHAT_HEADERS = {"x-service": "s", "x-env": "dev", "x-user": "u"}
# disabled 模式无 sessionId 时的隐式会话 id（service:env:user）
_IMPLICIT_SID = "s:dev:u"


def _spy_store():
    """返回 (store, appended)：append_message 被调时 appended 计数 +1。"""
    store = FakeStore()
    appended = []
    orig = store.append_message

    async def spy(*a, **k):
        appended.append(1)
        return await orig(*a, **k)

    store.append_message = spy
    return store, appended


def _counting_transport():
    """返回 (transport, calls)：LLM 出站请求计数。"""
    inner = make_llm_transport()
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return inner.handle_request(request)

    return httpx.MockTransport(handler), calls


# ---------------- 8.1 POST /chat ----------------
def test_chat_empty_message_rejected_without_side_effects():
    store, appended = _spy_store()
    transport, calls = _counting_transport()
    app, _, broker = build_test_app(llm_transport=transport, store=store)
    client = client_for(app)
    r = client.post("/chat", json={"message": ""}, headers=_CHAT_HEADERS)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert appended == [] and store.rows == []
    assert calls == []
    assert broker.replay(_IMPLICIT_SID, 0) == []
    assert app.state.chat_sessions.sessions == {}


def test_chat_blank_message_rejected_without_side_effects():
    store, appended = _spy_store()
    transport, calls = _counting_transport()
    app, _, broker = build_test_app(llm_transport=transport, store=store)
    client = client_for(app)
    for blank in (" ", "\t", "\n", "  \t\n  "):
        r = client.post("/chat", json={"message": blank}, headers=_CHAT_HEADERS)
        assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert appended == [] and store.rows == []
    assert calls == []
    assert broker.replay(_IMPLICIT_SID, 0) == []
    assert app.state.chat_sessions.sessions == {}


def test_chat_overlong_message_rejected_without_side_effects():
    store, appended = _spy_store()
    transport, calls = _counting_transport()
    app, _, broker = build_test_app(llm_transport=transport, store=store)
    client = client_for(app)
    r = client.post("/chat", json={"message": "x" * 8001}, headers=_CHAT_HEADERS)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert appended == [] and store.rows == []
    assert calls == []
    assert broker.replay(_IMPLICIT_SID, 0) == []
    assert app.state.chat_sessions.sessions == {}


def test_chat_message_strips_outer_whitespace_for_boundary():
    """strip 后超长（内部 8001 + 外围空白）同样拒绝。"""
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/chat", json={"message": " " + "x" * 8001 + " "}, headers=_CHAT_HEADERS)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert store.rows == []


def _consume_chat(client, message, extra=None):
    body = {"message": message}
    if extra:
        body.update(extra)
    with client.stream("POST", "/chat", json=body, headers=_CHAT_HEADERS) as r:
        assert r.status_code == 200
        return "\n".join(r.iter_lines())


def test_chat_boundary_lengths_accepted():
    app, store, broker = build_test_app()
    client = client_for(app)
    _consume_chat(client, "x")
    _consume_chat(client, "x" * 8000)
    assert len(store.rows) >= 2


def test_chat_message_surrounded_by_whitespace_accepted():
    """strip 后恰好 1 字符放行（外围空白不导致空消息拒绝）。"""
    app, store, broker = build_test_app()
    client = client_for(app)
    _consume_chat(client, "  x  ")


def test_chat_clarify_selection_value_too_long_rejected():
    store, appended = _spy_store()
    transport, calls = _counting_transport()
    app, _, broker = build_test_app(llm_transport=transport, store=store)
    client = client_for(app)
    r = client.post(
        "/chat",
        json={"message": "hi", "clarify_selection": {"value": "v" * 201}},
        headers=_CHAT_HEADERS,
    )
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert appended == [] and calls == []
    assert app.state.chat_sessions.sessions == {}


def test_chat_clarify_selection_value_boundary_accepted():
    app, store, broker = build_test_app()
    client = client_for(app)
    _consume_chat(client, "hi", extra={"clarify_selection": {"value": "v" * 200}})


# ---------------- 8.2 POST /internal/notify ----------------
def _notify_body(**over):
    body = {"sessionId": "S", "taskId": "T", "status": "SUCCESS"}
    body.update(over)
    return body


def test_notify_status_too_long_rejected_without_event():
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/internal/notify", json=_notify_body(status="s" * 33))
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert "eventSeq" not in r.json()
    assert broker.replay("S", 0) == []


def test_notify_message_too_long_rejected_without_event():
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/internal/notify", json=_notify_body(message="m" * 2001))
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert broker.replay("S", 0) == []


def test_notify_missing_task_id_rejected_without_event():
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/internal/notify", json={"sessionId": "S", "status": "SUCCESS"})
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert broker.replay("S", 0) == []


def test_notify_blank_status_rejected_without_event():
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/internal/notify", json=_notify_body(status=""))
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert broker.replay("S", 0) == []


def test_notify_blank_session_id_rejected_without_event():
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/internal/notify", json=_notify_body(sessionId=""))
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"
    assert broker.replay("", 0) == []


def test_notify_boundary_lengths_accepted():
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/internal/notify", json=_notify_body(status="s" * 32, message="m" * 2000))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    r2 = client.post("/internal/notify", json=_notify_body(taskId="T2"))
    assert r2.status_code == 200
