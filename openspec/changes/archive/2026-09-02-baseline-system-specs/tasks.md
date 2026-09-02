## 1. OpenSpec 能力规格固化

- [x] 1.1 创建 change `baseline-system-specs` 脚手架（`openspec new change`）
- [x] 1.2 编写 `proposal.md`：动机、9 个新能力、Impact（纯文档无代码变更）
- [x] 1.3 编写 `chat-sse-protocol` 规格（结构化事件、工具配对、错误不杀轮次、上限终止、eventSeq）
- [x] 1.4 编写 `conversation-broker` 规格（Broker、背压、producer/consumer、心跳、/stream 重放、/notify、轮次串行锁）
- [x] 1.5 编写 `agent-loop` 规格（无状态图、递归上限、token 裁剪、孤儿工具清洗、事件/持久化顺序）
- [x] 1.6 编写 `llm-adapter` 规格（OpenAI 兼容适配、流式分片兼容、错误分类、stub）
- [x] 1.7 编写 `skill-plugin` 规格（基类/注册/env 过滤/服务注入/埋点错误受控/入参别名）
- [x] 1.8 编写 `governance-security` 规格（多模式鉴权含 jwt fail-closed、env 白名单、限流、脱敏、审计、统一错误、notify 鉴权）
- [x] 1.9 编写 `web-auth-session` 规格（PBKDF2、Cookie+Session、防枚举/登录限流、用户多会话、归属校验、CORS/CSRF）
- [x] 1.10 编写 `observability` 规格（trace 传播、span 树、低基数 metric、severity、日志注入、健康检查）
- [x] 1.11 编写 `deployment` 规格（Compose 形态、配置优先级、SSE 反代、单实例约束、多实例/HTTPS 演进）
- [x] 1.12 `openspec validate --strict baseline-system-specs` 通过

> design.md 刻意省略：本次为已验证现状的规格固化与文档同步，无跨模块代码改动、无新依赖/数据模型/迁移，技术决策已记录于 docs/00、docs/01。

## 2. 修订旧文档与代码一致性

- [x] 2.1 `docs/00-AGENT综合设计.md`：鉴权模式补 `session`、jwt fail-closed；事件表 `turn_start` 补 `sessionId`；模块/进度表补 Web 认证能力；导航补 02/03/错题本
- [x] 2.2 `docs/01-AGENT实现设计.md`：Bug 表补 B10/B11/B12（E7/E8/E9）；D3 鉴权补 session 与登录限流；落点补 turn_lock/auth/user_store/chat_session_store/auth_routes/sessions
- [x] 2.3 `docs/02-Web前端与认证设计.md` §3.2：前端目录补 `src/chat/model.ts`、`src/api/types.ts`
- [x] 2.4 `README.md` 文档章节：补 02、03、workflow.svg、错题本、openspec/specs 链接

## 3. 验证与归档

- [x] 3.1 运行 pytest，确认文档/规格变更未影响代码（72 passed）
- [x] 3.2 一致性 grep：auth_mode/sessionId/fail-closed 等 docs 与代码对齐
- [ ] 3.3 `openspec archive baseline-system-specs`，规格落到 `openspec/specs/`
- [ ] 3.4 归档后 `openspec list --specs` 与 `openspec validate`（specs）确认 9 能力就位
