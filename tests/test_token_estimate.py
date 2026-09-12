"""token 估算测试（U1）：CJK 感知——中文每字约 1 token，ASCII 每 4 字符约 1 token。"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from general_agent.runner import _est_tokens


def test_est_tokens_pure_cjk_about_one_per_char():
    # 纯中文 4000 字：新实现约 4000 token（旧 //4 实现仅约 1000）
    m = HumanMessage(content="你" * 4000)
    t = _est_tokens(m)
    assert 3900 <= t <= 4100
    assert t == 4000 + 1  # 每字 1 token + 每条消息底数 1


def test_est_tokens_pure_ascii_quarter():
    # 纯 ASCII 4000 字符：保持 //4 语义 + 底数 1
    m = HumanMessage(content="a" * 4000)
    assert _est_tokens(m) == 4000 // 4 + 1


def test_est_tokens_mixed_by_category():
    # 4 个 CJK + 8 个 ASCII：4*1 + 8//4 + 底数 1 = 7
    m = HumanMessage(content="你好世界abcd1234")
    assert _est_tokens(m) == 4 + 8 // 4 + 1


def test_est_tokens_cjk_ranges():
    # D1：CJK 标点/符号（\u3000-\u303f）、统一表意文字（\u4e00-\u9fff）、
    # 全角符号（\uff00-\uffef）各取代表字符，每个约 1 token
    m = HumanMessage(content="\u3000\u4e00\uff0c")  # 表意空格 / 一 / 全角逗号
    assert _est_tokens(m) == 3 + 1


def test_est_tokens_tool_calls_args_counted():
    # tool_calls 的 args 计入；其中中文按每字 1 token
    # 拼接串 = "x" + '{"a": "中文中文"}'：4 CJK、10 个其他字符 -> 4 + 10//4 + 1 = 7
    m = AIMessage(
        content="x",
        tool_calls=[{"id": "c1", "name": "f", "args": {"a": "中文中文"}, "type": "tool_call"}],
    )
    assert _est_tokens(m) == 7
    assert _est_tokens(m) > _est_tokens(AIMessage(content="x"))
