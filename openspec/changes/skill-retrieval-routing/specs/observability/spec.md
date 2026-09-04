## ADDED Requirements

### Requirement: 路由 Span 与指标

系统 SHALL 为 Skill 路由决策建立 `intent_route` span，记录路由路径（rule/vector/llm/clarify/fallback/degraded）、命中规则（若有）、向量检索 top-k 工具名与相似度分数、top1/top2 分差、路由 LLM 选定类别与置信度（若有）、最终暴露工具集合、embedding 模型 id 与索引版本；该 span SHALL 位于 `run_turn` 之下、`agent_graph` 之前。系统 SHALL 导出低基数路由指标（路由路径计数/分布、兜底 LLM 触发率、用户澄清率、路由降级率），metric 标签限于路由路径/类别等低基数字段，userId/sessionId/turnId MUST NOT 进入 metric。

#### Scenario: 路由 span 记录决策

- **WHEN** 一轮对话经过向量检索高置信收窄
- **THEN** trace 中 run_turn 下存在 intent_route span，属性含 path=vector、top-k 工具名与分数、分差、最终工具集、embedding 模型与索引版本，且无 userId/sessionId metric 标签

#### Scenario: 路由降级可观测

- **WHEN** embedding 端点异常导致路由降级为全量工具
- **THEN** intent_route span 记录 path=degraded 与错误原因，路由降级指标递增，审计日志可查
