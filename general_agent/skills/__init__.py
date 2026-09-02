"""Skill 插件包（自研 Skill 插件机制）。

显式注册所有 Skill（非自动扫描，避免 magic）。业务方在 build_registry 中 register 自己的 Skill；
框架默认不注册任何业务 Skill。
"""
from .base import Skill, SkillContext, SkillRegistry

__all__ = ["Skill", "SkillContext", "SkillRegistry", "build_registry"]


def build_registry() -> SkillRegistry:
    registry = SkillRegistry()
    return registry
