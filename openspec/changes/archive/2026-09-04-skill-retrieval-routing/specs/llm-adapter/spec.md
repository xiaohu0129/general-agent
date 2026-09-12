## ADDED Requirements

### Requirement: Embedding 兼容端点适配

系统 SHALL 提供 OpenAI 兼容的 embedding 客户端，通过 `POST {base_url}/embeddings` 获取文本向量（输入文本列表，输出向量列表），`base_url` 与模型 id SHALL 可配置，API Key 仅保存在内存、MUST NOT 写入日志。embedding 端点未配置时 SHALL 默认指向本地 stub（与 chat 端点同基址）。

#### Scenario: 获取文本向量

- **WHEN** 路由对用户消息与 Skill 文本请求 embedding
- **THEN** 系统向 `{embedding.base_url}/embeddings`（或默认 stub）发起请求并返回与输入一一对应的向量，相同文本在模型版本不变时返回相同向量

#### Scenario: embedding 失败可降级

- **WHEN** embedding 端点返回错误或超时
- **THEN** 错误被分类并上报路由层，路由降级处理（兜底 LLM 或全量工具），错误信息不泄露 API Key，整轮对话不失败

### Requirement: 确定性采样参数

系统 SHALL 支持为 ChatModel 调用透传采样参数：执行 LLM 与路由兜底 LLM 的调用 SHALL 固定 `temperature=0` 以降低采样随机性、提升相同输入下的结果一致性；该参数 SHALL 可配置但默认确定性。

#### Scenario: 模型调用固定 temperature

- **WHEN** 系统发起执行 LLM 或路由兜底 LLM 调用
- **THEN** 请求携带 `temperature=0`（除非显式配置覆盖），相同输入下工具选择与参数填充结果稳定

### Requirement: 本地 stub embedding

本地 stub LLM 服务 SHALL 提供 `/v1/embeddings` 端点，返回确定性向量（如基于输入文本内容的哈希/确定性派生），使路由索引构建与检索在无真实 embedding 端点时可端到端联调与测试，且相同文本始终得到相同向量。

#### Scenario: stub embedding 确定性

- **WHEN** 测试中两次请求相同文本的 stub embedding
- **THEN** 两次返回的向量完全一致，且语义构造（含相同 token 的文本）向量相近，可支撑路由检索的确定性单测
