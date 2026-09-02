## Why

general-agent 的核心能力（SSE 对话协议、长任务稳定性、治理安全、Web 登录认证、Agent Loop、Skill 插件、LLM 适配、可观测、部署）均已实现并由测试验证，但 `openspec/specs/` 为空——没有一份权威、可测试的"系统应做什么"规格。现有 `docs/` 叙事文档（00/01）写于 Web 认证能力之前，鉴权模式、事件字段、并发/清洗机制等已滞后于代码。本次把已验证的现状固化为 OpenSpec 能力规格，并同步修订 docs 与代码的不一致，作为后续演进的基线。

## What Changes

- 新增 9 个 OpenSpec 能力规格（`openspec/specs/<capability>/spec.md`），以 ADDED Requirements + WHEN/THEN Scenarios 描述当前已实现行为；不改变任何代码行为。
- 修订 `docs/00-AGENT综合设计.md`：鉴权模式补 `session`（Cookie+Session Web 登录）、`jwt` 占位改为 fail-closed；标准事件表 `turn_start` 补 `sessionId` 字段；模块/进度表补 Web 前端与认证能力；文档导航补 02/03/错题本链接。
- 修订 `docs/01-AGENT实现设计.md`：Bug 表补 E7（末尾孤儿 tool_calls 清洗）、E8（jwt fail-open）、E9（重叠轮次/孤儿会话/前端假状态）；鉴权决策 D3 补 session 模式与登录失败限流；实现落点补 `turn_lock.py`/`auth.py`/`user_store.py`/`chat_session_store.py`/`auth_routes.py`/`sessions.py` 与 `app.state` 新增项。
- 修订 `docs/02-Web前端与认证设计.md` §3.2 前端目录（补实际存在的 `src/chat/model.ts`、`src/api/types.ts`）。
- 修订 `README.md` 文档章节：补 02、03、workflow.svg、错题本链接。
- 无代码变更、无 API 变更、无依赖变更；纯文档与规格固化。

## Capabilities

### New Capabilities
- `chat-sse-protocol`: 面向 UI 的结构化 SSE 事件协议（turn_*/tool_*/error/notification）、eventSeq 续传语义、工具调用与错误不杀轮次、turn_start 携带 sessionId。
- `conversation-broker`: 会话级事件中枢 Broker（seq 分配、ring buffer 续传窗口、fan-out 订阅、背压丢弃）、POST /chat producer/consumer 解耦与心跳、GET /stream 持久通道与 Last-Event-ID 重放、POST /internal/notify 异步通知、同会话轮次串行锁。
- `agent-loop`: 无状态 LangGraph 推理-行动循环、每轮从 MySQL 载入历史、max_tool_rounds 递归上限受控终止、上下文 token 预算裁剪与工具调用配对清洗。
- `llm-adapter`: OpenAI 兼容 ChatModel 适配层（/v1/chat/completions、非流式 _generate、SSE 流式 _astream、bind_tools/tool_calls）、错误分类、本地 stub LLM。
- `skill-plugin`: 自研 Skill 基类与 SkillRegistry、显式注册、按 env 动态过滤产出 StructuredTool、SkillContext.services 业务注入、工具异常转 errorCode 不杀轮次。
- `governance-security`: 多模式鉴权（disabled/api_key/session，jwt fail-closed）、env 白名单、令牌桶限流、凭证脱敏、审计留痕、GovernanceError 统一 JSON 错误、服务间 notify 鉴权。
- `web-auth-session`: Web 终端用户层——PBKDF2 密码哈希、Cookie+服务端 Session 登录态（即时吊销/滑动续期/登录失败限流）、用户体系、多会话 CRUD 与归属校验、历史消息查询。
- `observability`: OpenTelemetry traces+metrics（OTLP/console）、traceparent/X-Trace-Id 双收传播、低基数 metric、错误 severity 映射、structlog 注入 trace 上下文、健康检查观测状态。
- `deployment`: 单机 Docker Compose 部署形态（单实例后端 + Nginx 托管前端 + 外部 MySQL + 真实 LLM）、配置覆盖优先级、SSE 反代要点、单实例约束与多实例/HTTPS 演进路径。

### Modified Capabilities
<!-- 无：openspec/specs/ 此前为空，本次全部为新建能力规格。 -->

## Impact

- 文档：`docs/00`、`docs/01`、`docs/02`、`README.md` 修订；新增 `openspec/changes/baseline-system-specs/` 提案工件，归档后落到 `openspec/specs/`。
- 代码：无变更（`general_agent/`、`front/` 不动）。
- API/依赖/系统：无变更。
- 验证：`openspec validate --strict` 通过；`pytest` 保持全绿（确认文档修订未误伤代码）。
