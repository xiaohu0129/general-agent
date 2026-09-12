"""Skill 向量索引：多向量段 embedding 构建 + 元数据哈希本地缓存 + 段内 max-sim top-k。

每个 Skill 切多条语义段：base 段（类别/技能名/description）一条 + 每条 example 一条，
分别 embedding；检索时 query 与 Skill 段内所有向量求余弦取 max 为该 Skill 得分，
一条 Skill 的多种用法各自拥有向量，命中任一用法即得高分，不被其余 example 拉低。
几百个 Skill 向量启动时一次性构建，纯 Python 余弦（归一化后点积）微秒级；
索引按 Skill 元数据哈希缓存到本地文件，元数据未变重启免 embedding，
缓存带结构版本键，旧（单向量）缓存自动识别并重建。
embedding 不可用且无缓存时 ready=False（路由降级，不阻断启动）。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ..logging_setup import get_logger
from ..skills.base import Skill

logger = get_logger(__name__)

# 缓存结构版本：多向量扁平矩阵 + spans 对齐；旧单向量缓存（无 v）不兼容 -> 重建
CACHE_VERSION = 2


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def index_text(s: Skill) -> str:
    """base 段聚合文本：category + name + description（examples 各自成段，不在此聚合）。"""
    cat = f"技能类别：{s.category}\n" if s.category else ""
    return f"{cat}技能名：{s.name}\n{s.description or ''}"


def segment_texts(s: Skill, multi_vector: bool) -> list[str]:
    """Skill 的索引段：base 一条；multi_vector 时每条 example 原文再各一条。"""
    texts = [index_text(s)]
    if multi_vector and s.examples:
        texts.extend(s.examples)
    return texts


def metadata_hash(skills: list[Skill]) -> str:
    payload = json.dumps(
        [
            {
                "name": s.name,
                "category": s.category,
                "description": s.description,
                "examples": list(s.examples),
                "allowed_envs": list(s.allowed_envs or []),
            }
            for s in sorted(skills, key=lambda x: x.name)
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class SkillIndex:
    def __init__(
        self,
        embedder,
        *,
        cache_dir: str = "",
        model_id: str = "",
        multi_vector: bool = True,
    ) -> None:
        self.embedder = embedder
        self.cache_dir = cache_dir
        self.model_id = model_id
        self.multi_vector = multi_vector
        self.skills: list[Skill] = []
        self._vectors: list[list[float]] = []  # 按 Skill 顺序拼接的扁平归一化向量矩阵
        self.spans: list[dict] = []  # 每元素 {"skill", "has_examples", "count"}，与 _vectors 切片对齐
        self.hash = ""
        self.ready = False

    @property
    def version(self) -> str:
        return f"{self.model_id}:{self.hash}"

    @property
    def skills_without_examples(self) -> list[str]:
        """无 examples 的 Skill 名清单（供 health/可观测筛选）。"""
        return [sp["skill"] for sp in self.spans if not sp["has_examples"]]

    async def build(self, skills: list[Skill]) -> "SkillIndex":
        self.skills = list(skills)
        self.hash = metadata_hash(skills)
        cached = self._load_cache()
        if cached is not None:
            self._vectors, self.spans = cached
            self.ready = True
            logger.info(
                "skill_index_cache_hit",
                model=self.model_id,
                hash=self.hash,
                skills=len(skills),
                segments=len(self._vectors),
            )
            return self
        if not skills:
            self.spans = []
            self.ready = True  # 无 Skill：索引空但可用（检索返回空）
            return self
        # 逐 Skill 分批 embed 以获得"部分失败"粒度：多向量失败 -> 退 base 单段；仍失败 -> 缺段继续
        all_vecs: list[list[float]] = []
        spans: list[dict] = []
        degraded: list[str] = []
        for s in skills:
            segs = segment_texts(s, self.multi_vector)
            vecs = await self._embed_segments(segs)
            fell_back = False
            if vecs is None and len(segs) > 1:
                # 多向量段构建失败：退而只 embed base 单段
                vecs = await self._embed_segments([index_text(s)])
                fell_back = vecs is not None
                if fell_back:
                    degraded.append(s.name)
            span_vecs = vecs or []
            if fell_back:
                logger.warning(
                    "skill_index_skill_degraded_single_vector",
                    skill=s.name,
                    has_examples=bool(s.examples),
                )
            all_vecs.extend(span_vecs)
            spans.append(
                {"skill": s.name, "has_examples": bool(s.examples), "count": len(span_vecs)}
            )
        if not all_vecs:
            # 所有 Skill 都无可用向量：整体降级（沿用 ready=False 路径，不写缓存）
            self._vectors, self.spans, self.ready = [], [], False
            logger.warning("skill_index_build_failed_degraded", model=self.model_id)
            return self
        self._vectors = all_vecs
        self.spans = spans
        self.ready = True
        # 存在"未拿到完整段"的 Skill（多向量退 base 单段，或 base 也失败缺段）时不落缓存：
        # 否则降级状态被永久缓存，embedding 端点恢复后重启仍命中缓存，example 段永不重建
        missing = [sp["skill"] for sp in spans if sp["count"] == 0]
        if degraded or missing:
            logger.warning(
                "skill_index_partial_degraded_not_cached",
                model=self.model_id,
                degraded_single_vector=degraded,
                missing_segments=missing,
            )
        else:
            self._save_cache()
        without = self.skills_without_examples
        logger.info(
            "skill_index_built",
            model=self.model_id,
            hash=self.hash,
            skills=len(skills),
            segments=len(self._vectors),
            degraded_single_vector=degraded,
            skills_without_examples=len(without),
        )
        if without:
            # 无 examples 的 Skill 检索质量偏低，warning 汇总便于后续 health 筛选
            logger.warning(
                "skill_index_skills_without_examples",
                has_examples=False,
                count=len(without),
                skills=without,
            )
        return self

    async def _embed_segments(self, texts: list[str]) -> list[list[float]] | None:
        """批量 embed 一组段并归一化；失败或返回结构异常返回 None（调用方降级，不抛到 build 外）。"""
        try:
            vecs = await self.embedder.embed_texts(texts)
        except Exception as exc:
            logger.warning("skill_index_segment_embed_failed", error=str(exc), segments=len(texts))
            return None
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            logger.warning(
                "skill_index_segment_embed_shape_mismatch",
                expected=len(texts),
                got=len(vecs) if isinstance(vecs, list) else type(vecs).__name__,
            )
            return None
        return [_normalize(v) for v in vecs]

    def search(self, query_vec: list[float], candidates: list[Skill], top_k: int) -> list[tuple[Skill, float]]:
        if not self.ready or not self._vectors:
            return []
        # Skill -> 其段在扁平矩阵中的偏移区间
        offsets: dict[str, tuple[int, int]] = {}
        offset = 0
        for sp in self.spans:
            offsets[sp["skill"]] = (offset, offset + sp["count"])
            offset += sp["count"]
        q = _normalize(query_vec)
        scored: list[tuple[Skill, float]] = []
        for s in candidates:
            bounds = offsets.get(s.name)
            if bounds is None:
                continue
            start, end = bounds
            if start == end:
                continue  # 该 Skill 段缺失（构建时降级）
            # 段内 max-sim：命中任一用法即得该 Skill 的分
            score = max(cosine(q, self._vectors[i]) for i in range(start, end))
            scored.append((s, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _cache_path(self) -> Path | None:
        if not self.cache_dir:
            return None
        return Path(self.cache_dir) / "skill_index.json"

    def _load_cache(self) -> tuple[list[list[float]], list[dict]] | None:
        path = self._cache_path()
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # 结构版本不符（旧单向量缓存无 v）或结构异常 -> 不兼容，返回 None 触发重建
            if data.get("v") != CACHE_VERSION:
                return None
            # 多向量模式切换（元数据哈希不变）也必须重建，避免段数/语义错配
            if bool(data.get("mv")) != self.multi_vector:
                return None
            if data.get("hash") != self.hash or data.get("model") != self.model_id:
                return None
            names = [s.name for s in self.skills]
            spans = data.get("spans")
            if data.get("names") != names or not isinstance(spans, list) or len(spans) != len(names):
                return None
            vectors = data.get("vectors")
            if not isinstance(vectors, list):
                return None
            norm_spans: list[dict] = []
            for name, sp in zip(names, spans):
                if not isinstance(sp, dict) or sp.get("skill") != name:
                    return None
                count = sp.get("count")
                if not isinstance(count, int) or count < 0 or not isinstance(sp.get("has_examples"), bool):
                    return None
                norm_spans.append({"skill": name, "has_examples": sp["has_examples"], "count": count})
            if sum(sp["count"] for sp in norm_spans) != len(vectors):
                return None
            return [_normalize(v) for v in vectors], norm_spans
        except Exception as exc:
            logger.warning("skill_index_cache_read_failed", error=str(exc))
            return None

    def _save_cache(self) -> None:
        path = self._cache_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "v": CACHE_VERSION,
                        "mv": self.multi_vector,
                        "model": self.model_id,
                        "hash": self.hash,
                        "names": [s.name for s in self.skills],
                        "spans": self.spans,
                        "vectors": self._vectors,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("skill_index_cache_write_failed", error=str(exc))
