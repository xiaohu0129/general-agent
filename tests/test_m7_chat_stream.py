"""验证：POST /chat（producer/consumer + eventSeq）、GET /stream 续传与通知实时下发。

GET /stream 是无限 SSE 流，TestClient 无法读取（await app 完成才返回 response.start），
故 GET /stream 用后台 uvicorn 真实服务器 + httpx 验证；POST /chat 有界可走 TestClient。
工具链路由 conftest.DemoSkill 驱动（消息含"工具/任务"等触发词时 stub LLM 发起工具调用）。
"""
from __future__ import annotations

import json

import httpx

from conftest import build_test_app, client_for, make_llm_transport, parse_sse, run_server

_TRIGGER_MSG = "请调用工具执行一个演示任务"


def _chat(client, message=_TRIGGER_MSG, headers=None):
    h = {"x-service": "s", "x-env": "dev", "x-user": "u"}
    if headers:
        h.update(headers)
    with client.stream("POST", "/chat", json={"message": message}, headers=h) as r:
        assert r.status_code == 200
        text = "\n".join(r.iter_lines())
    return parse_sse(text)


# ---------------- POST /chat：TestClient 有界流 ----------------
def test_chat_happy_path_events_and_eventseq():
    app, store, broker = build_test_app()
    client = client_for(app)
    evs = _chat(client)
    names = [e for e, _, _ in evs]
    assert names[:1] == ["turn_start"]
    assert "tool_start" in names and "tool_end" in names
    assert names[-1] == "turn_end"
    tool_end = next(d for e, d, _ in evs if e == "tool_end")
    assert tool_end["status"] == "success" and tool_end["result"]["taskId"] == "J123"
    ids = [int(i) for _, _, i in evs if i]
    assert ids == sorted(ids) and ids[0] == 1
    assert all(d.get("eventSeq") for _, d, _ in evs if d)


def test_chat_persists_roles():
    app, store, broker = build_test_app()
    client = client_for(app)
    _chat(client)
    roles = [r["role"] for r in store.rows]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_chat_tool_error_not_kill_turn():
    app, store, broker = build_test_app(
        llm_transport=make_llm_transport(
            tool_name="demo_skill", final_text="查询失败，请稍后重试。", args={"task_id": "bad"}
        ),
    )
    client = client_for(app)
    evs = _chat(client, message="查一下任务 bad 的状态")
    names = [e for e, _, _ in evs]
    tool_end = next(d for e, d, _ in evs if e == "tool_end")
    assert tool_end["status"] == "error" and tool_end["errorCode"] == "NOT_FOUND"
    assert names[-1] == "turn_end"
    assert next(d for e, d, _ in evs if e == "turn_end")["finishReason"] == "stop"
    assert any(r["role"] == "tool" for r in store.rows)


def test_tool_call_forwards_session_id_to_skill():
    app, store, broker = build_test_app()
    client = client_for(app)
    h = {"x-service": "s", "x-env": "dev", "x-user": "u"}
    with client.stream(
        "POST", "/chat", json={"message": _TRIGGER_MSG, "sessionId": "sess-abc"}, headers=h
    ) as r:
        assert r.status_code == 200
        text = "\n".join(r.iter_lines())
    evs = parse_sse(text)
    tool_end = next(d for e, d, _ in evs if e == "tool_end")
    assert tool_end["status"] == "success"
    assert tool_end["result"]["sessionId"] == "sess-abc"


def test_notify_buffers_when_no_subscriber():
    app, store, broker = build_test_app()
    client = client_for(app)
    resp = client.post("/internal/notify", json={"sessionId": "S2", "taskId": "J2", "status": "FAILED"})
    assert resp.status_code == 200 and resp.json()["status"] == "accepted"
    assert broker.active_subscribers("S2") == 0
    replay = broker.replay("S2", 0)
    assert len(replay) == 1 and replay[0]["event"] == "notification"


# ---------------- GET /stream：后台 uvicorn 真实服务器 ----------------
def test_stream_replays_buffered_events():
    app, store, broker = build_test_app()
    with run_server(app) as base:
        # 先 POST 一轮，事件入 broker ring
        with httpx.Client(base_url=base, timeout=5) as c:
            with c.stream("POST", "/chat", json={"message": _TRIGGER_MSG},
                          headers={"x-service": "s", "x-env": "dev", "x-user": "u"}) as r:
                _ = "\n".join(r.iter_lines())
            # GET /stream 续传重放
            with c.stream("GET", "/stream?sessionId=s:dev:u&lastEventId=0") as s:
                assert s.status_code == 200
                lines = []
                for line in s.iter_lines():
                    lines.append(line)
                    if line.startswith("event:") and "turn_end" in line:
                        break
    replayed = parse_sse("\n".join(lines))
    rnames = [e for e, _, _ in replayed if e]
    assert "turn_start" in rnames and "turn_end" in rnames
    rids = [int(i) for _, _, i in replayed if i]
    assert min(rids) == 1


def test_chat_heartbeat_when_idle():
    app, store, broker = build_test_app()  # heartbeat=0.5s（conftest env）
    with run_server(app) as base:
        with httpx.Client(base_url=base, timeout=5) as c:
            with c.stream("GET", "/stream?sessionId=HB1&lastEventId=0") as s:
                assert s.status_code == 200
                saw = False
                for line in s.iter_lines():
                    if line.startswith(": heartbeat"):
                        saw = True
                        break
    assert saw, "expected heartbeat comment line when idle"


def test_stream_receives_notification_live():
    """GET /stream 实时接收 notification 通知（续传 + 不阻塞 POST 路径）。"""
    app, store, broker = build_test_app()
    with run_server(app) as base:
        with httpx.Client(base_url=base, timeout=5) as c:
            with c.stream("GET", "/stream?sessionId=S1&lastEventId=0") as s:
                assert s.status_code == 200
                # 后台线程延迟投递通知
                import threading

                def deliver():
                    _time.sleep(0.3)
                    c.post("/internal/notify", json={
                        "sessionId": "S1", "taskId": "J1", "status": "SUCCESS", "message": "done"
                    })

                t = threading.Thread(target=deliver)
                t.start()
                text = ""
                for line in s.iter_lines():
                    text += line + "\n"
                    # 等 notification 的 data 行到达（含 taskId）再断
                    if line.startswith("data:") and "taskId" in line:
                        break
                t.join()
    evs = parse_sse(text)
    note = next(d for e, d, _ in evs if e == "notification")
    assert note["taskId"] == "J1" and note["status"] == "SUCCESS"


import time as _time  # noqa: E402


class _BoomStore:
    """load_messages 抛异常，验证 turn_start 先到 + error 兜底（不崩）。"""
    async def load_messages(self, *a, **k):
        raise RuntimeError("db down")
    async def append_message(self, *a, **k):
        return 0


def test_chat_load_history_failure_yields_turn_start_then_error():
    from conftest import build_test_app, client_for, parse_sse
    app, store, broker = build_test_app(store=_BoomStore())
    client = client_for(app)
    with client.stream("POST", "/chat", json={"message": "hi"},
                       headers={"x-service": "s", "x-env": "dev", "x-user": "u"}) as r:
        assert r.status_code == 200
        text = "\n".join(r.iter_lines())
    evs = parse_sse(text)
    names = [e for e, _, _ in evs]
    assert names[0] == "turn_start"
    assert names[-1] == "error"
