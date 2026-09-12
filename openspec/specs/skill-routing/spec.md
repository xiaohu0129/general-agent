# skill-routing Specification

## Purpose
提供大规模 Skill 下的意图路由与工具渐进式加载能力：在环境硬过滤之后、构建 Agent 之前，按"规则路由 → 向量检索 → 低置信 LLM 兜底 → 用户澄清"的确定性分级链路，将数百个候选 Skill 收窄为模型可见的小工具集，并保证路由决策可观测、相同输入可复现。

## Requirements

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

系统 SHALL 为每个候选 Skill 构建可检索的向量表示，并采用**多向量 + max-sim** 召回以消除单向量语义平均：每个 Skill 的 `description` 建一条向量，其每条 `example`（示例话语）各建一条向量；检索时对用户 query 计算 embedding，与某 Skill 的全部向量求相似度并取**最大值**作为该 Skill 的得分，再在当前环境候选集合内按得分返回 top-k Skill（k 可配置，默认 15~20），返回每个结果的相似度分数与排序。索引在服务启动时基于注册表批量构建，并以 Skill 元数据（名称/描述/示例/category/允许环境）的哈希作为版本键缓存于本地，元数据未变更时复用缓存；缓存结构 SHALL 能容纳每 Skill 多条向量。向量存储 SHALL 为进程内存实现（余弦相似度），MUST NOT 依赖外部向量数据库。没有 `examples` 的 Skill SHALL 仅以 description 建向量，并在索引构建与路由记录中标注 `has_examples=false`，且该清单 SHALL 可经日志/健康信息筛出，以推动补全示例。

#### Scenario: 按语义检索相关 Skill

- **WHEN** 用户消息语义与某 Skill 的某条示例话语相近，且该 Skill 在当前环境可用
- **THEN** 该 Skill 经 max-sim 得出高分并出现在 top-k 结果中、携带相似度分数，无关 Skill 不出现

#### Scenario: 多用法 Skill 不因语义平均漏召回

- **WHEN** 一个 Skill 含多条差异较大的示例话语，用户消息只匹配其中一种用法
- **THEN** 该 Skill 以"与最匹配那条示例的相似度"计分（max-sim），得分不被其余示例拉低，仍能进入 top-k

#### Scenario: 元数据未变复用索引缓存

- **WHEN** 服务重启且注册表 Skill 元数据哈希与缓存一致
- **THEN** 系统加载本地缓存索引（含每 Skill 多向量）而不重新调用 embedding 构建；元数据变更（描述/示例/category/环境）时哈希变化触发重建

#### Scenario: 无示例 Skill 被标注且可筛出

- **WHEN** 某注册 Skill 未提供 `examples`
- **THEN** 该 Skill 仅以 description 建向量，索引/启动日志中出现 `has_examples=false` 标注且可按 Skill 名筛出，路由不因此报错

#### Scenario: 多向量构建失败降级为单向量

- **WHEN** 多向量索引构建过程中 embedding 部分失败或缓存结构不兼容
- **THEN** 系统记录告警并降级为每 Skill 单向量（或既有不可用降级路径），路由仍可服务，MUST NOT 因索引问题导致启动或整轮失败

#### Scenario: embedding 批量构建按批大小分批请求

- **WHEN** 多向量索引待 embed 文本总量（Skill 数 + examples 总数）超过 `embedding.batch_size`
- **THEN** 系统 SHALL 按批大小分多次请求 embedding，并按输入顺序拼接各批向量（不错乱、不丢失）；单批失败按既有容错路径处理（该 Skill 退化或整体降级），MUST NOT 将千级文本塞入单次 POST 而触发端点批量/超时上限

### Requirement: 置信分流与低置信兜底

系统 SHALL 依据检索置信度分流：当 top-1 相似度高于配置阈值且 top-1 与 top-2 分差大于配置 margin 时，判定高置信，直接以 top-k 作为工具集；不满足时判定低置信，SHALL 调用一次结构化输出的路由 LLM（固定 temperature=0）。路由 LLM 的输出 SHALL 包含：目标 Skill 名列表（可选，仅限当前 env 候选集内的合法 Skill）、目标 category（可选）、置信度 confidence、理由。系统 SHALL 按 confidence 三级分流：① 高置信且点名了候选集内合法 Skill 时，工具集收窄为被点名的 Skill；② 中等置信或仅给出合法 category 时，工具集为该 category 域内的全部候选 Skill；③ 低置信/unknown/chitchat 时分别进入澄清或纯对话。路由 LLM 返回的 confidence MUST NOT 被忽略。检索服务异常或返回低质结果时，系统 SHALL 降级为全量工具，MUST NOT 因路由故障导致整轮失败。

