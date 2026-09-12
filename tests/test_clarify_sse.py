"""4b.1/4b.2：结构化澄清的协议下发层 e2e。

覆盖：
- 澄清轮 SSE 事件流 turn_start -> turn_delta -> clarify -> turn_end，
  clarify data 含 question/options（带前缀 value 原样）与公共 turnId/traceId/eventSeq，
  该轮无 tool_* 事件；options 为空时不下发 clarify（等价旧纯文本澄清）。
- clarify 事件经 broker 编号入 ring，真实服务器 GET /stream + Last-Event-ID 断线重放含该事件。
- 历史消息 API（session 登录模式）回放透出 options/selected，keyset 分页边界不重不漏，
  存量 meta=NULL 行不报错且 options/selected 缺省。
- 真实 HTTP 入口：clarify_selection body 装配进路由（selection 透传、label 落 user 行、
  带前缀 value 不进任何 content）。
纯内存/fake/真实 uvicorn 本机端口，不依赖 MySQL/外部网络。
"""
from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import DemoSkill, build_test_app, parse_sse, run_server
from general_agent.config import get_settings
from general_agent.skill_router.router import (
    PATH_CLARIFY,
    PATH_OPTION,
    RouteDecision,
)

_OPTIONS = [
    {"label": "退款", "value": "category:refund"},
    {"label": "开具发票", "value": "skill:invoice_skill"},
]


class _ScriptRouter:
    """确定性路由替身：记录 selection 入参，固定返回注入决策。"""

    def __init__(self, decision):
        self.decision = decision
        self.last_selection = object()

    async def route(self, message, candidates, *, history=None, prev_clarify=None, selection=None):
        self.last_selection = selection
        return self.decision


def _clarify_decision(text="你想办哪类业务？", options=None):
    return RouteDecision(
        PATH_CLARIFY,
        [],
        clarify_text=text,
        clarify_options=_OPTIONS if options is None else options,
        details={"categories": ["refund", "billing"]},
    )


def _post_chat(app, payload):
    client = TestClient(app)
    with client.stream("POST", "/chat", json=payload) as r:
        text = "".join(chunk for chunk in r.iter_text())
    return parse_sse(text)


# ---------------- 澄清轮 SSE 事件流 ----------------
class TestClarifySseEvent:
    def test_clarify_event_after_delta_with_question_options_and_public_ids(self):
        app, store, broker = build_test_app()
        app.state.skill_router = _ScriptRouter(_clarify_decision())
        evs = _post_chat(app, {"message": "帮个忙"})

        names = [e for e, _, _ in evs]
        assert names == ["turn_start", "turn_delta", "clarify", "turn_end"]
        # 该轮无工具事件
        assert not {"tool_start", "tool_end"} & set(names)

        start = next(d for e, d, _ in evs if e == "turn_start")
        delta = next(d for e, d, _ in evs if e == "turn_delta")
        clarify = next(d for e, d, _ in evs if e == "clarify")
        end = next(d for e, d, _ in evs if e == "turn_end")

        assert delta["content"] == "你想办哪类业务？"
        assert clarify["question"] == "你想办哪类业务？"
        assert clarify["options"] == _OPTIONS
        # 公共字段与同轮一致
        assert clarify["turnId"] == start["turnId"]
        assert clarify["traceId"] == start["traceId"]
        assert clarify["traceId"]
        assert isinstance(clarify["eventSeq"], int) and clarify["eventSeq"] >= 1
        # 事件流 eventSeq 单调
        seqs = [d["eventSeq"] for _, d, _ in evs if d and "eventSeq" in d]
        assert seqs == sorted(seqs)
        assert end["finishReason"] == "stop"

    def test_empty_options_clarify_emits_no_clarify_event(self):
        app, store, broker = build_test_app()
        app.state.skill_router = _ScriptRouter(
            RouteDecision(
                PATH_CLARIFY, [],
                clarify_text="再说清楚点？",
                clarify_options=None,
                details={"categories": ["refund"]},
            )
        )
        evs = _post_chat(app, {"message": "嗯嗯"})
        names = [e for e, _, _ in evs]
        assert "clarify" not in names
        assert names == ["turn_start", "turn_delta", "turn_end"]

    def test_explicit_empty_options_list_emits_no_clarify_event(self):
        app, store, broker = build_test_app()
        app.state.skill_router = _ScriptRouter(_clarify_decision(options=[]))
        evs = _post_chat(app, {"message": "嗯嗯"})
        assert "clarify" not in [e for e, _, _ in evs]


