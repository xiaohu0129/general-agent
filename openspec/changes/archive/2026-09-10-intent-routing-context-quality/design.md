## Context

见 proposal.md - Why。现状（`skill-retrieval-routing` 已归档上线）：

- `SkillRouter.route(message, candidates)`（`skill_router/router.py:63`）只接收当前轮原文；规则 → 向量（单向量余弦 top-k）→ 路由 LLM 选 category → 澄清。
- 向量索引 `skill_router/index.py`：每 Skill 把 category+name+description+examples **拼成一段** embed 成**单向量**；`metadata_hash` 版本键 + 本地缓存。
- 兜底 LLM（`router.py:_classify`）只输出 category，返回该域**全部** Skill；返回的 `confidence` 存入 details 但分流逻辑未使用。
- 澄清（`runner.py:173-181`）以 direct_reply 文本轮次输出并持久化，下一轮路由不感知。
- 工具执行：`skills/base.py:to_tool` 的 `arun` 直接 `skill.run(ctx, **kwargs)`；参数由 LangChain StructuredTool 绑定 `args_schema`，缺参时为框架默认校验错误，无"追问用户"引导。
- 可观测：`observability.record_intent_route(path, category)` 仅路径计数；runner 的 `on_tool_end` 能拿到实际调用工具名，但**未与路由推荐集回连**。
- 历史在执行侧已可用（`runner.py` load_history），但**未传入路由层**。

约束：测试不依赖外部服务（stub chat / 确定性 fake embedding / 内存 FakeStore）；不引入新重依赖；确定性诉求（路由 LLM/改写 LLM 均 temperature=0）；各新特性需可独立开关与降级。

## Goals / Non-Goals

**Goals:**

- 路由层获得跨轮上下文：历史拼接为主 + 按需 LLM query 改写，修复指代消解；澄清形成闭环。
- 召回质量：多向量 + max-sim；兜底 LLM 可点名 Skill 且 confidence 三级分流生效。
- 半槽位填充：工具调用前参数校验守卫，缺参定向追问，不引入 DST。
- 可观测补齐：误杀率（推荐 vs 实际）、分数分布 histogram、改写率、缺参追问率、`has_examples` 标注——**且每条都有可测 Scenario**（吸取上一 change 降维丢失教训）。

**Non-Goals:**

- 不做 category 层级化/命名规范（依赖业务方约定，框架无法单方面定型；Open Question 保留）。
- 不做 supervisor 多子代理/handoff、不做图内 `search_skills` 元工具（仍列为后续，依据本期误杀率/兜底率数据决定）。
- 不引入向量数据库；不做长期记忆/语料 RAG。
- 不做独立 DST/表单引擎/槽位状态机（半槽位填充刻意复用 ReAct + 历史）。
- 不调整 `score_threshold`/`margin`/`top_k` 默认值（本期只给 histogram 观测手段，校准留待数据）。
- 不做前端选项卡片渲染/点击交互（`front/` 消息模型、卡片组件、点击回发、刷新还原归独立前端 change `structured-clarification-options`）；本 change 只定稿 SSE options 协议、后端产出/回传识别/持久化回放。不做选项之外的富交互（图片/下拉级联）与通用多字段槽位卡片。

## Decisions

### D1：路由输入增历史，query 构造"拼接为主 + 按需改写"

`route()` 签名扩展为 `route(message, candidates, *, history: list[dict] | None = None, prev_clarify: dict | None = None)`（history 为最近 N 轮 {role, content}；N=`routing.context_turns`，默认 3，0=关闭）。

**接线落点修正（原 D1 误写为 runner 传参）**：路由在 `api/chat.py` 同步段（build_agent 前）执行，而会话历史在后台 producer（`runner._produce`）里才 load——runner 无法给路由传参。修正为：**chat 层在路由前自行 load 一小段历史**（`message_store.load_messages(limit=context_turns*2+1)`，索引覆盖、开销可忽略；runner 喂图的全量历史加载保持不变，两次 load 可接受），截取最近 N 轮传入 `route(history=...)`。`prev_clarify` 同样在 chat 层构造（见 D3 + meta 持久化）。

- **规则层仍作用于当前轮原文**（`message`），保证确定性逃生门语义不被上下文污染。
- 向量检索 query：默认 `q = 拼接(最近N轮) + message`，直接 embed，**0 额外 LLM 调用**。
- **按需改写**：满足触发条件（D2）时，调用一次路由 LLM（temperature=0）把 `历史 + message` 改写为语义独立查询 `q'`，用 `q'` 检索；改写结果与"是否改写"记入 details。
- 兜底 LLM 的判定 prompt 同样注入历史摘要与 prev_clarify。

备选：① 始终改写（每轮多一次 LLM，延迟/成本/采样不确定性，弃为主力）；② 仅拼接不改写（0 成本但指代消解依赖 embedding 自身能力，作为降级底座保留）。→ 采用混合（用户已确认）。验收：spec skill-routing《路由跨轮上下文与按需 query 改写》Requirement 下五 Scenario（《指代消息经改写后命中正确 Skill》《默认拼接不产生额外 LLM 调用》《query 改写失败降级为拼接》《关闭上下文后行为同单轮》《拼接低置信经改写重检后高置信可跳过路由 LLM》）。

### D2：query 改写触发条件、双检索控制流与降级

