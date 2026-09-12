"""测试基础设施：关 OTel/限流，设 env 在 app import 前，构造 stub LLM（OpenAI SSE）+ 内存 store。

关键约束：httpx MockTransport（LLM OpenAI SSE）+ TestClient 覆盖 app.state；工具链路用内联 DemoSkill。
"""
from __future__ import annotations

import json
import os
import uuid

# 在 app import 前设 env，关闭 console 导出 + 关闭治理
os.environ.setdefault("AGENT_OBSERVABILITY__ENABLED", "false")
os.environ.setdefault("AGENT_SECURITY__AUTH_MODE", "disabled")
os.environ.setdefault("AGENT_SECURITY__RATE_LIMIT__ENABLED", "false")
os.environ.setdefault("AGENT_BROKER__HEARTBEAT_INTERVAL", "0.5")
# 路由索引构建需访问 embedding 端点；单测默认关闭（避免 lifespan 连网络），
# 路由专项测试自行构造 SkillRouter 注入 app.state.skill_router。
os.environ.setdefault("AGENT_ROUTING__ENABLED", "false")

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from general_agent.app import create_app
from general_agent.broker import Broker
from general_agent.llm import OpenAICompatibleModel
from general_agent.message_store import _tool_status
from general_agent.security import TokenBucket
from general_agent.skills import Skill, SkillContext

USAGE = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}


class FakeStore:
    """内存版测试用 MessageStore。"""

    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0

    def _scoped(self, service, env, user_id, session_id):
        return [
            r
            for r in self.rows
            if r["service"] == service
            and r["env"] == env
            and r["user_id"] == user_id
            and r["session_id"] == session_id
        ]

    async def load_messages(self, service, env, user_id, session_id, limit=None):
        rows = self._scoped(service, env, user_id, session_id)
        if limit is not None:
            rows = rows[-limit:]
        return [dict(r) for r in rows]

    async def append_message(
        self, service, env, user_id, session_id, turn_id, role, content,
        tool_calls=None, tool_call_id=None, meta=None,
    ):
        self._seq += 1
        self.rows.append(
            {
                "id": self._seq,
                "service": service,
                "env": env,
                "user_id": user_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
                "meta": meta,
                "content_ref": None,
                "content_size": None,
                "content_kind": None,
            }
        )
        return self._seq

    async def count_messages(self, service, env, user_id, session_id, *a, **k):
        return len(self._scoped(service, env, user_id, session_id))

    async def load_web_messages(self, service, env, user_id, session_id, *, before=None, limit=50):
        limit = max(1, min(int(limit), 200))
        rows = self._scoped(service, env, user_id, session_id)
        if before is not None:
            rows = [r for r in rows if r["id"] < before]
        desc = list(reversed(rows))[: limit + 1]
        has_more = len(desc) > limit
        page = list(reversed(desc[:limit]))
        messages = []
        for r in page:
            meta = r.get("meta") or {}
            messages.append(
                {
                    "messageId": r["id"],
                    "turnId": r.get("turn_id"),
                    "role": r["role"],
                    "content": r.get("content") or "",
                    "toolCalls": r.get("tool_calls"),
                    "toolCallId": r.get("tool_call_id"),
                    "createdAt": None,
                    "contentRef": r.get("content_ref"),
                    "contentSize": r.get("content_size"),
                    "contentKind": r.get("content_kind"),
                    "status": _tool_status(r.get("content") or "") if r["role"] == "tool" else None,
                    "options": meta.get("options"),
                    "selected": meta.get("selected"),
                }
            )
        return {"messages": messages, "nextCursor": page[0]["id"] if has_more and page else None, "hasMore": has_more}

    async def update_clarify_selected(
        self, service, env, user_id, session_id, turn_id, selected
    ) -> int:
        """按归属键 + turn_id 定位上一轮 assistant 澄清行，读-改-写合并 selected 到 meta。

        不覆盖已有 options；meta 为 None 时新建 dict；返回更新行数（0 表示未定位到）。
        """
        changed = 0
        for r in self._scoped(service, env, user_id, session_id):
            if r["turn_id"] == turn_id and r["role"] == "assistant":
                meta = dict(r.get("meta") or {})
                meta["selected"] = selected
                r["meta"] = meta
                changed += 1
        return changed

    async def list_artifact_refs(self, *a, **k):
        return [r["content_ref"] for r in self.rows if r.get("content_ref")]

    async def get_artifact(self, *a, **k):
        return None


