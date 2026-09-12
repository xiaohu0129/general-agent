# general-agent

通用 LLM Agent 服务：无状态 LangGraph 推理循环 + 持久 SSE 通道（断线续传/心跳）+ 多租户治理（鉴权/限流/脱敏/审计）+ 可插拔 Skill 插件 + OpenTelemetry 全链路可观测。业务方通过注册 Skill 与注入服务（`app.state.services`）扩展，框架本身不内置任何业务依赖。

技术栈：Python ≥3.11 · FastAPI · LangGraph · langchain-core · sse-starlette · aiomysql · redis-py · OpenTelemetry。

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖（uv）
uv sync --no-install-project        # 或：python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"

# 2. 本地联调：起 OpenAI 协议 stub LLM（:9094）
.venv\Scripts\python -m general_agent.stub_llm

# 3. 起主服务（:9093；llm.base_url 留空时自动指向 stub :9094）
.venv\Scripts\python -m general_agent
# 安装后也可用 console script：general-agent
```

- 健康检查：`GET http://localhost:9093/health`
- 对话（SSE）：`POST /chat`，header `x-service/x-env/x-user`，body `{"message": "...", "sessionId": "可选"}`
- 持久通道（SSE）：`GET /stream?sessionId=...&lastEventId=...`（支持 `Last-Event-ID` 断线续传）
- 异步通知接入：`POST /internal/notify`

## Web 前端（注册/登录/多会话）

`front/` 为 Vite + React + TypeScript 前端，豆包风格界面，与后端真实交互（Cookie+Session 登录、SSE 流式对话、工具调用卡片、会话管理）。

```bash
cd front
npm install
npm run dev          # http://localhost:5173 （开发代理到后端 :9093，cookie 同源）
```

- 后端 `config.yaml` 需 `security.auth_mode: "session"`（默认）并配置 `cors_origins` 白名单；
- 需可连接 MySQL（用户 `agent_user`、会话 `agent_chat_session`、消息 `agent_message` 三表惰性自建）；
- 大工具产物（超过 `artifacts.inline_threshold`，默认 32KB）外置到本地 blob 目录（默认 `./artifacts/`），
  MySQL 行内仅存 head 摘要 + `content_ref` 引用，历史回放经 `GET /sessions/{id}/artifacts/{messageId}` 下载；
  容器部署需把该目录挂持久卷（见 docs/03）；
- 认证：密码 PBKDF2 加盐哈希，登录态 HttpOnly + SameSite=Lax cookie，服务端 Session 可即时吊销；
- 接口：`POST /auth/register|login|logout`、`GET /auth/me`、`GET/POST/PATCH/DELETE /sessions`（详见 docs/02）。

## 接入真实 LLM

`config.yaml` 的 `llm` 段配置任意 OpenAI 兼容端点：

```yaml
llm:
  base_url: "https://your-openai-compatible-endpoint"   # POST {base_url}/v1/chat/completions
  api_key: ""
  model: "gpt-4o-mini"
  timeout: 60.0
```

## 扩展业务 Skill

1. 实现 `general_agent/skills/` 下的 `Skill` 子类：元数据 `name`/`description`/`args_schema`/`allowed_envs` + **`category`（技能域标签）+ `examples`（2~5 条典型用户说法，意图路由向量检索的主要语义来源）**，并实现 async `run(ctx, **kwargs)`；
   - **examples 直接决定召回质量**：向量索引按每条 example 单独建向量（max-sim 计分），多用法 Skill 应给差异化的多条说法；无 examples 的 Skill 仅以 description 兜底，会被 health `no_examples_skills` 与启动日志标注，建议补全；必填参数在 `args_schema` 用 `Field(description=...)` 提供字段提示，缺参时模型能定向追问；
2. 在 `skills/__init__.py` 的 `build_registry()` 中 `register`；
3. 业务客户端放入 `app.state.services`（dict），Skill 内通过 `ctx.services["key"]` 取用；
4. 工具异常建议抛出带 `code` 属性的异常，会映射为 `tool_end` 的 `errorCode`。

## 意图识别与 Skill 路由

Skill 规模较大时，框架在 env 硬过滤后、建 Agent 前执行**意图路由**（可 `routing.enabled=false` 关闭退回全量平铺）：

```
规则路由（正则/命令，作用于当前轮原文，0 LLM 确定）
  -> BM25 关键词路（订单号/型号/缩写精确召回，纯本地零依赖，全程只跑一次）
  -> 向量检索 Tool RAG（检索 query 默认拼接最近 N 轮历史，`context_turns` 可配）
     ├─ 高置信（top1≥threshold 且 gap≥margin）：向量路直接收窄（0 LLM）
     ├─ 命中指代词或低置信 -> 按需一次 LLM query 改写（temperature=0）后重检
     └─ 仍低置信 -> 路由 LLM 三级分流（高置信点名 Skill / 中置信选 category 域 / 低置信澄清）
  -> 澄清：文本 + 结构化 options（category:/skill: 前缀 value），SSE `clarify` 事件下发
     └─ 下一轮点选回传（clarify_selection.value）确定性收窄（跳过向量/LLM），手打走文本闭环
```