改写是"按需"而非始终触发，且触发时机分两类：(a) 当前消息命中指代表述（内置指代词/省略模式表，如"它/那个/这个/再来/继续/上面"等，可配置补充）——**检索前**即可判定；(b) 拼接 query 向量检索**低置信**（未达 threshold/margin）——**先检索一次才知道**。为避免子代理实现分叉，控制流钉死如下（BM25 对当前消息跑一次；向量路最多两次 embed）：

1. **规则**匹配作用于**当前消息原文**（确定性逃生门，不受上下文污染），命中即 rule。
2. **BM25 关键词检索**（hybrid 开）作用于**当前消息** `message`（精确符号订单号/型号在原文；拼接历史会稀释、改写可能丢失符号），全程只跑一次。
3. **拼接向量检索**：`q = 拼接(最近N轮) + message`，embed q → 向量 top-k `vec`；与 BM25 经 RRF 融合得 `fused`。
4. **首次高置信门**：若 vec top1 ≥ threshold 且 gap ≥ margin，且**未命中指代词** → floor 截断 + BM25 rank≤3 保底并入 → **vector 路径（0 LLM，结束）**。（命中指代词时即便 vec 看似高置信也走改写，防止"它/那个"被表层相似误收窄。）
5. **按需改写**：`query_rewrite` 开且（命中指代词 **或** 步骤 4 低置信）→ 调一次改写 LLM（复用 `app.state.model`，temperature=0；prompt 要求输出"脱离上下文也能独立理解的检索查询"，JSON `{query, used_context}`）得 `q'`；embed q' → 向量重检 `vec'`，与**同一 BM25**（不重跑关键词）RRF 融合得 `fused'`。
   - q' 高置信 → **直接 vector 路径（跳过路由 LLM）**（改写把指代/低置信纠正为明确语义，无需再兜底）；
   - 否则候选集 = `fused'`，进入步骤 6。
   - 改写 LLM 异常/返回不可解析 → 静默记 `query_rewrite_failed`、沿用 `fused` 继续，MUST NOT 中断。
6. **路由 LLM 兜底**：prompt 注入候选（`fused`/`fused'`）与 BM25 命中，走 D5 三级分流。

- **成本上界**：高置信路径 1 次 embed、0 LLM（与现状一致）；低置信/指代路径最多 2 次 embed + 1 次改写 LLM（该路径本就要付路由 LLM，增量仅 1 次 embed/1 次改写 LLM）。
- **降级**：`context_turns=0` 或 `query_rewrite=false` → 不拼接/不改写，步骤 4 低置信直接带 `fused` 进步骤 6（行为同单轮 + 混合检索）。

验收：《指代消息经改写后命中正确 Skill》《默认拼接不产生额外 LLM 调用》《query 改写失败降级为拼接》《关闭上下文后行为同单轮》；补《拼接低置信经改写重检后高置信可跳过路由 LLM》（双检索链路独立 Scenario）。

### D3：澄清闭环与澄清状态持久化

路由接收 `prev_clarify`（chat 层依据"历史末条 assistant 消息是否带澄清 meta 标记"构造，含当时列出的 categories）。若 prev_clarify 存在：

- 先在 prev_clarify.categories 范围内裁决当前回答（向量/LLM 优先映射到这些方向）；命中即收窄，不再全量重猜。
- 仍无法对应 → 可再澄清，但携带历史候选方向（不表现为无记忆反问）。

**澄清标记持久化（D3 前置缺口）**：现状澄清轮（`runner.py:173-181`）仅把澄清文本存为普通 assistant 消息，候选方向不落库——下一轮无法识别"上轮是澄清"。且现状链路有 6 处断点（经代码核实）：表无 meta 列、`append_message`/INSERT 不支持 meta、`run_turn(direct_reply=str)` 拿不到 options、`load_messages` 的 SELECT 不取 meta/turn_id、MessageStore 无回写 selected 的 UPDATE 方法、`load_web_messages` DTO 不透 options。方案逐项补齐：

