## ADDED Requirements

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
