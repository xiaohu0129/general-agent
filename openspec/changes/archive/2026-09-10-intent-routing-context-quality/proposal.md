## Why

意图路由（已归档 change `skill-retrieval-routing`）上线复盘发现三类缺口：

1. **路由无跨轮上下文**：`SkillRouter.route(message, candidates)` 只吃当前轮原文（`skill_router/router.py:63`），不看会话历史。指代消解失效（"查订单 123 状态" → "那它的退款呢"，第二句单独 embed 离任何 Skill 都远）；澄清轮把反问持久化后即"失忆"，下一轮用户回答被当全新消息，可能重复澄清或误判。
2. **误杀不可观测 + design 承诺漏落地**：归档 design 的 Risks/D4 承诺了"推荐工具集 vs 模型实际调用"比对（误杀率）、无 examples 的 Skill 标注 `has_examples=false`、检索分数分布——但降维成 spec 时这些横切承诺丢失了 Scenario，导致 tasks 未派、测试未写、代码未实现。后果是路由把正确工具收窄掉（误杀）时**静默无 metric**，系统无法被验证和调优。
3. **召回质量与缺参追问**：每个 Skill 把所有 examples 拼成单向量，多用法 Skill 语义被"平均"；LLM 兜底只选粗 category 并返回整域工具、返回的 `confidence` 空转不分流；模型调用工具缺必填参数时框架无引导（可能幻觉参数或徒手作答），缺少"缺哪个参数就追问哪个"的半槽位填充行为。
4. **澄清是"软闭环"、无结构化交互通道**：低置信澄清只产出纯文本（"我可以处理订单、退款……请补充"），用户只能自由打字（"那个退款的吧"），把指代表述又带回来——下一轮仍需重新裁决，D3 文本闭环无法 100% 确定；候选方向（categories）不落库、不随事件下发，刷新/断线后丢失；SSE 协议与消息模型只有文本 delta，没有可点选项这一确定性闭环通道（钉钉/Slack 交互卡片、Rasa buttons 的标准模式缺失）。

## What Changes

- **路由带跨轮上下文**：检索 query 默认拼接最近 N 轮对话文本（0 额外 LLM 调用）；当检测到指代表述或向量检索低置信时，**按需**触发一次 LLM query 改写（结合历史把当前消息改写成语义独立的查询，再走向量检索）；改写失败降级为拼接。
- **澄清闭环**：路由感知"上一轮为澄清"及其列出的候选方向，下一轮用户回答直接在该候选集合内判定，不再全量重猜、不重复澄清。
- **结构化澄清选项与硬回传闭环**：路由产出澄清时除文本外 SHALL 产出 `options: [{label, value}]`（value 为候选集内合法的 category 名或 Skill 名，label 为展示文案，来源于本轮候选方向）；选项随 SSE 事件下发并进入断线续传/历史回放。用户点击选项时前端以约定字段回传 value，后端识别到回传标记时 SHALL **跳过向量/LLM 再裁决，直接确定性收窄**到该 category/Skill（value 经 env 候选交集校验，非法/集外则回落软闭环）；用户仍可手打，手打走 D3 文本闭环。澄清消息（含 options 与已选 value）SHALL 持久化于 `meta` 列并经历史消息 API 回放透出。**本 change 只定稿协议契约与后端产出/回传识别/持久化；前端选项卡片渲染归独立前端 change（`structured-clarification-options`）消费本协议，旧前端忽略 options 即降级为纯文本澄清。**
- **缺参定向追问（半槽位填充）**：工具执行前用 Skill 的 `args_schema` 校验必填参数；缺失时**不执行业务逻辑**，返回结构化"缺少哪些参数"的引导回喂模型，ReAct 下一轮模型据此向用户定向追问，用户答复经历史累积后重试。**不引入独立 DST/表单引擎**。
- **召回质量**：向量索引改为**每 example 一条向量 + max-sim 召回**（description 单独一条），消除单向量语义平均；LLM 兜底从"只选 category 返回整域"增强为"在检索候选内直接点名 Skill"，并**启用 confidence 三级分流**（高置信点名 Skill / 中置信选 category 域 / 低置信澄清）。
- **关键词混合检索（BM25 + RRF）**：新增纯本地、零依赖、确定性的 BM25 关键词路（英文/数字整体成词保订单号/型号/缩写、中文 bi-gram），与向量路经 RRF 按排名融合——精确符号不再被 embedding 语义化漏召回；向量高置信判定阈值不变，放行集保底并入 BM25 前列结果；向量低置信时 BM25 结果注入兜底 LLM prompt。顺带修复现存缺陷：stub 哈希向量无语义但仍参与高置信判定（app.py 已标 `mode=rule-only` 而 router 不感知），改为无语义模式跳过向量路。
- **降级路径升级**：embedding 故障/stub 时不再直接全量平铺——BM25 纯本地不受影响，可独立收窄候选后进入 LLM 兜底（degraded 不等于放弃路由）。
- **可观测补齐**：把路由决策（推荐工具集）传入执行层，在工具实际调用后回连比对，产出**误杀率** metric 与 span/审计；新增 top1 分数/分差 **histogram**、query 改写率、缺参追问率 metric；无 examples 的 Skill 在索引/启动日志标注 `has_examples=false` 并可筛出。
- **阈值校准依据**：分数分布 histogram 支撑数据驱动调整 `score_threshold`/`margin`/`top_k`（本期不改默认值，只给观测手段）。

