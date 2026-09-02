"""无状态 Agent 图：create_react_agent，无 checkpointer。
tools 由 SkillRegistry 按请求过滤后注入（每请求重建图，动态加载）；无注册 Skill 时 tools 为空，
Agent 退化为纯对话。
工具异常不杀轮次：ToolNode 开启 handle_tool_errors，异常转为 status=error 的 ToolMessage，
LLM 可继续反应；错误内容为 JSON（errorCode/message），runner 解析后产出 tool_end{error}。
"""
from __future__ import annotations

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent
from langgraph.prebuilt.tool_node import ToolNode


def _tool_error_handler(exc: Exception) -> str:
    """工具异常 -> 错误 ToolMessage 内容（JSON）。errorCode 供 runner 解析产出 tool_end{error}。"""
    code = getattr(exc, "code", None) or "INTERNAL"
    return json.dumps({"errorCode": code, "message": str(exc)}, ensure_ascii=False)


def build_agent(model: BaseChatModel, tools, system_prompt: str = ""):
    tool_node = ToolNode(tools, handle_tool_errors=_tool_error_handler)
    prompt = SystemMessage(system_prompt) if system_prompt else None
    return create_react_agent(model, tool_node, checkpointer=None, prompt=prompt)
