"""测试基础设施：关 OTel/限流，设 env 在 app import 前，构造 stub LLM（OpenAI SSE）+ 内存 store。

关键约束：httpx MockTransport（LLM OpenAI SSE）+ TestClient 覆盖 app.state；工具链路用内联 DemoSkill。
"""
from __future__ import annotations

import json
import os

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
from general_agent.security import TokenBucket
from general_agent.skills import Skill, SkillContext

USAGE = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}


class FakeStore:
    """内存版测试用 MessageStore。"""

    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0

    async def load_messages(self, service, env, user_id, session_id, limit=None):
        rows = list(self.rows)
        if limit is not None:
            rows = rows[-limit:]
        return [dict(r) for r in rows]

    async def append_message(
        self, service, env, user_id, session_id, turn_id, role, content,
        tool_calls=None, tool_call_id=None,
    ):
        self._seq += 1
        self.rows.append(
            {
                "id": self._seq,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
                "content_ref": None,
                "content_size": None,
                "content_kind": None,
            }
        )
        return self._seq

    async def count_messages(self, *a, **k):
        return len(self.rows)

    async def load_web_messages(self, service, env, user_id, session_id, *, before=None, limit=50):
        limit = max(1, min(int(limit), 200))
        rows = self.rows
        if before is not None:
            rows = [r for r in rows if r["id"] < before]
        desc = list(reversed(rows))[: limit + 1]
        has_more = len(desc) > limit
        page = list(reversed(desc[:limit]))
        messages = [
            {
                "messageId": r["id"],
                "turnId": f"turn-{r['id']}",
                "role": r["role"],
                "content": r.get("content") or "",
                "toolCalls": r.get("tool_calls"),
                "toolCallId": r.get("tool_call_id"),
                "createdAt": None,
                "contentRef": r.get("content_ref"),
                "contentSize": r.get("content_size"),
                "contentKind": r.get("content_kind"),
            }
            for r in page
        ]
        return {"messages": messages, "nextCursor": page[0]["id"] if has_more and page else None, "hasMore": has_more}

    async def list_artifact_refs(self, *a, **k):
        return [r["content_ref"] for r in self.rows if r.get("content_ref")]

    async def get_artifact(self, *a, **k):
        return None


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
