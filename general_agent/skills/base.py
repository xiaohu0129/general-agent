"""Skill 插件基类与注册表（自研 Skill 插件机制）。

Skill 三要素：元数据(name/description/args_schema) + 执行逻辑(run) + 权限(allowed_envs)。
SkillRegistry 显式注册，按 env 过滤后产出 LangChain StructuredTool（套 tool_call span + agent.tool.* metric）。
业务依赖（如 REST 客户端）统一由 SkillContext.services 注入，框架本身不内置任何业务客户端。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from .. import observability
from ..logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class SkillContext:
    """单次请求内 Skill 共享的上下文。

    services：业务服务注入点（app.state.services），Skill 内按 key 取用，如
    ctx.services["my_client"]；框架不预置任何 key。
    """

    env: str
    user: str
    session_id: str = ""
    services: dict[str, Any] = field(default_factory=dict)


class Skill:
    """Skill 基类。子类定义 name/description/args_schema/allowed_envs 并实现 run。"""

    name: str = ""
    description: str = ""
    args_schema: type[BaseModel] | None = None
    allowed_envs: list[str] | None = None  # None=全部环境可用；列表=仅这些 env

    async def run(self, ctx: SkillContext, **kwargs) -> Any:  # noqa: D401
        raise NotImplementedError

    def allowed(self, env: str) -> bool:
        if self.allowed_envs is None:
            return True
        return env in self.allowed_envs

    def to_tool(self, ctx: SkillContext) -> BaseTool:
        """产出带 span+metric 的 LangChain StructuredTool（async）。"""
        skill = self

        async def arun(**kwargs):
            tracer = observability.get_tracer()
            with tracer.start_as_current_span("tool_call") as span:
                span.set_attribute("tool_name", skill.name)
                start = time.monotonic()
                status = "success"
                error_code = None
                try:
                    return await skill.run(ctx, **kwargs)
                except Exception as exc:
                    status = "error"
                    error_code = getattr(exc, "code", None) or "INTERNAL"
                    observability.record_span_error(span, error_code, str(exc))
                    raise
                finally:
                    observability.record_tool(
                        skill.name,
                        (time.monotonic() - start) * 1000,
                        status=status,
                        error_code=error_code,
                    )

        return StructuredTool.from_function(
            coroutine=arun,
            name=self.name,
            description=self.description,
            args_schema=self.args_schema,
        )


class SkillRegistry:
    """显式注册 Skill；按 env 过滤产出 LangChain tools。"""

    def __init__(self) -> None:
        self._skills: list[Skill] = []

    def register(self, skill: Skill) -> None:
        self._skills.append(skill)

    def get_tools(self, ctx: SkillContext) -> list[BaseTool]:
        return [s.to_tool(ctx) for s in self._skills if s.allowed(ctx.env)]