- **① meta 列 + 迁移**：`mysql_client.SCHEMA_DDL` 建表语句直接加 `meta JSON NULL`（新库一步到位）；`init_schema` 对存量库查 `information_schema.columns`，缺列则惰性 `ALTER TABLE agent_message ADD COLUMN meta JSON NULL`（不依赖 `ADD COLUMN IF NOT EXISTS` 的版本支持，兼容低版本 MySQL）。
- **② 写 options（透传链）**：`MessageStore.append_message(...)` 增 `meta: dict|None=None` 参数，INSERT 增 meta 列（`json.dumps`）。chat 同步段把澄清的 meta（`{kind:"clarify",categories,options}`）从 `decision.details`/`clarify_options` 经 `_produce` → `run_turn` 透传：`run_turn` 与 `_produce` 在 `direct_reply` 之外并列增 `clarify_meta: dict|None=None` 参数（不把 direct_reply 改成 dict，改动最小），runner 澄清分支落 assistant 行时一并写 `meta=clarify_meta`。
- **③ 读 prev_clarify**：`load_messages` 的 SELECT 列补 `meta, turn_id`（喂图的 `_row_to_message` 忽略多余键，安全）；chat 层在路由前 load 最近历史，读末条 assistant 行：`meta.kind=="clarify"` → 构造 `prev_clarify={"categories":meta.categories,"options":meta.options,"turn_id":该行 turn_id}` 传入 `route`；meta 缺失/NULL → None。
- **④ 回写 selected（落点在 chat 同步段，非 runner）**：新增 `MessageStore.update_clarify_selected(service,env,user,session_id,turn_id,selected)`——按归属键 + `turn_id` 定位**上一轮**澄清行，`UPDATE ... SET meta = JSON_SET(COALESCE(meta,'{}'),'$.selected',%s) WHERE 归属键 AND turn_id=%s AND role='assistant'`（MySQL 8 `JSON_SET` 对 NULL meta 也可写；不覆盖已有 options；归属 WHERE 与现有查询一致防越权）。调用时机：**点选轮** chat 同步段识别 `clarify_selection` 且确定性收窄成功后（此时 ③ 已读到上轮澄清行的 turn_id 且能访问 `app.state.message_store`）；runner 是无状态图执行、不感知"选项闭环"，故不在 runner 回写。
- **⑤ 历史回放透出**：`load_web_messages` 的 SELECT 补 `meta`，消息 DTO 从 meta 解出 `options`/`selected` 透出（meta 为 NULL 的存量行不报错，字段缺省）。
- **⑥ 测试替身**：`tests/conftest.py` 的 `FakeStore` 同步改造——append 的行补存 `turn_id`/`meta`，`load_messages` 返回含之，`load_web_messages` DTO 透 `options`/`selected`，并补 `update_clarify_selected` 的内存实现（读-改-写合并 selected）。单列一条 task，避免各 e2e 各写 mock。
- 澄清轮 meta 形态：`{"kind":"clarify","categories":[...],"options":[{label,value}]}`，options 的 value 带 `category:`/`skill:` 前缀（见 D11）；选项闭环后 `meta.selected` 存带前缀 value。
- 选项闭环那一轮的 **user 行 content 存 label**（人类可读，随历史回放气泡与 ReAct 模型历史），带前缀 value 只走 `clarify_selection` 与 `meta.selected`，MUST NOT 进 user 行 content。
- meta 为通用扩展列，结构化澄清选项（D11）的 options/selected 同落此列，无需再 ALTER；旧前端忽略 options 字段即可，不影响既有渲染。存量行 meta 为 NULL（视为非澄清），兼容。

验收：《澄清后用户回答在候选方向内闭环》《澄清后用户回答仍无法对应任何方向》《澄清状态从历史末条消息构造》《澄清选项遵循会话归属校验》《query embedding 单次请求失败降级不杀轮次》；meta 持久化/回放另设测试（澄清行落库带 kind=clarify 与 options、点选后上轮行 meta.selected 被回写、重启/刷新后下一轮 prev_clarify 据末条 meta 正确构造、历史 DTO 透 options/selected、存量 NULL 行不报错）。

### D4：多向量索引 + max-sim + has_examples 标注

- 每 Skill 产出多条索引文本：description 一条 + 每条 example 一条；分别 embed，缓存为"每 Skill 向量段"（`vectors` 存扁平矩阵，另存 `spans: [{skill, has_examples, count}]` 对齐）。
- 检索：query 向量与某 Skill 段内所有向量求余弦取 **max** 为该 Skill 得分，再全局 top-k。
- `metadata_hash` 纳入 examples 条数（内容哈希已含 examples 文本，结构变化自然失效）；缓存版本键升级，旧缓存（单向量结构）检测不兼容 → 重建。
- 无 examples 的 Skill：段内仅 description 向量，`has_examples=false`，构建日志逐条/汇总输出并进入 routing 状态/日志可筛。
- 构建容错：多向量 embedding 部分失败 → 该 Skill 退回单向量或整体降级（沿用 ready=False 路径），不阻断启动。
- **embedding 分批（现存缺口，多向量后成硬伤）**：`EmbeddingClient.embed_texts` 现状把全部文本塞进**单次** POST；多向量后待 embed 文本数 = Skill 数 + examples 总数（数百 Skill × ~4 ≈ 千级），单请求会撞端点批量/token 上限（413）或超时导致整个索引构建失败。SHALL 分批请求（批大小可配，`embedding.batch_size` 默认 64），批间顺序拼接、整批失败按既有容错处理（该 Skill 退化/整体降级）。query 侧单条不受影响。

验收：《多用法 Skill 不因语义平均漏召回》《无示例 Skill 被标注且可筛出》《多向量构建失败降级为单向量》《元数据未变复用索引缓存》《embedding 批量构建按批大小分批请求》。

### D5：兜底 LLM 三级分流 + confidence 生效

`_classify` 输出扩展为 `{skills: [名], category, confidence, reason, clarify_question}`；分流：

1. `confidence >= high` 且 `skills` 非空且全部在候选集内 → 工具集 = 这些 Skill（点名收窄）；
2. 否则若 category 合法（在候选 categories 内）→ 工具集 = 该域全部候选 Skill；
3. chitchat → 空工具纯对话；unknown/低置信 → 澄清。
- 点名的 Skill 名**与 env 候选集取交集**，集外名丢弃（若全部非法则落到 2/3）。
- confidence 高/中阈值配置化（`routing.llm_conf_high` 等），默认保守。

验收：《兜底 LLM 高置信点名具体 Skill》《兜底 LLM 中置信选定技能域》《兜底 LLM 点名了候选集外的非法 Skill》。

### D6：缺参校验守卫（半槽位填充）

**框架校验时机（经 langchain-core 1.6.1 源码与探针核实）**：`StructuredTool` 在调用用户 `arun` **之前**，于 `BaseTool._to_args_and_kwargs → _parse_input` 内用绑定的 `args_schema.model_validate(tool_input)` 校验；缺必填字段时此处即抛 pydantic `ValidationError`，**用户 `arun` 函数体根本不会执行**（探针 `arun` 体 `executed=False`）。且默认 `handle_validation_error=False`，该 `ValidationError` 被原样 re-raise，冒泡到 `ToolNode(handle_tool_errors=_tool_error_handler)`；`_tool_error_handler` 读 `getattr(exc,"code")`，而 `ValidationError` 无 `code` 属性 → 归为 `INTERNAL`。

