"""M8 可观测性：OpenTelemetry traces + metrics。

方案A（OTel 全家桶，traces+metrics 经 OTLP）。
入站双收：traceparent 优先 + X-Trace-Id 兜底（自定义复合 propagator）。
采样：100%（ParentBased(root=ALWAYS_ON)）。
Metric：严格低基数标签（env/model/tool_name/status/error_code/finish_reason/kind）。
错误 severity：agent 侧映射表。
LLM/工具 span 手动建（不依赖 instrumentation-langchain，避免拉入完整 langchain 包）。
范围：仅 agent Python 侧；出站经 httpx 自动 instrumentation 注入 traceparent，下游服务即串联。
"""
from __future__ import annotations

import contextvars
import os
import socket

from opentelemetry import context as context_api
from opentelemetry import metrics, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import TextMapPropagator, default_getter, default_setter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider, sampling
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import NonRecordingSpan, get_current_span
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
    format_span_id,
    format_trace_id,
)
from opentelemetry.trace.span import SpanContext, TraceFlags, TraceState
from opentelemetry.trace.status import Status, StatusCode

from . import __version__
from .logging_setup import get_logger

logger = get_logger(__name__)

# ---------------- 请求级 contextvar（metric env 标签 + 日志绑定） ----------------
env_var: contextvars.ContextVar[str] = contextvars.ContextVar("agent_env", default="dev")
user_var: contextvars.ContextVar[str] = contextvars.ContextVar("agent_user", default="anonymous")
session_var: contextvars.ContextVar[str] = contextvars.ContextVar("agent_session", default="")
turn_var: contextvars.ContextVar[str] = contextvars.ContextVar("agent_turn", default="")


def bind_request_context(*, env: str, user: str, session_id: str, turn_id: str = "") -> None:
    """绑定请求级上下文（env/user/session/turn），供 metric 标签与日志字段读取。"""
    env_var.set(env or "dev")
    user_var.set(user or "anonymous")
    session_var.set(session_id or "")
    turn_var.set(turn_id or "")


def current_env() -> str:
    return env_var.get()


# ---------------- 错误 severity 映射表 ----------------
ERROR_SEVERITY: dict[str, str] = {
    "AUTH": "critical",
    "UNAVAILABLE": "critical",
    "RATE_LIMIT": "warning",
    "TIMEOUT": "warning",
    "INTERNAL": "error",
    "CONTENT_FILTER": "info",
}


def severity_of(error_code: str | None) -> str:
    return ERROR_SEVERITY.get(error_code or "", "error")


# ---------------- X-Trace-Id 兜底 propagator ----------------
_TRACE_HEADER = "x-trace-id"


def _is_valid_trace_id(raw: str) -> bool:
    if len(raw) != 32:
        return False
    try:
        int(raw, 16)
        return True
    except ValueError:
        return False


class XTraceIdPropagator(TextMapPropagator):
    """入站兜底：无 traceparent 时用 X-Trace-Id(32hex) 构造 trace 上下文。出站不注入。"""

    def extract(self, carrier, context=None, getter=default_getter):
        context = context if context is not None else context_api.get_current()
        span = get_current_span(context)
        span_ctx = span.get_span_context() if span is not None else None
        if span_ctx is not None and span_ctx.is_valid:
            return context
        values = getter.get(carrier, _TRACE_HEADER)
        raw = values[0] if values else None
        if isinstance(raw, bytes):
            raw = raw.decode("latin-1")
        if raw and _is_valid_trace_id(raw):
            span_context = SpanContext(
                trace_id=int(raw, 16),
                span_id=int.from_bytes(os.urandom(8), 'big') or 1,
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
                trace_state=TraceState(),
            )
            return trace.set_span_in_context(NonRecordingSpan(span_context), context)
        return context

    def inject(self, carrier, context=None, setter=default_setter):
        # 出站仅由 W3C TraceContext 注入 traceparent；X-Trace-Id 不外传
        return

    @property
    def fields(self):
        return {_TRACE_HEADER}


def _setup_propagator() -> None:
    set_global_textmap(
        CompositePropagator(
            [
                TraceContextTextMapPropagator(),
                W3CBaggagePropagator(),
                XTraceIdPropagator(),
            ]
        )
    )


# ---------------- 上下文/属性 辅助 ----------------
def current_trace_id() -> str:
    span = get_current_span()
    sc = span.get_span_context() if span is not None else None
    if sc is not None and sc.is_valid:
        return format_trace_id(sc.trace_id)
    return ""


def current_span_id() -> str:
    span = get_current_span()
    sc = span.get_span_context() if span is not None else None
    if sc is not None and sc.is_valid:
        return format_span_id(sc.span_id)
    return ""


def record_span_error(span, error_code: str | None, message: str | None = None) -> None:
    code = error_code or "INTERNAL"
    span.set_status(Status(StatusCode.ERROR, message or code))
    span.set_attribute("error_code", code)
    span.set_attribute("severity", severity_of(code))


def get_tracer():
    return trace.get_tracer("general.agent", __version__)


# ---------------- providers ----------------
_initialized = False
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None

# metric instruments（setup_observability 后赋值）
_turn_duration = None
_llm_duration = None
_llm_tokens = None
_llm_errors = None
_tool_duration = None
_tool_calls = None
_tool_errors = None
_rate_limit_hits = None
_intent_route = None
_intent_clarify = None
_intent_degrade = None


