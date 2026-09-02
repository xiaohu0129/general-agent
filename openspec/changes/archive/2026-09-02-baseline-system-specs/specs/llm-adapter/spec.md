## Purpose

提供模型中立的 LLM 接入层：以 langchain-core ChatModel 抽象包装任意 OpenAI 兼容端点（/v1/chat/completions，含 tools 与 SSE 流式），支持非流式与流式调用、工具调用解析与错误分类，使框架可对接 ARK/Ollama/DeepSeek/内部网关等多种 provider，并提供本地 stub 供无真实端点时联调。

## ADDED Requirements

### Requirement: OpenAI 兼容端点适配

系统 SHALL 通过 OpenAI 兼容协议（`POST {base_url}/v1/chat/completions`）调用 LLM；`base_url` 可配置且 MUST NOT 包含 `/v1` 后缀（由适配层拼接）。适配层 SHALL 实现 langchain-core `BaseChatModel`，支持非流式生成与 SSE 流式生成，并支持 `bind_tools` 与工具调用（tool_calls）解析。

当 `llm.base_url` 留空时，系统 SHALL 默认指向本地 stub LLM（`http://localhost:9094`）。

#### Scenario: 接入任意兼容端点

- **WHEN** 配置 `llm.base_url` 为某 OpenAI 兼容服务地址
- **THEN** 系统向 `{base_url}/v1/chat/completions` 发起请求，非流式返回文本、流式返回增量，工具调用能被正确解析为 tool_calls

#### Scenario: 未配置端点时使用本地 stub

- **WHEN** `llm.base_url` 为空
- **THEN** 系统默认连接本地 stub LLM（:9094），本地联调无需真实 LLM 服务

### Requirement: 流式分片协议兼容

系统在 SSE 流式接收模型分片时，SHALL 始终以列表类型传递工具调用分片字段（无工具分片时为空列表 `[]`），MUST NOT 传 `None`，以兼容 langchain-core 1.x 对该字段的类型约束。

#### Scenario: 纯文本增量分片

- **WHEN** 流式响应中某分片仅含文本增量、无工具调用
- **THEN** 适配层构造的消息分片工具调用字段为空列表，不触发校验错误，轮次正常继续

### Requirement: LLM 错误分类

系统 SHALL 将 LLM 调用失败按 HTTP/网络错误分类为错误码（如鉴权/不可用、限流、超时、内部错误、内容过滤等），错误 SHALL 携带分类码用于事件 `errorCode`、span 错误属性与 metric 标签；API Key 仅保存在内存，MUST NOT 写入日志。

#### Scenario: 上游返回错误状态

- **WHEN** LLM 端点返回错误 HTTP 状态码或请求超时
- **THEN** 系统映射为对应错误码并上报，错误信息不泄露 API Key

### Requirement: 本地 stub LLM

系统 SHALL 提供一个 OpenAI 协议本地 stub 服务（独立运行，默认 :9094），支持 `/v1/chat/completions`（含 SSE 流式与工具调用脚本），供无真实端点时端到端联调与测试。

#### Scenario: 用 stub 跑通对话

- **WHEN** 主服务 `llm.base_url` 留空并启动 stub LLM
- **THEN** `POST /chat` 可端到端返回完整 `turn_start..turn_end` 事件流，无需外部 LLM