# ---------------- ring buffer 断线重放（真实服务器） ----------------
def test_clarify_event_replayed_after_last_event_id():
    app, store, broker = build_test_app()
    app.state.skill_router = _ScriptRouter(_clarify_decision())
    with run_server(app) as base:
        with httpx.Client(base_url=base, timeout=5) as c:
            headers = {"x-service": "s", "x-env": "dev", "x-user": "u"}
            with c.stream(
                "POST", "/chat",
                json={"message": "帮个忙", "sessionId": "s:dev:u"},
                headers=headers,
            ) as r:
                assert r.status_code == 200
                _ = "\n".join(r.iter_lines())
            # 从 seq=1 之后重连：应重放 turn_delta(2)/clarify(3)/turn_end(4)
            with c.stream(
                "GET", "/stream?sessionId=s:dev:u&lastEventId=1", headers=headers
            ) as s:
                assert s.status_code == 200
                lines = []
                for line in s.iter_lines():
                    lines.append(line)
                    if line.startswith("event:") and "turn_end" in line:
                        break
    replayed = parse_sse("\n".join(lines))
    names = [e for e, _, _ in replayed if e]
    assert names == ["turn_delta", "clarify", "turn_end"]
    clarify = next(d for e, d, _ in replayed if e == "clarify")
    assert clarify["question"] == "你想办哪类业务？"
    assert clarify["options"] == _OPTIONS
    assert isinstance(clarify["eventSeq"], int)
    rids = [int(i) for _, _, i in replayed if i]
    assert rids == [2, 3, 4]


# ---------------- 历史消息 API（session 登录模式） ----------------
class _FakeUserStore:
    def __init__(self):
        self.users: dict[str, dict] = {}

    async def create_user(self, username, password_hash):
        uid = uuid.uuid4().hex
        self.users[username] = {"uid": uid, "username": username, "password_hash": password_hash}
        return uid

    async def get_by_username(self, username):
        return self.users.get(username)

    async def get_by_uid(self, uid):
        return next((u for u in self.users.values() if u["uid"] == uid), None)


