# agent-loop Specification

## Purpose
定义 Agent 推理-行动循环：基于 LangGraph 的无状态图编排，每轮从持久化存储载入消息历史、调用模型与工具、流式产出 UI 事件并持久化新消息，同时提供递归上限防护、上下文 token 预算裁剪与工具调用配对清洗，保证长历史与异常中断下协议始终合法。

## Requirements

### Requirement: 无状态图与历史载入

系统 SHALL 以无状态图（不使用 checkpointer）运行推理-行动循环：每轮开始时从消息存储按会话作用域（service/env/user/sessionId）载入历史消息，轮次结束后将本轮新消息持久化。历史载入 SHALL 设行数硬上限（默认 500）作为防护。

载入历史构建模型上下文时，被外置到 blob 存储的消息（工具结果/超长助手文本，`content_ref` 非空）SHALL 仅以消息行内的 head 摘要进入上下文，系统 MUST NOT 为构建上下文而拉取 blob 全量内容——完整产物不进入模型上下文（其体量本就超出 token 预算，会被裁剪）。

#### Scenario: 每轮从存储重建上下文

- **WHEN** 某会话发起新一轮对话
- **THEN** 系统从消息存储载入该会话历史、追加本轮用户消息后送入图，本轮产生的 assistant/tool 消息在结束时持久化

#### Scenario: 外置大结果不拉全量进上下文

- **WHEN** 历史中某条工具消息对应一个外置的大产物（`content_ref` 非空）
- **THEN** 送入模型的该条消息内容为行内 head 摘要而非 blob 全量，构建上下文过程不触发大对象读取

### Requirement: 递归上限受控终止

系统 SHALL 将 `max_tool_rounds`（默认 8）映射为图的递归上限（`recursion_limit = max_tool_rounds * 2 + 1`）；达到上限时系统 MUST 以 `turn_end{finishReason:"max_tool_rounds"}` 受控收尾，MUST NOT 抛出未处理异常。

#### Scenario: 工具循环达到上限

- **WHEN** 模型连续请求工具调用直至触发递归上限
- **THEN** 系统捕获递归错误、下发受控 `turn_end`，本轮已有消息在清洗后持久化

### Requirement: 上下文 token 预算裁剪

系统 SHALL 在送入模型前按 token 预算（`max_context_tokens`，默认 24000）裁剪历史，保留 system 消息与最近消息；裁剪 MUST 保持工具调用配对——若裁剪后首条是缺少对应 assistant 工具调用的孤儿 ToolMessage，SHALL 向后跳过，避免工具协议错乱。

token 估算 SHALL 按字符类别分别计算：**CJK 统一表意文字（含中文标点）按每字约 1 token 计**，其余文本沿用每 4 字符约 1 token（`len//4`）；对中文内容 MUST NOT 再以 `//4` 低估（旧实现对中文低估 2~4 倍，长会话实际 token 数超出模型窗口触发 context-length 400）。工具调用参数 SHALL 计入估算。

#### Scenario: 超预算裁剪不产生孤儿工具消息

- **WHEN** 历史消息估算 token 超过预算
- **THEN** 系统保留最近消息且裁剪结果不以无配对的 ToolMessage 开头，模型输入的工具调用/工具消息始终成对

#### Scenario: 中文长会话实际 token 不超窗

- **WHEN** 历史由约 6000 个汉字组成（旧 `//4` 估算约 1500 token、新估算约 6000 token）且预算为 24000
- **THEN** 送入模型的估算值接近实际分词结果（误差不导致超窗 400），仍在预算内时正常发送；若新估算超预算则按既有规则裁剪

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

### Requirement: 工具集经路由确定

每轮构建 Agent 前，模型可见的工具集 SHALL 由 Skill 路由层在环境硬过滤的基础上确定（规则/向量检索/兜底 LLM 收窄为子集）；路由关闭或发生降级时，工具集为环境过滤后的全量 Skill。Agent SHALL 仍每请求重建以应用动态工具集。

#### Scenario: 路由收窄后构建 Agent

- **WHEN** 路由为某轮确定工具子集 {A, C}
- **THEN** 该轮构建的 Agent 仅绑定 A、C 工具，模型无法调用子集外的 Skill；下一轮按新消息重新路由

