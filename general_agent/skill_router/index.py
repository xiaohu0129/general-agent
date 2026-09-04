"""Skill 向量索引：embedding 构建 + 元数据哈希本地缓存 + 内存余弦 top-k。

几百个 Skill 向量启动时一次性构建，纯 Python 余弦（归一化后点积）微秒级；
索引按 Skill 元数据哈希缓存到本地文件，元数据未变重启免 embedding。
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


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def index_text(s: Skill) -> str:
    """聚合 Skill 语义文本：category + name + description + examples（示例为语义主体）。"""
    parts = [s.description or ""]
    if s.examples:
        parts.extend(s.examples)
    body = "\n".join(p for p in parts if p)
    cat = f"技能类别：{s.category}\n" if s.category else ""
    return f"{cat}技能名：{s.name}\n{body}"


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
    def __init__(self, embedder, *, cache_dir: str = "", model_id: str = "") -> None:
        self.embedder = embedder
        self.cache_dir = cache_dir
        self.model_id = model_id
        self.skills: list[Skill] = []
        self._vectors: list[list[float]] = []
        self.hash = ""
        self.ready = False

    @property
    def version(self) -> str:
        return f"{self.model_id}:{self.hash}"

    async def build(self, skills: list[Skill]) -> "SkillIndex":
        self.skills = list(skills)
        self.hash = metadata_hash(skills)
        cached = self._load_cache()
        if cached is not None:
            self._vectors = cached
            self.ready = True
            logger.info("skill_index_cache_hit", model=self.model_id, hash=self.hash, skills=len(skills))
            return self
        if not skills:
            self.ready = True  # 无 Skill：索引空但可用（检索返回空）
            return self
        try:
            vecs = await self.embedder.embed_texts([index_text(s) for s in skills])
            self._vectors = [_normalize(v) for v in vecs]
            self.ready = True
            self._save_cache()
            logger.info("skill_index_built", model=self.model_id, hash=self.hash, skills=len(skills))
        except Exception as exc:
            # embedding 不可用且无缓存：标记不可用，路由降级（不阻断进程启动）
            self._vectors = []
            self.ready = False
            logger.warning("skill_index_build_failed_degraded", error=str(exc), model=self.model_id)
        return self

    def search(self, query_vec: list[float], candidates: list[Skill], top_k: int) -> list[tuple[Skill, float]]:
        if not self.ready or not self._vectors:
            return []
        by_name = {s.name: i for i, s in enumerate(self.skills)}
        q = _normalize(query_vec)
        scored: list[tuple[Skill, float]] = []
        for s in candidates:
            idx = by_name.get(s.name)
            if idx is None:
                continue
            scored.append((s, cosine(q, self._vectors[idx])))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _cache_path(self) -> Path | None:
        if not self.cache_dir:
            return None
        return Path(self.cache_dir) / "skill_index.json"

    def _load_cache(self) -> list[list[float]] | None:
        path = self._cache_path()
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("hash") != self.hash or data.get("model") != self.model_id:
                return None
            if data.get("names") != [s.name for s in self.skills]:
                return None
            return [_normalize(v) for v in data.get("vectors", [])]
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
                        "model": self.model_id,
                        "hash": self.hash,
                        "names": [s.name for s in self.skills],
                        "vectors": self._vectors,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("skill_index_cache_write_failed", error=str(exc))