> 结论：守卫**不能**写在 `to_tool` 的 `arun` 内（那是死代码，框架已先校验并 re-raise）。正确钩子是 `StructuredTool.from_function(..., handle_validation_error=<回调>)`。

- **落点**：`skills/base.py:to_tool` 构造工具时，若 `routing.arg_guard=true`，传 `handle_validation_error` 回调；回调接收 `ValidationError`，从 `e.errors()` 提取 `type=="missing"`（及非法）字段名，**返回**（而非抛出）结构化 JSON 字符串 `{"errorCode":"MISSING_ARGS","missing":[{"field","hint"}],"message":"参数不足，请勿编造，请先向用户询问以下参数：…"}`。框架据此产出 **`ToolMessage(status="error")`** 回流 ReAct（不杀轮次），业务 `skill.run` 不执行（探针验证）。
- **arg_guard 是"构造期开关"，不控制是否校验**：`args_schema` 校验是 LangChain 内置、每次调用必经、无法关闭；`arg_guard` 只决定"构造工具时传不传 `handle_validation_error`"，即校验失败后的**反馈形态**。`arg_guard=false` 时不传回调（缺省 `False`）→ 框架 re-raise `ValidationError` → `ToolNode` 兜底成 `errorCode=INTERNAL`，与改动前逐字节一致（回落现状）。
- **runner 事件映射必须同步改（探针核实的关键落点）**：`handle_validation_error` 接住异常后**不 re-raise**，故该结果在 `astream_events` 中走 **`on_tool_end`**（不是 `on_tool_error`），且 `out.status=="error"`。现状 `runner.py` 的 `on_tool_end` 分支把 `tool_end` 状态写死 `"success"`、不解析 `errorCode`——需改为：`isinstance(out, ToolMessage) and out.status=="error"`（或解析 content 内 `errorCode`）时发 `tool_end(status="error", error_code=...)`；`error_code=="MISSING_ARGS"` 时记缺参追问 metric（**不记为业务错误**）。业务异常 re-raise 路径仍走 `on_tool_error`（`runner.py:214`），维持不变。
- system_prompt 补充一句"工具报缺参数时，先向用户询问，不要编造参数"。

验收：agent-loop《缺必填参数时不执行并回喂缺参引导》《参数补齐后正常执行》《参数校验不泄露为未处理异常》《关闭校验守卫回落既有行为》。

### D7：误杀比对（推荐 vs 实际）

- `RouteDecision` 已含推荐 `tools`；runner 接收推荐工具名集合（经 chat 装配传入 `_produce`）。
- 在工具实际调用处（`on_tool_start`/`on_tool_end` 拿到工具名）比对：若 path ∈ {rule, vector, llm, **option**} 且被调用工具名不在推荐集 → `record_intent_miss(path, env, retrieval)` + span/审计标注 `missed_tool`；degraded/fallback/chitchat/clarify 不计（`option` 路径虽确定性收窄，仍纳入误杀口径以观测选项是否误导）。
- **降级轮次的 path 归类（钉死，防实现分叉）**：path 语义 = "本轮靠什么**裁决方法**成功收窄"，检索输入条件不改变裁决归类。embedding 故障/stub 下 BM25 收窄后路由 LLM 成功点名/选域 → `path="llm"`（照常计入误杀口径）；LLM 无法定夺 → `clarify`；LLM 异常 → `fallback`（reason=route_llm_failed）；BM25 也无结果 → `degraded`（reason=embedding_unavailable）。**理由**：降级期收窄质量本就偏低、恰是误杀高发期，豁免会让降级期误杀完全不可见；用 `retrieval=vector|keyword` metric 标签分群即可对比"降级期 vs 正常期误杀率"，信息量优于埋进 degraded。
- **误杀 metric 增 `retrieval` 低基数标签**（vector|keyword，无 keyword 参与的轮次记 vector），`details.degraded_keyword`/`details.semantic_off` 照旧进 span/审计；`record_intent_route` 的 path 标签同步按上述归类（BM25+LLM 记 llm 不记 degraded）。
- **运行中单次 query embed 抛错**（现状 `router.py` 直接 DEGRADED 全量）改为与启动期 embedding 故障同构：先尝试 BM25（已建）收窄 → LLM 兜底；BM25 无结果才全量 degraded。`reason=query_embed_failed` 保留进 details。
- 定位为**信号**而非惩罚：模型徒手作答（不调工具）不计；仅"调用了推荐集外工具"计。

验收：skill-routing《模型调用推荐集外工具记为误杀》《模型调用推荐集内工具不误杀》《关键词降级轮次的路径归类》《误杀指标在模型越出推荐集时累加》；observability《误杀指标在模型越出推荐集时累加》。

### D8：可观测指标增补

`observability.py` 新增：`agent.intent.miss.count`（counter，标签 env/path/retrieval，retrieval=vector|keyword 见 D7）、`agent.intent.score`（histogram，记录 top1 分数，带检索路标签 vector/bm25/rrf）、`agent.intent.score_gap`（histogram）、`agent.intent.rewrite.count`（counter，标签 env/result=success|failed）、`agent.tool.missing_args.count`（counter，标签 env/tool）。均低基数，无 userId/sessionId/turnId。

