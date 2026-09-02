"""M6 集成测试：鉴权 / env 白名单 / 限流，经完整 app（governance_dep）。"""
from __future__ import annotations

import httpx
import pytest

from conftest import build_test_app, client_for


def _settings_with(monkeypatch, **kw):
    from general_agent import security
    from general_agent.config import Settings, RateLimitSettings

    s = Settings()
    rl = kw.pop("rate_limit", None)
    s.security = type(s.security)(
        auth_mode=kw.get("auth_mode", "disabled"),
        api_keys=kw.get("api_keys", []),
        allowed_envs=kw.get("allowed_envs", []),
        rate_limit=rl or RateLimitSettings(enabled=False),
    )
    monkeypatch.setattr(security, "get_settings", lambda: s)
    return s


def test_auth_api_key_rejects_without_key(monkeypatch):
    _settings_with(monkeypatch, auth_mode="api_key", api_keys=["secret"])
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/chat", json={"message": "hi"}, headers={"x-service": "s", "x-env": "dev", "x-user": "u"})
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH"


def test_auth_api_key_rejects_wrong_key(monkeypatch):
    _settings_with(monkeypatch, auth_mode="api_key", api_keys=["secret"])
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/chat", json={"message": "hi"}, headers={"x-api-key": "wrong"})
    assert r.status_code == 401 and r.json()["code"] == "AUTH"


def test_auth_api_key_accepts_valid_key(monkeypatch):
    _settings_with(monkeypatch, auth_mode="api_key", api_keys=["secret"])
    app, store, broker = build_test_app()
    client = client_for(app)
    with client.stream("POST", "/chat", json={"message": "x"},
                       headers={"x-service": "s", "x-env": "dev", "x-user": "u", "x-api-key": "secret"}) as r:
        assert r.status_code == 200


def test_env_whitelist_rejects_unknown_env(monkeypatch):
    _settings_with(monkeypatch, allowed_envs=["prod"])
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/chat", json={"message": "hi"}, headers={"x-service": "s", "x-env": "dev", "x-user": "u"})
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION"


def test_env_whitelist_accepts_allowed_env(monkeypatch):
    _settings_with(monkeypatch, allowed_envs=["dev", "prod"])
    app, store, broker = build_test_app()
    client = client_for(app)
    with client.stream("POST", "/chat", json={"message": "x"},
                       headers={"x-service": "s", "x-env": "dev", "x-user": "u"}) as r:
        assert r.status_code == 200


def test_rate_limit_returns_429(monkeypatch):
    from general_agent.config import RateLimitSettings
    from general_agent.security import TokenBucket

    _settings_with(monkeypatch, rate_limit=RateLimitSettings(enabled=True, rps=0.0, burst=1))
    # governance 用 app.state.rate_limiter，故注入低容量桶
    app, store, broker = build_test_app(rate_limiter=TokenBucket(rate=0.0, capacity=1))
    client = client_for(app)
    with client.stream("POST", "/chat", json={"message": "x"},
                       headers={"x-service": "s", "x-env": "dev", "x-user": "u"}) as r:
        assert r.status_code == 200
        _ = "\n".join(r.iter_lines())
    r2 = client.post("/chat", json={"message": "y"}, headers={"x-service": "s", "x-env": "dev", "x-user": "u"})
    assert r2.status_code == 429 and r2.json()["code"] == "RATE_LIMIT"


def test_notify_requires_service_auth(monkeypatch):
    _settings_with(monkeypatch, auth_mode="api_key", api_keys=["svc"])
    app, store, broker = build_test_app()
    client = client_for(app)
    r = client.post("/internal/notify", json={"sessionId": "X", "taskId": "J", "status": "SUCCESS"})
    assert r.status_code == 401 and r.json()["code"] == "AUTH"
    r2 = client.post("/internal/notify", json={"sessionId": "X", "taskId": "J", "status": "SUCCESS"},
                     headers={"x-api-key": "svc"})
    assert r2.status_code == 200


def test_health_exempt_from_auth(monkeypatch):
    _settings_with(monkeypatch, auth_mode="api_key", api_keys=["secret"])
    app, store, broker = build_test_app()
    client = client_for(app)
    assert client.get("/health").status_code == 200
