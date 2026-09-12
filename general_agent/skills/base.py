"""Skill 插件基类与注册表（自研 Skill 插件机制）。

Skill 三要素：元数据(name/description/args_schema) + 执行逻辑(run) + 权限(allowed_envs)。
SkillRegistry 显式注册，按 env 过滤后产出 LangChain StructuredTool（套 tool_call span + agent.tool.* metric）。
业务依赖（如 REST 客户端）统一由 SkillContext.services 注入，框架本身不内置任何业务客户端。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ValidationError

from .. import observability
from ..logging_setup import get_logger

logger = get_logger(__name__)

SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _make_missing_args_handler(args_schema: type[BaseModel] | None):
    """构造 StructuredTool.handle_validation_error 回调：缺参/非法字段 -> MISSING_ARGS JSON（返回非抛出）。

    框架在调用 arun 前于 _parse_input 做 model_validate，校验失败时执行本回调，
    返回的字符串成为 ToolMessage(status="error") 的 content，经 on_tool_end 回流。
    """
    hints: dict[str, str] = {}
    if args_schema is not None:
        for name, info in args_schema.model_fields.items():
            hints[name] = info.description or info.title or name

    def handler(e: ValidationError) -> str:
        missing: list[dict[str, str]] = []
        for err in e.errors():
            loc = err.get("loc") or ()
            field_name = str(loc[-1]) if loc else ""
            missing.append({"field": field_name, "hint": hints.get(field_name, field_name)})
        names = "、".join(m["field"] for m in missing)
        message = f"参数不足，请勿编造参数，请先向用户询问以下参数：{names}"
        return json.dumps(
            {"errorCode": "MISSING_ARGS", "missing": missing, "message": message},
            ensure_ascii=False,
        )

    return handler


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
    """Skill 基类。子类定义 name/description/args_schema/allowed_envs/category/examples 并实现 run。"""

    name: str = ""
    description: str = ""
    args_schema: type[BaseModel] | None = None
    allowed_envs: list[str] | None = None  # None=全部环境可用；列表=仅这些 env
    category: str = ""  # 技能域标签（用于向量检索元数据过滤与兜底 LLM 选域）
    examples: list[str] = []  # 2~5 条典型用户说法，作为向量检索的主要语义来源

    async def run(self, ctx: SkillContext, **kwargs) -> Any:  # noqa: D401
        raise NotImplementedError

    def allowed(self, env: str) -> bool:
        if self.allowed_envs is None:
            return True
        return env in self.allowed_envs

    def to_tool(self, ctx: SkillContext, arg_guard: bool = True) -> BaseTool:
        """产出带 span+metric 的 LangChain StructuredTool（async）。

        arg_guard=True 时安装缺参校验回调（arun 前由框架校验，失败回流 MISSING_ARGS）；
        False 时不安装，回落框架默认错误处理。
        """
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

        kwargs: dict[str, Any] = dict(
            coroutine=arun,
            name=self.name,
            description=self.description,
            args_schema=self.args_schema,
        )
        if arg_guard:
            kwargs["handle_validation_error"] = _make_missing_args_handler(self.args_schema)
        return StructuredTool.from_function(**kwargs)


class SkillRegistry:
    """显式注册 Skill；按 env 过滤产出 LangChain tools。"""

    def __init__(self) -> None:
        self._skills: list[Skill] = []

    def register(self, skill: Skill) -> None:
        name = skill.name
        if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                f"非法 Skill 名称 {name!r}：必须非空且匹配 ^[A-Za-z0-9_-]{{1,64}}$"
            )
        if any(s.name == name for s in self._skills):
            raise ValueError(f"Skill 名称 {name!r} 重复注册：注册范围内名称必须唯一")
        self._skills.append(skill)

    def get_tools(self, ctx: SkillContext, arg_guard: bool = True) -> list[BaseTool]:
        return [s.to_tool(ctx, arg_guard=arg_guard) for s in self._skills if s.allowed(ctx.env)]

    def list_allowed(self, ctx: SkillContext) -> list[Skill]:
        """返回当前 env 可用的 Skill 对象（供路由层检索/收窄后再 to_tool）。"""
        return [s for s in self._skills if s.allowed(ctx.env)]

    def list_all(self) -> list[Skill]:
        """返回全部已注册 Skill（跨 env，供启动时构建向量索引）。"""
        return list(self._skills)