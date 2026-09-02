## MODIFIED Requirements

### Requirement: 无状态图与历史载入

系统 SHALL 以无状态图（不使用 checkpointer）运行推理-行动循环：每轮开始时从消息存储按会话作用域（service/env/user/sessionId）载入历史消息，轮次结束后将本轮新消息持久化。历史载入 SHALL 设行数硬上限（默认 500）作为防护。

载入历史构建模型上下文时，被外置到 blob 存储的消息（工具结果/超长助手文本，`content_ref` 非空）SHALL 仅以消息行内的 head 摘要进入上下文，系统 MUST NOT 为构建上下文而拉取 blob 全量内容——完整产物不进入模型上下文（其体量本就超出 token 预算，会被裁剪）。

#### Scenario: 每轮从存储重建上下文

- **WHEN** 某会话发起新一轮对话
- **THEN** 系统从消息存储载入该会话历史、追加本轮用户消息后送入图，本轮产生的 assistant/tool 消息在结束时持久化

#### Scenario: 外置大结果不拉全量进上下文

- **WHEN** 历史中某条工具消息对应一个外置的大产物（`content_ref` 非空）
- **THEN** 送入模型的该条消息内容为行内 head 摘要而非 blob 全量，构建上下文过程不触发大对象读取
