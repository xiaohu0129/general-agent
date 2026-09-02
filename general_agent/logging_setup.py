"""结构化日志：structlog JSON 输出，注入 OTel trace_id/span_id + 请求上下文（M8）+ 脱敏（M6）。

trace_id/span_id 取 OTel 当前 span context；env/user/session_id/turn_id 取
observability 的 contextvar（请求级，由 bind_request_context 设置）。
M6：redact_processor 掩码敏感键（api_key/token/password/authorization/secret 等），递归处理嵌套。
"""
from __future__ import annotations

import logging

import structlog

# 敏感键集合（小写匹配），命中即掩码
_SENSITIVE_KEYS = {
    "api_key",
    "api_keys",
    "apikey",
    "token",
    "password",
    "authorization",
    "secret",
    "cookie",
    "x-api-key",
    "x-trace-id",  # 见下：从敏感集合移除以保留可观测性
}
# x-trace-id 是请求追踪标识，需保留完整值用于串联，故移出敏感集合
_SENSITIVE_KEYS.discard("x-trace-id")


def _mask(value) -> str:
    s = str(value)
    if len(s) <= 4:
        return "***"
    return s[:2] + "***" + s[-2:]


def _is_sensitive(key: str) -> bool:
    return key.lower() in _SENSITIVE_KEYS


def _redact_nested(value):
    """递归处理 dict/list 内的敏感键"""
    if isinstance(value, dict):
        return {
            k: (_mask(v) if _is_sensitive(k) else _redact_nested(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_nested(x) for x in value]
    return value


def redact_processor(logger, method_name, event_dict):
    """对 event_dict 顶层与嵌套结构中的敏感键值进行掩码"""
    return {
        k: (_mask(v) if _is_sensitive(k) else _redact_nested(v))
        for k, v in event_dict.items()
    }


def _add_context(logger, method_name, event_dict):
    # OTel trace/span
    try:
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import format_span_id, format_trace_id

        span = trace.get_current_span()
        sc = span.get_span_context() if span is not None else None
        if sc is not None and sc.is_valid:
            event_dict["trace_id"] = format_trace_id(sc.trace_id)
            event_dict["span_id"] = format_span_id(sc.span_id)
    except Exception:
        pass
    # 请求级上下文（来自 observability contextvar）
    try:
        from . import observability as _obs

        event_dict.setdefault("env", _obs.env_var.get())
        event_dict.setdefault("user", _obs.user_var.get())
        event_dict.setdefault("session_id", _obs.session_var.get())
        event_dict.setdefault("turn_id", _obs.turn_var.get())
    except Exception:
        pass
    return event_dict


def configure_logging(level: str = "INFO", use_json: bool = True, redact_secrets: bool = True) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    processors = [
        structlog.contextvars.merge_contextvars,
        _add_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if redact_secrets:
        processors.append(redact_processor)
    processors.append(
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