- embedding 走 OpenAI 兼容 `POST {base_url}/v1/embeddings`，配置 `embedding.*`（方舟示例 `base_url=https://ark.cn-beijing.volces.com/api/coding/v3`、`model=doubao-embedding-vision`）；**留空时指向本地 stub（哈希向量无语义），路由自动降级为规则+BM25+LLM 兜底（`mode=rule+keyword`）并启动 WARN**；
- 向量索引内存实现（**每 Skill 多向量：description 一条 + 每条 example 一条，段内 max-sim 召回**；启动批量构建、按 Skill 元数据哈希本地缓存 `.skill_index_cache/`，`embedding.batch_size` 分批请求），不引入向量数据库；无 examples 的 Skill 在 health `no_examples_skills` 与启动日志标注；
- **混合检索**：BM25 与向量结果经 RRF 融合排序（`routing.hybrid` 可关）；高置信判定仍只看向量余弦，放行集保底并入 BM25 rank≤3（防精确符号被向量路漏召回）；embedding 故障/stub 时 BM25 仍可独立收窄进 LLM 兜底（degraded 不等于放弃路由）；
- **澄清闭环**：澄清轮 assistant 消息落 `meta={kind:"clarify",categories,options}`；下一轮路由感知 `prev_clarify` 在候选方向内裁决，不重复反问；选项点选经 `clarify_selection` 回传后**跳过向量/LLM 直接确定性收窄**（path=option），并回写上轮 `meta.selected`；
- **缺参追问（半槽位填充）**：工具执行前按 `args_schema` 校验，缺必填参数时不执行业务、回流 `errorCode=MISSING_ARGS` 结构化引导，模型下一轮定向追问（`routing.arg_guard` 可关，关闭回落框架默认）；
- **误杀可观测**：收窄路径（rule/vector/llm/option）下模型实际调用推荐集外工具计入 `agent.intent.miss.count`（env/path/retrieval 标签）；检索分数分布 histogram、query 改写率、缺参追问率、澄清结果（option/text/repeat）均有低基数 metric；
- 执行 LLM 与路由 LLM 固定 `llm.temperature=0` 保证同输入同选择；路由决策（含 top-k/BM25/RRF 结果序列化）经 `intent_route` span、`agent.intent.*` metric 与审计日志可回放；`GET /health` 返回 `routing` 状态（mode/multi_vector/hybrid/semantic/clarify_options/no_examples_skills）。

SSE 协议新增 `clarify` 事件（data 含 `question`/`options:[{label,value}]`）与上行 `clarify_selection:{value}` 回传字段，均为 additive：旧前端忽略未知事件/字段即降级纯文本澄清（详见 docs/00 协议章节）；选项卡片前端渲染归独立 change `structured-clarification-options`。

## 测试

```bash
.venv\Scripts\python -m pytest
```

全部测试不依赖外部服务（LLM/业务用 httpx MockTransport、消息历史用内存 FakeStore、Redis 用 mock）。

## 文档

- [docs/00-AGENT综合设计.md](docs/00-AGENT综合设计.md)：架构、SSE 协议、长任务稳定性、治理、Skill、可观测设计
- [docs/01-AGENT实现设计.md](docs/01-AGENT实现设计.md)：Bug 记录、决策、实现落点、验证矩阵
- [docs/02-Web前端与认证设计.md](docs/02-Web前端与认证设计.md)：Web 前端、Cookie+Session 登录、用户体系、多会话
- [docs/03-部署方案.md](docs/03-部署方案.md)：Docker Compose 单机部署、SSE 反代、单实例/多实例约束
- [docs/04-意图路由待讨论问题清单.md](docs/04-意图路由待讨论问题清单.md)：意图识别 explore 沉淀的未确认问题 backlog（离线 golden set、用户反馈、元工具/supervisor、规则误杀、索引热更新等），含已确认决策索引
- [docs/错题本.md](docs/错题本.md)：迁移/调试踩坑记录（现象、根因、修复）
- [docs/general-agent-arch.svg](docs/general-agent-arch.svg) / [docs/workflow.svg](docs/workflow.svg)：架构图与流程图
- `openspec/specs/`：各能力的权威可测规格（chat-sse-protocol / conversation-broker / agent-loop / llm-adapter / skill-plugin / governance-security / web-auth-session / observability / deployment），可用 `openspec list --specs` 查看

## 变更记录（OpenSpec Changes）

正式变更（新功能/架构调整/涉及 spec）在 `openspec/changes/` 下以 change 管理（proposal/design/specs/tasks 四工件），完成归档后移入 `openspec/changes/archive/` 并合并进主规格。

**进行中（`openspec/changes/`）：**

