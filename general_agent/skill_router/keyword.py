"""Skill 关键词索引：零依赖分词 + 纯内存 BM25（tf/df/avgdl）+ 段内 max 聚合。

与向量路（index.py）同构的多段结构：每 Skill 的 name+category+description 为 base 段，
每条 example 各为一段；段内标准 BM25 打分，Skill 得分取各段 max（命中任一用法即得高分）。
订单号/型号/拼音缩写（"SKU-8800""OA""订单 123"）等精确 token 不被语义化，是稠密向量路
的确定性补召回手段。纯内存 dict 统计、不落盘、不占缓存版本键、不依赖 embedder/网络。
"""
from __future__ import annotations

import math
import re

from ..logging_setup import get_logger
from ..skills.base import Skill
from .index import index_text

logger = get_logger(__name__)

# BM25 调节参数（design D10 钉死）
BM25_K1 = 1.5
BM25_B = 0.75

# 拉丁字母/数字串整体成 token，连字符可串联多段（SKU-8800 整体保留）；统一 lower
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_CJK_RE = re.compile(r"[一-鿿]")

# 高频虚词：不产出 token，且作为中文连续段分隔符（不参与 bi-gram，避免"的订"噪声）
_STOPWORDS = frozenset("的了吗呢是我你这那把被不没在有和与啊呀吧么")


def tokenize(text: str) -> list[str]:
    """零依赖分词纯函数：拉丁/数字串整体 lower 保留；中文非停用字按 bi-gram 滑窗，孤立单字成 unigram。"""
    tokens: list[str] = []
    cursor = 0
    for m in _TOKEN_RE.finditer(text):
        tokens.extend(_cjk_grams(text[cursor:m.start()]))
        tokens.append(m.group(0).lower())
        cursor = m.end()
    tokens.extend(_cjk_grams(text[cursor:]))
    return tokens


def _cjk_grams(chunk: str) -> list[str]:
    """拉丁 token 间隙中的文本：连续非停用汉字成段，段内相邻两字 bi-gram；单字段成 unigram。"""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) == 1:
            out.append(run[0])
        elif len(run) > 1:
            out.extend("".join(run[i:i + 2]) for i in range(len(run) - 1))

    for ch in chunk:
        if _CJK_RE.match(ch) and ch not in _STOPWORDS:
            run.append(ch)
        else:
            flush()
            run = []
    flush()
    return out


class KeywordIndex:
    """纯内存 BM25 索引：段为统计文档，Skill 得分为其各段得分的 max。"""

    def __init__(self, *, k1: float = BM25_K1, b: float = BM25_B) -> None:
        self.k1 = k1
        self.b = b
        self.skills: list[Skill] = []
        self._seg_tokens: list[list[str]] = []
        self._seg_tf: list[dict[str, int]] = []
        self._skill_segs: dict[str, list[int]] = {}
        self.df: dict[str, int] = {}
        self.doc_count = 0
        self.avgdl = 0.0

    def build(self, skills: list[Skill]) -> "KeywordIndex":
        self.skills = list(skills)
        self._seg_tokens = []
        self._seg_tf = []
        self._skill_segs = {s.name: [] for s in skills}
        self.df = {}
        for s in skills:
            segments = [index_text(s), *(s.examples or [])]
            for text in segments:
                toks = tokenize(text)
                seg_id = len(self._seg_tokens)
                self._seg_tokens.append(toks)
                tf: dict[str, int] = {}
                for t in toks:
                    tf[t] = tf.get(t, 0) + 1
                self._seg_tf.append(tf)
                self._skill_segs[s.name].append(seg_id)
                for t in tf:
                    self.df[t] = self.df.get(t, 0) + 1
        self.doc_count = len(self._seg_tokens)
        total_len = sum(len(toks) for toks in self._seg_tokens)
        self.avgdl = total_len / self.doc_count if self.doc_count else 0.0
        logger.info(
            "keyword_index_built",
            skills=len(skills),
            segments=self.doc_count,
            vocab=len(self.df),
        )
        return self

    def search(
        self, query: str, candidates: list[Skill], top_k: int
    ) -> list[tuple[Skill, float]]:
        """对 candidates 内 Skill 按段内 max BM25 计分，降序返回前 top_k；空索引/无命中返回 []。"""
        if not self.doc_count:
            return []
        q_tokens = [t for t in tokenize(query) if t in self.df]
        if not q_tokens:
            return []
        scored: list[tuple[Skill, float]] = []
        for s in candidates:
            seg_ids = self._skill_segs.get(s.name)
            if not seg_ids:
                continue
            score = max(self._segment_score(seg_id, q_tokens) for seg_id in seg_ids)
            if score > 0:
                scored.append((s, score))
        # 同分时按 Skill 名兜底，保证排序确定性可回放
        scored.sort(key=lambda x: (-x[1], x[0].name))
        return scored[:top_k]

    def _segment_score(self, seg_id: int, q_tokens: list[str]) -> float:
        tf = self._seg_tf[seg_id]
        dl = len(self._seg_tokens[seg_id])
        score = 0.0
        for q in q_tokens:
            f = tf.get(q, 0)
            if not f:
                continue
            n = self.df[q]
            idf = math.log(1.0 + (self.doc_count - n + 0.5) / (n + 0.5))
            denom = f + self.k1 * (1.0 - self.b + self.b * dl / (self.avgdl or 1.0))
            score += idf * f * (self.k1 + 1.0) / denom
        return score
