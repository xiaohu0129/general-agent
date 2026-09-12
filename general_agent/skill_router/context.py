"""跨轮路由上下文：历史拼接构造向量 query + 按需 LLM query 改写 + 指代词判定。

- build_context_query：把最近 N 轮 {role, content} 与当前消息拼接为单个检索串（0 额外 LLM）；
- contains_referral：当前消息是否命中指代词/省略表述（子串匹配，检索前即可判定）；
- rewrite_query：命中指代或拼接检索低置信时，调一次 temperature=0 的 LLM 把
  “历史 + 当前消息”改写为脱离上下文也能独立理解的检索查询；失败/不可解析时抛异常，
  由路由层捕获并静默降级为拼接 query（函数自身不吞异常）。
"""
from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import RoutingSettings
from ..logging_setup import get_logger

logger = get_logger(__name__)

# 内置常用中文指代词/省略模式默认表：单一来源为 config.RoutingSettings.refer_terms，
# 此处仅引用同一定义（生产/测试/回退一致，禁止再手工同步第二份字面量）
DEFAULT_REFER_TERMS: list[str] = list(RoutingSettings.model_fields["refer_terms"].get_default())

_ROLE_LABELS = {"user": "用户", "assistant": "助手"}


def build_context_query(history: list[dict] | None, message: str) -> str:
    """把最近 N 轮历史与当前消息拼接为单个检索串；history 为空/None 时逐字节返回 message。"""
    if not history:
        return message
    lines = ["历史对话："]
    for turn in history:
        role = _ROLE_LABELS.get(turn.get("role", ""), turn.get("role", ""))
        lines.append(f"{role}：{turn.get('content', '')}")
    lines.append(f"当前问题：{message}")
    return "\n".join(lines)


def contains_referral(message: str, refer_terms: list[str]) -> bool:
    """当前消息命中任一指代词/省略表述即 True。

    单字"他/它"需排除"其他/其它"中的命中（前一字符为"其"）；
    其余词（含"她"及多字词）维持普通子串匹配。
    """
    for term in refer_terms or []:
        if not term:
            continue
        start = 0
        while True:
            pos = message.find(term, start)
            if pos < 0:
                break
            if term in ("他", "它") and pos > 0 and message[pos - 1] == "其":
                # "其他/其它"不判为指代，继续找该字的其他出现位置
                start = pos + 1
                continue
            return True
    return False


async def rewrite_query(llm, history: list[dict] | None, message: str) -> str:
    """结合历史把当前消息改写为脱离上下文也能独立理解的检索查询。

    输出 JSON {"query": "...", "used_context": ...}（used_context 可选，只取 query）；
    LLM 异常、返回非 JSON、缺 query 键或 query 为空均抛异常，由路由层捕获降级。
    """
    context = build_context_query(history, message)
    sys = (
        "你是检索查询改写器。根据对话历史与用户当前问题，把当前问题改写为"
        "脱离上下文也能独立理解的检索查询：补全指代词与省略的主语/对象，"
        "不要回答问题、不要添加历史中没有的信息。只输出 JSON，不要输出其他内容。"
    )
    user = (
        f"{context}\n"
        '输出 JSON：{"query": "改写后的独立检索查询", "used_context": true或false}'
    )
    resp = await llm.ainvoke([SystemMessage(content=sys), HumanMessage(content=user)])
    data = _parse_json(getattr(resp, "content", "") or "")
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("rewrite_query: 响应缺少非空 query 字段")
    return query.strip()


def _parse_json(content: str) -> dict:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("rewrite_query: 响应不是 JSON 对象")
    return data