#### Scenario: 高置信直接收窄

- **WHEN** 向量检索 top-1 分数高于阈值且与 top-2 分差大于 margin
- **THEN** 系统不调用路由 LLM，直接以检索确定的工具子集构建 Agent

#### Scenario: 低置信由路由 LLM 选域

- **WHEN** top-1 分数低于阈值或 top1/top2 分差不足
- **THEN** 系统调用结构化输出 LLM（temperature=0），按其 confidence 与输出收窄：高置信点名具体 Skill，否则选定合法 category 域，低置信/unknown 进入澄清

#### Scenario: 兜底 LLM 高置信点名具体 Skill

- **WHEN** 向量检索低置信，路由 LLM 返回高 confidence 且点名了候选集内存在的 Skill {A}
- **THEN** 模型可见工具收窄为 {A}（经 env 过滤交集），不放大到整个 category

#### Scenario: 兜底 LLM 中置信选定技能域

- **WHEN** 路由 LLM 返回中等 confidence 或仅给出合法 category 而未点名 Skill
- **THEN** 模型可见工具为该 category 域内、经 env 过滤的全部 Skill

#### Scenario: 兜底 LLM 点名了候选集外的非法 Skill

- **WHEN** 路由 LLM 返回的 Skill 名不在当前 env 候选集内
- **THEN** 系统忽略非法 Skill 名，按其 category（若合法）收窄或降级为澄清/全量，MUST NOT 暴露候选集外工具

#### Scenario: 检索故障降级不杀轮次

- **WHEN** embedding 端点不可用或向量检索过程异常
- **THEN** 系统记录路由错误；若关键词检索（BM25）可用，SHALL 先经关键词路收窄候选再进入路由 LLM 兜底裁决，关键词路同样无结果时才降级为 env 过滤后全量工具；对话轮次正常进行，前端收到正常 turn_start..turn_end

#### Scenario: 关键词降级轮次的路径归类

- **WHEN** embedding 不可用或指向 stub（无语义），BM25 关键词收窄候选后路由 LLM 兜底成功点名 Skill 或选定 category
- **THEN** 该轮决策路径 SHALL 记为 `llm`（裁决方法归类，不因检索输入来自关键词路而记 degraded），并照常纳入"推荐 vs 实际"误杀比对口径（以检索路标签 vector/keyword 区分观测）；仅当 LLM 兜底也无法收窄（澄清/fallback）或 BM25 无结果（degraded 全量）时才记对应路径

#### Scenario: query embedding 单次请求失败降级不杀轮次

- **WHEN** 会话进行中单次 query embedding 请求抛错（索引就绪但端点瞬时故障）
- **THEN** 系统 SHALL 与启动期 embedding 故障同构处理：优先经关键词路（BM25）收窄候选进入路由 LLM 兜底，关键词路无结果才降级为全量工具（details 保留 query_embed_failed 原因），轮次正常进行，MUST NOT 因单次 embed 失败直接全量平铺

#### Scenario: 无语义 embedding 模式跳过向量路

- **WHEN** embedding 指向本地 stub（哈希向量无语义）或索引未就绪
- **THEN** 系统 MUST NOT 将无语义向量相似度用于高置信收窄，规则路由之后 SHALL 直接以关键词路（BM25）收窄并进入路由 LLM 兜底，避免哈希向量造成伪高置信误杀

### Requirement: 低置信用户澄清

当规则未命中、向量检索与路由 LLM 均无法确定意图时，系统 SHALL 生成一条澄清回复向用户询问更多细节（列出可能的能力方向请用户选择/补充描述），该轮 SHALL 以普通 assistant 文本轮次结束（turn_start..turn_delta..turn_end），MUST NOT 构建 ReAct 图、MUST NOT 调用任何业务工具。澄清决策 SHALL 记录本次列出的候选方向（categories）。系统 SHALL 支持澄清闭环：下一轮路由若识别到上一轮为澄清，SHALL 将用户的回答结合上轮候选方向进行裁决（在候选方向内优先判定），避免把回答当作无上下文的全新消息而重复澄清或误判。