def setup_observability(settings) -> None:
    global _initialized, _tracer_provider, _meter_provider
    global _turn_duration, _llm_duration, _llm_tokens, _llm_errors
    global _tool_duration, _tool_calls, _tool_errors
    global _rate_limit_hits
    global _intent_route, _intent_clarify, _intent_degrade
    if _initialized:
        return
    _setup_propagator()
    obs = settings.observability
    if not obs.enabled:
        _initialized = True
        logger.info("observability_disabled")
        return

    instance_id = f"{socket.gethostname()}:{settings.server.port}"
    resource = Resource.create(
        {
            SERVICE_NAME: obs.service_name,
            SERVICE_VERSION: __version__,
            "deployment.environment": obs.deployment_environment,
            "service.instance.id": instance_id,
        }
    )

    # traces：100% 采样（ParentBased，root=ALWAYS_ON）
    root_sampler = sampling.ALWAYS_ON if obs.traces.sampling >= 1.0 else sampling.TraceIdRatioBased(obs.traces.sampling)
    tp = TracerProvider(resource=resource, sampler=sampling.ParentBased(root=root_sampler))
    if obs.otlp.endpoint:
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=obs.otlp.endpoint)))
    if obs.console:
        tp.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tp)
    _tracer_provider = tp

    # metrics
    readers = []
    if obs.otlp.endpoint:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=obs.otlp.endpoint),
                export_interval_millis=obs.metrics.export_interval_ms,
            )
        )
    if obs.console:
        readers.append(
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(),
                export_interval_millis=obs.metrics.export_interval_ms,
            )
        )
    mp = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(mp)
    _meter_provider = mp

    meter = metrics.get_meter("general.agent", __version__)
    _turn_duration = meter.create_histogram("agent.turn.duration", unit="ms", description="Agent turn duration (ms)")
    _llm_duration = meter.create_histogram("agent.llm.duration", unit="ms", description="LLM call duration (ms)")
    _llm_tokens = meter.create_counter("agent.llm.tokens", description="LLM token consumption by kind")
    _llm_errors = meter.create_counter("agent.llm.errors", description="LLM errors by code")
    _tool_duration = meter.create_histogram("agent.tool.duration", unit="ms", description="Tool call duration (ms)")
    _tool_calls = meter.create_counter("agent.tool.calls", description="Tool calls by status")
    _tool_errors = meter.create_counter("agent.tool.errors", description="Tool errors by code")
    _rate_limit_hits = meter.create_counter("agent.rate_limit.hits", description="Rate limit hits by env")
    _intent_route = meter.create_counter("agent.intent.route.count", description="Skill routing decisions by path")
    _intent_clarify = meter.create_counter("agent.intent.clarify.count", description="Routing clarify fallbacks")
    _intent_degrade = meter.create_counter("agent.intent.degrade.count", description="Routing degraded/fallback decisions")

    # httpx 出站自动 instrumentation（注入 traceparent 给 LLM / 业务下游）
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        logger.warning("httpx_instrument_failed", error=str(exc))

    _initialized = True
    logger.info(
        "observability_initialized",
        otlp_endpoint=obs.otlp.endpoint or "",
        console=obs.console,
        sampling=obs.traces.sampling,
    )


def instrument_app(app) -> None:
    """FastPI 自动 instrumentation：根 server span + 入站上下文提取。须在 app 构建后调用。"""
    if not is_enabled():
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # pragma: no cover
        logger.warning("fastapi_instrument_failed", error=str(exc))


def shutdown_observability() -> None:
    global _tracer_provider, _meter_provider
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception as exc:  # pragma: no cover
            logger.warning("tracer_shutdown_failed", error=str(exc))
        _tracer_provider = None
    if _meter_provider is not None:
        try:
            _meter_provider.shutdown()
        except Exception as exc:  # pragma: no cover
            logger.warning("meter_shutdown_failed", error=str(exc))
        _meter_provider = None


def is_enabled() -> bool:
    return _initialized and _tracer_provider is not None


# ---------------- metric 记录 ----------------
def record_turn(duration_ms: float, finish_reason: str) -> None:
    if _turn_duration is None:
        return
    _turn_duration.record(duration_ms, {"env": current_env(), "finish_reason": finish_reason})


def record_llm(
    duration_ms: float,
    *,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    error_code: str | None = None,
) -> None:
    env = current_env()
    mk = model or ""
    if _llm_duration is not None:
        _llm_duration.record(duration_ms, {"env": env, "model": mk})
    if error_code is None:
        if _llm_tokens is not None and (prompt_tokens or completion_tokens):
            _llm_tokens.add(prompt_tokens, {"env": env, "model": mk, "kind": "prompt"})
            _llm_tokens.add(completion_tokens, {"env": env, "model": mk, "kind": "completion"})
    else:
        if _llm_errors is not None:
            _llm_errors.add(1, {"env": env, "model": mk, "error_code": error_code})


def record_tool(tool_name: str, duration_ms: float, *, status: str, error_code: str | None = None) -> None:
    env = current_env()
    tn = tool_name or ""
    if _tool_duration is not None:
        _tool_duration.record(duration_ms, {"env": env, "tool_name": tn})
    if _tool_calls is not None:
        _tool_calls.add(1, {"env": env, "tool_name": tn, "status": status})
    if status == "error" and error_code and _tool_errors is not None:
        _tool_errors.add(1, {"env": env, "tool_name": tn, "error_code": error_code})


def record_rate_limit_hit(env: str) -> None:
    if _rate_limit_hits is not None:
        _rate_limit_hits.add(1, {"env": env or "dev"})


def record_intent_route(path: str, *, category: str = "") -> None:
    """记录 Skill 路由决策：path 为 rule/vector/llm/chitchat/clarify/fallback/degraded（低基数）。"""
    env = current_env()
    if _intent_route is not None:
        _intent_route.add(1, {"env": env, "path": path, "category": category or "none"})
    if path == "clarify" and _intent_clarify is not None:
        _intent_clarify.add(1, {"env": env})
    if path in ("fallback", "degraded") and _intent_degrade is not None:
        _intent_degrade.add(1, {"env": env, "path": path})
