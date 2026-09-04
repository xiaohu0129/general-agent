"""Skill 意图路由（Tool RAG）：规则前置 -> 向量检索 -> 低置信 LLM 选域 -> 用户澄清。

- rules.py：规则前置路由（确定性逃生门）
- index.py：Skill 向量索引（embedding 构建/本地缓存/余弦 top-k）
- router.py：SkillRouter 编排分级链路与置信分流
"""
from .router import RouteDecision, SkillRouter

__all__ = ["RouteDecision", "SkillRouter"]
