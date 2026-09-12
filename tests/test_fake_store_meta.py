"""FakeStore（conftest 内存测试替身）澄清 meta 能力测试（task 4.3）。

覆盖：append 补存 turn_id/meta、load_messages 透出、load_web_messages DTO
从 meta 解 options/selected、update_clarify_selected 按归属+turn_id 读-改-写合并。
纯内存，不依赖 MySQL/网络。
"""
from __future__ import annotations

from conftest import FakeStore


CLARIFY_META = {
    "kind": "clarify",
    "categories": ["订单", "退款"],
    "options": [
        {"label": "查询订单", "value": "category:order"},
        {"label": "申请退款", "value": "category:refund"},
    ],
}


async def _clarify_row(store, turn_id="t1", service="svc", env="dev", user="u1", session="s1"):
    await store.append_message(
        service, env, user, session, turn_id, "assistant", "请选择方向", meta=dict(CLARIFY_META)
    )


async def test_append_stores_turn_id_and_meta_and_load_messages_exposes():
    store = FakeStore()
    await store.append_message("svc", "dev", "u1", "s1", "t1", "user", "我要退款", meta=None)
    await _clarify_row(store, "t1")
    rows = await store.load_messages("svc", "dev", "u1", "s1")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["turn_id"] == "t1"
    assert rows[0]["meta"] is None  # 存量普通行 meta=None 不报错
    assert rows[1]["turn_id"] == "t1"
    assert rows[1]["meta"] == CLARIFY_META  # 含 kind/categories/options 原样透出


async def test_load_messages_scoped_by_ownership():
    store = FakeStore()
    await _clarify_row(store, "t1", user="u1", session="s1")
    await _clarify_row(store, "t1", user="u2", session="s1")
    await _clarify_row(store, "t1", user="u1", session="s2")
    rows = await store.load_messages("svc", "dev", "u1", "s1")
    assert len(rows) == 1


async def test_load_web_messages_exposes_options_and_selected_from_meta():
    store = FakeStore()
    await store.append_message("svc", "dev", "u1", "s1", "t1", "user", "退款", meta=None)
    await _clarify_row(store, "t1")
    page = await store.load_web_messages("svc", "dev", "u1", "s1", limit=10)
    msgs = page["messages"]
    assert [m["turnId"] for m in msgs] == ["t1", "t1"]  # 用真实落库 turn_id，不再合成
    plain = msgs[0]
    assert plain.get("options") is None and plain.get("selected") is None  # 存量行字段缺省
    clarify = msgs[1]
    assert clarify["options"] == CLARIFY_META["options"]
    assert clarify["selected"] is None  # 尚未点选


async def test_load_web_messages_tool_status_derived_from_appended_content():
    import json

    store = FakeStore()
    await store.append_message("svc", "dev", "u1", "s1", "t1", "user", "帮我查", meta=None)
    await store.append_message("svc", "dev", "u1", "s1", "t1", "assistant", "好的", meta=None)
    await store.append_message(
        "svc", "dev", "u1", "s1", "t1", "tool",
        json.dumps({"errorCode": "NOT_FOUND", "message": "task bad not found"}, ensure_ascii=False),
        tool_call_id="call_1",
    )
    await store.append_message(
        "svc", "dev", "u1", "s1", "t1", "tool",
        json.dumps({"taskId": "J123", "status": "PENDING"}, ensure_ascii=False),
        tool_call_id="call_2",
    )
    page = await store.load_web_messages("svc", "dev", "u1", "s1", limit=10)
    assert [m["status"] for m in page["messages"]] == [None, None, "error", "success"]


async def test_update_clarify_selected_merges_without_overwriting_options():
    store = FakeStore()
    await _clarify_row(store, "t1")
    changed = await store.update_clarify_selected("svc", "dev", "u1", "s1", "t1", "category:refund")
    assert changed == 1
    rows = await store.load_messages("svc", "dev", "u1", "s1")
    meta = rows[0]["meta"]
    assert meta["selected"] == "category:refund"
    assert meta["options"] == CLARIFY_META["options"]  # 读-改-写，不覆盖已有 options
    assert meta["kind"] == "clarify"
    page = await store.load_web_messages("svc", "dev", "u1", "s1", limit=10)
    assert page["messages"][0]["selected"] == "category:refund"


async def test_update_clarify_selected_scoped_ignores_other_sessions_and_users():
    store = FakeStore()
    await _clarify_row(store, "t9", user="u1", session="s1")
    await _clarify_row(store, "t9", user="u2", session="s1")
    await _clarify_row(store, "t9", user="u1", session="s2")
    changed = await store.update_clarify_selected("svc", "dev", "u1", "s1", "t9", "category:refund")
    assert changed == 1
    untouched = await store.load_messages("svc", "dev", "u2", "s1")
    assert untouched[0]["meta"].get("selected") is None
    other_sid = await store.load_messages("svc", "dev", "u1", "s2")
    assert other_sid[0]["meta"].get("selected") is None


async def test_update_clarify_selected_only_assistant_row_and_null_meta_writable():
    store = FakeStore()
    # 同 turn_id 的 user 行不应被回写
    await store.append_message("svc", "dev", "u1", "s1", "t1", "user", "退款", meta=None)
    # 匹配的 assistant 澄清行，meta 为 None 时按 COALESCE 语义可写 selected
    await store.append_message("svc", "dev", "u1", "s1", "t1", "assistant", "?", meta=None)
    changed = await store.update_clarify_selected("svc", "dev", "u1", "s1", "t1", "category:refund")
    assert changed == 1
    rows = await store.load_messages("svc", "dev", "u1", "s1")
    assert rows[0]["meta"] is None  # user 行不动
    assert rows[1]["meta"] == {"selected": "category:refund"}


async def test_update_clarify_selected_missing_returns_zero():
    store = FakeStore()
    await _clarify_row(store, "t1")
    changed = await store.update_clarify_selected("svc", "dev", "u1", "s1", "nope", "category:refund")
    assert changed == 0
    rows = await store.load_messages("svc", "dev", "u1", "s1")
    assert "selected" not in rows[0]["meta"]
