# chat-sse-protocol Specification

## Purpose
定义 general-agent 面向前端/下游的结构化 SSE 事件协议：把底层 Agent 执行过程翻译为稳定、可理解的 UI 生命周期事件，支持工具调用展示、错误降级与断线续传，使前端 SDK 可按事件类型解析且协议可演进。

## Requirements

### Requirement: 结构化 SSE 事件流

系统 SHALL 以 SSE（Server-Sent Events）下发对话事件，每个事件由 `event: <类型>` 行与 `data: <JSON>` 行组成；所有事件的 `data` SHALL 含公共字段 `turnId`（轮次 ID，前端据此归位消息气泡）与 `traceId`（全链路追踪 ID）。

系统 SHALL 支持以下事件类型：

| event | 触发时机 | data 额外字段 |
|---|---|---|
| `turn_start` | 轮次开始 | `sessionId`（会话 ID，可能缺省） |
| `turn_delta` | LLM 文本增量 | `content` |
| `tool_start` | 工具调用开始 | `toolCallId`、`toolName`、`args` |
| `tool_end` | 工具调用结束 | `toolCallId`、`status`、`result` 或 `error`+`errorCode` |
| `turn_end` | 轮次结束 | `finishReason`（`stop`/`max_tool_rounds`） |
| `error` | 系统级异常 | `message`（用户可读）、`code` |
| `notification` | 异步任务通知 | `taskId`、`status`、可选 `message`；不含 `turnId` |

#### Scenario: 一轮正常对话的事件序列

- **WHEN** 客户端向 `POST /chat` 发送一条消息且 Agent 正常产出文本回答
- **THEN** 系统按序下发 `turn_start` -> 若干 `turn_delta` -> `turn_end{finishReason:"stop"}`，且每个事件 data 均含 `turnId` 与 `traceId`

#### Scenario: turn_start 首帧携带会话 ID

- **WHEN** 客户端在 Web 登录模式下发起一轮对话
- **THEN** `turn_start` 事件的 data 含 `sessionId` 字段，前端可在首帧即获知/更新会话标识

### Requirement: 工具调用事件配对

系统 SHALL 为每次工具调用下发成对的 `tool_start` 与 `tool_end`，`toolCallId` 全局唯一并取自模型工具调用 ID，前端据此配对起止；一轮内 MAY 包含多对工具事件（含并行调用）。

`tool_end.status` 为 `success` 时 SHALL 含结构化 `result`；为 `error` 时 SHALL 含 `error` 与 `errorCode`。

#### Scenario: 工具成功调用

- **WHEN** Agent 调用某工具且工具正常返回
- **THEN** 系统下发 `tool_start` 后下发 `tool_end{status:"success", result:<结构化结果>}`，二者 `toolCallId` 相同

#### Scenario: 异步工具快速返回待办态

- **WHEN** 工具表示已创建耗时任务（异步重任务）
- **THEN** `tool_end` 以 `status:"success"` 返回含任务标识的结果（如 `{taskId, status:"PENDING"}`），任务真正完成不发第二次 `tool_end`，而由 `notification` 事件通知

### Requirement: 工具错误不杀轮次

单次工具调用失败 SHALL 仅表现为该次 `tool_end{status:"error", errorCode}`，Agent MUST NOT 因此终止整轮；系统 SHALL 把错误作为工具结果返回给模型，允许其重试或改用其他工具并在同一轮继续。工具异常携带 `code` 属性时 SHALL 映射为 `errorCode`，否则 `errorCode` 为 `INTERNAL`。

仅系统级故障 SHALL 下发独立 `error` 事件终止轮次，且 `error.message` MUST 为用户可读文本、不暴露内部堆栈。

#### Scenario: 工具抛错后模型继续反应

- **WHEN** 工具抛出带 `code` 的异常（如 `NOT_FOUND`）
- **THEN** 系统下发 `tool_end{status:"error", errorCode:"NOT_FOUND"}`，模型据此继续生成，最终仍以 `turn_end{finishReason:"stop"}` 收尾，轮次不崩溃

### Requirement: 工具调用轮数上限受控终止

系统 SHALL 对单轮工具调用次数设上限 `max_tool_rounds`（默认 8，可配置）；达到上限时系统 MUST 以 `turn_end{finishReason:"max_tool_rounds"}` 受控收尾，MUST NOT 下发 `error`，也 MUST NOT 计入错误 span。

#### Scenario: 达到工具调用上限

- **WHEN** 模型在单轮内持续请求工具调用直至达到 `max_tool_rounds`
- **THEN** 系统捕获递归上限并下发 `turn_end{finishReason:"max_tool_rounds"}`，连接正常关闭而非报错

### Requirement: eventSeq 断线续传标识

每个经事件中枢下发的事件 SHALL 携带单调递增的 `eventSeq`（写入 data 并作为 SSE `id:` 行）；客户端断线重连时携 `Last-Event-ID`，系统 SHALL 重放 `eventSeq` 大于该游标的事件。

#### Scenario: 断线后重放缺失事件

- **WHEN** 客户端在收到 `eventSeq=N` 后断开，并以 `Last-Event-ID: N` 重连持久通道
- **THEN** 系统重放窗口内所有 `eventSeq > N` 的事件（含未送达的 `notification`）
