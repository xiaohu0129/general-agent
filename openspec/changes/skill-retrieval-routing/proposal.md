## Why

当前 Agent 的"意图识别"没有独立层：Skill 经 env 过滤后**全部平铺**给模型，由 LLM function calling 隐式选择（纯 ReAct）。业务规划中 Skill 规模将达到**几百个**——平铺几百个工具会使模型选择质量显著下降（工具描述挤占上下文、选择面过大挑错/挑花眼），且决策过程黑盒、无置信度、无兜底、不可回放，相同输入因模型采样产生不一致结果。生产可控性要求"决策可观测、相同操作得到相同结果"。

## What Changes

- **新增 Skill 路由层（Skill Retrieval & Routing）**：在现有 env 硬过滤之后、`build_agent` 之前插入一道路由，按确定性从高到低分三级收窄工具集——
  - **[1] 规则路由**：配置化 pattern/命令前缀（正则），命中即映射到确定 Skill 集合，0 次 LLM 调用、结果可复现，作为高频/必须确定意图的逃生门；
  - **[2] 向量检索（Tool RAG，主力）**：对 Skill 的描述+示例话语构建 embedding 索引（启动时批量构建、内存余弦相似度、元数据哈希本地缓存），每轮 embed 用户消息检索 top-k 工具；最高分过阈值且 top1/top2 分差明确时直接收窄；
  - **[3] 低置信兜底**：向量检索无把握时，调用一次结构化输出的路由 LLM（temperature=0，输出意图类别+置信度+理由）选择技能域；仍不明确则**向用户反问澄清、索取更多细节**（以普通 assistant 文本轮次返回，不进入 ReAct）。
- **Skill 元数据升级**：Skill 基类新增 `category`（域标签，用于元数据过滤与兜底选域）与 `examples`（2~5 条示例话语，作为 embedding 主体），作为业务方接入规范。
- **Embedding 接入**：新增 embedding 客户端（OpenAI 兼容 `POST {base_url}/embeddings`，与 `llm.py` 并列），默认配置方舟 `doubao-embedding-vision`；提供确定性 stub embedding（哈希向量）使测试不依赖外部服务；stub LLM 同步补 `/v1/embeddings` 端点。
- **确定性保障**：执行 LLM 与路由兜底 LLM 统一固定 `temperature=0`（`llm.py` 补参数透传）；embedding 模型 id 与索引版本记录进路由决策，保证同输入同检索结果；不追求回复文本逐字一致。
- **路由可观测**：新增 `intent_route` span（路由路径 rule/vector/llm/clarify、top-k 工具名+相似度分数、分差、最终暴露工具、embedding 模型/索引版本）与路由审计日志；新增低基数 metric `agent.intent.route.*`（路径分布、兜底率、澄清率），高基数 ID 仅进 span/日志。
- **渐进式加载分期**：本期实现"图外预检索"（路由结果作为 `build_agent` 的工具入参，`agent.py` 机制不变）；模型在 ReAct 中主动换批的 `search_skills` 元工具（图内动态工具集）列为二期，依据本期路由日志数据再决定。
- **非目标（明确排除）**：不引入 Milvus/向量数据库等中间件（几百向量内存计算足够）；不做 supervisor 多子代理/handoff 架构（category 分组为其预留依据，本期不建子图）；SSE 协议不新增事件（路由静默执行，澄清为普通文本流）；不实现长期记忆/RAG 语料检索（另一议题）。

## Capabilities

### New Capabilities
- `skill-routing`: Skill 工具集的意图路由与渐进式加载——规则前置路由、向量检索 Tool RAG（索引构建/缓存/top-k 检索/置信分流）、低置信 LLM 选域兜底与用户澄清、确定性（temperature=0、模型/索引版本固定）、路由决策可观测（span/审计日志/metric）。

### Modified Capabilities
- `skill-plugin`: Skill 基类元数据新增 `category` 与 `examples`（接入规范）；注册表在 env 过滤后需支持按 Skill 集合/类别进一步收窄工具产出。
- `agent-loop`: 每轮构建 Agent 前 SHALL 经过 Skill 路由层确定工具子集（替代 env 过滤后全量平铺）；澄清场景 SHALL 以普通 assistant 文本轮次返回且不进入 ReAct 图。
- `llm-adapter`: 新增 OpenAI 兼容 embedding 客户端（`/embeddings`）与确定性 stub embedding；ChatModel 调用 SHALL 支持透传 `temperature=0`。
- `observability`: 新增 `intent_route` span 与路由决策审计日志、`agent.intent.route.*` 低基数 metric（路由路径/兜底/澄清）。

## Impact

- **新增代码**：`general_agent/skill_router/`（路由编排：规则匹配器、向量索引与检索、embedding 客户端、置信分流、兜底 LLM 选域、澄清）；`general_agent/embedding.py`（或并入 llm 适配层）；stub_llm 补 embeddings 端点。
- **修改代码**：`skills/base.py`（Skill 加 category/examples；Registry 支持按集合收窄）；`api/chat.py`（get_tools 与 build_agent 之间插入路由步骤；澄清分支直接产出文本轮次）；`llm.py`（temperature 透传）；`config.py`/`config.yaml`（新增 `routing` 与 `embedding` 配置块：开关、top_k、阈值、规则表、模型 id）；`observability.py`（intent_route span 与 route metric）；`app.py`（启动时构建/加载向量索引并注入）。
- **数据/依赖**：不新增外部中间件，也不新增第三方依赖（向量余弦为纯 Python 实现）；embedding 索引为进程内存态 + 本地缓存文件（不入 MySQL）；运行时需可访问 embedding 端点（测试用 stub）。
- **业务方接入**：新注册 Skill 需提供 `category` 与 `examples`；存量无示例的 Skill 以 description 兜底参与索引（检索质量较低，日志可观测）。
- **测试**：新增路由分级/检索收窄/兜底/澄清/确定性复现/规则命中的单测与 e2e（stub embedding 驱动，不依赖外部服务）；现有 72 项测试须保持通过（路由关闭或无 Skill 时行为退回现状）。
- **兼容性**：路由可通过配置关闭（`routing.enabled=false`），关闭时行为与现状完全一致；无 API/SSE 破坏性变更。