系统 SHALL 在产出澄清时同时给出结构化选项 `options: [{label, value}]`（`routing.clarify_options` 可关，默认开）：`label` 为人类可读展示文案；`value` 为确定性可路由值，MUST 带类型前缀消歧——`category:<名>`（路由 LLM 给的候选技能域，收窄到该域全部候选工具）或 `skill:<名>`（路由 LLM 点名的 Skill 与向量/关键词融合 top-N 候选 Skill，收窄到单个工具），`<名>` 均为当前 env 候选集内合法标识；一张卡片可混含两种前缀（两种粒度）。选项来源于本轮候选方向，数量有上限。当本轮请求携带用户对选项的回传选择（`clarify_selection.value`）时，系统 SHALL 解析其前缀决定收窄粒度，在该 value 匹配上一轮澄清选项且经 env 候选交集合法时，**跳过向量检索与路由 LLM 再裁决，直接确定性收窄**到对应 category 域或 Skill 工具集；value 缺失、前缀非法、不匹配或集外非法时 SHALL 忽略回传标记并回落文本闭环裁决，MUST NOT 暴露候选集外工具。用户点选时必填的 `message` 字段携带所选项 label（人类可读，照常落库并进入模型上下文），带前缀 value 仅经 `clarify_selection` 回传、MUST NOT 进入 message；未携带回传选择的普通手打消息 SHALL 走文本闭环，选项不强制。结构化选项的 SSE 下发、断线/历史回放与上行字段约定见 `chat-sse-protocol` 与 `web-auth-session` capability。

#### Scenario: 无法确定意图时反问

- **WHEN** 路由 LLM 输出低置信/无明确类别
- **THEN** 本轮返回澄清文本（请用户补充或选择方向），不执行工具调用，澄清决策记录候选方向

#### Scenario: 澄清后用户回答在候选方向内闭环

- **WHEN** 上一轮为澄清并列出方向 {订单, 退款}，本轮用户回答指向其中"退款"
- **THEN** 系统在 {订单, 退款} 上下文内裁决，直接收窄到退款相关工具，MUST NOT 再次返回澄清

#### Scenario: 澄清后用户回答仍无法对应任何方向

- **WHEN** 上一轮为澄清，本轮用户回答仍无法对应任何候选方向
- **THEN** 系统可再次澄清或按低置信兜底处理，但 SHALL 携带历史候选方向，不表现为无记忆的全新反问

#### Scenario: 澄清决策产出结构化选项

- **WHEN** 路由低置信进入澄清且 `routing.clarify_options` 开启
- **THEN** 澄清决策除文本外 SHALL 产出 `options:[{label,value}]`，每个 value 均带 `category:`/`skill:` 前缀且 `<名>` 为当前 env 候选集内合法标识（category 域选项→`category:`，点名/融合 Skill 选项→`skill:`），选项来源于本轮候选方向且数量不超过上限；关闭配置时不产出 options、回落纯文本澄清

#### Scenario: 澄清状态从历史末条消息构造

- **WHEN** chat 层在路由前加载最近历史，末条 assistant 消息带 `meta.kind=="clarify"`（含 categories/options/turn_id）
- **THEN** 系统 SHALL 据此构造 `prev_clarify`（含候选方向、选项与该澄清行的 turn_id）传入路由，使下一轮在候选方向内闭环；末条 assistant 消息 meta 缺失/为 NULL（存量普通消息）时 `prev_clarify` 为空，行为同无上下文路由，MUST NOT 因读取无 meta 的历史行报错

#### Scenario: 选项回传在候选集内确定性收窄且跳过再裁决

- **WHEN** 上一轮澄清选项为 {订单→`category:order`, 退款→`category:refund`}，本轮请求 `message`="退款"（label）并携带 `clarify_selection.value="category:refund"`
- **THEN** 系统不调用向量检索与路由 LLM，按 `category:` 前缀直接将工具集收窄到退款域候选 Skill（经 env 交集；`skill:` 前缀则收窄到单个 Skill），MUST NOT 再次澄清，决策路径标记为选项确定性收窄；label 作为用户消息落库/进模型历史，带前缀 value 不进 message

#### Scenario: 回传非法或集外 value 回落软闭环

- **WHEN** 本轮请求携带的回传 value 为空、前缀非法/不识别、不在上一轮 options 内，或指向 env 候选集外
- **THEN** 系统忽略该回传标记，按文本闭环在候选方向内裁决（`message` 里的 label 仍作为普通用户消息参与），MUST NOT 因伪造 value 暴露候选集外工具或报错中断

#### Scenario: 手打回答走文本闭环不强制选项

- **WHEN** 上一轮为澄清（含 options），本轮用户未点选而是自由打字（如"那个退款的吧"）
- **THEN** 系统按文本闭环在候选方向内裁决收窄，不要求必须携带回传字段

### Requirement: 路由确定性

