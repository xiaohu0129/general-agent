"""M6 治理单元测试：鉴权/env 白名单/限流/脱敏。"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from general_agent.config import RateLimitSettings, SecuritySettings
from general_agent.logging_setup import _is_sensitive, _mask, _redact_nested, redact_processor
from general_agent.security import (
    GovernanceError,
    TokenBucket,
    _check_auth,
    _check_env,
    resolve_identity,
)


def _settings(auth_mode="disabled", api_keys=None, allowed_envs=None, rl_enabled=False):
    from general_agent.config import Settings

    s = Settings()
    s.security = SecuritySettings(
        auth_mode=auth_mode,
        api_keys=api_keys or [],
        allowed_envs=allowed_envs or [],
        rate_limit=RateLimitSettings(enabled=rl_enabled, rps=1.0, burst=2),
    )
    return s


def _request(headers=None, api_key=None):
    r = MagicMock()
    h = {"x-service": "s", "x-env": "dev", "x-user": "u"}
    if api_key:
        h["x-api-key"] = api_key
    if headers:
        h.update(headers)
    r.headers = h
    return r


def test_resolve_identity_from_headers():
    ident = resolve_identity(_request())
    assert ident.service == "s" and ident.env == "dev" and ident.user == "u"
    assert ident.rate_key == "u:dev"


def test_check_auth_disabled_passes():
    _check_auth(_settings("disabled"), _request())  # 放行


def test_check_auth_api_key_valid():
    _check_auth(_settings("api_key", api_keys=["secret"]), _request(api_key="secret"))


def test_check_auth_api_key_missing_rejected():
    with pytest.raises(GovernanceError) as ei:
        _check_auth(_settings("api_key", api_keys=["secret"]), _request())
    assert ei.value.status_code == 401 and ei.value.code == "AUTH"


def test_check_auth_api_key_wrong_rejected():
    with pytest.raises(GovernanceError):
        _check_auth(_settings("api_key", api_keys=["secret"]), _request(api_key="wrong"))


def test_check_env_whitelist_pass():
    _check_env(_settings(allowed_envs=["dev", "prod"]), "dev")


def test_check_env_whitelist_rejects_unknown():
    with pytest.raises(GovernanceError) as ei:
        _check_env(_settings(allowed_envs=["prod"]), "evil")
    assert ei.value.status_code == 400 and ei.value.code == "VALIDATION"


def test_check_env_empty_whitelist_skips():
    _check_env(_settings(), "anything")  # 空=不限


def test_token_bucket_allows_burst_then_rejects():
    tb = TokenBucket(rate=0.0, capacity=2)  # 不补充令牌
    assert tb.allow("k") is True
    assert tb.allow("k") is True
    assert tb.allow("k") is False  # 耗尽


def test_token_bucket_refills_over_time():
    tb = TokenBucket(rate=10.0, capacity=1)
    assert tb.allow("k") is True
    assert tb.allow("k") is False
    time.sleep(0.15)  # 补 ~1.5 令牌
    assert tb.allow("k") is True


def test_token_bucket_independent_keys():
    tb = TokenBucket(rate=0.0, capacity=1)
    assert tb.allow("a") is True
    assert tb.allow("b") is True  # 不同 key 独立
    assert tb.allow("a") is False


# ---------------- 脱敏 ----------------
def test_is_sensitive_keys():
    assert _is_sensitive("api_key")
    assert _is_sensitive("Authorization")
    assert _is_sensitive("PASSWORD")
    assert not _is_sensitive("user")
    assert not _is_sensitive("x-trace-id")


def test_mask_short_and_long():
    assert _mask("ab") == "***"
    assert _mask("secret123").startswith("se") and _mask("secret123").endswith("23")


def test_redact_nested_dict():
    out = _redact_nested({"api_key": "secret123", "user": "u", "nested": {"token": "tk12345678", "ok": 1}})
    assert out["api_key"] == "se***23"
    assert out["user"] == "u"
    assert out["nested"]["token"] == "tk***78"
    assert out["nested"]["ok"] == 1


def test_redact_processor_top_level():
    ed = {"event": "x", "api_key": "secret123", "env": "dev"}
    out = redact_processor(None, None, ed)
    assert out["api_key"] == "se***23"
    assert out["event"] == "x"
    assert out["env"] == "dev"
