# llm-adapter Specification

## Purpose
提供模型中立的 LLM 接入层：以 langchain-core ChatModel 抽象包装任意 OpenAI 兼容端点（/v1/chat/completions，含 tools 与 SSE 流式），支持非流式与流式调用、工具调用解析与错误分类，使框架可对接 ARK/Ollama/DeepSeek/内部网关等多种 provider，并提供本地 stub 供无真实端点时联调。

## Requirements

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

对 SSE 数据行的解析 SHALL 逐分片容错：单个 `data:` 分片内容不是合法 JSON（或不符合预期结构）时，系统 SHALL 跳过该分片并记录告警，继续处理后续分片，MUST NOT 因单个畸形分片终止整个流式响应或杀掉该轮次。

#### Scenario: 纯文本增量分片

- **WHEN** 流式响应中某分片仅含文本增量、无工具调用
- **THEN** 适配层构造的消息分片工具调用字段为空列表，不触发校验错误，轮次正常继续

#### Scenario: 单个畸形分片不中断流

- **WHEN** SSE 流中某个 `data:` 分片为损坏的 JSON，其前后分片均正常
- **THEN** 系统跳过该畸形分片（记录一条告警），前后正常分片照常产出，轮次继续直到正常 `turn_end`

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

### Requirement: Embedding 兼容端点适配

系统 SHALL 提供 OpenAI 兼容的 embedding 客户端，通过 `POST {base_url}/embeddings` 获取文本向量（输入文本列表，输出向量列表），`base_url` 与模型 id SHALL 可配置，API Key 仅保存在内存、MUST NOT 写入日志。embedding 端点未配置时 SHALL 默认指向本地 stub（与 chat 端点同基址）。

#### Scenario: 获取文本向量

- **WHEN** 路由对用户消息与 Skill 文本请求 embedding
- **THEN** 系统向 `{embedding.base_url}/embeddings`（或默认 stub）发起请求并返回与输入一一对应的向量，相同文本在模型版本不变时返回相同向量

#### Scenario: embedding 失败可降级

- **WHEN** embedding 端点返回错误或超时
- **THEN** 错误被分类并上报路由层，路由降级处理（兜底 LLM 或全量工具），错误信息不泄露 API Key，整轮对话不失败

### Requirement: 确定性采样参数

系统 SHALL 支持为 ChatModel 调用透传采样参数：执行 LLM 与路由兜底 LLM 的调用 SHALL 默认固定 `temperature=0` 以降低采样随机性、提升相同输入下的结果一致性；该参数 SHALL 可通过 `llm.temperature` 配置覆盖，且该配置在构造模型实例时 MUST 真实生效（MUST NOT 被静默忽略）。

#### Scenario: 模型调用固定 temperature

- **WHEN** 系统发起执行 LLM 或路由兜底 LLM 调用且未配置覆盖值
- **THEN** HTTP 请求体携带 `temperature=0`（构造模型实例时读取配置，默认值即 0），相同输入下工具选择与参数填充结果稳定

#### Scenario: 显式配置的 temperature 生效

- **WHEN** 配置 `llm.temperature=0.7`
- **THEN** 执行 LLM 请求体实际携带 `temperature=0.7`（配置值传入模型实例而非只存在于配置中）

### Requirement: 本地 stub embedding

本地 stub LLM 服务 SHALL 提供 `/v1/embeddings` 端点，返回确定性向量（如基于输入文本内容的哈希/确定性派生），使路由索引构建与检索在无真实 embedding 端点时可端到端联调与测试，且相同文本始终得到相同向量。

#### Scenario: stub embedding 确定性

- **WHEN** 测试中两次请求相同文本的 stub embedding
- **THEN** 两次返回的向量完全一致，且语义构造（含相同 token 的文本）向量相近，可支撑路由检索的确定性单测
