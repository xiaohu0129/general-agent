## Context

见 proposal.md - Why。现状链路（`api/chat.py`）：`SkillRegistry.get_tools(ctx)` 按 env 过滤产出**全部** Skill 工具 → `build_agent(model, tools, system_prompt)`（`agent.py`，LangGraph `create_react_agent`，每请求重建）→ `runner.run_turn` 跑 ReAct。约束：

- 框架默认注册表为空，业务方在 `skills/__init__.py` 的 `build_registry()` 显式 register；Skill 元数据为 `name/description/args_schema/allowed_envs`（`skills/base.py`）。
- LLM 经 `llm.py` 的 `OpenAICompatibleModel`（langchain-core BaseChatModel，OpenAI 兼容 `/v1/chat/completions`），本地联调用 `stub_llm.py`（:9094）；目前**未透传 temperature**。
- 可观测：OTel span 树 server → run_turn → load_history/agent_graph → llm_call/tool_call → append_messages；metric 低基数；审计为独立 structlog logger。
- 无 Skill 时 tools 为空退化为纯对话；工具异常不杀轮次（ToolNode handle_tool_errors）。
- 规划 Skill 规模数百；embedding 端点：方舟 `doubao-embedding-vision`，OpenAI 兼容 `/embeddings`，与 chat 同 base_url/key（coding plan 端点）。

## Goals / Non-Goals

**Goals:**

- 在不改动 `build_agent` 机制与 SSE 协议的前提下，于 env 过滤后、建图前插入路由，把模型可见工具从数百收窄到十几。
- 路由链路确定性分级：规则（数学确定）→ 向量检索（固定模型/索引版本，实质确定）→ LLM 兜底（temperature=0）→ 澄清；路由故障降级不杀轮次。
- 路由决策全程留痕（span + 审计日志 + 低基数 metric），支持离线回放与"推荐 vs 实际选择"比对。
- 测试不依赖外部服务：确定性 stub embedding。

**Non-Goals:**

- 不做 supervisor 多子代理/handoff（category 分组为其预留依据）。
- 不做图内 `search_skills` 元工具（二期，数据驱动）。
- 不引入向量数据库/Milvus；不做长期记忆/RAG 语料检索。
- 不新增 SSE 事件；不追求回复文本逐字一致。

## Decisions

### D1：图外预检索（一期），不建子代理、不做图内元工具

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 图外预检索：路由在 `get_tools` 与 `build_agent` 之间完成，产出工具子集 | `agent.py` 零改动；与"每请求重建图 + env 过滤"天然衔接；链路简单 | 模型执行中无法换批，误杀靠阈值/兜底防 | **一期采用** |
| B. supervisor 多子代理 | 工具彻底隔离、handoff 可回退 | 需引入子图/子代理概念，与 SkillRegistry 模型不匹配；0 个 Skill 时边界无从谈起 | 弃（category 预留） |
| C. 图内元工具 `search_skills`（LangGraph tool-retrieval 模式，ToolNode 持全量、按 state 动态暴露） | 模型可主动换批，消除误杀 | 图改动大；每轮仍需全量工具元数据驻留 | 二期，依据一期日志（模型抱怨无工具率/兜底率）决定 |

### D2：向量检索为主力，LLM 分类降为兜底

数百规模 + 确定性诉求下，主力路由 MUST NOT 依赖 LLM 采样：向量检索不调 chat LLM、毫秒级、同输入同输出；结构化路由 LLM 仅在检索低置信时触发（预期占比小）。规则层为 0 成本逃生门。备选"LLM 分类为主力"（Dify 问题分类器模式）在数百类别下枚举过长、每次多一次 LLM 调用且结果随采样波动，弃为主力。

### D3：内存向量索引 + 本地缓存，不引入向量数据库

数百个 Skill 向量（启动时一次性 embed），进程内余弦相似度（向量归一化后点积，**纯 Python 实现**，几百维×几百向量微秒级；不引入 numpy，避免新增重依赖与国内安装风险）；索引随注册表元数据哈希缓存到本地文件（如 `.skill_index_cache/`，进 `.gitignore`），元数据未变重启免重建。Milvus/pgvector 为百万级语料场景，对此规模是过度中间件。备选"每次请求实时 embed 全部 Skill"弃（数百次 embed/轮，不可接受）。

