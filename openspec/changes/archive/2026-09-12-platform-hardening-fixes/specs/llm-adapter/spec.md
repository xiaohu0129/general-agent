## MODIFIED Requirements

### Requirement: 确定性采样参数

系统 SHALL 支持为 ChatModel 调用透传采样参数：执行 LLM 与路由兜底 LLM 的调用 SHALL 默认固定 `temperature=0` 以降低采样随机性、提升相同输入下的结果一致性；该参数 SHALL 可通过 `llm.temperature` 配置覆盖，且该配置在构造模型实例时 MUST 真实生效（MUST NOT 被静默忽略）。

#### Scenario: 模型调用固定 temperature

- **WHEN** 系统发起执行 LLM 或路由兜底 LLM 调用且未配置覆盖值
- **THEN** HTTP 请求体携带 `temperature=0`（构造模型实例时读取配置，默认值即 0），相同输入下工具选择与参数填充结果稳定

#### Scenario: 显式配置的 temperature 生效

- **WHEN** 配置 `llm.temperature=0.7`
- **THEN** 执行 LLM 请求体实际携带 `temperature=0.7`（配置值传入模型实例而非只存在于配置中）

### Requirement: 流式分片协议兼容

系统在 SSE 流式接收模型分片时，SHALL 始终以列表类型传递工具调用分片字段（无工具分片时为空列表 `[]`），MUST NOT 传 `None`，以兼容 langchain-core 1.x 对该字段的类型约束。

对 SSE 数据行的解析 SHALL 逐分片容错：单个 `data:` 分片内容不是合法 JSON（或不符合预期结构）时，系统 SHALL 跳过该分片并记录告警，继续处理后续分片，MUST NOT 因单个畸形分片终止整个流式响应或杀掉该轮次。

#### Scenario: 纯文本增量分片

- **WHEN** 流式响应中某分片仅含文本增量、无工具调用
- **THEN** 适配层构造的消息分片工具调用字段为空列表，不触发校验错误，轮次正常继续

#### Scenario: 单个畸形分片不中断流

- **WHEN** SSE 流中某个 `data:` 分片为损坏的 JSON，其前后分片均正常
- **THEN** 系统跳过该畸形分片（记录一条告警），前后正常分片照常产出，轮次继续直到正常 `turn_end`
