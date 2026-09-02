"""消息历史持久化（MySQL）：身份作用域 CRUD，越权在 SQL WHERE 层即被挡。

C2：无状态图 + MySQL 消息表。每轮 load_messages 载入历史（trim/summarize 后喂图），
新消息 append_message 追加。身份 (service,env,user) + session_id 进 WHERE，跨租户/用户读不到。
"""
from __future__ import annotations

import json
import re
from typing import Any

import aiomysql

from .logging_setup import get_logger

logger = get_logger(__name__)

TABLE = "agent_message"
_TRUNC_MARK = "\n…[完整内容已外置到产物存储，此处为截断摘要]"
_SEG_SANITIZE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_seg(value: str) -> str:
    """清洗 blob 路径段（身份键），保证只含安全字符，防路径穿越。"""
    return _SEG_SANITIZE.sub("_", value or "x") or "x"


def _detect_kind(content: str) -> tuple[str, str]:
    """返回 (kind, ext)：JSON 内容用 .json，其余按文本 .txt。"""
    if content.lstrip().startswith(("{","[")):
        return "json", ".json"
    return "text", ".txt"


class MessageStore:
    """MySQL 消息历史。可注入 pool/blob_store 以便测试，默认用 get_mysql()/配置。"""

    def __init__(
        self,
        pool: aiomysql.Pool | None = None,
        blob_store=None,
        inline_threshold: int | None = None,
        head_chars: int | None = None,
    ) -> None:
        self._pool = pool
        self._blob_store = blob_store
        self._inline_threshold = inline_threshold
        self._head_chars = head_chars

    async def _pool_obj(self):
        if self._pool is not None:
            return self._pool
        from .mysql_client import get_mysql

        return await get_mysql()

    @property
    def blob_store(self):
        return self._blob_store

    def _artifact_cfg(self):
        """返回 (blob_store, inline_threshold, head_chars)；未注入时从配置读。"""
        if self._inline_threshold is not None:
            return self._blob_store, self._inline_threshold, self._head_chars or 2000
        from .config import get_settings

        a = get_settings().artifacts
        return self._blob_store, a.inline_threshold, a.head_chars

    async def append_message(
        self,
        service: str,
        env: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
    ) -> int:
        content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        blob_store, threshold, head_chars = self._artifact_cfg()

        content_ref = None
        content_size = None
        content_kind = None
        body = content.encode("utf-8")
        stored_content = content
        if blob_store is not None and len(body) > threshold:
            # 大产物外置：完整内容写 blob，行内仅保留 head 摘要。
            # 外置失败（磁盘满/目录不可写）时回退为内联全量，best-effort 不中断落库；
            # 超过 MEDIUMTEXT(16MB) 时退化为 head 截断，避免 INSERT 失败丢整轮。
            kind, ext = _detect_kind(content)
            scope = (_safe_seg(user_id), _safe_seg(session_id), _safe_seg(turn_id))
            try:
                content_ref = await blob_store.put(scope, body, ext=ext)
                content_size = len(body)
                content_kind = kind
                stored_content = content[:head_chars] + _TRUNC_MARK
            except Exception as exc:
                logger.warning("artifact_offload_failed_inline_fallback", sessionId=session_id, error=str(exc))
                content_ref = None
                content_size = None
                content_kind = None
                max_inline = 16 * 1024 * 1024
                stored_content = content if len(body) <= max_inline else content[:head_chars] + _TRUNC_MARK

        pool = await self._pool_obj()
        sql = (
            f"INSERT INTO {TABLE} "
            "(service, env, user_id, session_id, turn_id, role, content, tool_calls, "
            "tool_call_id, content_ref, content_size, content_kind) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        args = (
            service,
            env,
            user_id,
            session_id,
            turn_id,
            role,
            stored_content,
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls is not None else None,
            tool_call_id,
            content_ref,
            content_size,
            content_kind,
        )
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                mid = cur.lastrowid
        logger.debug("message_appended", id=mid, sessionId=session_id, role=role, offloaded=content_ref is not None)
        return mid

    async def load_messages(
        self,
        service: str,
        env: str,
        user_id: str,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        pool = await self._pool_obj()
        if limit is not None:
            sql = (
                f"SELECT role, content, tool_calls, tool_call_id FROM {TABLE} "
                "WHERE service=%s AND env=%s AND user_id=%s AND session_id=%s "
                "ORDER BY id DESC LIMIT %s"
            )
            args: tuple = (service, env, user_id, session_id, limit)
        else:
            sql = (
                f"SELECT role, content, tool_calls, tool_call_id FROM {TABLE} "
                "WHERE service=%s AND env=%s AND user_id=%s AND session_id=%s "
                "ORDER BY id ASC"
            )
            args = (service, env, user_id, session_id)
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, args)
                rows = await cur.fetchall()
        if limit is not None:
            rows = list(reversed(rows))
        for r in rows:
            if r.get("tool_calls"):
                r["tool_calls"] = json.loads(r["tool_calls"])
        return list(rows)

    async def count_messages(
        self, service: str, env: str, user_id: str, session_id: str
    ) -> int:
        pool = await self._pool_obj()
        sql = (
            f"SELECT COUNT(*) FROM {TABLE} "
            "WHERE service=%s AND env=%s AND user_id=%s AND session_id=%s"
        )
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (service, env, user_id, session_id))
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def load_web_messages(
        self,
        service: str,
        env: str,
        user_id: str,
        session_id: str,
        *,
        before: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """面向前端的历史消息：keyset 分页（按 id 倒序取一页后反转为升序）。

        返回 {messages: [...升序...], nextCursor, hasMore}。
        超大外置产物仅返回行内 head + contentRef/contentSize/contentKind 标志，不内联 blob。
        """
        limit = max(1, min(int(limit), 200))  # 默认 50、硬上限 200
        pool = await self._pool_obj()
        sql = (
            f"SELECT id, turn_id, role, content, tool_calls, tool_call_id, created_at, "
            f"content_ref, content_size, content_kind FROM {TABLE} "
            "WHERE service=%s AND env=%s AND user_id=%s AND session_id=%s"
        )
        args: list = [service, env, user_id, session_id]
        if before is not None:
            sql += " AND id < %s"
            args.append(before)
        sql += " ORDER BY id DESC LIMIT %s"
        args.append(limit + 1)  # 多取一条判断是否还有更多
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, tuple(args))
                rows = await cur.fetchall()

        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = page[-1]["id"] if has_more and page else None
        page = list(reversed(page))  # 反转为时间升序

        messages: list[dict[str, Any]] = []
        for r in page:
            tcs = r.get("tool_calls")
            if isinstance(tcs, str):
                try:
                    tcs = json.loads(tcs)
                except Exception:
                    tcs = None
            messages.append(
                {
                    "messageId": r.get("id"),
                    "turnId": r.get("turn_id"),
                    "role": r.get("role"),
                    "content": r.get("content") or "",
                    "toolCalls": tcs,
                    "toolCallId": r.get("tool_call_id"),
                    "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
                    "contentRef": r.get("content_ref"),
                    "contentSize": r.get("content_size"),
                    "contentKind": r.get("content_kind"),
                }
            )
        return {"messages": messages, "nextCursor": next_cursor, "hasMore": has_more}

    async def delete_older_than(self, days: int) -> int:
        pool = await self._pool_obj()
        sql = f"DELETE FROM {TABLE} WHERE created_at < (NOW() - INTERVAL %s DAY)"
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (days,))
                return cur.rowcount

    async def list_artifact_refs(
        self, service: str, env: str, user_id: str, session_id: str
    ) -> list[str]:
        """列出某会话所有外置产物的 content_ref（供删除会话时级联清理 blob）。"""
        pool = await self._pool_obj()
        sql = (
            f"SELECT content_ref FROM {TABLE} "
            "WHERE service=%s AND env=%s AND user_id=%s AND session_id=%s "
            "AND content_ref IS NOT NULL"
        )
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (service, env, user_id, session_id))
                rows = await cur.fetchall()
        return [r["content_ref"] for r in rows if r.get("content_ref")]

    async def get_artifact(
        self, service: str, env: str, user_id: str, session_id: str, message_id: int
    ) -> tuple[Any, str] | None:
        """按归属取某条外置消息的 blob 本地路径与类型；不存在/未外置/越权/文件缺失返回 None。

        返回本地文件路径（而非全量字节），供端点 FileResponse 流式发送，避免大文件全量进内存。
        """
        blob_store, _, _ = self._artifact_cfg()
        if blob_store is None:
            return None
        pool = await self._pool_obj()
        sql = (
            f"SELECT content_ref, content_kind FROM {TABLE} "
            "WHERE id=%s AND service=%s AND env=%s AND user_id=%s AND session_id=%s "
            "AND content_ref IS NOT NULL"
        )
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (message_id, service, env, user_id, session_id))
                row = await cur.fetchone()
        if not row:
            return None
        try:
            path = blob_store.local_path(row["content_ref"])
        except Exception:
            return None
        if not path.is_file():
            return None
        return path, (row.get("content_kind") or "text")