- **structured-clarification-options**（澄清选项前端 + /stream 接入，**已实现待归档**；纯前端、depends-on intent-routing-context-quality）：消费上一 change 定稿的 `clarify` 事件/options 回放/`clarify_selection` 回传字段，在 `front/` 实现澄清选项卡片（状态机：轮次进行中/异常轮禁用 → turn_end 后且为最新 live 轮可点 → 点选或历史 selected 后整卡禁用；新 clarify/新轮 turn_start 作废旧 live 卡片、迟到 turn_end 不复活；selected 脏数据整卡禁用）、点击乐观置已选并随下一轮 POST /chat 回发 `clarify_selection:{value}`（手打不携带）、刷新后据历史回放还原卡片与已选态；**接入 `GET /stream` 持久通道**——EventSource 按名监听七事件（不用 onmessage）、业务/连接两类 error 同名分流（MessageEvent 带 JSON data 恰好投递一次；无 data 且 readyState=CLOSED 则 close 熔断降级仅即时流，CONNECTING 交浏览器原生重连）、Last-Event-ID 原生续传，POST 即时流与 /stream 经会话级 eventGate 按 `(turnId,eventSeq)` 幂等合并，provenance 对账（restored 轮整事件忽略、未知 turnId 不建气泡、notification 不入模型），会话级 seq 高水位含不渲染的 notification、ring 缺口触发一次静默历史重拉合并（活动轮延后到收尾后、不 abort/不整体替换、提示"事件已过期，已刷新"），停止生成后同轮事件经 /stream 补齐；**前置修复 SSE 手写解析**——兼容 sse-starlette 默认 CRLF 及 LF/CR 三种空行分帧、跨 chunk 缓冲与 CJK 多字节（旧 `\n\n` 分块对流式 CRLF 帧永不切分）；**新增 vitest 前端测试基建**（vitest + @testing-library/react + jsdom，字节流夹具/纯函数/组件/ChatPage 集成四层用例）。不碰后端/协议，旧后端或无/空 options 消息降级为纯文本澄清，`/stream` 失败不阻断即时流。

**已归档（`openspec/changes/archive/`）：**

- **platform-hardening-fixes**（2026-09-12）：平台硬化：缺陷探测 21 项修复（跨 9 个能力规格）：CJK 感知 token 估算（修中文低估 2~4 倍致长会话超窗 400）；Broker/限流器/前端去重集有界回收（会话状态空闲 TTL、ring 默认 256→2048、令牌桶懒清扫、gate LRU）；全鉴权模式会话归属统一校验（owner 落 MySQL，POST 首次写入者认领、/stream 跨身份 404，**BREAKING**：api_key/disabled 模式跨身份混用 sessionId 被拒）；注册按 IP 限流、登录 dummy PBKDF2 时序拉平、失败锁定增加 username 跨 IP 维度；历史工具调用真实 error/success 状态回放；`llm.temperature` 配置生效、httpx 客户端复用、流式坏 chunk 跳过；MySQL `pool_recycle` 保活；Skill 重名/非法名注册即失败；`/chat` 消息 1~8000 字、`/internal/notify` 字段长度 400 校验；`/health` 区分 ok/degraded；前端缺口检测仅由 /stream 通道产生、提示 3s 自动消失；**移除未装配的半成品 RedisBroker 与只写不读的 SessionStore**、清理死代码并在 lifespan 关闭 Redis 客户端。
- **intent-routing-context-quality**（2026-09-10）：**意图识别增强**——路由跨轮上下文（历史拼接为主 + 按需 LLM query 改写，修指代消解）+ 澄清闭环；向量索引改每-example 多向量 + max-sim；**BM25 关键词路 + RRF 混合检索**（精确符号召回、零依赖确定性、embedding 故障/stub 时可独立收窄，顺带修复 stub 哈希向量伪高置信缺陷）；兜底 LLM 可点名 Skill 且 confidence 三级分流；工具调用前参数校验守卫（缺参定向追问，半槽位填充）；**结构化澄清选项与硬回传闭环（D11）**——路由产出 `options:[{label,value}]`、新增 `clarify` SSE 事件下发并随断线续传/历史回放、用户点选经 `clarify_selection` 回传后跳过再裁决确定性收窄（手打仍走文本闭环）；补齐可观测漏项——误杀率、分数分布 histogram、改写率、缺参追问率、澄清 `result=option|text|repeat` 维度、`has_examples` 标注。
- **skill-retrieval-routing**（2026-09-04）：**意图识别**——数百 Skill 规模下的工具路由与渐进式加载。env 硬过滤后经"规则路由（正则逃生门）→ 向量检索 Tool RAG（embedding 语义 top-k 收窄，主力）→ 低置信 LLM 结构化选域 → 用户澄清"三级链路把模型可见工具从数百收窄到十几；Skill 元数据新增 `category`/`examples`；执行/兜底 LLM 固定 `temperature=0`、embedding 模型与索引版本固定以保证同输入同结果；新增 `intent_route` span 与路由 metric 可回放。
- **baseline-system-specs**（2026-09-02）：基线系统规格固化——9 个能力规格共 49 条 requirements（SSE 协议、Broker、Agent Loop、LLM 适配、Skill 插件、治理安全、Web 认证、可观测、部署）。
- **offload-large-artifacts**（2026-09-02）：大产物外置存储——本地 blob 存储（`BlobStore`）、消息按阈值分流（>32KB 外置，行内存 head + `content_ref` 引用）、历史回放 keyset 分页、产物下载端点（归属/路径安全）、会话删除级联清理 blob。
