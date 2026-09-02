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

1. 实现 `general_agent/skills/` 下的 `Skill` 子类（`name`/`description`/`args_schema`/`allowed_envs` + async `run(ctx, **kwargs)`）；
2. 在 `skills/__init__.py` 的 `build_registry()` 中 `register`；
3. 业务客户端放入 `app.state.services`（dict），Skill 内通过 `ctx.services["key"]` 取用；
4. 工具异常建议抛出带 `code` 属性的异常，会映射为 `tool_end` 的 `errorCode`。

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
