"""规则前置路由：配置化 pattern（正则）命中即映射到确定 Skill 集合。

0 次 LLM/embedding 调用，结果对相同输入恒定；多条规则按配置顺序取第一条命中。
目标集合（Skill 名 + category）与 env 候选取交集，规则只能收窄、不能越权放大。
"""
from __future__ import annotations

import re

from ..config import RouteRule
from ..skills.base import Skill


class RuleMatcher:
    def __init__(self, rules: list[RouteRule]) -> None:
        # 启动期编译，非法正则立即报错（配置错误早暴露）
        self._compiled: list[tuple[re.Pattern, RouteRule]] = [
            (re.compile(r.pattern), r) for r in rules
        ]

    def match(self, message: str, candidates: list[Skill]) -> list[Skill] | None:
        """返回第一条命中规则的目标 Skill 集合（candidates 交集）；未命中返回 None。"""
        by_name = {s.name: s for s in candidates}
        by_category: dict[str, list[Skill]] = {}
        for s in candidates:
            by_category.setdefault(s.category, []).append(s)
        for regex, rule in self._compiled:
            if regex.search(message or ""):
                picked: dict[str, Skill] = {}
                for name in rule.skills:
                    if name in by_name:
                        picked[name] = by_name[name]
                if rule.category and rule.category in by_category:
                    for s in by_category[rule.category]:
                        picked[s.name] = s
                return list(picked.values())
        return None
