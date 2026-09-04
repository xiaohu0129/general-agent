# Tasks: skill-retrieval-routing（意图识别：Skill 路由与渐进式加载）

> 实现纪律：每个实现任务按 TDD（先写失败测试 → 跑红 → 实现 → 跑绿）；测试全部不依赖外部服务（embedding/chat 用 stub 或确定性 fake）。命令：`$env:PYTHONPATH="."; .venv\Scripts\python -m pytest`。

## 1. 配置与依赖

- [x] 1.1 `config.py`/`config.yaml` 新增 `routing` 块（`enabled` 默认 true、`top_k=20`、`score_threshold`、`margin`、`rules: []`）与 `embedding` 块（`base_url`/`api_key`/`model`，base_url 留空指向 stub 基址，model 默认占位由部署覆盖）、`llm.temperature`（默认 0）；验证：配置加载单测（默认值、yaml/env 覆盖）通过
- [x] 1.2 余弦相似度用纯 Python 实现（不引入 numpy，避免新增重依赖/国内安装风险）；`.skill_index_cache/` 加入 `.gitignore` 与 `.dockerignore`（验证：git status 不出现缓存文件）

## 2. Embedding 客户端与 stub

- [x] 2.1 新增 `general_agent/embedding.py`：`EmbeddingClient`（httpx，`POST {base_url}/embeddings`，`async embed_texts(list[str]) -> list[list[float]]`），base_url 留空指向 stub；错误分类复用 LLMError 模式；api_key 仅内存不落日志；验证：单测用 MockTransport 校验请求路径/体、错误映射、key 不出现在异常信息
- [x] 2.2 `stub_llm.py` 增加 `/v1/embeddings` 端点：确定性哈希向量（token 哈希加权定维 + L2 归一化），相同文本同向量、共享 token 文本余弦相近；验证：stub 单测（同文本两次一致、相关文本相似度高于无关文本、返回 OpenAI 兼容结构）

## 3. Skill 元数据与注册表

- [x] 3.1 `skills/base.py`：Skill 基类新增 `category: str = ""` 与 `examples: list[str] = []`；`SkillRegistry` 新增 `list_allowed(ctx) -> list[Skill]`（返回 Skill 对象，env 过滤）；保留 `get_tools` 兼容；验证：单测（env 过滤返回 Skill 对象；无 examples/category 不报错）
- [x] 3.2 测试夹具补充：在 DemoSkill 基础上构造多个带 category/examples 的测试 Skill（不同域），供路由测试；验证：测试模块可构造含示例的注册表

## 4. 向量索引（构建/缓存/检索）

- [x] 4.1 新增 `skill_router/index.py`：索引文本聚合（category+name+description+examples 拼接）、元数据哈希（name/description/examples/category/allowed_envs）、启动批量 embed 构建、内存 numpy L2 归一化 + 余弦 top-k（返回 Skill + 分数，限 env 候选集）；验证：单测用 fake embedder（或 stub）验证 top-k 排序、env 候选过滤、k 截断
- [x] 4.2 索引本地缓存：按元数据哈希写/读缓存文件（向量矩阵 + Skill 名 + 模型 id），哈希一致复用、不一致重建；缓存目录可配置；验证：单测（首次构建写缓存、二次加载不调 embedder、改描述触发重建）
- [x] 4.3 索引构建容错：启动时 embedding 不可用且无缓存 → 标记索引不可用（路由降级），不阻断进程启动；验证：单测（embedder 抛错时应用可启动、路由走 degraded/fallback）

## 5. 规则路由

- [x] 5.1 新增 `skill_router/rules.py`：编译配置规则（正则/命令前缀 + 目标 Skill 名集合或 category），顺序匹配返回第一条命中；目标集合与 env 候选取交集；验证：单测（命中返回确定集合、未命中返回 None、多规则取第一条、env 交集、非法正则启动报错）

## 6. 路由编排、兜底 LLM 与澄清