系统 SHALL 保证路由决策的工程可复现性：规则匹配为纯确定性；向量检索的 embedding 模型 id 与索引版本固定并记录；执行 LLM 与路由兜底 LLM 调用 SHALL 固定 `temperature=0`；相同用户消息、相同历史与相同 Skill 注册表版本下，规则与向量路径 SHALL 产出相同工具集。系统不追求 assistant 回复文本的逐字一致。

#### Scenario: 相同输入路由结果一致

- **WHEN** 同一用户消息在注册表版本不变时连续发起两轮（历史一致）
- **THEN** 规则/向量路径选出的工具集相同，路由记录中的 embedding 模型与索引版本一致

### Requirement: 路由决策可观测

系统 SHALL 为每次路由决策记录可观测信息：路由路径（rule/vector/llm/option/clarify/chitchat/fallback/degraded，其中 `option` 为选项确定性收窄）、规则命中条目（若有）、向量检索 top-k 工具名与相似度分数、关键词路（BM25）top-k 与分数、RRF 融合结果、top1/top2 分差、路由 LLM 输出的 Skill/category 与置信度（若有）、最终暴露给模型的推荐工具名集合、embedding 模型 id 与索引版本、query 是否经过上下文改写。这些信息 SHALL 进入 OTel span（`intent_route`）与结构化审计日志。系统 SHALL 导出低基数路由指标（路由路径分布、兜底触发率、澄清率、路由降级率、query 改写率），并 SHALL 导出检索 top1 分数与分差的分布（histogram）以支撑阈值校准。工具名/类别等中低基数字段可作为 metric 标签，userId/sessionId/turnId MUST NOT 进入 metric（仅进 span/日志）。

系统 SHALL 实现"推荐 vs 实际"误杀比对：执行层在模型于本轮实际调用工具后，SHALL 将被调用工具名与路由推荐工具集比对；若收窄路径（rule/vector/llm/option，`option` 为选项确定性收窄）下模型调用了推荐集之外的工具，SHALL 记为一次路由误杀并计入误杀指标与 span/审计；全量兜底路径（degraded/fallback，推荐集即全量）不产生误杀。

#### Scenario: 路由决策可回放

- **WHEN** 一轮对话完成路由
- **THEN** trace 中存在 `intent_route` span，记录路径、top-k 分数（向量路与关键词/BM25 路及 RRF 融合结果）、分差、推荐工具集、是否改写 query 与模型/索引版本；审计日志含同决策记录；list/dict 型检索结果 SHALL 被序列化后写入（MUST NOT 因标量过滤被静默丢弃）

#### Scenario: 模型调用推荐集外工具记为误杀

- **WHEN** 路由经 vector/llm/rule 路径收窄推荐工具集为 {A, B}，模型本轮实际调用了工具 C（不在推荐集）
- **THEN** 系统记录一次路由误杀（误杀指标 +1），span/审计中标注被调用工具 C 与推荐集，供离线度量误杀率

#### Scenario: 模型调用推荐集内工具不误杀

- **WHEN** 路由收窄推荐集为 {A, B}，模型本轮实际调用 A
- **THEN** 不记录误杀，误杀指标不增加

#### Scenario: 检索分数分布可导出

- **WHEN** 发生向量检索路由
- **THEN** 系统将 top1 分数与 top1-top2 分差记录到分布指标（histogram），可据此观测分数分布并校准阈值

### Requirement: 路由跨轮上下文与按需 query 改写

系统 SHALL 支持路由利用会话历史进行跨轮意图理解：路由输入 SHALL 可接收最近 N 轮对话（N 由 `routing.context_turns` 配置，默认 3，可设 0 关闭）。构造向量检索 query 时，系统 SHALL 默认将最近 N 轮对话文本拼接到当前消息之前（不产生额外 LLM 调用）。当当前消息含指代表述（如"它/那个/再来一个"等）或向量检索为低置信时，系统 SHALL 可按需调用一次路由 LLM（temperature=0）将当前消息结合历史改写为语义独立、可独立检索的查询，再用改写结果走向量检索；query 改写 SHALL 可通过配置关闭，改写失败时 SHALL 静默降级为历史拼接，MUST NOT 因改写失败导致整轮失败。检索控制流 SHALL 确定：规则匹配作用于当前轮原文；关键词路（BM25，若启用）始终对当前消息原文检索一次、不随改写重跑；向量路先以拼接 query 检索并与 BM25 融合判定首次高置信门，未过门且触发改写时以改写 query 重检（与同一 BM25 结果融合），重检达高置信时系统 SHALL 直接以重检结果收窄、跳过路由 LLM 兜底，否则带融合候选进入路由 LLM。环境硬过滤与规则路由 SHALL 继续作用于当前轮原文（不受上下文改写影响），保持确定性逃生门语义；上下文仅用于向量检索 query 构造与兜底 LLM 判定。