**path 枚举与澄清结果计数（两个不同 counter，勿混）**：路由 path 低基数枚举在现有 `rule/vector/llm/chitchat/clarify/fallback/degraded` 基础上新增 `option`（选项确定性收窄，见 D11）。`record_intent_route(path, category)` 仍按轮次记录"本轮靠什么方法收窄"（`option` 计入 route counter 的 path 标签，且纳入 D7 误杀口径）。澄清闭环结果则由独立的 clarify counter 记 `result=option|text|repeat`：router 在 decision.details 带 `clarify_outcome`（本轮识别到上轮澄清并闭环=`option`(点选)/`text`(手打候选内裁决成功)/`repeat`(仍无法对应再澄清)），**chat 层**据 `clarify_outcome` 调 `record_clarify(result)`。二者落点不同：`record_intent_route(path="option")` 记本轮收窄方式，`record_clarify(result="option")` 记上一轮澄清的结局，同一轮 N+1 可同时触发但属不同指标、语义不重复。

**clarify counter 接线重构（防双计，现存耦合必须拆）**：现状 `record_intent_route` 内耦合 `if path=="clarify": _intent_clarify.add(1)`（observability.py）——本 change 把 clarify counter 改为**仅**经 `record_clarify(result)` 显式记录后，该内嵌分支 MUST 删除，否则 repeat 轮（path=clarify 且 clarify_outcome=repeat）双计。重构后语义：`record_clarify(result)` 只在"存在上一轮澄清"时被 chat 层触发（据 details.clarify_outcome）；**首轮澄清（无上一轮澄清）没有 clarify_outcome，不调 `record_clarify`**——首轮澄清的总量观测由 `record_intent_route(path="clarify")` 的 path 维度承担（path counter 一直记），clarify counter 专属度量"上一轮澄清的闭环结局"，两者职责正交、不重叠。`result` 枚举维持 `option|text|repeat` 不扩（首轮不属于任何"结局"）。

**top-k 可回放序列化（现存横切漏项修复）**：`api/chat.py` 的 span/审计透传有 `isinstance(v,(str,int,float,bool))` 标量过滤，而 `details["top_k"]`（及 D10 的 BM25/RRF 结果、`categories`）是 **list，被静默丢弃**——spec《路由决策可回放》要求的"top-k 工具名与分数"实际从未导出。修复：chat 层在写 span/审计前，将 list/dict 型 details 显式序列化为紧凑字符串（如 `route.top_k="refund:0.81,order:0.77"`、JSON 字符串），确保检索结果（含两路）可在 trace/审计回放。验收：skill-routing《路由决策可回放》补断言"span/审计含 top-k 工具名与分数（含 BM25/RRF 路）"；observability《置信度分布记录分数与分差》《改写与缺参追问可计数》。

### D9：配置与落点

- `config.py` RoutingSettings 增：`context_turns:int=3`、`query_rewrite:bool=true`、`refer_terms:list[str]`（内置默认）、`multi_vector:bool=true`、`llm_conf_high:float=0.7`、`arg_guard:bool=true`、`hybrid:bool=true`、`rrf_k:int=60`、`keyword_top_k:int=10`、`clarify_options:bool=true`、`clarify_option_max:int=4`；`EmbeddingSettings` 增 `batch_size:int=64`（索引构建分批）。
- 模块：新增 `skill_router/context.py`（历史拼接 + 改写 LLM 调用）、新增 `skill_router/keyword.py`（BM25 关键词索引 + 零依赖分词）；改 `router.py`（RRF 融合/`semantic` 入参/保底并入/`history`+`prev_clarify` 入参/澄清 `clarify_options` 产出 + 选项回传确定性收窄）、`index.py`、`embedding.py`（分批 embed）、`skills/base.py`（`to_tool` 按 `arg_guard` 挂 `handle_validation_error` 回调，把缺参 `ValidationError` 转为 `MISSING_ARGS` 引导 ToolMessage；**不改 `agent.py`**——缺参引导不经 `_tool_error_handler`）、`runner.py`（`on_tool_end` 识别 `status=error` 的 ToolMessage 下发 error 态 `tool_end` 并记缺参 metric；`run_turn` 增 `clarify_meta` 参数、澄清分支落 assistant 行时写 meta）、`events.py`（新增 `clarify` 事件携带 options、ring-buffer/`with_seq` 回放）、`api/chat.py`（路由前 load 历史并读末条 assistant 的 meta/turn_id 构造 prev_clarify、ChatRequest 回传字段 `clarify_selection`、确定性收窄成功后调 `update_clarify_selected` 回写上轮行、把 clarify_meta 透传 `_produce`、details 序列化进 span/审计）、`api/sessions.py`（历史消息 DTO 透出 options/selected）、`app.py`（传 `semantic`）、`message_store.py`（`append_message` 增 meta 参数+INSERT 列、`load_messages` SELECT 补 `meta,turn_id`、新增 `update_clarify_selected` 按归属键+turn_id 用 JSON_SET 回写 selected、`load_web_messages` SELECT 补 meta 且 DTO 透 options/selected）+`mysql_client.py`（SCHEMA_DDL 建表加 `meta JSON NULL` 列 + `init_schema` 查 information_schema 对存量库惰性 ALTER）、`observability.py`（澄清 result 维度）、`config.py`。
- 各特性独立降级：改写失败→拼接；多向量失败→单向量/既有 degraded；守卫可关；`clarify_options=false` 回落纯文本澄清；`routing.enabled=false` 整体回落。

### D10：BM25 关键词路 + RRF 混合检索（含 degraded/stub 路径升级）