class _FakeChatSessions:
    def __init__(self):
        self.sessions: dict[str, dict] = {}

    async def create(self, uid, service, env, title, session_id=None):
        sid = session_id or uuid.uuid4().hex
        self.sessions[sid] = {"session_id": sid, "uid": uid, "service": service, "env": env, "title": title}
        return {"sessionId": sid, "title": title, "createdAt": None, "updatedAt": None}

    async def list_for_user(self, uid, limit=50):
        return [
            {"sessionId": s["session_id"], "title": s["title"], "createdAt": None, "updatedAt": None}
            for s in self.sessions.values() if s["uid"] == uid
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


def _seed(store, scope, turn_id, role, content, meta=None):
    """向 FakeStore 直接塞行；meta 默认 None（模拟存量 NULL）。id 与 store._seq 同源，
    避免后续 /chat append 的行 id 撞号。"""
    service, env, uid, sid = scope
    store._seq += 1
    store.rows.append(
        {
            "id": store._seq,
            "service": service,
            "env": env,
            "user_id": uid,
            "session_id": sid,
            "turn_id": turn_id,
            "role": role,
            "content": content,
            "tool_calls": None,
            "tool_call_id": None,
            "meta": meta,
            "content_ref": None,
            "content_size": None,
            "content_kind": None,
        }
    )


@pytest.fixture
def web_app():
    settings = get_settings()
    saved = settings.security.auth_mode
    settings.security.auth_mode = "session"
    app, store, broker = build_test_app(skill=DemoSkill())
    app.state.user_store = _FakeUserStore()
    app.state.chat_sessions = _FakeChatSessions()
    try:
        yield app, store
    finally:
        settings.security.auth_mode = saved


class TestHistoryMessagesOptions:
    def test_options_and_selected_exposed_across_keyset_pages(self, web_app):
        app, store = web_app
        with TestClient(app) as c:
            c.post("/auth/register", json={"username": "alice", "password": "password123"})
            uid = app.state.user_store.users["alice"]["uid"]
            sid = c.post("/sessions", json={"title": "t"}).json()["sessionId"]
            scope = ("web", "dev", uid, sid)

            # 4 条旧行（含 meta=NULL 存量行），加澄清轮 2 行共 6 行，恰分两页
            _seed(store, scope, "old1", "user", "旧问题1")
            _seed(store, scope, "old1", "assistant", "旧回答1")
            _seed(store, scope, "old2", "user", "旧问题2")
            _seed(store, scope, "old2", "assistant", "旧回答2")

            # 第一轮：带 options 的澄清轮落库（最新行）
            app.state.skill_router = _ScriptRouter(_clarify_decision())
            resp = c.post("/chat", json={"message": "帮个忙", "sessionId": sid})
            assert resp.status_code == 200
            evs = parse_sse(resp.text)
            assert "clarify" in [e for e, _, _ in evs]
            total = count_messages(store, scope)

            # 两页拉取：options 在边界处不重不漏
            p1 = c.get(f"/sessions/{sid}/messages", params={"limit": 3}).json()
            assert p1["hasMore"] is True and p1["nextCursor"]
            p2 = c.get(
                f"/sessions/{sid}/messages",
                params={"limit": 3, "before": p1["nextCursor"]},
            ).json()
            ids1 = [m["messageId"] for m in p1["messages"]]
            ids2 = [m["messageId"] for m in p2["messages"]]
            assert not (set(ids1) & set(ids2))
            assert sorted(ids1 + ids2) == list(range(1, total + 1))
            with_opts = [
                m for pg in (p1, p2) for m in pg["messages"] if m.get("options")
            ]
            assert len(with_opts) == 1
            assert with_opts[0]["options"] == _OPTIONS
            assert with_opts[0]["selected"] is None

            # 第二轮：点选收窄 -> selected 回写，label 落 user 行
            app.state.skill_router = _ScriptRouter(
                RouteDecision(
                    PATH_OPTION, [DemoSkill()],
                    details={
                        "clarify_outcome": "option",
                        "clarify_selection": "category:refund",
                    },
                )
            )
            resp = c.post(
                "/chat",
                json={
                    "message": "退款",
                    "sessionId": sid,
                    "clarify_selection": {"value": "category:refund"},
                },
            )
            assert resp.status_code == 200
            assert app.state.skill_router.last_selection == "category:refund"

            msgs = c.get(f"/sessions/{sid}/messages", params={"limit": 50}).json()["messages"]
            clarify = next(m for m in msgs if m.get("options"))
            assert clarify["options"] == _OPTIONS
            assert clarify["selected"] == "category:refund"
            # 带前缀 value 原样透出在 options/selected，但绝不进任何 content
            user_msgs = [m for m in msgs if m["role"] == "user"]
            assert user_msgs[-1]["content"] == "退款"
            assert all("category:refund" not in (m["content"] or "") for m in msgs)

    def test_legacy_null_meta_rows_expose_none_without_error(self, web_app):
        app, store = web_app
        with TestClient(app) as c:
            c.post("/auth/register", json={"username": "bob", "password": "password123"})
            uid = app.state.user_store.users["bob"]["uid"]
            sid = c.post("/sessions", json={"title": "t"}).json()["sessionId"]
            scope = ("web", "dev", uid, sid)
            _seed(store, scope, "t1", "user", "老问题")
            _seed(store, scope, "t1", "assistant", "老回答")  # meta=None

            r = c.get(f"/sessions/{sid}/messages")
            assert r.status_code == 200
            msgs = r.json()["messages"]
            assert len(msgs) == 2
            assert all(m["options"] is None and m["selected"] is None for m in msgs)


def count_messages(store, scope):
    service, env, uid, sid = scope
    return len(store._scoped(service, env, uid, sid))


# ---------------- 真实 HTTP 入口：clarify_selection 装配 ----------------
def test_real_http_clarify_selection_assembles_into_routing():
    app, store, broker = build_test_app(skill=DemoSkill())
    scope = ("s", "dev", "u", "s:dev:u")
    # 预置上一轮澄清 meta（含合法 option）
    _seed(
        store, scope, "Tn", "assistant", "你想查订单还是退款？",
        meta={
            "kind": "clarify",
            "categories": ["order", "refund"],
            "options": [{"label": "退款", "value": "category:refund"}],
        },
    )
    rec = _ScriptRouter(
        RouteDecision(
            PATH_OPTION, [DemoSkill()],
            details={
                "clarify_outcome": "option",
                "clarify_selection": "category:refund",
            },
        )
    )
    app.state.skill_router = rec

    with run_server(app) as base:
        with httpx.Client(base_url=base, timeout=5) as c:
            headers = {"x-service": "s", "x-env": "dev", "x-user": "u"}
            with c.stream(
                "POST", "/chat",
                json={
                    "message": "退款",
                    "sessionId": "s:dev:u",
                    "clarify_selection": {"value": "category:refund"},
                },
                headers=headers,
            ) as r:
                assert r.status_code == 200
                text = "\n".join(r.iter_lines())

    evs = parse_sse(text)
    assert evs[-1][0] == "turn_end"
    assert rec.last_selection == "category:refund"
    user_rows = [r for r in store.rows if r["role"] == "user"]
    assert user_rows[-1]["content"] == "退款"
    assert all("category:refund" not in (r["content"] or "") for r in store.rows)
    clarify_row = next(r for r in store.rows if r["turn_id"] == "Tn")
    assert clarify_row["meta"]["selected"] == "category:refund"
