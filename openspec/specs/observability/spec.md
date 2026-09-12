# observability Specification

## Purpose
提供全链路可观测能力：基于 OpenTelemetry 的 traces 与 metrics（经 OTLP/console 导出）、入站/出站 trace 上下文传播、低基数指标、错误严重度映射，以及结构化日志注入 trace 上下文，使 Agent 各阶段（轮次/模型/工具）的耗时、成功率与错误可被定位，并通过健康检查暴露观测状态。

## Requirements

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

### Requirement: 路由 Span 与指标

系统 SHALL 为 Skill 路由决策建立 `intent_route` span，记录路由路径（rule/vector/llm/clarify/fallback/degraded）、命中规则（若有）、向量检索 top-k 工具名与相似度分数、top1/top2 分差、路由 LLM 选定类别与置信度（若有）、最终暴露工具集合、embedding 模型 id 与索引版本；该 span SHALL 位于 `run_turn` 之下、`agent_graph` 之前。系统 SHALL 导出低基数路由指标（路由路径计数/分布、兜底 LLM 触发率、用户澄清率、路由降级率），metric 标签限于路由路径/类别等低基数字段，userId/sessionId/turnId MUST NOT 进入 metric。

#### Scenario: 路由 span 记录决策

- **WHEN** 一轮对话经过向量检索高置信收窄
- **THEN** trace 中 run_turn 下存在 intent_route span，属性含 path=vector、top-k 工具名与分数、分差、最终工具集、embedding 模型与索引版本，且无 userId/sessionId metric 标签

#### Scenario: 路由降级可观测

- **WHEN** embedding 端点异常导致路由降级为全量工具
- **THEN** intent_route span 记录 path=degraded 与错误原因，路由降级指标递增，审计日志可查

### Requirement: 路由质量指标

系统 SHALL 在既有路由计数指标之外，导出衡量意图识别质量的低基数指标，使路由误杀、检索置信度分布、上下文改写与缺参追问可被度量：

- 路由误杀计数：模型本轮实际调用的工具不在路由推荐工具集内时 +1（rule/vector/llm/option 收窄路径计，其中 `option` 为选项确定性收窄；degraded/fallback 全量路径不计），标签含 env、路由路径 path、检索路 retrieval（vector/keyword，区分降级期与正常期误杀率）；BM25 关键词收窄后经 LLM 兜底成功裁决的轮次按 `llm` 路径照常计入误杀口径，MUST NOT 因检索输入来自关键词路而豁免；
- 检索置信度分布：向量检索的 top1 相似度分数与 top1-top2 分差，以 histogram 记录；关键词路（BM25）与 RRF 融合路的 top1 分数 SHALL 以检索路标签（vector/bm25/rrf）区分记入同一分布指标，支撑两路阈值分别校准；
- query 改写触发计数：路由因指代/低置信触发上下文 query 改写时 +1（含成功/失败标签）；
- 缺参追问触发计数：工具因缺少必填参数未执行、回喂缺参引导时 +1，标签含 env、工具名。

这些指标 MUST NOT 携带 userId/sessionId/turnId 等高基数字段（此类标识仅进 span/审计日志）。指标 SHALL 可通过既有 metrics 导出通道（console/OTLP）观测。

#### Scenario: 误杀指标在模型越出推荐集时累加

- **WHEN** 路由收窄推荐集为 {A,B} 而模型实际调用了推荐集外工具
- **THEN** 误杀计数 +1，标签含 env、路由路径与检索路（vector/keyword），且不含 userId/sessionId/turnId；BM25 关键词降级轮次经 LLM 兜底收窄的误杀同样累加且以 retrieval=keyword 区分

#### Scenario: 置信度分布记录分数与分差

- **WHEN** 发生一次向量检索路由，或关键词路（BM25）/RRF 融合产生 top1 分数
- **THEN** 向量 top1 分数与分差、以及 BM25/RRF 路 top1 分数被记入 histogram（以检索路标签区分），可聚合出各路分数分布用于阈值校准

#### Scenario: 改写与缺参追问可计数

- **WHEN** 路由触发 query 改写，或工具因缺必填参数回喂引导
- **THEN** 对应计数指标 +1，并带成功/失败（改写）或工具名（缺参）标签

### Requirement: 澄清结果维度

系统 SHALL 为用户澄清率指标补充低基数结果维度 `result`，以度量结构化选项硬闭环相对手打文本闭环的收益：选项点选后经确定性收窄闭环记为 `option`；用户手打后经文本闭环成功收窄记为 `text`；澄清后用户回答仍无法对应、再次澄清记为 `repeat`。该维度标签 MUST 限于 `option|text|repeat` 等低基数枚举，MUST NOT 携带 userId/sessionId/turnId 或选项 value 原文。澄清结果计数 SHALL 仅由独立的澄清计数接口按"上一轮澄清的闭环结局"显式记录：仅当本轮路由识别到上一轮澄清并产生闭环结局（option/text/repeat）时计数；首轮澄清（无上一轮澄清）不属任何闭环结局、SHALL NOT 记入该维度（其总量由路由路径计数的 `clarify` path 维度承担）。既有路径计数接口内耦合的"path=clarify 即自动累加澄清计数"逻辑 SHALL 移除，MUST NOT 与显式澄清结果计数形成双计。

#### Scenario: 澄清结果按 option/text/repeat 计数

- **WHEN** 一轮澄清后，用户分别经点选选项闭环、手打后成功收窄、或再次触发澄清
- **THEN** 澄清计数对应 `result=option` / `result=text` / `result=repeat` 分别 +1，标签不含用户/会话/轮次标识与选项原文

#### Scenario: 首轮澄清与路径计数不双计

- **WHEN** 一轮全新低置信澄清（无上一轮澄清）产生，或一轮"再次澄清"（path=clarify 且闭环结局=repeat）产生
- **THEN** 首轮澄清仅使路由路径计数的 path=clarify 维度 +1、澄清结果计数不增加；repeat 轮澄清结果计数仅 +1 一次（显式 result=repeat），路径计数内的自动澄清累加逻辑已被移除，不产生双计
