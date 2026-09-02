## Purpose

提供全链路可观测能力：基于 OpenTelemetry 的 traces 与 metrics（经 OTLP/console 导出）、入站/出站 trace 上下文传播、低基数指标、错误严重度映射，以及结构化日志注入 trace 上下文，使 Agent 各阶段（轮次/模型/工具）的耗时、成功率与错误可被定位，并通过健康检查暴露观测状态。

## ADDED Requirements

### Requirement: Trace 上下文传播

系统 SHALL 为每次请求建立全链路 trace，并支持双收传播：入站优先解析 W3C `traceparent`，兜底接受 `X-Trace-Id`（32 位十六进制）；出站 HTTP 请求 SHALL 统一注入 `traceparent`；SSE 事件的 `traceId` SHALL 从当前 OTel span 派生（未启用观测时回退到 `X-Trace-Id` 头或后端生成）。

#### Scenario: 携带 traceparent 的请求贯穿全链

- **WHEN** 入站请求携带 `traceparent` 或 `X-Trace-Id`
- **THEN** 该 trace 上下文传播到所有下游 span、出站请求与 SSE 事件的 `traceId`；无头时后端生成 traceId

### Requirement: 关键阶段 Span

系统 SHALL 为请求与 Agent 各阶段建立 span：入站 HTTP server span、出站 LLM HTTP client span，以及手动的 `run_turn`/`load_history`/`agent_graph`/`append_messages`/`llm_call`/`tool_call` span，形成可定位的 span 树。

#### Scenario: 一轮对话的 span 树完整

- **WHEN** 一次 `POST /chat`（含工具调用）完成
- **THEN** trace 中包含 server -> run_turn -> load_history/agent_graph -> llm_call/tool_call -> append_messages 的层级结构

### Requirement: 低基数指标

系统 SHALL 导出低基数指标，至少包含轮次耗时、LLM 耗时/token/错误、工具耗时/调用次数/错误、限流命中；指标标签 MUST 限于低基数字段（env/model/tool_name/status/error_code/finish_reason/kind 等），MUST NOT 将 userId/sessionId/turnId 等高基数值作为指标标签（这些仅进 span 属性与日志）。

#### Scenario: 指标标签低基数

- **WHEN** 记录一次工具调用指标
- **THEN** 指标带 tool_name/status/error_code 等低基数标签，不含 userId/sessionId/turnId

### Requirement: 错误严重度映射与受控终止

系统 SHALL 将错误码映射为严重度（如鉴权/不可用=critical、限流/超时=warning、内部=error、内容过滤=info）并打到 span 与日志属性；`max_tool_rounds` 受控终止 MUST NOT 记为错误 span。

#### Scenario: 递归上限不计错误

- **WHEN** 一轮因达到 `max_tool_rounds` 受控收尾
- **THEN** 该轮次不产生 ERROR span，finish_reason 记为 `max_tool_rounds`

### Requirement: 结构化日志注入 trace 上下文

系统 SHALL 输出结构化日志（JSON 可选），并在日志中注入当前 OTel 的 `trace_id`/`span_id` 以及请求级上下文（env/user/session_id/turn_id）；敏感字段按治理要求脱敏。

#### Scenario: 日志可按 trace 关联

- **WHEN** 一轮对话中各阶段输出日志
- **THEN** 每条日志含相同 `trace_id` 与请求上下文字段，可据此串联一次请求的全部日志

### Requirement: 观测可配置与健康检查暴露

系统 SHALL 支持配置观测开关、服务名、部署环境、console/OTLP 导出与采样率；未配置 OTLP 端点时仅 console 导出、不导出 OTLP。`GET /health` SHALL 返回观测状态（是否启用、OTLP 端点、是否已初始化）。观测关闭时 MUST NOT 干扰正常请求与流式传输。

#### Scenario: 健康检查报告观测状态

- **WHEN** 调用 `GET /health`
- **THEN** 响应含 `observability.{enabled, otlp_endpoint, initialized}` 字段

#### Scenario: 关闭观测不影响流式

- **WHEN** `observability.enabled=false`
- **THEN** 应用不安装 instrumentation，SSE 流式对话与测试传输正常工作