**动机**：纯稠密向量把订单号、型号、拼音缩写（"OA 审批""SKU-8800""订单 123"）这类精确 token 语义化掉了——embedding 擅长近义、不擅精确符号匹配。新增一条零依赖、纯本地、确定性的 BM25 关键词路与向量路混合（ToolLLM 等业界 Tool RAG 的标配双路）。

- **新增 `skill_router/keyword.py`：`KeywordIndex`**。
  - 分词（零依赖）：`[A-Za-z0-9]+` 整体成 token 并 lower（数字/型号/缩写完整保留，是关键词路核心价值）；中文按字 bi-gram 滑窗（单字成 unigram）；内置小停用字表（的/了/吗/呢/是/我/你/这/那/把/被/不/没 等高频虚词）。
  - 索引文档与 D4 多向量对齐：每 Skill 多段（name+category+description 一段 + 每条 example 一段），段内标准 BM25（k1=1.5、b=0.75）打分，Skill 得分 = 段内 max（与向量路 max-sim 同构）。
  - 纯内存 dict 统计（tf/df/avgdl）；千级短文本构建微秒级，**不写磁盘缓存、不占缓存版本键**；embedding 不可用时照常构建。
- **双路检索 + RRF 融合**：向量路与关键词路各自 top-k，按 Reciprocal Rank Fusion 融合排序：`score(skill) = 1/(rrf_k + rank_向量) + 1/(rrf_k + rank_BM25)`，`rrf_k` 默认 60（配置 `routing.rrf_k`）。RRF 只依赖排名、不需校准两路分数量纲，是工业界混合检索标配。
- **置信判定与放行集**：
  - 高置信放行的**阈值判定仍以向量余弦为准**（top1 ≥ threshold 且 gap ≥ margin，RRF 分数量纲不可比、不引入新阈值）；
  - 放行集 = 融合候选中向量分 ≥ floor 的 Skill，**并保底并入 BM25 rank ≤ 3 的 Skill**（防精确 token 被向量路漏召回而误杀）；
  - 向量低置信走 LLM 兜底时，BM25 top 结果作为提示注入兜底 prompt（辅助 D5 点名 Skill）。
- **degraded / stub 路径升级**：
  - embedding 端点不可用（index 构建失败）：不再全量平铺——BM25 对 candidates 收窄候选后照常进入 LLM 兜底裁决（details 标 `degraded_keyword=true`）；BM25 也无结果才全量 fallback。
  - embedding 指向 stub（哈希向量无语义，`app.py` 已算 `mode="rule-only"` 但 router 不感知，现状下哈希向量仍参与高置信判定——**现存缺陷**）：router 增加 `semantic: bool` 入参（由 app.py 按 `is_stub`/index ready 传入）；`semantic=false` 时跳过向量路，规则之后直接 BM25 收窄 → LLM 兜底。
  - BM25 是**唯一**在 embedding 故障时仍可用的收窄手段，且完全确定性。
- **BM25 不独立直接放行**：它没有校准过的置信阈值，只负责"收窄候选集 + 喂 LLM + 保底并入放行集"；唯一高置信放行通道仍是向量路。BM25 top1 分数进 details/span 与 histogram（路标签区分），为后续是否允许独立放行攒数据。
- 配置：`routing.hybrid:bool=true`、`routing.rrf_k:int=60`、`routing.keyword_top_k:int=10`；`hybrid=false` 完全回落纯向量现状。
- 验收：skill-routing《含订单号/型号的消息经关键词路命中》《双路 RRF 融合共同支持项居前》《embedding 不可用时关键词收窄不全量》《无语义 embedding 模式跳过向量路》《关闭混合回落纯向量》《关键词降级轮次的路径归类》。

## Risks / Trade-offs

### D11：结构化澄清选项与硬回传闭环

**动机**：D3 文本闭环依赖下一轮对用户手打回答再裁决，用户常带指代表述（"那个退款的吧"）或偏离候选措辞，无法 100% 确定意图。提供"选项"这一确定性通道：用户点选即回传确定 value，后端跳过向量/LLM 再裁决直接收窄（钉钉/Slack 交互卡片、Rasa buttons 的标准模式）。本 change 只做**后端产出 + 协议契约 + 回传识别 + 持久化回放**；前端卡片渲染归独立前端 change。

- **选项产出**：路由澄清分支（D5 分流③）在澄清文本外，从本轮候选方向构造 `options: [{label, value}]`，挂到 `RouteDecision.clarify_options` 与 details。
  - `value` 为确定性可路由值，**统一带类型前缀**以消歧 category 与 Skill 同名：`category:<名>`（收窄到该域全部候选工具）或 `skill:<名>`（收窄到单个 Skill）；`<名>` 均为经 env 过滤的候选集内合法标识。`label` 为人类可读展示文案（category 显示名 / Skill 短描述）。
  - 来源与粒度：LLM 兜底给出的候选 **category** → `category:<名>`；LLM 点名 **Skill** 与向量路+BM25 路融合（D10 RRF）top-N 候选（检索返回 Skill 对象）→ `skill:<名>`。一张澄清卡片可混含两种前缀（两种粒度），用户点哪个即按其前缀收窄到对应粒度。数量上限 `routing.clarify_option_max`（默认 4），避免长列表。