### D4：Skill 向量表示——一期每 Skill 单向量，examples 为语义主体

每个 Skill 聚合一条索引文本：`category + name + description + examples（逐条换行拼接）`，embed 一次得单向量；examples（2~5 条真实用户说法）是语义匹配主体，description 补充。无 examples 的存量 Skill 仅用 description 并在索引/路由记录中标注 `has_examples=false`。备选"每 example 一条向量、检索取 max-sim"召回更细但索引/缓存复杂度翻倍，列为二期可调项。

### D5：置信分流——保守收窄，宁兜底不误杀

- 高置信收窄条件：`top1_score >= score_threshold` 且 `top1_score - top2_score >= margin` → 工具集 = top-k（k 默认 20，按 score 下限截断）。
- 低置信 → 路由 LLM（structured output，temperature=0）：候选类别 enum **从注册表 category 清单动态派生**（不另维护路由表，避免与 Skill 元数据腐化脱节），输出 `{category | "chitchat" | "unknown", confidence, reason}`；选定 category → 工具集为该域全部 Skill（经 env 过滤）；`chitchat` → 纯对话（tools=[]）；`unknown`/低置信 → 澄清。
- 澄清：路由层产出澄清文本（LLM 基于 category 清单生成选项式反问），runner 以普通文本轮次输出（turn_start..turn_delta..turn_end），不建 ReAct 图、不调业务工具；澄清消息持久化为 assistant 消息。
- 阈值/ margin/k 全部配置化；默认值保守（误杀代价 > 工具略多的代价），上线后按路由日志调优。

### D6：Embedding 客户端与 stub

- 新增 `general_agent/embedding.py`：`EmbeddingClient`（httpx，`POST {base_url}/embeddings`，OpenAI 兼容），接口 `async embed_texts(list[str]) -> list[list[float]]`；错误分类复用 `llm.py` 的 LLMError/`_classify_error` 模式；api_key 仅内存。
- `config.yaml` 新增 `embedding:` 块（base_url/api_key/model，base_url 留空指向 stub 基址；model 默认 `doubao-embedding-vision` 由部署配置覆盖）。
- `stub_llm.py` 增 `/v1/embeddings`：确定性哈希向量（token 哈希加权定维向量 + L2 归一化），相同文本同向量、共享 token 文本余弦相近，支撑检索单测。
- **生产防护**：`routing.enabled=true` 但 embedding 指向 stub/未配置真实端点时，启动日志 WARN + `/health` 标注 `routing.embedding=stub`（stub 向量无语义，检索不可用），路由自动降级为"仅规则 + 全量工具"。

### D7：确定性措施

- 执行 LLM 与路由 LLM 调用统一 `temperature=0`：`llm.py` 支持默认/透传 temperature（`OpenAICompatibleModel` 请求体注入，配置 `llm.temperature`，默认 0）。
- 路由记录 embedding 模型 id 与索引版本（元数据哈希）；索引缓存按版本键失效。
- 诚实边界：temperature=0 在多数 provider 上高概率一致但非数学保证；回复文本逐字差异不视为缺陷。

### D8：模块落点

- 新增 `general_agent/skill_router/`：
  - `rules.py`：规则匹配器（编译配置的正则/前缀，顺序匹配，返回 Skill 名集合或 category）。
  - `index.py`：Skill 向量索引（构建、元数据哈希、本地缓存读写、内存余弦 top-k）。
  - `router.py`：`SkillRouter.route(ctx, message, candidate_skills) -> RouteDecision`（path: rule/vector/llm/clarify/chitchat/fallback/degraded；tools: list[Skill]；clarify_text?；决策明细用于 span/日志）。
  - 兜底 LLM 与澄清文本生成复用 `OpenAICompatibleModel`（structured output 用 JSON mode/pydantic 解析，失败按 unknown 处理）。
