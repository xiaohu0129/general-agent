"""历史消息 keyset 分页 + 产物标志测试（load_web_messages）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from general_agent.message_store import MessageStore


class FakeCursor:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, args=None):
        self.pool.last_sql = sql
        self.pool.last_args = args

    async def fetchall(self):
        return list(self.pool.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    def cursor(self, *a, **k):
        return FakeCursor(self.pool)


class FakePool:
    def __init__(self):
        self.rows = []
        self.last_sql = None
        self.last_args = None

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)


def _row(mid, role="user", content="hi", ref=None):
    return {
        "id": mid, "turn_id": f"t{mid}", "role": role, "content": content,
        "tool_calls": None, "tool_call_id": None, "created_at": None,
        "content_ref": ref, "content_size": 1234 if ref else None,
        "content_kind": "json" if ref else None,
    }


def _store(rows):
    pool = FakePool()
    pool.rows = rows
    return MessageStore(pool=pool), pool


async def test_first_page_desc_then_asc_with_cursor():
    # 模拟 SQL LIMIT n+1 返回最新 4 行（DESC：6,5,4,3），触发 hasMore
    ms, pool = _store([_row(6), _row(5), _row(4), _row(3)])
    page = await ms.load_web_messages("s", "dev", "u", "sid", limit=3)
    ids = [m["messageId"] for m in page["messages"]]
    assert ids == [4, 5, 6]  # 截取最新 3 行并反转为升序
    assert page["hasMore"] is True
    assert page["nextCursor"] == 4  # 本页最早 id
    # 核心分页 SQL 子句与 n+1 探测参数
    assert "ORDER BY id DESC" in pool.last_sql
    assert "LIMIT %s" in pool.last_sql
    assert pool.last_args[-1] == 4  # limit+1
    assert "id < %s" not in pool.last_sql  # 首页无游标


async def test_last_page_no_more():
    ms, pool = _store([_row(2), _row(1)])
    page = await ms.load_web_messages("s", "dev", "u", "sid", before=3, limit=3)
    assert [m["messageId"] for m in page["messages"]] == [1, 2]
    assert page["hasMore"] is False
    assert page["nextCursor"] is None
    # before 游标透传：SQL 含 id < %s，且参数含游标值，LIMIT 仍为 n+1
    assert pool.last_sql is not None
    assert "id < %s" in pool.last_sql
    assert 3 in pool.last_args
    assert pool.last_args[-1] == 4  # limit+1


async def test_offloaded_message_exposes_artifact_flags():
    ms, _ = _store([_row(9, role="tool", content="head…", ref="u/sid/t/abc.json")])
    page = await ms.load_web_messages("s", "dev", "u", "sid", limit=10)
    m = page["messages"][0]
    assert m["contentRef"] == "u/sid/t/abc.json"
    assert m["contentSize"] == 1234
    assert m["contentKind"] == "json"
    assert m["content"] == "head…"  # 行内仅 head，不含 blob
