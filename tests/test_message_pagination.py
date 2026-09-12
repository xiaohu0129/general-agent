"""历史消息 keyset 分页 + 产物标志测试（load_web_messages）。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from general_agent.message_store import MessageStore, _tool_status


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


# ---------------- tool 行历史回放 status（task 11.1） ----------------
def test_tool_status_pure_function():
    # 错误结果（真实落库形态：{"errorCode": ..., "message": ...}）
    assert _tool_status(json.dumps({"errorCode": "NOT_FOUND", "message": "x"})) == "error"
    assert _tool_status('   {"errorCode": "MISSING_ARGS", "message": "y"}  ') == "error"
    # 普通 JSON 结果（即使含别的 status 键）/ 非 JSON 文本 / 空串 → success
    assert _tool_status(json.dumps({"taskId": "J1", "status": "PENDING"})) == "success"
    assert _tool_status("纯文本工具输出") == "success"
    assert _tool_status("") == "success"
    # errorCode 空串 / null / 非字符串 → success
    assert _tool_status(json.dumps({"errorCode": ""})) == "success"
    assert _tool_status(json.dumps({"errorCode": None})) == "success"
    assert _tool_status(json.dumps({"errorCode": 0})) == "success"
    # JSON 但非对象 → success
    assert _tool_status(json.dumps([1, 2])) == "success"
    assert _tool_status(json.dumps("oops")) == "success"


async def test_tool_row_status_derived_from_content_json():
    err = json.dumps({"errorCode": "NOT_FOUND", "message": "task bad not found"}, ensure_ascii=False)
    ok = json.dumps({"taskId": "J123", "status": "PENDING"}, ensure_ascii=False)
    ms, _ = _store(
        [
            _row(3, role="tool", content=err),
            _row(2, role="tool", content=ok),
            _row(1, role="tool", content="纯文本结果"),
        ]
    )
    msgs = (await ms.load_web_messages("s", "dev", "u", "sid", limit=10))["messages"]
    assert [m["status"] for m in msgs] == ["success", "success", "error"]


async def test_tool_row_blank_error_code_is_success():
    ms, _ = _store(
        [
            _row(2, role="tool", content=json.dumps({"errorCode": ""})),
            _row(1, role="tool", content=json.dumps({"errorCode": None, "message": "?"})),
        ]
    )
    msgs = (await ms.load_web_messages("s", "dev", "u", "sid", limit=10))["messages"]
    assert [m["status"] for m in msgs] == ["success", "success"]


async def test_non_tool_rows_have_null_status():
    err = json.dumps({"errorCode": "NOT_FOUND", "message": "x"}, ensure_ascii=False)
    ms, _ = _store(
        [
            _row(3, role="tool", content=err),
            _row(2, role="assistant", content="调用工具"),
            _row(1, role="user", content="帮我查"),
        ]
    )
    msgs = (await ms.load_web_messages("s", "dev", "u", "sid", limit=10))["messages"]
    assert [m["status"] for m in msgs] == [None, None, "error"]


async def test_offloaded_tool_row_status_derived_from_inline_head():
    # 错误 JSON 极小不会外置；外置 tool 行仅按行内 head 派生，不读 blob：
    # head 完整可解析且带 errorCode -> error；head 已被截断标记破坏 -> 回落 success
    good_head = json.dumps({"errorCode": "E", "message": "x"})
    truncated_head = '{"errorCode": "E", "messag' + "…[截断]"
    ms, _ = _store(
        [
            _row(2, role="tool", content=good_head, ref="u/sid/t/a.json"),
            _row(1, role="tool", content=truncated_head, ref="u/sid/t/b.json"),
        ]
    )
    msgs = (await ms.load_web_messages("s", "dev", "u", "sid", limit=10))["messages"]
    assert [m["status"] for m in msgs] == ["success", "error"]