class FakeChatSessions:
    """内存版 agent_chat_session：完整接口（含 claim_if_absent/get_owned_scoped）。

    claim 模拟 INSERT IGNORE 原子语义：已存在且四元组归属一致 -> 幂等返回行；
    已属其他身份 -> None（不覆盖）；不存在 -> 插入并返回行。
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}

    async def create(self, uid, service, env, title, session_id=None):
        sid = session_id or uuid.uuid4().hex
        self.sessions[sid] = {
            "session_id": sid, "uid": uid, "service": service, "env": env,
            "title": (title or "新会话")[:128],
        }
        return {"sessionId": sid, "title": title, "createdAt": None, "updatedAt": None}

    async def list_for_user(self, uid, limit=50):
        return [
            {"sessionId": s["session_id"], "title": s["title"], "createdAt": None, "updatedAt": None}
            for s in self.sessions.values() if s["uid"] == uid
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


class DemoSkillError(Exception):
    """模拟业务 Skill 抛出的带 errorCode 异常。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DemoArgs(BaseModel):
    query: str = ""
    task_id: str = ""


class DemoSkill(Skill):
    """内联测试 Skill：task_id=bad 抛 NOT_FOUND，否则返回固定结果（回显 sessionId）。"""

    name = "demo_skill"
    description = "演示用工具：执行一个演示任务并返回结果。"
    args_schema = DemoArgs
    allowed_envs = None

    async def run(self, ctx: SkillContext, *, query: str = "", task_id: str = "") -> dict:
        if task_id == "bad":
            raise DemoSkillError("NOT_FOUND", f"task {task_id} not found")
        return {"taskId": "J123", "status": "PENDING", "sessionId": ctx.session_id, "query": query}


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def make_llm_transport(tool_name="demo_skill", final_text="工具执行完成，当前状态为 PENDING。", args=None):
    """构造 stub OpenAI SSE transport：先 tool_calls，工具结果后回终答。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        last = body["messages"][-1] if body.get("messages") else {}

        def gen():
            base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": 1, "model": "stub"}
            yield _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            if last.get("role") == "tool":
                yield _sse({**base, "choices": [{"index": 0, "delta": {"content": final_text}, "finish_reason": None}]})
                yield _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            else:
                tc_args = args if args is not None else {"query": "do something"}
                yield _sse(
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": tool_name,
                                                "arguments": json.dumps(tc_args, ensure_ascii=False),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                yield _sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            yield _sse({**base, "choices": [], "usage": USAGE})
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, stream=httpx.ByteStream(b"".join(gen())))

    return httpx.MockTransport(handler)


def build_test_app(llm_transport=None, store=None, broker=None, rate_limiter=None, skill=None):
    """构建带 stub 的测试 app，返回 (app, store, broker)。"""
    app = create_app()
    app.state.model = OpenAICompatibleModel(
        base_url="http://stub", model="stub", transport=llm_transport or make_llm_transport()
    )
    app.state.services = {}
    app.state.skill_registry.register(skill or DemoSkill())
    store = store or FakeStore()
    app.state.message_store = store
    app.state.broker = broker or Broker(ring_size=16, sub_queue_size=64)
    app.state.rate_limiter = rate_limiter or TokenBucket(rate=1000, capacity=1000)
    # 全鉴权模式统一 owner 校验：默认注入内存 fake，避免 disabled/api_key 测试打到真实 MySQL
    app.state.chat_sessions = FakeChatSessions()
    return app, store, app.state.broker


def parse_sse(text: str):
    """解析 SSE 文本为 [(event, data_dict, id)] 列表（event/None, data dict or None, id or None）。"""
    events = []
    cur_event = None
    cur_data = None
    cur_id = None
    for line in text.splitlines():
        if line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            try:
                cur_data = json.loads(raw)
            except Exception:
                cur_data = raw
        elif line.startswith("id:"):
            cur_id = line[3:].strip()
        elif line.startswith(":"):
            events.append(("heartbeat", None, None))
            cur_event = cur_data = cur_id = None
        elif line == "":
            if cur_event is not None or cur_data is not None:
                events.append((cur_event, cur_data, cur_id))
            cur_event = cur_data = cur_id = None
    # 末尾未 flush 的事件
    if cur_event is not None or cur_data is not None:
        events.append((cur_event, cur_data, cur_id))
    return events


def client_for(app):
    return TestClient(app)

import socket
import threading
import time as _time
from contextlib import contextmanager

import uvicorn


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def run_server(app):
    """后台线程跑 uvicorn（TestClient 无法读取无限 SSE 流，故用真实服务器测 GET /stream）。"""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with httpx.Client(timeout=0.5) as probe:
                probe.get(f"{base}/health")
            break
        except Exception:
            _time.sleep(0.1)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=3)
