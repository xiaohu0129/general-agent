"""任务 1.1：RoutingSettings/EmbeddingSettings 新增字段的默认值与覆盖测试。"""
from __future__ import annotations

import pytest

from general_agent.config import EmbeddingSettings, RoutingSettings, Settings


def test_routing_settings_defaults():
    s = RoutingSettings()
    assert s.context_turns == 3
    assert s.query_rewrite is True
    assert s.multi_vector is True
    assert s.llm_conf_high == 0.7
    assert s.arg_guard is True
    assert s.hybrid is True
    assert s.rrf_k == 60
    assert s.keyword_top_k == 10
    assert s.clarify_options is True
    assert s.clarify_option_max == 4


def test_embedding_settings_defaults():
    assert EmbeddingSettings().batch_size == 64


def test_embedding_batch_size_must_be_positive():
    # 修复2：batch_size 下界 ge=1；0 与负数非法
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        EmbeddingSettings(batch_size=0)
    with pytest.raises(pydantic.ValidationError):
        EmbeddingSettings(batch_size=-1)
    # 默认值与合法值正常
    assert EmbeddingSettings().batch_size == 64
    assert EmbeddingSettings(batch_size=128).batch_size == 128


def test_refer_terms_default_is_non_empty_str_list():
    terms = RoutingSettings().refer_terms
    assert isinstance(terms, list)
    assert len(terms) > 0
    assert all(isinstance(t, str) and t for t in terms)
    # 至少覆盖常用中文指代/省略说法
    for word in ["它", "那个", "这个", "他", "她", "它们", "再来", "继续", "上面", "刚说的", "那家", "那款"]:
        assert word in terms


def test_constructor_overrides_fields():
    s = RoutingSettings(context_turns=0, hybrid=False, rrf_k=30, clarify_option_max=2)
    assert s.context_turns == 0
    assert s.hybrid is False
    assert s.rrf_k == 30
    assert s.clarify_option_max == 2


def test_env_overrides_routing_switches(monkeypatch):
    monkeypatch.setenv("AGENT_ROUTING__HYBRID", "false")
    monkeypatch.setenv("AGENT_ROUTING__CLARIFY_OPTIONS", "false")
    monkeypatch.setenv("AGENT_ROUTING__CONTEXT_TURNS", "0")
    s = Settings()
    assert s.routing.hybrid is False
    assert s.routing.clarify_options is False
    assert s.routing.context_turns == 0


def test_env_overrides_embedding_batch_size(monkeypatch):
    monkeypatch.setenv("AGENT_EMBEDDING__BATCH_SIZE", "128")
    assert Settings().embedding.batch_size == 128