#### Scenario: 指代消息经改写后命中正确 Skill

- **WHEN** 上一轮用户问"订单 123 的状态"，本轮消息为"那它的退款呢"，触发按需 query 改写
- **THEN** 系统将其改写为语义独立查询（如"订单 123 的退款相关"）后检索，退款相关 Skill 进入推荐集

#### Scenario: 默认拼接不产生额外 LLM 调用

- **WHEN** 当前消息不触发改写条件（无指代且向量高置信）
- **THEN** 系统仅以历史拼接构造检索 query，本轮路由不发生 query 改写 LLM 调用

#### Scenario: query 改写失败降级为拼接

- **WHEN** 触发了 query 改写但改写 LLM 调用异常或返回不可用
- **THEN** 系统记录告警并回退到历史拼接 query 继续检索，对话轮次正常进行

#### Scenario: 关闭上下文后行为同单轮

- **WHEN** 配置 `routing.context_turns=0` 或关闭 query 改写
- **THEN** 路由仅以当前轮原文检索（不拼接历史、不改写 query），检索行为与无上下文路由一致；澄清闭环（prev_clarify 构造、文本子集裁定与 `clarify_selection` 选项收窄）SHALL 不受 `context_turns` 影响——`context_turns=0` 仅关闭跨轮拼接，MUST NOT 连带关闭澄清闭环

#### Scenario: 拼接低置信经改写重检后高置信可跳过路由 LLM

- **WHEN** 拼接历史的向量检索未达高置信门（或命中指代词），系统触发 query 改写，改写后的独立 query 重检达高置信
- **THEN** 系统以改写重检结果（与同一 BM25 融合）直接收窄、不再调用路由 LLM 兜底；BM25 关键词路全程只对当前消息原文检索一次、不因改写重跑；首次拼接检索与重检共至多两次向量 embed

### Requirement: 关键词检索与混合路由

系统 SHALL 在向量检索之外提供一条纯本地、确定性、无外部依赖的关键词检索路（BM25），对 Skill 名称/category/描述/每条示例话语建立词频统计索引（英文与数字整体成词、中文按字 bi-gram，附停用字表），并以与向量多向量相同的"每示例一段、段内取最高分"结构聚合到 Skill。系统 SHALL 将关键词路与向量路结果经 Reciprocal Rank Fusion（RRF，按排名融合、`routing.rrf_k` 可配，默认 60）融合为统一候选排序：高置信放行的阈值判定 SHALL 仍以向量余弦分数为准，放行工具集 SHALL 保底并入关键词路排名前列（默认 rank ≤ 3）的 Skill，以防精确符号（订单号/型号/缩写）被向量路漏召回；向量检索低置信进入路由 LLM 兜底时，关键词路命中结果 SHALL 作为提示注入兜底判定。关键词路 SHALL 可通过 `routing.hybrid=false` 整体关闭并回落纯向量行为。embedding 不可用或无语义（stub）时，关键词路 SHALL 仍能独立收窄候选（进入 LLM 兜底而非直接全量平铺）。关键词检索 MUST NOT 依赖外部服务或新增重依赖。

#### Scenario: 含订单号/型号的消息经关键词路命中

- **WHEN** 用户消息包含精确符号（如订单号、型号"SKU-8800"、缩写"OA"）而语义与某 Skill 示例描述用词差异较大
- **THEN** 该 Skill 经关键词路获得高排名并保底进入推荐工具集，不被向量路漏召回

#### Scenario: 双路 RRF 融合共同支持项居前

- **WHEN** 某 Skill 同时出现在向量路与关键词路 top-k
- **THEN** 其 RRF 融合排名高于仅单路命中的 Skill；高置信放行判定仍依据向量余弦阈值

#### Scenario: embedding 不可用时关键词收窄不全量

- **WHEN** embedding 端点故障（向量索引未就绪），关键词路可用
- **THEN** 系统以关键词路对候选收窄后进入路由 LLM 兜底裁决，MUST NOT 直接全量平铺；关键词路也无结果时才全量 fallback

#### Scenario: 关闭混合回落纯向量

- **WHEN** 配置 `routing.hybrid=false`
- **THEN** 系统不构建/不使用关键词索引，路由行为与纯向量现状一致（含既有降级路径）
