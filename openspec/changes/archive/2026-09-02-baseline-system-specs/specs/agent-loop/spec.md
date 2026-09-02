## Purpose

定义 Agent 推理-行动循环：基于 LangGraph 的无状态图编排，每轮从持久化存储载入消息历史、调用模型与工具、流式产出 UI 事件并持久化新消息，同时提供递归上限防护、上下文 token 预算裁剪与工具调用配对清洗，保证长历史与异常中断下协议始终合法。

## ADDED Requirements

### Requirement: 无状态图与历史载入

系统 SHALL 以无状态图（不使用 checkpointer）运行推理-行动循环：每轮开始时从消息存储按会话作用域（service/env/user/sessionId）载入历史消息，轮次结束后将本轮新消息持久化。历史载入 SHALL 设行数硬上限（默认 500）作为防护。

#### Scenario: 每轮从存储重建上下文

- **WHEN** 某会话发起新一轮对话
- **THEN** 系统从消息存储载入该会话历史、追加本轮用户消息后送入图，本轮产生的 assistant/tool 消息在结束时持久化

### Requirement: 递归上限受控终止

系统 SHALL 将 `max_tool_rounds`（默认 8）映射为图的递归上限（`recursion_limit = max_tool_rounds * 2 + 1`）；达到上限时系统 MUST 以 `turn_end{finishReason:"max_tool_rounds"}` 受控收尾，MUST NOT 抛出未处理异常。

#### Scenario: 工具循环达到上限

- **WHEN** 模型连续请求工具调用直至触发递归上限
- **THEN** 系统捕获递归错误、下发受控 `turn_end`，本轮已有消息在清洗后持久化

### Requirement: 上下文 token 预算裁剪

系统 SHALL 在送入模型前按 token 预算（`max_context_tokens`，默认 24000；以 `len(content)//4` 粗估）裁剪历史，保留 system 消息与最近消息；裁剪 MUST 保持工具调用配对——若裁剪后首条是缺少对应 assistant 工具调用的孤儿 ToolMessage，SHALL 向后跳过，避免工具协议错乱。

#### Scenario: 超预算裁剪不产生孤儿工具消息

- **WHEN** 历史消息估算 token 超过预算
- **THEN** 系统保留最近消息且裁剪结果不以无配对的 ToolMessage 开头，模型输入的工具调用/工具消息始终成对

### Requirement: 孤儿工具调用清洗

系统 SHALL 在两处清洗"无对应 ToolMessage 的 tool_calls"（孤儿工具调用）：① 载入历史后；② 持久化新消息前。清洗规则为仅保留有匹配 ToolMessage 的 tool_call；清洗后既无文本内容也无工具调用的空 assistant 消息 SHALL 被丢弃。

该清洗 MUST 覆盖轮次异常中断（如递归上限）场景，防止"assistant(tool_calls) 后直接跟 human"被发送给模型导致请求永久失败。

#### Scenario: 递归上限遗留的未应答工具调用不落库

- **WHEN** 轮次在最后一次工具执行前因递归上限中断，留下一条含 tool_calls 但无对应 ToolMessage 的 assistant 消息
- **THEN** 持久化前该消息的未应答 tool_calls 被剔除；若剔除后为空则整条丢弃，后续轮次载入历史不会触发模型 400

#### Scenario: 已污染的旧历史在载入时修复

- **WHEN** 历史中已存在末尾未应答 tool_calls 的脏数据
- **THEN** 系统在载入后清洗孤儿 tool_calls，使发送给模型的历史协议合法

### Requirement: 流式事件与持久化角色顺序

系统 SHALL 将图执行流翻译为 UI 事件（`turn_start` 先于任何可能失败的 I/O、`turn_delta` 文本增量、`tool_start`/`tool_end`、`turn_end`）；持久化的消息角色顺序 SHALL 为 `user -> assistant(tool_calls) -> tool -> assistant`。

`turn_start` MUST 在历史加载之前下发，使后续历史加载/持久化失败时前端仍能收到 `turn_start -> error` 的完整语义。

#### Scenario: 历史加载失败仍有 turn_start

- **WHEN** 轮次开始后历史加载（存储 I/O）失败
- **THEN** 前端已先收到 `turn_start`，随后收到 `error`，不会出现只有错误而无轮次起始的情况
