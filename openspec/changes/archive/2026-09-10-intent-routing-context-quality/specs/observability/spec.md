## ADDED Requirements

### Requirement: 路由质量指标

系统 SHALL 在既有路由计数指标之外，导出衡量意图识别质量的低基数指标，使路由误杀、检索置信度分布、上下文改写与缺参追问可被度量：

- 路由误杀计数：模型本轮实际调用的工具不在路由推荐工具集内时 +1（rule/vector/llm/option 收窄路径计，其中 `option` 为选项确定性收窄；degraded/fallback 全量路径不计），标签含 env、路由路径 path、检索路 retrieval（vector/keyword，区分降级期与正常期误杀率）；BM25 关键词收窄后经 LLM 兜底成功裁决的轮次按 `llm` 路径照常计入误杀口径，MUST NOT 因检索输入来自关键词路而豁免；
- 检索置信度分布：向量检索的 top1 相似度分数与 top1-top2 分差，以 histogram 记录；关键词路（BM25）与 RRF 融合路的 top1 分数 SHALL 以检索路标签（vector/bm25/rrf）区分记入同一分布指标，支撑两路阈值分别校准；
- query 改写触发计数：路由因指代/低置信触发上下文 query 改写时 +1（含成功/失败标签）；
- 缺参追问触发计数：工具因缺少必填参数未执行、回喂缺参引导时 +1，标签含 env、工具名。

这些指标 MUST NOT 携带 userId/sessionId/turnId 等高基数字段（此类标识仅进 span/审计日志）。指标 SHALL 可通过既有 metrics 导出通道（console/OTLP）观测。

#### Scenario: 误杀指标在模型越出推荐集时累加

- **WHEN** 路由收窄推荐集为 {A,B} 而模型实际调用了推荐集外工具
- **THEN** 误杀计数 +1，标签含 env、路由路径与检索路（vector/keyword），且不含 userId/sessionId/turnId；BM25 关键词降级轮次经 LLM 兜底收窄的误杀同样累加且以 retrieval=keyword 区分

#### Scenario: 置信度分布记录分数与分差

- **WHEN** 发生一次向量检索路由，或关键词路（BM25）/RRF 融合产生 top1 分数
- **THEN** 向量 top1 分数与分差、以及 BM25/RRF 路 top1 分数被记入 histogram（以检索路标签区分），可聚合出各路分数分布用于阈值校准

#### Scenario: 改写与缺参追问可计数

- **WHEN** 路由触发 query 改写，或工具因缺必填参数回喂引导
- **THEN** 对应计数指标 +1，并带成功/失败（改写）或工具名（缺参）标签

### Requirement: 澄清结果维度

系统 SHALL 为用户澄清率指标补充低基数结果维度 `result`，以度量结构化选项硬闭环相对手打文本闭环的收益：选项点选后经确定性收窄闭环记为 `option`；用户手打后经文本闭环成功收窄记为 `text`；澄清后用户回答仍无法对应、再次澄清记为 `repeat`。该维度标签 MUST 限于 `option|text|repeat` 等低基数枚举，MUST NOT 携带 userId/sessionId/turnId 或选项 value 原文。澄清结果计数 SHALL 仅由独立的澄清计数接口按"上一轮澄清的闭环结局"显式记录：仅当本轮路由识别到上一轮澄清并产生闭环结局（option/text/repeat）时计数；首轮澄清（无上一轮澄清）不属任何闭环结局、SHALL NOT 记入该维度（其总量由路由路径计数的 `clarify` path 维度承担）。既有路径计数接口内耦合的"path=clarify 即自动累加澄清计数"逻辑 SHALL 移除，MUST NOT 与显式澄清结果计数形成双计。

#### Scenario: 澄清结果按 option/text/repeat 计数

- **WHEN** 一轮澄清后，用户分别经点选选项闭环、手打后成功收窄、或再次触发澄清
- **THEN** 澄清计数对应 `result=option` / `result=text` / `result=repeat` 分别 +1，标签不含用户/会话/轮次标识与选项原文

#### Scenario: 首轮澄清与路径计数不双计

- **WHEN** 一轮全新低置信澄清（无上一轮澄清）产生，或一轮"再次澄清"（path=clarify 且闭环结局=repeat）产生
- **THEN** 首轮澄清仅使路由路径计数的 path=clarify 维度 +1、澄清结果计数不增加；repeat 轮澄清结果计数仅 +1 一次（显式 result=repeat），路径计数内的自动澄清累加逻辑已被移除，不产生双计