**明确不做**（详见 design.md Non-Goals / Open Questions）：不做 category 层级化（依赖业务命名规范，框架无法单方面定型）；不做 supervisor 多子代理/handoff；不做图内 `search_skills` 元工具；不引入向量数据库；不做独立任务型对话状态跟踪（DST）/表单引擎；**不做前端选项卡片渲染**（`front/` 消息模型/卡片组件/点击回发/历史还原归独立前端 change `structured-clarification-options`，本 change 只定稿 SSE options 协议与后端）；不做选项之外的富交互（图片/下拉级联）与通用多字段槽位卡片（缺参追问仍走 D6 模型对话式半槽位）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `skill-routing`：路由输入增加跨轮上下文（历史拼接 + 按需 LLM query 改写）；澄清状态闭环；**澄清决策产出结构化 `options`（来源于候选方向），识别选项回传时在候选集内确定性收窄（跳过向量/LLM 再裁决），手打回答走文本闭环**；向量索引改每-example 多向量 + max-sim；**新增 BM25 关键词路 + RRF 混合检索**（精确符号召回、degraded/stub 路径可独立收窄）；LLM 兜底支持直接点名 Skill 与 confidence 三级分流；无 examples Skill 标注；产出"推荐工具集"供执行层比对。
- `chat-sse-protocol`：澄清轮 SSE 事件携带结构化 `options`（label/value）；选项随事件流与断线续传（eventSeq ring-buffer）回放；定义用户选项回传的上行消息约定（协议契约在本 change 定稿，前端只消费）。
- `web-auth-session`（后端）：澄清消息（含 options 与已选 value）持久化于 `meta` 列，会话历史消息 API（`/sessions/{id}/messages`）回放透出 options 结构；兼容存量 NULL 行。前端消息模型/卡片渲染不在本 change。
- `observability`：新增路由质量指标——误杀率（模型实际调用工具不在推荐集）、检索 top1 分数/分差分布 histogram、query 改写触发率、缺参追问触发率；澄清率指标增加 `result=option|text|repeat` 维度（选项硬闭环 / 手打后成功 / 再次澄清）。
- `agent-loop`：工具调用前参数校验守卫——缺必填参数时不执行、回喂结构化缺参引导，驱动模型定向追问（半槽位填充），不杀轮次。

## Impact

- **代码**：
  - `skill_router/router.py`：`route()` 签名增加历史/澄清状态入参；confidence 三级分流；LLM 兜底可点名 Skill；澄清闭环判定。
  - `skill_router/index.py`：每-example 多向量构建/缓存/检索（max-sim）；`has_examples` 标注。
  - 新增 `skill_router/context.py`（或并入 router）：历史拼接 + 按需 LLM query 改写。
  - 新增 `skill_router/keyword.py`：BM25 关键词索引（零依赖分词、每-example 分段 max）；`router.py` 增加双路检索与 RRF 融合、`semantic` 入参（stub/故障跳过向量路）、放行集保底并入、兜底 prompt 注入关键词命中。
  - `embedding.py`：`embed_texts` 按批大小分批请求（`embedding.batch_size` 默认 64），防多向量千级文本单请求超限。
  - `app.py`：向 SkillRouter 传入 `semantic`（按 `is_stub`/index ready）。
  - `skills/base.py`：`to_tool()` 的 `arun` 包参数校验守卫（pydantic 必填校验 → 结构化缺参引导，不抛裸错误）。
  - `message_store.py` + `mysql_client.py`：`agent_message` 增 `meta JSON NULL` 列（惰性 ALTER 迁移）；澄清轮落 `meta={"kind":"clarify","categories":...}`，`load_*` 透出 meta。
  - `runner.py`：澄清分支写 meta（含 options）；接收路由推荐集并在 `on_tool_end` 回连实际调用，上报误杀；缺参引导走正常 ToolMessage 回流。
  - `events.py`：澄清事件携带 `options`（新增 `clarify` 事件类型或在 turn 事件带 options 字段，design D11 定）；`with_seq`/ring-buffer 回放兼容，options 随 eventSeq 重放。
  - `api/chat.py`：**路由前由 chat 层 load 最近历史**（路由在 chat 同步段、历史原仅在 runner 加载，故路由历史在 chat 层取），截取 N 轮传 `route(history=...)`、据末条 assistant 的澄清 meta 构造 `prev_clarify`；ChatRequest 增可选选项回传字段（携带选中 value），识别回传标记时走确定性收窄；list/dict 型 details（top_k/BM25/categories/options）序列化进 span/审计（修现存标量过滤导致的可回放漏项）。
  - `api/sessions.py`：历史消息结构透出澄清 options（`load_web_messages` 已透出 meta，此处经消息 DTO 下发 options/已选 value）。
  - `observability.py`：新增误杀/分数分布（含关键词路）/改写率/追问率 metric；澄清率增 `result=option|text|repeat` 维度。
  - `config.py`/`config.yaml`：`routing` 新增 `context_turns`、`query_rewrite`（开关/触发阈值）、`multi_vector`（开关）、`hybrid`（开关）、`rrf_k`、`keyword_top_k` 等，`embedding` 新增 `batch_size`，均有默认值与降级。
- **依赖**：无新增外部依赖（query 改写复用既有 OpenAI 兼容 chat LLM，temperature=0）。
- **测试**：全部使用 stub chat / 确定性 fake embedding / 内存 FakeStore，不依赖外部服务；新增 e2e 覆盖指代消解、澄清闭环（含选项回传确定性收窄、手打回落）、options 事件下发与断线/历史回放、缺参追问、多向量召回、误杀上报。
- **兼容性**：`routing.enabled=false` 行为不变；各新特性独立开关与降级（改写失败→拼接、多向量构建失败→单向量、校验守卫可关）；SSE options 与回传字段均为**可选新增**，旧前端忽略 options 即降级为纯文本澄清、旧消息行 meta 为 NULL 视为非澄清；前端选项卡片渲染不在本 change（归 `structured-clarification-options`，依赖本 change 定稿的协议）。
