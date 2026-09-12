## Purpose

提供大规模 Skill 下的意图路由与工具渐进式加载能力：在环境硬过滤之后、构建 Agent 之前，按"规则路由 → 向量检索 → 低置信 LLM 兜底 → 用户澄清"的确定性分级链路，将数百个候选 Skill 收窄为模型可见的小工具集，并保证路由决策可观测、相同输入可复现。

## ADDED Requirements

### Requirement: 路由分级链路与开关

系统 SHALL 在每轮构建 Agent 前执行 Skill 路由，按确定性从高到低依次为：① 环境硬过滤（既有 env 白名单）；② 规则路由（配置化 pattern/命令）；③ 向量检索（embedding 相似度 top-k）；④ 低置信兜底（结构化输出 LLM 选择技能域）；⑤ 用户澄清。路由 SHALL 可通过配置整体关闭（`routing.enabled=false`），关闭时工具集为环境过滤后的全量 Skill，行为与无路由时完全一致。环境硬过滤始终最先执行且不可被路由绕过。

#### Scenario: 路由关闭时退回全量工具

- **WHEN** 配置 `routing.enabled=false`
- **THEN** 系统不执行规则/向量/兜底路由，模型可见工具为 env 过滤后的全部 Skill，行为与现状一致

#### Scenario: 环境硬过滤先于路由

- **WHEN** 某 Skill 的 `allowed_envs` 不包含请求环境
- **THEN** 该 Skill 不进入规则匹配、向量索引检索与兜底选域的任何候选集合，模型在该环境无法调用它

### Requirement: 规则前置路由

系统 SHALL 支持配置化的规则路由：每条规则包含匹配模式（正则/命令前缀，作用于用户消息文本）与目标 Skill 集合（按 Skill 名称或 category）。规则命中时 SHALL 直接确定工具集，不调用 embedding 与 LLM，结果对相同输入恒定可复现；多条规则命中时取配置顺序的第一条。规则路由用于高频固定说法与必须 100% 确定的意图，是模型自由选择的逃生门。

#### Scenario: 规则命中零 LLM 确定工具

- **WHEN** 用户消息匹配某条配置规则，该规则映射到 Skill 集合 {A, B}
- **THEN** 模型可见工具仅为 A、B（经 env 过滤后的交集），本轮不发生 embedding 与兜底 LLM 调用，相同消息每次得到相同工具集

#### Scenario: 规则未命中进入向量检索

- **WHEN** 用户消息不匹配任何规则
- **THEN** 系统进入向量检索阶段确定候选工具

### Requirement: Skill 向量索引与检索

系统 SHALL 为每个候选 Skill 构建可检索的向量表示：embedding 输入由 Skill 的 `description` 与 `examples`（示例话语）组成；索引在服务启动时基于注册表批量构建，并以 Skill 元数据（名称/描述/示例/category/允许环境）的哈希作为版本键缓存于本地，元数据未变更时复用缓存。检索时 SHALL 对用户消息计算 embedding，在当前环境候选集合内按向量相似度返回 top-k Skill（k 可配置，默认 15~20），并返回每个结果的相似度分数与排序。向量存储 SHALL 为进程内存实现（余弦相似度），MUST NOT 依赖外部向量数据库。

#### Scenario: 按语义检索相关 Skill

- **WHEN** 用户消息语义与某 Skill 的示例话语相近，且该 Skill 在当前环境可用
- **THEN** 该 Skill 出现在 top-k 检索结果中并携带相似度分数，无关 Skill 不出现

#### Scenario: 元数据未变复用索引缓存

- **WHEN** 服务重启且注册表 Skill 元数据哈希与缓存一致
- **THEN** 系统加载本地缓存索引而不重新调用 embedding 构建；元数据变更（描述/示例/category/环境）时哈希变化触发重建

### Requirement: 置信分流与低置信兜底

