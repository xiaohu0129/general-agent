"""大产物外置存储（Tier2）：BlobStore 抽象 + 本地磁盘实现。

对话中体量大的工具结果/生成文件不内联进 MySQL，而以相对路径存放在本服务工作目录下
（默认 ./artifacts/），消息行只保留 head 摘要 + content_ref 引用。

- key 完全由服务端 id 构成（uid/session/turn/<uuid>.ext），不拼接任何用户输入，杜绝路径穿越；
- BlobStore 为薄抽象，业务代码仅依赖它，未来可加 S3/OSS 实现 drop-in 替换（对齐 Broker/RedisBroker）；
- 本地实现不跨实例共享，属单实例约束（与内存 Broker/登录态一致），多实例需换对象存储后端。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .logging_setup import get_logger

logger = get_logger(__name__)

# 单个路径段只允许字母数字、下划线、连字符（uid/session/turn 均为 hex/id，天然满足）
_SAFE_SEG = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_EXT = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


class BlobStore(Protocol):
    async def put(self, scope: tuple[str, ...], body: bytes, *, ext: str = ".bin") -> str: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    def local_path(self, key: str) -> Path: ...


class LocalBlobStore:
    """本地磁盘 blob 存储：产物以相对 key 存于 root 目录下。"""

    def __init__(self, root: str | os.PathLike = "artifacts") -> None:
        self._root = Path(root)

    def _validate_segments(self, segments: tuple[str, ...]) -> None:
        for seg in segments:
            if not seg or not _SAFE_SEG.match(seg):
                raise ValueError(f"非法 blob 路径段: {seg!r}")

    def _resolve(self, key: str) -> Path:
        # key 必须是相对路径，且每段安全；resolve 后不得越出 root（防穿越）
        if not key or key.startswith("/") or "\\" in key or ".." in key.split("/"):
            raise ValueError(f"非法 blob key: {key!r}")
        root = self._root.resolve()
        target = (self._root / key).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"blob key 越界: {key!r}")
        return target

    async def put(self, scope: tuple[str, ...], body: bytes, *, ext: str = ".bin") -> str:
        self._validate_segments(scope)
        if not _SAFE_EXT.match(ext):
            ext = ".bin"
        name = f"{uuid4().hex}{ext}"
        key = "/".join([*scope, name])
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        logger.debug("artifact_stored", key=key, bytes=len(body))
        return key

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    async def delete(self, key: str) -> None:
        try:
            path = self._resolve(key)
        except ValueError:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # best-effort：删除失败不阻断调用方
            logger.warning("artifact_delete_failed", key=key, error=str(exc))

    def local_path(self, key: str) -> Path:
        return self._resolve(key)
