"""SkillRouter：意图路由编排（规则 -> 向量检索 -> LLM 选域兜底 -> 用户澄清）。

纯决策逻辑，返回 RouteDecision（path/tools/clarify_text/details）；
span/metric/审计由调用方（api/chat）依据 details 记录，便于单测与回放。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..logging_setup import get_logger
from ..skills.base import Skill
from .index import SkillIndex
from .rules import RuleMatcher

logger = get_logger(__name__)

# 路由路径（低基数，用于 metric 标签）
PATH_RULE = "rule"
PATH_VECTOR = "vector"
PATH_LLM = "llm"
PATH_CHITCHAT = "chitchat"
PATH_CLARIFY = "clarify"
PATH_FALLBACK = "fallback"  # 路由 LLM 异常等
PATH_DEGRADED = "degraded"  # embedding/索引不可用

_CHITCHAT = "chitchat"
_UNKNOWN = "unknown"


@dataclass
class RouteDecision:
    path: str
    tools: list[Skill]
    clarify_text: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class SkillRouter:
    def __init__(
        self,
        *,
        index: SkillIndex,
        rule_matcher: RuleMatcher,
        llm,
        embedder,
        top_k: int = 20,
        score_threshold: float = 0.5,
        margin: float = 0.1,
    ) -> None:
        self.index = index
        self.rules = rule_matcher
        self.llm = llm
        self.embedder = embedder
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.margin = margin

    async def route(self, message: str, candidates: list[Skill]) -> RouteDecision:
        details: dict[str, Any] = {
            "embedding_model": self.index.model_id,
            "index_version": self.index.version,
            "candidate_count": len(candidates),
        }

        # 无可用 Skill：纯对话
        if not candidates:
            return RouteDecision(PATH_CHITCHAT, [], details=details)

        # [1] 规则前置（0 LLM/embedding，确定性）
        matched = self.rules.match(message, candidates)
        if matched:
            details["rule_skills"] = [s.name for s in matched]
            return RouteDecision(PATH_RULE, matched, details=details)

        # [2] 向量检索
        if not self.index.ready:
            details["reason"] = "index_not_ready"
            return RouteDecision(PATH_DEGRADED, list(candidates), details=details)
        try:
            qvecs = await self.embedder.embed_texts([message])
            qvec = qvecs[0]
        except Exception as exc:
            details["reason"] = f"query_embed_failed: {getattr(exc, 'code', 'INTERNAL')}"
            logger.warning("route_query_embed_failed", error=str(exc))
            return RouteDecision(PATH_DEGRADED, list(candidates), details=details)

        results = self.index.search(qvec, candidates, self.top_k)
        details["top_k"] = [{"skill": s.name, "score": round(score, 4)} for s, score in results]
        if results:
            top1 = results[0][1]
            top2 = results[1][1] if len(results) > 1 else 1.0
            details["top1_score"] = round(top1, 4)
            details["score_gap"] = round(top1 - top2, 4)
            if top1 >= self.score_threshold and (top1 - top2) >= self.margin:
                # 高置信：top-k 再按分数下限截断（丢弃零相关/噪声结果），避免收窄集仍含无关工具
                floor = self.score_threshold * 0.5
                tools = [s for s, score in results if score >= floor]
                return RouteDecision(PATH_VECTOR, tools, details=details)

        # [3] 低置信 -> 路由 LLM 选域兜底
        return await self._llm_fallback(message, candidates, details)

    async def _llm_fallback(
        self, message: str, candidates: list[Skill], details: dict
    ) -> RouteDecision:
        categories = sorted({s.category for s in candidates if s.category})
        details["categories"] = categories
        try:
            result = await self._classify(message, categories)
        except Exception as exc:
            logger.warning("route_llm_failed", error=str(exc))
            details["reason"] = "route_llm_failed"
            return RouteDecision(PATH_FALLBACK, list(candidates), details=details)

        category = result.get("category") or _UNKNOWN
        confidence = result.get("confidence")
        details["llm_category"] = category
        details["llm_confidence"] = confidence
        details["llm_reason"] = result.get("reason")

        if category == _CHITCHAT:
            return RouteDecision(PATH_CHITCHAT, [], details=details)
        if category and category != _UNKNOWN and category in categories:
            tools = [s for s in candidates if s.category == category]
            if tools:
                return RouteDecision(PATH_LLM, tools, details=details)
        # unknown / 无法确定 -> 澄清
        clarify = result.get("clarify_question") or self._default_clarify(categories)
        return RouteDecision(PATH_CLARIFY, [], clarify_text=clarify, details=details)

    async def _classify(self, message: str, categories: list[str]) -> dict:
        cat_list = "、".join(categories) if categories else "（无明确类别）"
        sys = (
            "你是意图路由分类器。根据用户消息，从给定技能类别中选出最匹配的一个类别；"
            "若只是闲聊寒暄选 chitchat；若信息不足无法判断选 unknown。只输出 JSON，不要输出其他内容。"
        )
        user = (
            f"可选技能类别：{cat_list}\n"
            f"用户消息：{message}\n"
            f'输出 JSON：{{"category": "类别名或 {_CHITCHAT} 或 {_UNKNOWN}", '
            f'"confidence": 0到1的数, "reason": "简述", '
            f'"clarify_question": "当 category 为 unknown 时，向用户提出的澄清问题（列出可选方向）"}}'
        )
        resp = await self.llm.ainvoke([SystemMessage(content=sys), HumanMessage(content=user)])
        return self._parse_json(getattr(resp, "content", "") or "")

    @staticmethod
    def _parse_json(content: str) -> dict:
        text = content.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        else:
            brace = re.search(r"\{.*\}", text, re.DOTALL)
            if brace:
                text = brace.group(0)
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _default_clarify(categories: list[str]) -> str:
        if categories:
            return f"我可以帮你处理以下方向的事务：{'、'.join(categories)}。请补充你具体想做什么，或选择一个方向。"
        return "我没太理解你的需求，能再详细描述一下你想做什么吗？"