系统 SHALL 依据检索置信度分流：当 top-1 相似度高于配置阈值且 top-1 与 top-2 分差大于配置 margin 时，判定高置信，直接以 top-k（可含 category 同域扩充）作为工具集；不满足时判定低置信，SHALL 调用一次结构化输出的路由 LLM（固定 temperature=0），在检索候选与 category 清单中输出目标技能域/意图类别与置信度、理由。路由 LLM 选择确定时按所选 category/集合收窄工具；路由 LLM 仍无法确定时进入用户澄清。检索服务异常或返回低质结果时，系统 SHALL 降级为兜底 LLM 或全量工具，MUST NOT 因路由故障导致整轮失败。

#### Scenario: 高置信直接收窄

- **WHEN** 向量检索 top-1 分数高于阈值且与 top-2 分差大于 margin
- **THEN** 系统不再调用路由 LLM，直接以检索确定的工具子集构建 Agent

#### Scenario: 低置信由路由 LLM 选域

- **WHEN** top-1 分数低于阈值或 top1/top2 分差不足
- **THEN** 系统调用结构化输出 LLM 输出目标 category 与置信度；选定后模型仅可见该域工具集

#### Scenario: 检索故障降级不杀轮次

- **WHEN** embedding 端点不可用或检索过程异常
- **THEN** 系统记录路由错误并降级（兜底 LLM 或 env 过滤后全量工具），对话轮次正常进行，前端收到正常 turn_start..turn_end

### Requirement: 低置信用户澄清

当规则未命中、向量检索与路由 LLM 均无法确定意图时，系统 SHALL 生成一条澄清回复向用户询问更多细节（如列出可能的能力方向请用户选择/补充描述），该轮 SHALL 以普通 assistant 文本轮次结束（turn_start..turn_delta..turn_end），MUST NOT 构建 ReAct 图、MUST NOT 调用任何业务工具。澄清回复内容由 LLM 基于可见 category 清单生成。

#### Scenario: 无法确定意图时反问

- **WHEN** 路由 LLM 输出低置信/无明确类别
- **THEN** 本轮返回澄清文本（请用户补充或选择方向），不执行工具调用；用户补充后的下一轮重新走路由

### Requirement: 路由确定性

系统 SHALL 保证路由决策的工程可复现性：规则匹配为纯确定性；向量检索的 embedding 模型 id 与索引版本固定并记录；执行 LLM 与路由兜底 LLM 调用 SHALL 固定 `temperature=0`；相同用户消息、相同历史与相同 Skill 注册表版本下，规则与向量路径 SHALL 产出相同工具集。系统不追求 assistant 回复文本的逐字一致。

#### Scenario: 相同输入路由结果一致

- **WHEN** 同一用户消息在注册表版本不变时连续发起两轮（历史一致）
- **THEN** 规则/向量路径选出的工具集相同，路由记录中的 embedding 模型与索引版本一致

### Requirement: 路由决策可观测

系统 SHALL 为每次路由决策记录可观测信息：路由路径（rule/vector/llm/clarify/fallback/degraded）、规则命中条目（若有）、向量检索 top-k 工具名与相似度分数、top1/top2 分差、路由 LLM 输出类别与置信度（若有）、最终暴露给模型的工具名集合、embedding 模型 id 与索引版本。这些信息 SHALL 进入 OTel span（`intent_route`）与结构化审计日志；系统 SHALL 导出低基数路由指标（路由路径分布、兜底触发率、澄清率、路由降级率），工具名/类别等中低基数字段可作为 metric 标签，userId/sessionId/turnId MUST NOT 进入 metric（仅进 span/日志），以支持路由决策的离线回放与"检索推荐 vs 模型实际选择"的比对。

#### Scenario: 路由决策可回放

- **WHEN** 一轮对话完成路由
- **THEN** trace 中存在 `intent_route` span，记录路径、top-k 分数、分差、最终工具集与模型/索引版本；审计日志含同决策记录，可据此复现"为何选出这些工具"