#### Scenario: 路由关闭使用全量工具

- **WHEN** `routing.enabled=false`
- **THEN** Agent 绑定环境过滤后的全部 Skill，与无路由行为一致

### Requirement: 澄清轮次不进入推理图

当路由判定需向用户澄清时，系统 SHALL 直接产出一条 assistant 文本回复并以正常轮次事件流结束（`turn_start`..`turn_delta`..`turn_end`），MUST NOT 构建 ReAct 推理图、MUST NOT 调用任何业务工具；该澄清回复 SHALL 作为 assistant 消息持久化，用户补充后的下一轮重新执行路由。

#### Scenario: 澄清轮次无工具调用

- **WHEN** 路由三级（规则/向量/兜底 LLM）均无法确定意图
- **THEN** 本轮事件流仅含文本增量与正常 turn_end，无 tool_start/tool_end，持久化消息中无工具调用记录

### Requirement: 工具调用前参数校验与缺参定向追问

系统 SHALL 在 Skill 工具实际执行业务逻辑前完成参数校验，并在缺参/非法参数时把"缺哪个参数"的结构化引导回流给模型，驱动其下一轮定向追问。参数校验由工具框架在执行业务函数前依据该 Skill 的 `args_schema` 完成（校验先于业务函数，业务函数 MUST NOT 被调用）；当必填参数缺失或不合法时，系统 MUST NOT 执行业务逻辑、MUST NOT 以未定义行为编造参数，而是 SHALL 经工具的校验错误回调产出一条 `status="error"` 的工具结果消息（ToolMessage），其内容为结构化 JSON：`errorCode="MISSING_ARGS"`、缺失/非法字段名列表（含字段含义提示）与引导语（"参数不足，请勿编造，请先向用户询问以下参数：…"）。该消息作为正常工具结果回流 ReAct 循环（不杀轮次、不中断会话），使模型在下一轮向用户发起定向追问；用户答复经会话历史累积后，模型再次调用工具时参数补齐即可正常执行。此为"半槽位填充"：框架只负责参数校验与缺参回喂，跨轮参数累积与追问话术由 ReAct 循环与会话历史承担，系统 MUST NOT 引入独立的对话状态跟踪（DST）/表单引擎。该校验守卫 SHALL 可通过配置关闭：关闭时不安装校验错误回调，工具回落框架默认行为（校验失败抛出的异常经既有工具错误处理转为普通错误工具消息，`errorCode` 非 `MISSING_ARGS`），与本期改动前一致。

缺参回流的工具结果经执行事件流以 `tool_end`（状态 error、携带 `errorCode=MISSING_ARGS`）下发与持久化；系统 SHALL 据此识别缺参追问并计入对应指标，MUST NOT 将其计为业务工具失败。

#### Scenario: 缺必填参数时不执行并回喂缺参引导

- **WHEN** 模型调用某 Skill 工具但缺少必填参数（如未提供"出发城市"）
- **THEN** 框架在执行业务函数前校验失败，业务函数未被调用；模型收到一条 `status="error"` 的工具结果，内容含 `errorCode="MISSING_ARGS"` 与缺失字段名（如"出发城市"）及"先向用户询问、勿编造参数"的引导，模型据此在下一轮向用户定向追问该参数

#### Scenario: 参数补齐后正常执行

- **WHEN** 上一轮因缺参数回喂引导，用户补充后模型再次调用同一工具且必填参数齐全
- **THEN** 工具正常执行业务逻辑并返回结果，会话不中断

#### Scenario: 参数校验不泄露为未处理异常

- **WHEN** 参数缺失或非法
- **THEN** 反馈以受控的工具结果（`status="error"` 的 ToolMessage）形式回流 ReAct，MUST NOT 抛出导致轮次失败的未处理异常，轮次正常 `turn_end`

#### Scenario: 关闭校验守卫回落既有行为

- **WHEN** 配置关闭参数校验守卫
- **THEN** 工具不安装缺参引导回调，缺参/非法按框架默认校验失败路径处理（异常经既有工具错误处理转为普通错误消息，`errorCode` 不为 `MISSING_ARGS`、不产生缺参引导话术），行为与本期改动前一致
