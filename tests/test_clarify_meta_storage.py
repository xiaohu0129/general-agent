"""meta 列存储断点测试（tasks 4.2）：append/load/load_web/update + DDL 惰性迁移。

全部用 fake aiomysql pool/cursor（照 test_message_pagination.py 风格），不连真实 MySQL。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import general_agent.mysql_client as mysql_client
from general_agent.message_store import MessageStore


class FakeCursor:
    def __init__(self, pool, dict_cursor=False):
        self.pool = pool
        self.rowcount = pool.rowcount
        self.lastrowid = pool.lastrowid

    async def execute(self, sql, args=None):
        self.pool.executed.append((sql, args))

    async def fetchall(self):
        return list(self.pool.rows)

    async def fetchone(self):
        if self.pool.fetchone_values:
            return self.pool.fetchone_values.pop(0)
        return None

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
    def __init__(self, rows=None, *, rowcount=0, fetchone_values=None, lastrowid=1):
        self.rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.fetchone_values = list(fetchone_values or [])
        self.executed: list[tuple] = []
        self.last_sql = None
        self.last_args = None

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)

    def _last(self):
        return self.executed[-1]


def _store(rows=None, *, rowcount=0, fetchone_values=None):
    pool = FakePool(rows, rowcount=rowcount, fetchone_values=fetchone_values)
    return MessageStore(pool=pool), pool


# ---------------- append_message：meta 写入 ----------------
async def test_append_meta_dict_serialized_into_meta_column():
    ms, pool = _store()
    meta = {"kind": "clarify", "categories": ["退款"], "options": [{"label": "退款", "value": "category:refund"}]}
    await ms.append_message("svc", "dev", "u1", "s1", "t1", "assistant", "请选择", meta=meta)
    sql, args = pool._last()
    assert "meta" in sql
    # 列数与占位符数一致（13 列）
    col_part = sql[sql.index("(") + 1: sql.index(")")]
    assert sql.count("%s") == len([c for c in col_part.split(",") if c.strip()])
    assert json.dumps(meta, ensure_ascii=False) in args
    # ensure_ascii=False：中文不转义
    assert any(isinstance(a, str) and "退款" in a for a in args)


async def test_append_meta_none_binds_null():
    ms, pool = _store()
    await ms.append_message("svc", "dev", "u1", "s1", "t1", "user", "你好", meta=None)
    _, args = pool._last()
    # meta 列参数为 None（NULL）；其余既有参数位置不乱
    assert args[-1] is None


# ---------------- load_messages：meta/turn_id 读取与坏值容错 ----------------
async def test_load_messages_parses_meta_json_and_exposes_turn_id():
    rows = [
        # 模拟 LIMIT 倒序返回（新行在前），load_messages 内部反转为升序
        {"role": "assistant", "content": "请选择", "tool_calls": None, "tool_call_id": None,
         "turn_id": "t1", "meta": json.dumps({"kind": "clarify", "options": []}, ensure_ascii=False)},
        {"role": "user", "content": "退款", "tool_calls": None, "tool_call_id": None,
         "turn_id": "t1", "meta": None},
    ]
    ms, pool = _store(rows)
    out = await ms.load_messages("svc", "dev", "u1", "s1", limit=10)
    sql, _ = pool._last()
    assert "meta" in sql and "turn_id" in sql
    assert out[0]["turn_id"] == "t1" and out[0]["meta"] is None
    assert out[0]["role"] == "user"
    assert out[1]["meta"] == {"kind": "clarify", "options": []}


async def test_load_messages_broken_meta_and_missing_column_do_not_raise():
    rows = [
        # 坏 JSON：不抛，回落 None
        {"role": "assistant", "content": "x", "tool_calls": None, "tool_call_id": None,
         "turn_id": "t1", "meta": "{not json"},
        # 存量行无 meta 列（键缺失）：get 兜底 None
        {"role": "user", "content": "y", "tool_calls": None, "tool_call_id": None,
         "turn_id": "t2"},
    ]
    ms, _ = _store(rows)
    out = await ms.load_messages("svc", "dev", "u1", "s1")
    assert out[0]["meta"] is None
    assert out[1].get("meta") is None


# ---------------- load_web_messages：DTO options/selected ----------------
async def test_load_web_messages_dto_exposes_options_and_selected():
    meta = {
        "kind": "clarify",
        "options": [{"label": "退款", "value": "category:refund"}],
        "selected": "category:refund",
    }
    rows = [
        {"id": 2, "turn_id": "t1", "role": "assistant", "content": "请选择",
         "tool_calls": None, "tool_call_id": None, "created_at": None,
         "content_ref": None, "content_size": None, "content_kind": None,
         "meta": json.dumps(meta, ensure_ascii=False)},
        {"id": 1, "turn_id": "t1", "role": "user", "content": "退款",
         "tool_calls": None, "tool_call_id": None, "created_at": None,
         "content_ref": None, "content_size": None, "content_kind": None,
         "meta": None},
    ]
    ms, pool = _store(rows)
    page = await ms.load_web_messages("svc", "dev", "u1", "s1", limit=10)
    assert "meta" in pool._last()[0]
    msgs = page["messages"]
    assert msgs[0]["options"] is None and msgs[0]["selected"] is None
    assert msgs[1]["options"] == meta["options"]
    assert msgs[1]["selected"] == "category:refund"


async def test_load_web_messages_broken_meta_does_not_raise():
    rows = [
        {"id": 1, "turn_id": "t1", "role": "assistant", "content": "x",
         "tool_calls": None, "tool_call_id": None, "created_at": None,
         "content_ref": None, "content_size": None, "content_kind": None,
         "meta": "oops"},
    ]
    ms, _ = _store(rows)
    page = await ms.load_web_messages("svc", "dev", "u1", "s1", limit=10)
    m = page["messages"][0]
    assert m["options"] is None and m["selected"] is None


# ---------------- update_clarify_selected ----------------
async def test_update_clarify_selected_sql_keys_and_payload():
    ms, pool = _store(rowcount=1)
    changed = await ms.update_clarify_selected(
        "svc", "dev", "u1", "s1", "Tn", "category:refund"
    )
    sql, args = pool._last()
    assert "UPDATE agent_message" in sql
    assert "JSON_SET(COALESCE(meta" in sql
    assert "'$.selected'" in sql or "$.selected" in sql
    # selected 以 JSON 字符串字面量写入；四归属键 + turn_id + role 齐全
    assert args[0] == json.dumps("category:refund", ensure_ascii=False)
    assert args[1:6] == ("svc", "dev", "u1", "s1", "Tn")
    assert "role='assistant'" in sql.replace('"', "'") or "role = 'assistant'" in sql
    assert changed == 1  # 返回 cur.rowcount


async def test_update_clarify_selected_missing_returns_zero():
    ms, _ = _store(rowcount=0)
    changed = await ms.update_clarify_selected("svc", "dev", "u1", "s1", "nope", "category:refund")
    assert changed == 0


# ---------------- DDL 与惰性迁移 ----------------
def test_schema_ddl_contains_meta_json_column():
    # 新库建表一步到位：meta 位于 tool_call_id 之后
    ddl = mysql_client.SCHEMA_DDL
    assert "meta JSON NULL" in ddl
    agent_ddl = ddl.split("CREATE TABLE IF NOT EXISTS agent_user")[0]
    assert agent_ddl.index("tool_call_id") < agent_ddl.index("meta JSON NULL")


async def test_init_schema_alters_when_meta_column_missing():
    pool = FakePool(fetchone_values=[(0,)])  # information_schema 查无 meta 列
    await mysql_client.init_schema(pool)
    sqls = [s for s, _ in pool.executed]
    alters = [s for s in sqls if "ALTER TABLE agent_message ADD COLUMN meta JSON NULL" in s]
    assert len(alters) == 1


async def test_init_schema_skips_alter_when_meta_column_exists():
    pool = FakePool(fetchone_values=[(1,)])  # 列已存在
    await mysql_client.init_schema(pool)
    sqls = [s for s, _ in pool.executed]
    assert not any("ALTER TABLE" in s for s in sqls)
    checks = [s for s in sqls if "information_schema.columns" in s]
    assert len(checks) == 1