- `skills/base.py`：Skill 增 `category: str = ""`、`examples: list[str] = []`；`SkillRegistry` 增 `list_allowed(ctx) -> list[Skill]`（返回 Skill 对象而非仅 tool，供路由）；路由后再 `to_tool`。
- `api/chat.py`：`get_tools` 改为 `registry.list_allowed(ctx)` → `router.route(...)` → 澄清分支（直接走文本轮次，不建 agent）或 `build_agent(model, [s.to_tool(ctx) ...])`。
- `app.py`：lifespan 启动时构建/加载索引并注入 `app.state.skill_router`（embedding client + registry）。
- `config.py`/`config.yaml`：`routing:` 块（enabled、top_k、score_threshold、margin、rules 列表）、`embedding:` 块、`llm.temperature`。
- `observability.py`：`intent_route` span 包装路由调用；新增 metric `agent.intent.route.count`（标签 path/category）、`agent.intent.clarify.count`、`agent.intent.degrade.count`；审计 logger 记路由决策（event=intent_route）。

### D9：路由在轮次中的时序

```
run_turn 开始（turn_start 已下发）
  load_history / trim（既有）
  ── intent_route span ──
    env 过滤候选 → 规则匹配 → (未命中) embed(query) → 索引 top-k
    → 高置信: 收窄 / 低置信: 路由 LLM → 选域 / chitchat / unknown
    → unknown: 生成澄清文本
  ────────────────────────
  澄清 → 直接 yield turn_delta(澄清文本) → 持久化 assistant → turn_end（不建图）
  否则 → build_agent(收窄工具) → 既有 ReAct 流程
```

路由位于 `agent_graph` span 之前；embedding/路由 LLM 调用各自带 llm_call/client span。

## Risks / Trade-offs

- [embedding 端点故障/高延迟] → 索引启动构建失败则进程启动失败（配置错误应早暴露）；运行时 query embedding 失败/超时 → 路由降级全量工具 + degraded metric/日志；规则命中不调 embedding（短路省延迟）；索引本地缓存使重启不依赖端点即可服务（缓存命中时跳过启动 embed）。
- [检索误杀：正确工具不在 top-k] → 阈值/margin 默认保守（不满足即不收窄，走 LLM/全量）；低置信不收窄是硬策略；二期 `search_skills` 元工具彻底消除；路由日志记录"模型实际调用工具是否在推荐集"，持续度量误杀率。
- [数百 Skill 描述/示例质量参差] → category+examples 列为接入规范（skill-plugin spec）；无示例 Skill 标注并可在日志中筛出，推动业务方补全。
- [stub 误用于生产] → 健康检查 + 启动 WARN + 自动降级（D6）。
- [doubao-embedding-vision 为多模态模型、向量维度/中文效果待联调确认] → 维度运行时自适应（numpy 不敏感）；缓存键含模型 id，换模型自动重建；若语义效果不达预期，embedding 模型 id 可配置替换，架构不变。
- [规则配置错误把意图钉死到错误 Skill] → 规则为显式配置、审计可见、可配置关闭；规则仅收敛不放大（目标集合仍经 env 过滤交集）。
- [澄清体验：反问过多] → 澄清率有 metric 可观测；阈值调优；chitchat 直接纯对话不澄清。

## Migration Plan

1. 配置新增 `routing`/`embedding` 块与 `llm.temperature`，均有默认值；`routing.enabled` 默认 **true**（无 Skill 时路由无候选、行为=纯对话，零影响）。
2. 存量部署升级：配置真实 `embedding.base_url/api_key/model`（方舟）；未配置则启动 WARN + 路由降级为仅规则/全量（行为≈现状，不会错误收窄）。
3. 业务方新注册 Skill 补 `category`/`examples`；存量 Skill 缺 examples 不阻断（description 兜底 + 标注）。
4. 回滚：`routing.enabled=false` 即时回到全量平铺，无需回退代码。
5. 索引缓存目录 `.skill_index_cache/` 加入 `.gitignore`/`.dockerignore`（容器内可重建，不挂卷）。

## Open Questions

- `score_threshold`/`margin`/`top_k` 的具体默认数值：余弦分数分布依赖 embedding 模型，联调前无法定值；先给宽松默认（k=20、threshold 偏高以保兜底），上线后按 `agent.intent.route.*` 日志分布调优。
- 二期是否上 `search_skills` 元工具 / 每-example 多向量索引：由一期兜底率、误杀率日志数据决定。
- category 命名与粒度规范（如 `order`/`report`/`crm`...）由业务方在接入时约定，框架只要求非空字符串。
