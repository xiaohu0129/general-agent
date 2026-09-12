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
from .context import DEFAULT_REFER_TERMS, build_context_query, contains_referral, rewrite_query
from .index import SkillIndex
from .keyword import KeywordIndex
from .rules import RuleMatcher

logger = get_logger(__name__)

# 路由路径（低基数，用于 metric 标签）
PATH_RULE = "rule"
PATH_VECTOR = "vector"
PATH_LLM = "llm"
PATH_OPTION = "option"  # 用户点选澄清选项的确定性收窄
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
    clarify_options: list[dict] | None = None
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
        keyword_index: KeywordIndex | None = None,
        hybrid: bool = True,
        rrf_k: int = 60,
        keyword_top_k: int = 10,
        semantic: bool = True,
        context_turns: int = 3,
        query_rewrite: bool = True,
        refer_terms: list[str] | None = None,
        llm_conf_high: float = 0.7,
        clarify_options: bool = True,
        clarify_option_max: int = 4,
    ) -> None:
        self.index = index
        self.rules = rule_matcher
        self.llm = llm
        self.embedder = embedder
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.margin = margin
        self.keyword_index = keyword_index
        self.hybrid = hybrid
        self.rrf_k = rrf_k
        self.keyword_top_k = keyword_top_k
        self.semantic = semantic
        self.context_turns = context_turns
        self.query_rewrite = query_rewrite
        self.refer_terms = refer_terms if refer_terms is not None else list(DEFAULT_REFER_TERMS)
        self.llm_conf_high = llm_conf_high
        self.clarify_options = clarify_options
        self.clarify_option_max = clarify_option_max

    async def route(
        self,
        message: str,
        candidates: list[Skill],
        *,
        history: list[dict] | None = None,
        prev_clarify: dict | None = None,
        selection: str | None = None,
    ) -> RouteDecision:
        details: dict[str, Any] = {
            "embedding_model": self.index.model_id,
            "index_version": self.index.version,
            "candidate_count": len(candidates),
        }

        # 无可用 Skill：纯对话
        if not candidates:
            return RouteDecision(PATH_CHITCHAT, [], details=details)

        # [0] 选项回传确定性收窄：用户显式点选是确定信号，优先于规则逃生门与全部再裁决；
        # value 必须命中上一轮澄清选项且名经当前 env 候选集合法，否则忽略标记回落既有流程
        option_tools = self._narrow_by_selection(selection, prev_clarify, candidates, details)
        if option_tools is not None:
            details["clarify_outcome"] = "option"
            details["clarify_selection"] = selection
            return RouteDecision(PATH_OPTION, option_tools, details=details)

        # 澄清文本闭环：上一轮澄清候选方向存在时，规则路仍对原文 + 全量生效（确定性逃生门）；
        # 规则未命中后，BM25/向量/LLM 裁决仅限候选方向子集（交集为空视为无法对应 -> 再澄清）
        prev_categories = list(prev_clarify.get("categories") or []) if prev_clarify else []
        in_clarify = bool(prev_categories)
        routed = candidates

        # [1] 规则前置（0 LLM/embedding，确定性）
        matched = self.rules.match(message, candidates)
        if matched:
            details["rule_skills"] = [s.name for s in matched]
            return RouteDecision(PATH_RULE, matched, details=details)

        if in_clarify:
            wanted = set(prev_categories)
            routed = [s for s in candidates if s.category in wanted]
            if not routed:
                return self._repeat_clarify(prev_categories, details, candidates)

        # [2] BM25 关键词路先跑一次（hybrid 开且有关键词索引时；语义正常路径也只跑这一次）
        kw_results = self._keyword_search(message, routed, details)

        # [3] 语义可用性：semantic 入参（stub/显式关闭）与 index 就绪同时满足才走向量路
        semantic_on = self.semantic and self.index.ready
        if not semantic_on:
            details["semantic_off"] = True
            # index 未就绪与仅 semantic 关闭区分 reason，均不调用 embedder
            reason = "index_not_ready" if not self.index.ready else "semantic_off"
            details["reason"] = reason
            if kw_results:
                # BM25 收窄 -> LLM 兜底裁决（path 归类由 LLM 结果决定，非 degraded）
                details["degraded_keyword"] = True
                narrowed = [s for s, _ in kw_results]
                dec = await self._llm_fallback(
                    message, narrowed, details, kw_results, routed,
                    env_candidates=candidates,
                    history_categories=prev_categories or None,
                    retrieval_skills=list(narrowed),
                )
                return self._tag_clarify_outcome(dec, prev_categories, in_clarify)
            if in_clarify:
                return self._repeat_clarify(prev_categories, details, candidates)
            return RouteDecision(PATH_DEGRADED, list(candidates), details=details)

        # [4] 拼接向量检索：context_turns>0、history 非空且改写开关开时拼接最近 N 轮；
        # query_rewrite=false 属整体降级，不拼接也不改写（design D2 步骤 8），q=message 与单轮逐字节一致
        use_context = self.context_turns > 0 and self.query_rewrite and bool(history)
        # history 每条为一行消息：一轮 = user+assistant 两条，取最近 2N 条即最近 N 轮
        recent = history[-2 * self.context_turns :] if use_context else None
        q = build_context_query(recent, message) if use_context else message
        try:
            qvecs = await self.embedder.embed_texts([q])
            qvec = qvecs[0]
        except Exception as exc:
            reason = f"query_embed_failed: {getattr(exc, 'code', 'INTERNAL')}"
            logger.warning("route_query_embed_failed", error=str(exc))
            if kw_results:
                # 运行中单次 embed 抛错与启动期故障同构：BM25 收窄 -> LLM 兜底
                details["reason"] = reason
                details["degraded_keyword"] = True
                narrowed = [s for s, _ in kw_results]
                dec = await self._llm_fallback(
                    message, narrowed, details, kw_results, routed,
                    env_candidates=candidates,
                    history_categories=prev_categories or None,
                    retrieval_skills=list(narrowed),
                )
                return self._tag_clarify_outcome(dec, prev_categories, in_clarify)
            details["reason"] = reason
            if in_clarify:
                return self._repeat_clarify(prev_categories, details, candidates)
            return RouteDecision(PATH_DEGRADED, list(candidates), details=details)

        results = self.index.search(qvec, routed, self.top_k)
        details["top_k"] = [{"skill": s.name, "score": round(score, 4)} for s, score in results]
        rrf_order = self._rrf_fuse(results, kw_results, details)
        if results:
            details["top1_score"] = round(results[0][1], 4)
            details["score_gap"] = round(
                results[0][1] - (results[1][1] if len(results) > 1 else 1.0), 4
            )

        # [5] 首次高置信门：向量高置信且未命中指代词 -> vector 路径（0 LLM，结束）。
        # 指代词仅在跨轮上下文开启且改写开关开启时才拦截高置信（无 history/关闭改写时行为同单轮）。
        referred = contains_referral(message, self.refer_terms)
        rewrite_enabled = self.query_rewrite and use_context
        if self._is_high_confidence(results) and not (rewrite_enabled and referred):
            order = self._release_vector(results, rrf_order, kw_results, routed)
            dec = RouteDecision(PATH_VECTOR, order, details=details)
            return self._tag_clarify_outcome(dec, prev_categories, in_clarify)

        # [6] 按需改写：开关开且（命中指代词 或 首次低置信）-> 一次改写 LLM + 同 BM25 重检
        if rewrite_enabled and (referred or not self._is_high_confidence(results)):
            try:
                rewritten = await rewrite_query(self.llm, recent, message)
                rqvecs = await self.embedder.embed_texts([rewritten])
                rqvec = rqvecs[0]
            except Exception as exc:
                # 改写 LLM 异常/不可解析或重检 embed 失败：静默沿用首次拼接 fused，MUST NOT 中断
                logger.warning("route_query_rewrite_failed", error=str(exc))
                details["query_rewrite_failed"] = True
                details["query_rewrite_error"] = getattr(exc, "code", None) or type(exc).__name__
            else:
                details["query_rewritten"] = True
                details["rewritten_query"] = rewritten
                results2 = self.index.search(rqvec, routed, self.top_k)
                # BM25 全程只对当前消息跑一次：重检复用同一 kw_results，仅重算 RRF
                rrf_order2 = self._rrf_fuse(results2, kw_results, details, details_key="rrf_rewritten")
                # 回放字段以最终裁决所用检索结果为准
                details["top_k"] = [{"skill": s.name, "score": round(score, 4)} for s, score in results2]
                if results2:
                    details["top1_score"] = round(results2[0][1], 4)
                    details["score_gap"] = round(
                        results2[0][1] - (results2[1][1] if len(results2) > 1 else 1.0), 4
                    )
                if self._is_high_confidence(results2):
                    # 改写纠正为明确语义后高置信：直接 vector，跳过路由 LLM
                    order = self._release_vector(results2, rrf_order2, kw_results, routed)
                    dec = RouteDecision(PATH_VECTOR, order, details=details)
                    return self._tag_clarify_outcome(dec, prev_categories, in_clarify)
                results, rrf_order = results2, rrf_order2

        # [7] 低置信 -> 路由 LLM 选域兜底（prompt 仍只使用当前 message，不注入历史摘要）。
        # 跨轮流程：候选收窄为最终 RRF 融合候选（向量余弦>0 或 BM25 命中，有正证据），
        # spec《拼接低置信经改写重检…》：“否则带融合候选进入路由 LLM”；
        # 两路均无证据（fused 为空）时回退系统全量（与检索异常降级全量同构）并打标。
        # 单轮流程（use_context=False）：维持现状传全量 candidates，不打标。
        # 澄清闭环轮：裁决范围恒为候选方向子集（routed），不回退系统全量。
        fallback_candidates = routed if in_clarify else candidates
        if use_context:
            fused_skills = self._fused_candidate_skills(rrf_order, results, kw_results)
            if fused_skills:
                fallback_candidates = fused_skills
                details["fallback_candidates"] = "fused"
            else:
                if not in_clarify:
                    details["fallback_candidates"] = "full"
        dec = await self._llm_fallback(
            message, fallback_candidates, details, kw_results, all_candidates=routed,
            env_candidates=candidates,
            history_categories=prev_categories or None,
            retrieval_skills=[s for s, _ in rrf_order],
        )
        return self._tag_clarify_outcome(dec, prev_categories, in_clarify)

    def _narrow_by_selection(
        self,
        selection: str | None,
        prev_clarify: dict | None,
        candidates: list[Skill],
        details: dict[str, Any],
    ) -> list[Skill] | None:
        """选项回传确定性收窄：命中上轮 options 且名经当前 env 候选合法 -> 收窄集；否则 None。

        携带了 selection（含空串/非法/不匹配/点旧卡片）但未收窄时标 clarify_selection_ignored；
        selection 为 None（手打消息）不打标。
        """
        if selection is None:
            return None

        def _ignored() -> None:
            details["clarify_selection_ignored"] = True

        if not isinstance(selection, str) or not prev_clarify:
            _ignored()
            return None
        options = prev_clarify.get("options") if isinstance(prev_clarify, dict) else None
        if not any(isinstance(o, dict) and o.get("value") == selection for o in (options or [])):
            _ignored()
            return None
        prefix, sep, name = selection.partition(":")
        if not sep or not name or prefix not in ("category", "skill"):
            _ignored()
            return None
        if prefix == "category":
            tools = [s for s in candidates if s.category == name]
        else:
            tools = [s for s in candidates if s.name == name][:1]
        if not tools:
            _ignored()
            return None
        return tools

    def _build_clarify_options(
        self,
        *,
        result: dict | None,
        env_candidates: list[Skill],
        scope_skill_names: set[str],
        retrieval_skills: list[Skill],
        history_categories: list[str] | None,
    ) -> list[dict] | None:
        """确定性构造澄清选项：来源 A=category（先历史合法方向、再 LLM 候选、再融合候选所属域），
        来源 B=skill（先 LLM 点名、再融合候选）；先 A 后 B、按 value 去重、截断到上限。

        历史方向（闭环轮）置顶且其余来源限定在历史范围内；名均须在当前 env 候选集内合法。
        """
        legal_cats = {s.category for s in env_candidates if s.category}
        by_name = {s.name: s for s in env_candidates}
        history = [c for c in (history_categories or []) if c in legal_cats]
        in_history = set(history) if history_categories is not None else None

        def _cat_allowed(c: str) -> bool:
            return c in legal_cats and (in_history is None or c in in_history)

        cat_values = list(dict.fromkeys(history))
        raw_cats = (result or {}).get("categories")
        if isinstance(raw_cats, list):
            for c in raw_cats:
                if isinstance(c, str) and _cat_allowed(c) and c not in cat_values:
                    cat_values.append(c)
        if not cat_values:
            # LLM 未给合法候选方向：用本轮融合/向量 top 候选 Skill 所属 categories（按融合序去重）补
            for s in retrieval_skills:
                if s.category and _cat_allowed(s.category) and s.category not in cat_values:
                    cat_values.append(s.category)

        skill_names: list[str] = []
        raw_skills = (result or {}).get("skills")
        if isinstance(raw_skills, list):
            for n in raw_skills:
                if (
                    isinstance(n, str)
                    and n in scope_skill_names
                    and n in by_name
                    and n not in skill_names
                ):
                    skill_names.append(n)
        for s in retrieval_skills:
            if s.name in scope_skill_names and s.name in by_name and s.name not in skill_names:
                skill_names.append(s.name)

        options: list[dict] = [{"label": c, "value": f"category:{c}"} for c in cat_values]
        for n in skill_names:
            s = by_name[n]
            label = (s.description or "").strip()[:20] or n
            options.append({"label": label, "value": f"skill:{n}"})
        options = options[: self.clarify_option_max]
        return options or None

    def _repeat_clarify(
        self,
        prev_categories: list[str],
        details: dict,
        env_candidates: list[Skill],
    ) -> RouteDecision:
        """澄清闭环失败：仍无法对应任一候选方向 -> 携带历史方向再次澄清。"""
        cats = sorted(prev_categories)
        details["categories"] = cats
        details["clarify_outcome"] = "repeat"
        details["clarify_history_categories"] = cats
        options = None
        if self.clarify_options:
            options = self._build_clarify_options(
                result=None,
                env_candidates=env_candidates,
                scope_skill_names={s.name for s in env_candidates},
                retrieval_skills=[],
                history_categories=cats,
            )
        return RouteDecision(
            PATH_CLARIFY, [],
            clarify_text=self._default_clarify(cats),
            clarify_options=options,
            details=details,
        )

    @staticmethod
    def _tag_clarify_outcome(
        dec: RouteDecision, prev_categories: list[str], in_clarify: bool
    ) -> RouteDecision:
        """澄清闭环轮的结局标注：子集内成功裁决 -> text；仍澄清/unknown -> repeat；chitchat 不强加。"""
        if not in_clarify:
            return dec
        if dec.path in (PATH_CHITCHAT, PATH_FALLBACK):
            # 闲聊保持原语义；LLM 异常回退不强加闭环结局（其工具集已限为子集）
            return dec
        if dec.path == PATH_CLARIFY:
            cats = sorted({*prev_categories, *dec.details.get("categories", [])})
            dec.details["categories"] = cats
            dec.details["clarify_outcome"] = "repeat"
            dec.details["clarify_history_categories"] = sorted(prev_categories)
            # repeat 再澄清的结构化选项在历史方向内产出（历史合法方向置顶）；
            # 已在 _repeat_clarify/_llm_fallback 挂好，这里兜底同步 decision/details 两处
            options = dec.clarify_options
            if options is not None:
                dec.details["clarify_options"] = options
            return dec
        dec.details["clarify_outcome"] = "text"
        return dec

    @staticmethod
    def _fused_candidate_skills(
        rrf_order: list[tuple[Skill, float]],
        vec_results: list[tuple[Skill, float]],
        kw_results: list[tuple[Skill, float]],
    ) -> list[Skill]:
        """跨轮兜底候选：RRF 融合序中“有正证据”（向量余弦>0 或 BM25 命中）的 Skill，保持融合序。"""
        positive_vec = {s.name for s, score in vec_results if score > 0}
        kw_names = {s.name for s, _ in kw_results}
        return [s for s, _ in rrf_order if s.name in positive_vec or s.name in kw_names]

    def _keyword_enabled(self) -> bool:
        return self.hybrid and self.keyword_index is not None

    def _keyword_search(
        self, message: str, candidates: list[Skill], details: dict
    ) -> list[tuple[Skill, float]]:
        if not self._keyword_enabled():
            return []
        results = self.keyword_index.search(message, candidates, self.keyword_top_k)
        details["bm25_k"] = [{"skill": s.name, "score": round(score, 4)} for s, score in results]
        return results

    def _rrf_fuse(
        self,
        vec_results: list[tuple[Skill, float]],
        kw_results: list[tuple[Skill, float]],
        details: dict,
        details_key: str = "rrf",
    ) -> list[tuple[Skill, float]]:
        """Reciprocal Rank Fusion：两路各自按自身排名（rank 从 1 起）求和；仅一路出现只得那路项。"""
        if not kw_results:
            return list(vec_results)
        scores: dict[str, float] = {}
        vec_rank: dict[str, int] = {}
        kw_rank: dict[str, int] = {}
        skills: dict[str, Skill] = {}
        for rank, (s, _) in enumerate(vec_results, start=1):
            scores[s.name] = scores.get(s.name, 0.0) + 1.0 / (self.rrf_k + rank)
            vec_rank[s.name] = rank
            skills[s.name] = s
        for rank, (s, _) in enumerate(kw_results, start=1):
            scores[s.name] = scores.get(s.name, 0.0) + 1.0 / (self.rrf_k + rank)
            kw_rank[s.name] = rank
            skills[s.name] = s
        # 同分按向量路排名、再关键词路排名兜底，保证融合序确定可回放
        ordered = sorted(
            scores,
            key=lambda n: (-scores[n], vec_rank.get(n, len(vec_rank) + 1), kw_rank.get(n, len(kw_rank) + 1)),
        )
        fused = [(skills[n], scores[n]) for n in ordered]
        details[details_key] = [{"skill": n, "score": round(scores[n], 6)} for n in ordered]
        return fused

    def _is_high_confidence(self, results: list[tuple[Skill, float]]) -> bool:
        """向量高置信门：top1 ≥ threshold 且与 top2 分差 ≥ margin（只看向量余弦，不看 RRF 分）。"""
        if not results:
            return False
        top1 = results[0][1]
        top2 = results[1][1] if len(results) > 1 else 1.0
        return top1 >= self.score_threshold and (top1 - top2) >= self.margin

    def _release_vector(
        self,
        results: list[tuple[Skill, float]],
        rrf_order: list[tuple[Skill, float]],
        kw_results: list[tuple[Skill, float]],
        candidates: list[Skill],
    ) -> list[Skill]:
        """高置信放行集：向量 floor 截断 + BM25 rank≤3 保底并入，按 RRF 融合序排列。"""
        # 高置信：top-k 再按分数下限截断（丢弃零相关/噪声结果），避免收窄集仍含无关工具
        floor = self.score_threshold * 0.5
        tools = [s for s, score in results if score >= floor]
        # 保底并入 BM25 rank ≤ 3：防精确 token（订单号/型号/缩写）被向量路漏召回误杀
        tools = self._merge_keyword_guard(tools, kw_results, candidates)
        # 放行集按 RRF 融合序排列（无关键词路时融合序即向量序，行为不变）
        order = [s for s, _ in rrf_order if any(t.name == s.name for t in tools)]
        # 防御性补尾：tools 成员必来自 vec_results 或 kw_results（hybrid 有效时二者都在 rrf_order 中），
        # 正常流程不可达；保留以防未来放行集来源扩展后出现未被融合序覆盖的成员而丢失工具
        seen = {s.name for s in order}
        order.extend(s for s in tools if s.name not in seen)
        return order

    @staticmethod
    def _merge_keyword_guard(
        tools: list[Skill], kw_results: list[tuple[Skill, float]], candidates: list[Skill]
    ) -> list[Skill]:
        """高置信放行集保底并入 BM25 rank ≤ 3（限 candidates、按名去重、保持向量序后追加）。"""
        if not kw_results:
            return tools
        names = {s.name for s in tools}
        candidate_names = {s.name for s in candidates}
        merged = list(tools)
        for s, _ in kw_results[:3]:
            if s.name not in names and s.name in candidate_names:
                merged.append(s)
                names.add(s.name)
        return merged

    async def _llm_fallback(
        self,
        message: str,
        candidates: list[Skill],
        details: dict,
        kw_results: list[tuple[Skill, float]] | None = None,
        all_candidates: list[Skill] | None = None,
        env_candidates: list[Skill] | None = None,
        history_categories: list[str] | None = None,
        retrieval_skills: list[Skill] | None = None,
    ) -> RouteDecision:
        env_candidates = env_candidates if env_candidates is not None else candidates
        retrieval_skills = retrieval_skills if retrieval_skills is not None else list(candidates)
        categories = sorted({s.category for s in candidates if s.category})
        details["categories"] = categories
        skill_names = sorted({s.name for s in candidates})
        try:
            result = await self._classify(message, categories, skill_names, kw_results or [])
        except Exception as exc:
            logger.warning("route_llm_failed", error=str(exc))
            details["reason"] = "route_llm_failed"
            # LLM 自身异常时回退系统全量（降级收窄轮次传 route 持有原始全量；正常路 candidates 即全量）
            full = all_candidates if all_candidates is not None else candidates
            return RouteDecision(PATH_FALLBACK, list(full), details=details)

        category = result.get("category") or _UNKNOWN
        confidence = result.get("confidence")
        details["llm_category"] = category
        details["llm_confidence"] = confidence
        details["llm_reason"] = result.get("reason")

        if category == _CHITCHAT:
            return RouteDecision(PATH_CHITCHAT, [], details=details)

        # 一级：高置信且点名了候选集内 Skill -> 收窄到点名 Skill；
        # 集外名丢弃（交集），部分非法仍按合法名收窄，全部非法则落到选域/澄清
        named = self._named_skills(result.get("skills"), candidates)
        details["llm_skills"] = [s.name for s in named]
        if self._is_high_llm_confidence(confidence) and named:
            return RouteDecision(PATH_LLM, named, details=details)

        # 二级：合法 category -> 该域全部候选 Skill
        if category and category != _UNKNOWN and category in categories:
            tools = [s for s in candidates if s.category == category]
            if tools:
                return RouteDecision(PATH_LLM, tools, details=details)
        # 三级：unknown / 无法确定 -> 澄清
        clarify = result.get("clarify_question") or self._default_clarify(categories)
        options = None
        if self.clarify_options:
            options = self._build_clarify_options(
                result=result,
                env_candidates=env_candidates,
                scope_skill_names=set(skill_names),
                retrieval_skills=retrieval_skills,
                history_categories=history_categories,
            )
        if options is not None:
            details["clarify_options"] = options
        return RouteDecision(
            PATH_CLARIFY, [], clarify_text=clarify, clarify_options=options, details=details
        )

    def _is_high_llm_confidence(self, confidence: Any) -> bool:
        return isinstance(confidence, (int, float)) and not isinstance(confidence, bool) \
            and confidence >= self.llm_conf_high

    @staticmethod
    def _named_skills(raw: Any, candidates: list[Skill]) -> list[Skill]:
        """解析 LLM 点名 skills：按 LLM 给出顺序去重，与该次 fallback 候选集取交集。"""
        if not isinstance(raw, list):
            return []
        by_name = {s.name: s for s in candidates}
        named: list[Skill] = []
        seen: set[str] = set()
        for name in raw:
            if isinstance(name, str) and name in by_name and name not in seen:
                named.append(by_name[name])
                seen.add(name)
        return named

    async def _classify(
        self,
        message: str,
        categories: list[str],
        skill_names: list[str],
        kw_results: list[tuple[Skill, float]] | None = None,
    ) -> dict:
        cat_list = "、".join(categories) if categories else "（无明确类别）"
        skill_list = "、".join(skill_names) if skill_names else "（无）"
        sys = (
            "你是意图路由分类器。根据用户消息，从给定技能类别中选出最匹配的一个类别；"
            "若高置信且能定位到具体技能，可在 skills 中点名一个或多个具体技能名；"
            "若只是闲聊寒暄选 chitchat；若信息不足无法判断选 unknown。只输出 JSON，不要输出其他内容。"
        )
        hint = self._keyword_hint(kw_results or [])
        user = (
            f"可选技能类别：{cat_list}\n"
            f"可选技能名：{skill_list}\n"
            f"用户消息：{message}\n"
            f"{hint}"
            f'输出 JSON：{{"category": "类别名或 {_CHITCHAT} 或 {_UNKNOWN}", '
            f'"confidence": 0到1的数, '
            f'"skills": ["仅在高置信时填写、且必须来自上面可选技能名的具体技能名数组，无法确定时给空数组"], '
            f'"categories": ["无法确定唯一类别时，给2-4个可能的候选类别名，必须来自上面可选技能类别"], '
            f'"reason": "简述", '
            f'"clarify_question": "当 category 为 unknown 时，向用户提出的澄清问题（列出可选方向）"}}'
        )
        resp = await self.llm.ainvoke([SystemMessage(content=sys), HumanMessage(content=user)])
        return self._parse_json(getattr(resp, "content", "") or "")

    @staticmethod
    def _keyword_hint(kw_results: list[tuple[Skill, float]], top_n: int = 3) -> str:
        """BM25 命中文本注入兜底 prompt（只加提示文本，不改 JSON 契约）；无命中时为空，prompt 逐字节不变。"""
        if not kw_results:
            return ""
        lines = [f"- {s.name}：{s.description or ''}".rstrip("：") for s, _ in kw_results[:top_n]]
        return (
            "关键词检索提示（以下技能与消息中的精确词/型号更相关，供参考，不要照抄）：\n"
            + "\n".join(lines)
            + "\n"
        )

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