- **硬回传识别（确定性收窄）**：`ChatRequest` 增可选回传字段（`clarify_selection: {value}`，api/chat.py 解析；字段名在 docs/00 协议章节定稿）。
  - **上行双字段约定（点选时）**：`message` 必填字段携带所选项的 **label（人类可读文案）**——它与手打路径同构，照常落库为 user 行 content、作为 `HumanMessage` 喂 ReAct（模型据此"读到"用户所选，收窄后缺参可走 D6 半槽位追问）；`clarify_selection.value` 携带带前缀的机器可路由值，**对前端不透明、原样回传**，MUST NOT 进 message/content/模型历史。手打时 `message` 为用户原话、不携带 `clarify_selection`。
  - 路由前若识别到回传标记：解析 value 前缀——`category:<名>` → 收窄到该域全部候选工具；`skill:<名>` → 收窄到单个 Skill；`<名>` 命中 `prev_clarify.options` 中某 value **且**经 env 候选交集合法 → 直接确定性收窄，**跳过向量检索与 LLM 兜底**，决策 `path="option"`（新增低基数值；纳入 D7 误杀比对口径，与 rule/vector/llm 同属收窄路径）。该轮 router 在 details 带 `clarify_outcome="option"`，chat 层据此对上一轮澄清记 `record_clarify(result="option")`（见 D8，与本轮 `record_intent_route(path="option")` 是两个不同 counter，不重复计数）。
  - value 缺失 / 前缀非法 / 不匹配 options / 集外非法 → 忽略标记，回落 D3 软闭环（向量/LLM 在候选方向内裁决），MUST NOT 因伪造 value 暴露集外工具。
- **无标记手打**：不携带回传字段的普通消息走 D3 既有文本闭环，选项不强制。
- **SSE 下发形态（采用独立 `clarify` 事件）**：在既有事件表新增 `clarify` 事件类型（data 含 `question` 文本与 `options:[{label,value}]`，及公共 turnId/traceId/eventSeq）。选独立事件而非在 turn_end 塞字段，理由：语义清晰、前端按事件类型渲染卡片、与 turn_* 文本生命周期解耦。澄清轮仍以 `turn_start .. turn_delta(澄清文本) .. clarify(选项) .. turn_end{finishReason:stop}` 收尾，**不构建 ReAct 图、不调用业务工具**（沿用 D3 约束）。clarify 事件经 eventSeq 写入 ring buffer，断线重连 `with_seq` 重放时一并重放（含 options）。
- **历史回放**：options 与用户选中的 value 持久化在澄清消息 `meta`（D3，`meta.options` / `meta.selected`）；`/sessions/{id}/messages` 消息 DTO 透出 options/selected（web-auth-session delta），前端刷新后还原卡片、已选项标记禁用（渲染属前端 change，后端只透出数据）。
- **契约单点**：options shape（`{label,value}`）、`clarify` 事件字段、回传字段名在本 change 定稿（docs/00 同步）；前端 change 只消费不定义，保证后端先上线、旧前端忽略 clarify 事件即降级为纯文本澄清。
- **可观测**：澄清率 metric 增 `result` 维度——选项回传闭环 `option`、手打后软闭环成功 `text`、仍无法对应再次澄清 `repeat`（接线见 D8）。
- **配置/降级**：`routing.clarify_options:bool=true`（关闭时回落 D3 纯文本澄清）、`routing.clarify_option_max:int=4`；`routing.enabled=false` 整体不受影响。

验收：skill-routing《澄清决策产出结构化选项》《选项回传在候选集内确定性收窄且跳过再裁决》《回传非法或集外 value 回落软闭环》《手打回答走文本闭环不强制选项》；chat-sse-protocol《澄清事件携带结构化选项》《断线重连重放澄清选项事件》《旧前端忽略选项降级为纯文本澄清》《点选选项以专用字段回传》；web-auth-session《澄清选项随消息持久化》《历史消息回放透出澄清选项》《存量无 meta 消息回放不报错》；observability《澄清结果按 option/text/repeat 计数》《首轮澄清与路径计数不双计》。

