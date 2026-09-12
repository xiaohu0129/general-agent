"""任务 6.1：缺参校验守卫 / 半槽位填充（先红）。

- arg_guard=True：缺必填 -> StructuredTool 的 handle_validation_error 回调返回
  MISSING_ARGS JSON，Skill.run 不执行；经 create_react_agent 的 astream_events
  在 on_tool_end（非 on_tool_error）观测到 ToolMessage(status="error")。
- 参数齐全 -> 正常执行。
- arg_guard=False：不安装回调，缺参回落框架默认（ToolNode handle_tool_errors
  产出 errorCode=INTERNAL 的普通错误 ToolMessage，无引导话术）。
- runner：MISSING_ARGS 的 on_tool_end 映射为 SSE tool_end(status=error,errorCode)；
  系统提示词补充"缺参先问、勿编造"。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, Field, ValidationError

from conftest import build_test_app, client_for, make_llm_transport, parse_sse
from general_agent.agent import build_agent
from general_agent.config import DEFAULT_CONFIG_FILE
from general_agent.skills import Skill, SkillContext, SkillRegistry


class BookTripArgs(BaseModel):
    city: str = Field(description="出发城市，如北京")
    date: str = Field(description="出发日期，如 2026-10-01")


class BookTripSkill(Skill):
    """带两个必填字段的测试 Skill；run_count 标记业务执行是否发生。"""

    name = "book_trip"
    description = "预订出行行程：根据出发城市与出发日期安排行程。"
    args_schema = BookTripArgs

    def __init__(self) -> None:
        self.run_count = 0

    async def run(self, ctx: SkillContext, *, city: str, date: str) -> dict:
        self.run_count += 1
        return {"booked": True, "city": city, "date": date}


CTX = SkillContext(env="dev", user="u", session_id="s1")

# 复用单个常驻事件循环：asyncio.run() 结束后会关闭并清空主线程当前循环，
# 导致后续遗留测试（直接 asyncio.get_event_loop()）在 3.12 抛 RuntimeError。
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


def _run(coro):
    return LOOP.run_until_complete(coro)


def _payload_of(tool, args):
    result = _run(tool.ainvoke(args))
    assert isinstance(result, str)
    return json.loads(result)


# ---------------- 工具层：to_tool 守卫 ----------------

def test_missing_required_args_returns_missing_args_json_without_calling_run():
    skill = BookTripSkill()
    tool = skill.to_tool(CTX)

    payload = _payload_of(tool, {"city": "北京"})

    assert skill.run_count == 0
    assert payload["errorCode"] == "MISSING_ARGS"
    missing = {m["field"]: m for m in payload["missing"]}
    assert set(missing) == {"date"}
    assert "出发日期" in missing["date"]["hint"]
    assert "询问" in payload["message"] and "编造" in payload["message"]
    assert "date" in payload["message"]


def test_all_missing_fields_listed_with_schema_hints():
    skill = BookTripSkill()
    tool = skill.to_tool(CTX)

    payload = _payload_of(tool, {"unrelated": 1})

    fields = {m["field"]: m for m in payload["missing"]}
    assert set(fields) == {"city", "date"}
    assert "出发城市" in fields["city"]["hint"]
    assert skill.run_count == 0


def test_complete_args_execute_run_normally():
    skill = BookTripSkill()
    tool = skill.to_tool(CTX)

    result = _run(tool.ainvoke({"city": "北京", "date": "2026-10-01"}))

    assert skill.run_count == 1
    assert result == {"booked": True, "city": "北京", "date": "2026-10-01"}


def test_arg_guard_false_installs_no_callback_and_raises_validation_error():
    skill = BookTripSkill()
    tool = skill.to_tool(CTX, arg_guard=False)

    with pytest.raises(ValidationError):
        _run(tool.ainvoke({"city": "北京"}))
    assert skill.run_count == 0


def test_registry_get_tools_passes_arg_guard_false():
    skill = BookTripSkill()
    registry = SkillRegistry()
    registry.register(skill)

    guarded = registry.get_tools(CTX)
    payload = _payload_of(guarded[0], {})
    assert payload["errorCode"] == "MISSING_ARGS"

    raw = registry.get_tools(CTX, arg_guard=False)
    with pytest.raises(ValidationError):
        _run(raw[0].ainvoke({}))


# ---------------- 图层：事件通道 ----------------

class _ScriptedModel(BaseChatModel):
    tool_name: str
    tool_args: dict
    final_text: str = "好的，缺少的信息我先向你确认。"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if isinstance(last, BaseMessage) and last.type == "tool":
            msg = AIMessage(content=self.final_text)
        else:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {"id": "call_1", "name": self.tool_name, "args": dict(self.tool_args), "type": "tool_call"}
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "scripted-test"


async def _collect_tool_events(agent, tool_name):
    tool_ends, tool_errors, chain_tool_msgs = [], [], []
    async for ev in agent.astream_events(
        {"messages": [{"role": "user", "content": "帮我订行程"}]}, version="v2"
    ):
        event, name, data = ev["event"], ev.get("name"), ev.get("data", {})
        if event == "on_tool_end" and name == tool_name:
            tool_ends.append(data.get("output"))
        elif event == "on_tool_error" and name == tool_name:
            tool_errors.append(data.get("error"))
        elif event == "on_chain_end" and name == "tools":
            out = data.get("output")
            for m in (out.get("messages") if isinstance(out, dict) else None) or []:
                chain_tool_msgs.append(m)
    return tool_ends, tool_errors, chain_tool_msgs


def test_guard_on_missing_args_arrives_via_on_tool_end_as_error_tool_message():
    skill = BookTripSkill()
    agent = build_agent(_ScriptedModel(tool_name="book_trip", tool_args={"city": "北京"}), [skill.to_tool(CTX)])

    tool_ends, tool_errors, _ = _run(_collect_tool_events(agent, "book_trip"))

    assert skill.run_count == 0
    assert tool_errors == []
    assert len(tool_ends) == 1
    out = tool_ends[0]
    assert out.__class__.__name__ == "ToolMessage"
    assert out.status == "error"
    payload = json.loads(out.content)
    assert payload["errorCode"] == "MISSING_ARGS"
    assert [m["field"] for m in payload["missing"]] == ["date"]


def test_guard_off_missing_args_falls_back_to_internal_tool_message():
    skill = BookTripSkill()
    agent = build_agent(
        _ScriptedModel(tool_name="book_trip", tool_args={"city": "北京"}),
        [skill.to_tool(CTX, arg_guard=False)],
    )

    tool_ends, tool_errors, chain_msgs = _run(_collect_tool_events(agent, "book_trip"))

    assert skill.run_count == 0
    assert tool_ends == []
    assert len(tool_errors) == 1 and isinstance(tool_errors[0], ValidationError)
    fallback = next(m for m in chain_msgs if m.__class__.__name__ == "ToolMessage" and m.status == "error")
    payload = json.loads(fallback.content)
    assert payload["errorCode"] == "INTERNAL"
    assert "MISSING_ARGS" not in fallback.content and "询问" not in fallback.content


# ---------------- SSE/runner 层 ----------------

def _chat(client, message="帮我预订行程"):
    headers = {"x-service": "s", "x-env": "dev", "x-user": "u"}
    with client.stream("POST", "/chat", json={"message": message}, headers=headers) as r:
        assert r.status_code == 200
        text = "\n".join(r.iter_lines())
    return parse_sse(text)


def test_sse_missing_args_tool_end_is_error_with_missing_args_code():
    skill = BookTripSkill()
    app, store, broker = build_test_app(
        skill=skill,
        llm_transport=make_llm_transport(tool_name="book_trip", args={"city": "北京"}),
    )
    evs = _chat(client_for(app))

    tool_end = next(d for e, d, _ in evs if e == "tool_end")
    assert tool_end["status"] == "error"
    assert tool_end["errorCode"] == "MISSING_ARGS"
    assert "date" in json.dumps(tool_end["result"], ensure_ascii=False)
    assert skill.run_count == 0
    assert any(r["role"] == "tool" for r in store.rows)
    assert next(d for e, d, _ in evs if e == "turn_end")["finishReason"] == "stop"


def test_sse_complete_args_tool_end_success_and_run_called():
    skill = BookTripSkill()
    app, store, broker = build_test_app(
        skill=skill,
        llm_transport=make_llm_transport(
            tool_name="book_trip", args={"city": "北京", "date": "2026-10-01"}
        ),
    )
    evs = _chat(client_for(app))

    tool_end = next(d for e, d, _ in evs if e == "tool_end")
    assert tool_end["status"] == "success"
    assert tool_end["errorCode"] is None
    assert tool_end["result"]["booked"] is True
    assert skill.run_count == 1


def test_sse_arg_guard_disabled_falls_back_to_internal(monkeypatch):
    monkeypatch.setenv("AGENT_ROUTING__ARG_GUARD", "false")
    from general_agent.config import get_settings

    get_settings.cache_clear()
    try:
        skill = BookTripSkill()
        app, store, broker = build_test_app(
            skill=skill,
            llm_transport=make_llm_transport(tool_name="book_trip", args={"city": "北京"}),
        )
        evs = _chat(client_for(app))
    finally:
        get_settings.cache_clear()

    tool_end = next(d for e, d, _ in evs if e == "tool_end")
    assert tool_end["status"] == "error"
    assert tool_end["errorCode"] != "MISSING_ARGS"
    # 回落 ToolNode handle_tool_errors：持久化的 tool 行为 INTERNAL 普通错误，无引导话术
    tool_rows = [r for r in store.rows if r["role"] == "tool"]
    assert tool_rows and '"errorCode": "INTERNAL"' in tool_rows[0]["content"]
    assert "询问" not in tool_rows[0]["content"]
    assert skill.run_count == 0


# ---------------- 系统提示词 ----------------

def test_agent_system_prompt_default_guides_asking_for_missing_args():
    from general_agent.config import AgentSettings

    prompt = AgentSettings().system_prompt
    assert "缺少参数" in prompt
    assert "向用户询问" in prompt
    assert "编造" in prompt


def test_config_yaml_system_prompt_contains_missing_args_guidance():
    import yaml

    cfg = yaml.safe_load(Path(DEFAULT_CONFIG_FILE).read_text(encoding="utf-8"))
    prompt = cfg["agent"]["system_prompt"]
    assert "缺少参数" in prompt and "向用户询问" in prompt and "编造" in prompt
