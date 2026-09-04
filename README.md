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
2. 在 `skills/__init__.py` 的 `build_registry()` 中 `register`；
3. 业务客户端放入 `app.state.services`（dict），Skill 内通过 `ctx.services["key"]` 取用；
4. 工具异常建议抛出带 `code` 属性的异常，会映射为 `tool_end` 的 `errorCode`。

## 意图识别与 Skill 路由

Skill 规模较大时，框架在 env 硬过滤后、建 Agent 前执行**意图路由**（可 `routing.enabled=false` 关闭退回全量平铺）：

```
规则路由（正则/命令，0 LLM 确定）-> 向量检索 Tool RAG（embedding 语义 top-k 收窄，主力）
  -> 低置信 LLM 选域（structured output, temperature=0）-> 仍不明确则向用户澄清
```

- embedding 走 OpenAI 兼容 `POST {base_url}/v1/embeddings`，配置 `embedding.*`（方舟示例 `base_url=https://ark.cn-beijing.volces.com/api/coding/v3`、`model=doubao-embedding-vision`）；**留空时指向本地 stub（哈希向量无语义），路由自动降级为仅规则+全量兜底并启动 WARN**；
- 向量索引内存实现（启动批量构建、按 Skill 元数据哈希本地缓存 `.skill_index_cache/`），不引入向量数据库；
- 执行 LLM 与路由 LLM 固定 `llm.temperature=0` 保证同输入同选择；路由决策经 `intent_route` span、`agent.intent.route.*` metric 与审计日志可回放；`GET /health` 返回 `routing` 状态。

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
- [docs/错题本.md](docs/错题本.md)：迁移/调试踩坑记录（现象、根因、修复）
- [docs/general-agent-arch.svg](docs/general-agent-arch.svg) / [docs/workflow.svg](docs/workflow.svg)：架构图与流程图
- `openspec/specs/`：各能力的权威可测规格（chat-sse-protocol / conversation-broker / agent-loop / llm-adapter / skill-plugin / governance-security / web-auth-session / observability / deployment），可用 `openspec list --specs` 查看

## 变更记录（OpenSpec Changes）

正式变更（新功能/架构调整/涉及 spec）在 `openspec/changes/` 下以 change 管理（proposal/design/specs/tasks 四工件），完成归档后移入 `openspec/changes/archive/` 并合并进主规格。

**进行中（`openspec/changes/`）：**

- **skill-retrieval-routing**（已实现，待归档）：**意图识别**——数百 Skill 规模下的工具路由与渐进式加载。env 硬过滤后经"规则路由（正则逃生门）→ 向量检索 Tool RAG（embedding 语义 top-k 收窄，主力）→ 低置信 LLM 结构化选域 → 用户澄清"三级链路把模型可见工具从数百收窄到十几；Skill 元数据新增 `category`/`examples`；执行/兜底 LLM 固定 `temperature=0`、embedding 模型与索引版本固定以保证同输入同结果；新增 `intent_route` span 与路由 metric 可回放。详见 `openspec/changes/skill-retrieval-routing/`。

**已归档（`openspec/changes/archive/`）：**

- **baseline-system-specs**（2026-09-02）：基线系统规格固化——9 个能力规格共 49 条 requirements（SSE 协议、Broker、Agent Loop、LLM 适配、Skill 插件、治理安全、Web 认证、可观测、部署）。
- **offload-large-artifacts**（2026-09-02）：大产物外置存储——本地 blob 存储（`BlobStore`）、消息按阈值分流（>32KB 外置，行内存 head + `content_ref` 引用）、历史回放 keyset 分页、产物下载端点（归属/路径安全）、会话删除级联清理 blob。