- [x] 6.1 新增 `skill_router/router.py` 骨架与 `RouteDecision`（path: rule/vector/llm/chitchat/clarify/fallback/degraded；tools: list[Skill]；clarify_text?；决策明细字段）；接入规则层与向量层，实现高置信分流（top1≥threshold 且 top1−top2≥margin → top-k 收窄；否则低置信）；验证：单测（规则命中 path=rule 且不调 embedder；高置信 path=vector 收窄；低置信进入兜底分支）
- [x] 6.2 兜底路由 LLM：structured output（JSON，类别 enum 从注册表 category 清单动态派生 + chitchat/unknown，含 confidence/reason），temperature=0；解析失败/异常按 unknown 处理；选定 category → 该域工具集（env 过滤）；chitchat → 空工具集；验证：单测用 fake chat model（返回各类别 JSON、畸形 JSON、抛错）验证分流
- [x] 6.3 用户澄清：unknown/低置信时生成澄清文本（LLM 基于 category 清单产出选项式反问），RouteDecision.path=clarify 携带 clarify_text；验证：单测（clarify 决策含文本且 tools 为空；category 清单进入生成提示）
- [x] 6.4 降级路径：embedding/路由 LLM 异常时 path=degraded/fallback，工具集退回 env 过滤全量，异常被捕获并记录、不抛出；验证：单测（embedder 异常 → degraded 全量工具；路由 LLM 异常 → fallback 全量）

## 7. 轮次接线

- [x] 7.1 `api/chat.py`：`get_tools` 改为 `registry.list_allowed(ctx)` → `app.state.skill_router.route(...)`；非澄清决策以 `[s.to_tool(ctx) for s in decision.tools]` 调 `build_agent`（agent.py 不改）；验证：现有 chat e2e 测试在路由开启下仍通过（无 Skill 时纯对话）
- [x] 7.2 澄清分支接线：path=clarify 时不建 ReAct 图，runner/chat 直接以普通文本轮次输出 clarify_text（turn_start..turn_delta..turn_end），澄清 assistant 消息持久化，无 tool_start/tool_end；验证：e2e（unknown 输入返回澄清文本、无工具调用、消息落库、下一轮可正常路由）
- [x] 7.3 `app.py`：lifespan 启动构建/加载向量索引并装配 `SkillRouter`（embedding client + registry + 规则配置 + chat model）注入 `app.state.skill_router`；`routing.enabled=false` 时不装配（chat 走全量）；stub/未配置真实 embedding 时启动 WARN + health 标注；验证：启动测试（app 正常创建、state 注入、关闭开关时无 router）
- [x] 7.4 `llm.py`：ChatModel 请求体注入 `temperature`（配置 `llm.temperature`，默认 0），执行 LLM 与路由 LLM 均生效；验证：单测用 MockTransport 断言请求体含 temperature=0
- [x] 7.5 `/health` 增加 `routing.{enabled, embedding_model, index_ready, mode}` 字段；验证：health 测试断言新字段

## 8. 可观测

- [x] 8.1 `observability.py`：新增 `intent_route` span（包装 route 调用，位于 run_turn 下、agent_graph 前），属性含 path/命中规则/top-k 工具名+分数/分差/LLM 类别+置信度/最终工具集/embedding 模型 id/索引版本；验证：InMemory exporter 单测断言 span 存在且属性完整
- [x] 8.2 新增低基数 metric：`agent.intent.route.count`（标签 path、category）、`agent.intent.clarify.count`、`agent.intent.degrade.count`；高基数 ID 不进 metric；审计 logger 记录 event=intent_route 决策；验证：metric 单测（计数/标签）+ 日志断言（决策可回放、无 userId/sessionId metric 标签）

## 9. 文档与配置同步

- [x] 9.1 更新 `docs/00`（§六 Skill 插件：路由分级/渐进式加载）、`docs/01`（路由落点/决策）、`docs/02` 或 README（Skill 接入规范：category+examples 必填、routing/embedding 配置）；验证：文档与代码一致性自检（配置键、模块名、行为描述对齐）
- [x] 9.2 `config.yaml` 补 routing/embedding 注释样例与方舟 embedding 配置说明；`.env.example` 补 `AGENT_EMBEDDING__*` 变量；验证：按文档可完成一次配置

## 10. 全量验证

- [x] 10.1 路由端到端：stub chat + stub embedding + 多域 DemoSkill，覆盖 规则命中 / 向量高置信收窄 / 低置信 LLM 选域 / chitchat 纯对话 / unknown 澄清 / embedding 故障降级 六条路径；验证：新增 e2e 测试全绿
- [x] 10.2 确定性复现：相同输入（同消息、同注册表版本、stub embedding）两轮路由工具集一致、span 中模型/索引版本一致；验证：单测断言两次 RouteDecision.tools 与 path 相同
- [x] 10.3 回归：`routing.enabled=false` 与无 Skill 两种情况下既有全部测试通过；运行 `openspec validate --strict skill-retrieval-routing` 与全量 `pytest`；验证：命令真实输出 0 失败、validate 通过