- [query 改写增加延迟/成本，且 LLM 改写有采样波动] → 仅在指代/低置信时按需触发（D2），默认拼接 0 调用；改写 LLM temperature=0；失败静默降级拼接；有改写率 metric 可观测。验收：《默认拼接不产生额外 LLM 调用》《query 改写失败降级为拼接》。
- [多向量索引/缓存体积增大（数百 Skill × 数条 example）] → 纯内存余弦，向量段总量 = examples 总数（数百×~4 ≈ 千级向量，仍微秒级）；缓存结构版本键升级，旧缓存自动重建；不引 numpy。验收：《元数据未变复用索引缓存》《多向量构建失败降级为单向量》。
- [误杀比对误报：模型可能合理地不调工具或调用看似集外工具] → 仅"收窄路径下调用了推荐集外工具"计误杀，徒手作答/chitchat/兜底路径不计；作为离线调优信号，不影响在线行为。验收：《模型调用推荐集内工具不误杀》。
- [缺参守卫若写在 `arun` 内会是死代码：框架 `_parse_input` 先于 `arun` 用 args_schema 校验并在缺参时 re-raise（探针证实 `arun` 体不执行）] → 守卫落点改为 `StructuredTool.from_function(handle_validation_error=回调)`：回调把裸 `ValidationError` 转为"引导追问"的 `MISSING_ARGS` 受控 ToolMessage（status=error，经 `on_tool_end` 回流，业务不执行）；`arg_guard` 为构造期开关（false 时不传回调，回落框架默认 INTERNAL 行为）；runner `on_tool_end` 识别 error ToolMessage 下发 error 态 tool_end 并记缺参 metric。验收：《缺必填参数时不执行并回喂缺参引导》《关闭校验守卫回落既有行为》。
- [历史拼接引入无关上下文拉低向量召回] → 拼接仅作底座，低置信即触发改写纠正；`context_turns` 可调/可关；分数分布 histogram 支撑观察。验收：《关闭上下文后行为同单轮》+ histogram。
- [confidence 阈值依赖路由 LLM 的标定，初值不准] → 阈值配置化、默认保守（宁选 category/澄清不误点名）；上线后按误杀率/兜底率调优，不改架构。验收：《兜底 LLM 点名了候选集外的非法 Skill》（非法名兜底）。
- [上一 change 教训：横切承诺（metric/标注）在降维时丢失] → 本 design 每条 Decision/Risk 缓解均显式标注其验收 Scenario；propose 收尾与归档前执行 design↔spec 可追溯性核对（见流程改进，独立于本 change 代码）。
- [BM25 中文分词产生噪声 bi-gram（如"的订"）] → 停用字表压高频虚词；噪声 token df 高、IDF 自然趋零，对排序影响小；保底并入仅限 BM25 rank ≤ 3 且最终工具集仍经 LLM 兜底/向量置信，不会因噪声放大。验收：《双路 RRF 融合共同支持项居前》。
- [混合检索改变分数分布，现有阈值默认值更未校准] → 高置信判定仍只看向量余弦分（RRF 仅排序/保底），不引入新放行阈值；D8 histogram 同时记录两路分数，阈值校准仍由数据驱动，本期不改默认值。验收：《检索分数分布可导出》。
- [stub/无 embedding 模式下 BM25 收窄仍可能误杀] → 该模式本就仅为本地联调/降级（非生产目标形态）；BM25 收窄后仍经 LLM 兜底裁决，且有 `routing.enabled=false` 全量回退。**误杀口径（D7 已钉死）**：BM25 收窄经 LLM 成功裁决的轮次按 `llm` 路径**照常计入**误杀（降级期恰是误杀高发期，不可豁免不可见），以 `retrieval=keyword` 标签分群对比降级期 vs 正常期误杀率；仅 BM25 也无结果的全量 degraded 轮次不计。验收：《关闭混合回落纯向量》《关键词降级轮次的路径归类》《误杀指标在模型越出推荐集时累加》。
- [保底并入 BM25 高排项可能引入无关工具、稀释 LLM 上下文] → 保底集有 rank ≤ 3 硬上限；多用法/精确符号场景下这类工具本就是高相关；上线后以误杀率 vs 工具集大小平衡，可调。验收：《含订单号/型号的消息经关键词路命中》。
- [选项回传 value 被伪造/集外，暴露候选集外工具或误收窄] → 回传 value MUST 与 prev_clarify.options 匹配并经 env 候选交集校验，不匹配即忽略回落软闭环；确定性收窄路径仍纳入 D7 误杀比对口径。验收：《回传非法或集外 value 回落软闭环》。
- [`clarify` 事件为协议新增，旧前端不识别] → 事件 additive，旧前端忽略未知事件类型、仍渲染 turn_delta 澄清文本，等价纯文本澄清现状；options 字段全部可选。验收：《旧前端忽略选项降级为纯文本澄清》。
- [选项硬闭环依赖前端卡片才能被用户触达，本 change 只交付协议与后端] → 协议 additive 先上线不改变现状体验；前端卡片归 `structured-clarification-options` 独立 change 消费，后端 `clarify_options=false` 可关闭；两者文件零重叠、单向依赖。验收：skill-routing/chat-sse-protocol 选项相关 Scenario 用 stub 事件断言，不依赖前端。

## Migration Plan

1. 配置新增项均有默认值且默认开启各新特性；`routing.enabled=false` 行为完全不变。
2. 索引缓存结构升级：旧 `skill_index.json` 因版本键/结构不兼容自动重建（首次重启多一次 embedding 构建，之后命中缓存）。
3. 存量 Skill 无需改造即受益（多向量对只有 description 的 Skill 退化为单向量 + has_examples 标注）；业务方补 examples 可提升召回。
4. 回滚：逐项开关（`query_rewrite=false`/`multi_vector=false`/`arg_guard=false`/`context_turns=0`）或整体 `routing.enabled=false`；BM25 混合路可 `routing.hybrid=false` 单独关闭回落纯向量。
5. `.skill_index_cache/` 已在 gitignore/dockerignore（沿用）；BM25 索引纯内存、不落缓存文件，不影响缓存版本。
6. `semantic` 模式修复（stub 跳过向量路）改变 stub 联调行为：stub 下路由从"哈希向量误高置信"变为"规则 + BM25 收窄 + LLM 兜底"，更接近真实语义路由行为，属修复而非回归。
7. 澄清选项协议 additive 上线：新增 `clarify` SSE 事件与 `options`/回传字段均为可选，旧前端忽略即纯文本澄清（现状）；options 复用 D3 已建的 `meta` JSON 列（仅多 `options`/`selected` 键，无需再次 ALTER）；存量消息 meta 为 NULL 视为非澄清，回放不报错。前端卡片由独立 change `structured-clarification-options` 后随消费，不阻塞本 change 归档。

## Open Questions

- 指代表述词表的初始集合与是否需要配置化维护（先内置常用中文指代，联调按误改写率补充）。
- `llm_conf_high`/`llm_conf_mid` 具体阈值：依赖路由 LLM confidence 标定，联调前给保守默认，上线按 metric 调。
- category 命名/粒度规范仍由业务方接入时约定（本期不做层级化）。
