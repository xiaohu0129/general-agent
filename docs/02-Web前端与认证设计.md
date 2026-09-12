# 02-Web 前端与认证体系设计

> 范围：为 general-agent 增加浏览器可用的 Web 前端（`front/`，Vite + React + TS），
> 以及支撑它的后端能力：**Cookie + Session 登录认证**、**用户体系**、**多会话管理**、**历史消息查询**。
> 与 00/01 文档的关系：00 定义 SSE/治理/Skill 骨架；本文档定义"终端用户登录使用"这一层，不改动 LangGraph 推理循环与 Broker 事件协议主体。

## 一、总体决策

| 决策点 | 结论 | 理由 |
|---|---|---|
| 认证模式 | **Cookie + 服务端 Session** | 本项目为单/少实例 Web 应用（内存 Broker 已隐含单实例）；`GET /stream` 为原生 EventSource 设计，cookie 自动携带、自动重连、自动带 `Last-Event-ID`；即时吊销/踢下线；无 POST 非幂等重试问题 |
| 密码存储 | PBKDF2-HMAC-SHA256，随机 salt，200000 次迭代 | 标准库 `hashlib`，不引第三方依赖 |
| Session 存储 | 内存 TTL dict（默认）；Redis 可选增强（预留接口） | 与 Broker 一致：单实例内存即可工作，配 Redis 后可多实例 |
| 会话（对话）模型 | 新增 `agent_chat_session` 表，一个用户多个会话 | 替代 `session_key(service,env,user)` 的单会话模式 |
| 身份隔离键 | 注册分配 `uid`（uuid4 hex），消息表 `user_id = uid` | 用户名可改，历史不漂移 |
| 跨域 | CORS 白名单（可配置）+ `credentials=True` | 替代现有 `allow_origins=["*"]` |
| CSRF 防护 | SameSite=Lax + 仅接受 `Content-Type: application/json`（触发 CORS 预检）+ Origin 白名单 | 三重防护，不引入 CSRF token |
| 传输加密 | 应用层不做 TLS；本地 HTTP，生产由网关/Nginx 终止 HTTPS（cookie `Secure` 位可配置开启） | 部署层职责 |

### 术语区分（重要）

- **登录会话（Login Session）**：浏览器 cookie 中的 `ga_session` token ↔ 服务端登录态。本文档称 **session token**。
- **对话会话（Chat Session）**：`agent_chat_session` 表，一个对话窗口，含标题/历史消息。本文档称 **chat session**，其 ID 称 `sessionId`（与现有 SSE 协议中的 sessionId 同义）。

## 二、后端设计

### 2.1 数据模型

```sql
-- 用户表
CREATE TABLE IF NOT EXISTS agent_user (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  uid VARCHAR(32) NOT NULL UNIQUE,          -- uuid4 hex，身份隔离键
  username VARCHAR(64) NOT NULL UNIQUE,
  password_hash VARCHAR(256) NOT NULL,      -- pbkdf2$iterations$salt_hex$hash_hex
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 对话会话表
CREATE TABLE IF NOT EXISTS agent_chat_session (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id VARCHAR(64) NOT NULL UNIQUE,   -- uuid4 hex，= agent_message.session_id
  uid VARCHAR(32) NOT NULL,                 -- 属主
  service VARCHAR(64) NOT NULL,             -- 固定 "web"
  env VARCHAR(32) NOT NULL,                 -- 固定取配置 web_env（默认 "dev"）
  title VARCHAR(128) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_uid (uid, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- DDL 并入 `mysql_client.py` 的 `SCHEMA_DDL`（惰性建表，与 `agent_message` 同策略）。
- `agent_message` 表结构不变；Web 链路写入时 `service="web"`、`env=<配置>`、`user_id=<uid>`。

### 2.2 配置（config.yaml `security` 段新增）

```yaml
security:
  auth_mode: "session"        # disabled | api_key | jwt(预留) | session(新增，Web 登录)
  cors_origins:               # 白名单；["*"] 仅在 disabled 模式允许
    - "http://localhost:5173"
    - "http://127.0.0.1:5173"
  session:
    ttl_hours: 168            # 登录态有效期（7 天滑动）
    cookie_secure: false      # 生产 HTTPS 下设 true
    cookie_name: "ga_session"
  web:
    service: "web"            # Web 链路写入消息的 service 维度
    env: "dev"                # Web 链路的 env 维度
```

环境变量覆盖沿用现有规则（`AGENT_SECURITY__SESSION__TTL_HOURS` 等）。

### 2.3 新增模块

#### `general_agent/auth.py` — 密码哈希 + 登录会话

- `hash_password(password) -> str`：`pbkdf2$200000$<salt_hex>$<hash_hex>`，salt = `secrets.token_bytes(16)`。
- `verify_password(password, stored) -> bool`：`hmac.compare_digest` 常量时间比较。
- `LoginSession` dataclass：`token / uid / username / created_at / expires_at`。
- `LoginSessionStore`：
  - 内存实现：`dict[token, LoginSession]` + 懒过期清理；`create(uid, username, ttl) -> token`（token = `secrets.token_urlsafe(32)`）、`get(token)`（过期返回 None 并删除；dict 按键命中即对应会话，**不再做 token 自比的常量时间比较**——platform-hardening-fixes 删除了该处恒真比较）、`touch(session)`（滑动续期，剩余寿命 < ttl/2 时重置 expires）、`revoke(token)`、`revoke_all(uid)`（踢下线，预留）。
  - 登录失败锁定为**双维度计数**（窗口/阈值相同：5 次/10 分钟）：`ip|username` 与 `username`（跨 IP）各自独立计数，`login_locked` 任一维度命中即锁（429 `LOGIN_LOCKED`），成功登录清两维；新增 username 维度用于防"多 IP 各试 4 次"分布式撞库，代价是接受按用户名锁号的短窗口 DoS 面。
  - Redis 实现预留（`RedisLoginSessionStore`，key `general:agent:loginsession:<token>`，TTL），配置 redis 时启用；本期实现内存版，接口对齐。
- Cookie 辅助：`set_session_cookie(resp, token, ttl, secure)` / `clear_session_cookie(resp)`；属性 `HttpOnly; SameSite=Lax; Path=/; Max-Age=<ttl>`，`Secure` 按配置。

#### `general_agent/user_store.py` — UserStore（MySQL）

- `create_user(username, password) -> uid`：用户名冲突抛 `AuthError(409, "USER_EXISTS")`；用户名规则 `^[a-zA-Z0-9_\u4e00-\u9fa5]{2,32}$`，密码长度 8–64。
- `get_by_username(username)`：返回行或 None（platform-hardening-fixes 已删除从未调用的 `get_by_uid`）。
- 可注入 pool（测试用内存 Fake）。

#### `general_agent/chat_session_store.py` — ChatSessionStore（MySQL）

- `create(uid, service, env, title) -> session_id`（uuid4 hex）。
- `list_for_user(uid, limit=50) -> [{sessionId, title, updatedAt}]`（按 updated_at DESC）。
- `get_owned(session_id, uid) -> row | None`（归属校验，查不到即无权）。
- `rename(session_id, uid, title) -> bool`。
- `delete(session_id, uid) -> bool`（删 `agent_chat_session` 行 + `agent_message` 中该 session 行）。
- `touch(session_id)`：有新消息时刷新 updated_at。

#### `general_agent/api/auth_routes.py`

| 方法/路径 | 入参 | 行为 | 响应 |
|---|---|---|---|
| `POST /auth/register` | `{username, password}` | 按客户端 **IP 限流**后校验规则 → 建用户 → 自动登录（建 session + Set-Cookie）；限流配置 `security.register_rate.{enabled,rps,burst}`，默认约 10 次/分钟（rps=0.167）、突发 5，超限 429 `RATE_LIMIT` 且不建用户 | `{uid, username}` + Set-Cookie |
| `POST /auth/login` | `{username, password}` | 查用户 → verify_password（用户不存在时同样执行一次 dummy PBKDF2 校验，**拉平两条失败路径耗时**防时序枚举；与密码错误统一报 401 `INVALID_CREDENTIALS`，防用户枚举） | `{uid, username}` + Set-Cookie |
| `POST /auth/logout` | — | revoke 服务端 session + 清 cookie | `{ok: true}` |
| `GET /auth/me` | — | 需登录 | `{uid, username}` |

错误均走现有 `GovernanceError`（JSON `{code, message}`）：401 `UNAUTHORIZED`/`INVALID_CREDENTIALS`、409 `USER_EXISTS`、400 `VALIDATION`、429 `RATE_LIMIT`/`LOGIN_LOCKED`。

#### `general_agent/api/sessions.py`（均需登录）

| 方法/路径 | 行为 |
|---|---|
| `GET /sessions` | 当前用户会话列表 `[{sessionId, title, updatedAt}]` |
| `POST /sessions` | 显式新建会话 `{title?}` → `{sessionId, title}`（也可不建，/chat 自动建） |
| `GET /sessions/{sessionId}/messages` | 历史消息（归属校验）：`[{role, content, toolCalls, toolCallId, status, turnId, createdAt, options?, selected?}]`，按 id ASC；**含 `role:"tool"` 工具结果行**（platform-hardening-fixes 起输出，此前查出但丢弃），其 `status` 由后端派生——content strip 后可解析为 JSON 对象且含非空 `errorCode` → `"error"`，否则 `"success"`，前端据此真实回放工具卡成败，不再硬编码 success；澄清消息带 `options:[{label,value}]` 与已选 `selected`（带前缀 value 原样；存量无 meta 行两字段为 null 不报错） |
| `PATCH /sessions/{sessionId}` | `{title}` 重命名 |
| `DELETE /sessions/{sessionId}` | 删除会话及其消息 |

**澄清选项协议（后端已就绪，前端消费归独立 change）**：路由澄清轮 SSE 下发 `clarify` 事件（`question`/`options:[{label,value}]`，value 带 `category:`/`skill:` 前缀、对前端不透明原样回传），并随消息 meta 持久化、历史回放透出 options/selected。点选时下一轮 `POST /chat` 的 `message` 填所选项 label（照常落库/进模型上下文），并携带 `clarify_selection:{value}`；后端识别后跳过向量/LLM 直接确定性收窄。上述字段均为 additive，旧前端忽略即降级纯文本澄清。**选项卡片渲染/点击回发/刷新还原已由前端 change `structured-clarification-options` 落地（见 §3.5、§3.6）。**

### 2.4 现有链路改造

#### `security.py`

- `auth_mode` 增加 `"session"`：新增依赖 `web_session_dep(request) -> Identity`：
  1. 读 cookie token → `LoginSessionStore.get()`；无效/过期 → 401 `UNAUTHORIZED`；
  2. `touch()` 滑动续期（并重写 cookie Max-Age）；
  3. 返回 `Identity(service=settings.security.web.service, env=settings.security.web.env, user=session.uid)`。
- `governance_dep` 按 mode 分流：`session` → cookie 登录态；`disabled/api_key/jwt` → 现有 x-* 头逻辑（API 调用方不受影响）。
- token 校验要点（**重点**）：
  - token 仅经 cookie 传输（HttpOnly，JS 不可读，XSS 无法窃取）；
  - 服务端以 token 为键查 dict（命中即该会话，不做比较；早期"查找后常量时间比较"写法是恒真自比，已删除）；
  - 过期 session 立即清理并 401；
  - 登出/改密（预留）服务端 revoke 即时生效；
  - 限流仍按 `Identity.rate_key`（uid:env）生效。

#### `api/chat.py`

- 鉴权后 Identity 的 user=uid；sessionId 处理：
  - **未传 sessionId**：请求入口预生成 `session_id`（uuid4），会话行由 **producer 在轮次锁内创建**
    （title 取首条消息前 20 字符、去换行）。producer 不随 SSE 断开取消，故"建会话+跑轮次"必定完成，
    避免请求极早断开留下空孤儿会话；
  - **传了 sessionId**：入口即 `get_owned()` 归属校验，失败 → 404 `SESSION_NOT_FOUND`；
  - **同会话串行**：`TurnLockRegistry`（`general_agent/turn_lock.py`）按 session_id 提供 asyncio.Lock，
    producer 在锁内跑 `run_turn`——用户中途停止后立刻再发消息时，后轮等前轮落库完毕才载入历史，
    防止历史行交错。注册表按引用计数回收：等待者在 await 前即计数，故"释放瞬间有等待者"不会误删锁
    （避免 setdefault/pop 方案在释放与唤醒之间重建锁、互斥失效的竞态）；
  - 每轮结束（producer finally）`touch(session_id)` 刷新 updated_at。
- `events.turn_start` 增加 `sessionId` 字段（前端首帧即知会话 ID，用于侧边栏/URL 更新）。
- 前端"停止生成"仅断开 SSE（AbortController）；后端 producer 继续跑完落库，前端 abort 后把
  进行中的气泡/工具卡片标记为"已停止/已中断"，并在结束时刷新会话列表。

#### `api/stream.py`

- 同样走 `governance_dep`（session 模式即 cookie 校验）；`sessionId` 归属校验后才订阅。
- 浏览器原生 `EventSource` 自动带 cookie、自动重连、自动带 `Last-Event-ID`，无需前端手写。

#### `app.py`

- CORS：`allow_origins=settings.security.cors_origins`、`allow_credentials=True`、`allow_methods=["*"]`、`allow_headers=["*"]`；`auth_mode=session` 时禁止 `["*"]` 通配（启动校验报错）。
- `app.state.login_sessions = LoginSessionStore()`、`app.state.user_store = UserStore()`、`app.state.chat_sessions = ChatSessionStore()`。
- 注册新路由：`auth_routes.router`、`sessions.router`。
- `/health` 免鉴权（不变）。

### 2.5 边界与防护

- 未登录访问受保护接口 → 401（不重定向，前端路由自行跳登录页）。
- 越权访问他人 sessionId → 404（不暴露存在性）。
- 登录限流：失败计数（内存，5 次/10 分钟锁定）按 **`ip|username` 与 `username` 双维度**独立统计，任一命中即 429 `LOGIN_LOCKED`（username 维度防多 IP 分布式撞库，接受由此带来的短窗口锁号 DoS 面）——计数器放 `LoginSessionStore` 同模块，过期键随懒清理回收。
- 注册限流：`POST /auth/register` 按客户端 IP 走独立令牌桶（`security.register_rate`，默认约 10 次/分钟、突发 5），与对话限流分开；超限 429 `RATE_LIMIT` 且不建用户。
- 防用户枚举时序：登录用户不存在时也执行一次 200k 轮 dummy PBKDF2 校验（`auth._DUMMY_HASH`，结果忽略），使"用户不存在/密码错误"两路径响应耗时不可分辨；响应体始终统一 401。
- 用户名/密码长度与字符校验在注册与登录两侧一致（登录侧只做长度下限快速拒绝，不提示具体规则差异）。
- cookie 不设 Secure 时本地 HTTP 可用；配置 `cookie_secure: true` 后仅 HTTPS 传输。

## 三、前端设计（`front/`）

### 3.1 技术栈

Vite 5 + React 18 + TypeScript + 原生 CSS（CSS Modules/全局 CSS 变量，不引 UI 框架）；
依赖：`react`、`react-dom`、`react-markdown`（Markdown 渲染，防 XSS）、`remark-gfm`；
构建：`tsc -b && vite build`；开发：`vite`（:5173）。

### 3.2 目录结构

```
front/
  index.html
  package.json / tsconfig*.json / vite.config.ts / vitest.config.ts
  src/
    main.tsx
    App.tsx                     # 路由：未登录 → /login；已登录 → /
    api/
      client.ts                 # fetch 封装：credentials:'include'、JSON、错误归一化
      types.ts                  # API/事件 TypeScript 类型定义
      auth.ts                   # register/login/logout/me
      sessions.ts               # 会话 CRUD
      chat.ts                   # POST /chat 的 fetch+ReadableStream SSE 解析（CRLF/LF/CR 兼容）
      stream.ts                 # GET /stream 持久通道 EventSource 薄封装（按名监听/error 分流/熔断）
    chat/
      model.ts                  # 对话领域模型：消息/工具调用/澄清卡片/会话状态的前端建模
      applyEvent.ts             # 事件应用纯函数：事件状态推进、provenance 对账、旧卡片作废
      eventGate.ts              # 双通道幂等门：(turnId,eventSeq) 去重 + 会话级高水位/缺口检测
      history.ts                # 历史 DTO → ChatMessage 映射（restored；options/selected 与工具 error/success 状态还原）
    state/
      auth-context.tsx          # 登录态（启动 GET /auth/me 引导）
    pages/
      LoginPage.tsx             # 登录/注册切换
      ChatPage.tsx              # 主布局
    components/
      Sidebar.tsx               # 会话列表、新建、重命名、删除、退出登录
      Welcome.tsx               # 空会话欢迎页 + 示例 prompt
      MessageList.tsx           # 消息流 + 自动滚动
      MessageBubble.tsx         # 用户/助手气泡
      ClarifyOptions.tsx        # 结构化澄清选项按钮卡片（已选/禁用态）
      ToolCallCard.tsx          # tool_start/tool_end 折叠卡片（工具名/参数/结果/错误）
      Markdown.tsx              # react-markdown 包装
      Composer.tsx              # 底部输入框（自适应高度、发送、停止生成）
    styles/global.css           # 豆包风格设计 token 与基础样式
```

测试与源码同目录（`*.test.ts(x)`，vitest + jsdom + @testing-library/react），见 §四。

### 3.3 交互与数据流

- **引导**：App 启动 `GET /auth/me`（credentials: include）→ 401 则登录页。
- **对话**：
  - `POST /chat` 用 `fetch`（EventSource 不支持 POST）+ `ReadableStream` 手动解析 SSE 帧（`event:`/`data:`/`id:`），帧分隔兼容 CRLF/LF/CR（见 §3.7），`credentials:'include'`；
  - 事件处理：`turn_start`（取 sessionId → 必要时新建侧边栏条目/更新 URL、绑定活动轮气泡）→ `turn_delta`（追加打字）→ `tool_start/tool_end`（工具卡片）→ `clarify`（澄清选项卡片，见 §3.5）→ `turn_end`（收尾，finishReason 提示、满足条件时启用卡片）→ `error`（错误条，卡片永不启用）；`notification` 不渲染，仅推进 seq 高水位；
  - **停止生成**：`AbortController.abort()`（后端 producer 仍跑完落库，与现有设计一致）；接入 `/stream` 后同轮后续事件经持久通道继续补齐（见 §3.6）；
  - 刷新/切换会话后：`GET /sessions/{id}/messages` 回放历史（已完成的轮次内容不丢），映射为 `source:"restored"` 消息并还原澄清 options/selected（见 §3.5、§3.6 对账规则）；工具卡成败以历史 `role:"tool"` 行的派生 `status`（error/success）为准，失败工具刷新后仍显示失败（早期版本硬编码 success 的问题已修）；"stopped" 只是前端瞬时态、不落库，历史回放不表达；
  - 后端持久通道 `GET /stream` **已接入**：有当前会话时常开原生 EventSource（cookie/自动重连/`Last-Event-ID` 均浏览器原生承担），与 POST 即时流双通道按 `(turnId,eventSeq)` 幂等合并，断线或停止生成后同轮事件不丢，详见 §3.6。
- **会话管理**：侧边栏列表（`GET /sessions`）、新建（本地空态 + 首条消息时后端自动建）、重命名、删除（二次确认）、切换（拉历史消息）。
- **401 处理**：任意接口 401 → 清登录态 → 跳登录页（登录页提交后回到原路径）。

### 3.4 美术风格（参考豆包）

- 浅色为主：背景 `#f7f8fa`，侧边栏白/微灰，对话区居中最大宽度 768px；
- 圆角 12–16px，气泡：用户侧品牌色（蓝紫渐变 `#4d6bfe→#7b61ff`）白字，助手侧白底浅边框；
- 底部输入框：大圆角卡片、阴影、聚焦描边；发送按钮圆形品牌色；
- 工具卡片：浅灰底、左侧图标、可折叠，进行中转圈动画、成功/失败色态；
- 字体：系统字体栈；Markdown 代码块深色等宽。

### 3.5 结构化澄清选项卡片

- **消息模型**：`ChatMessage` 增 `clarify?: { options: {label,value}[]; selected?; disabled? }` 与 `source: "live" | "restored"`（本页实时产生的乐观气泡为 live，历史回放为 restored；缺省视为 live）。`value` 对前端不透明，MUST NOT 解析/拼装，仅原样暂存用于回传。
- **渲染**：澄清文本以同轮 `turn_delta` 流式文本为准，`clarify` 事件只取 `options`（`question` 与 delta 同源，不重复渲染）；`ClarifyOptions` 为独立按钮卡片，挂在助手气泡下方；无 options/空数组（旧后端、clarify_options 关闭）降级为纯文本气泡，不报错、不渲染空卡片。
- **卡片状态机（live）**：
  - `disabled`：轮次进行中（clarify 到达即 disabled=true，等待 turn_end）或异常轮（`error` 收尾、用户停止生成且 `/stream` 未补齐）；
  - `clickable`：`turn_end` 到达后、无 selected——且该轮必须是当前**最新 live 轮**才启用，旧轮迟到的 turn_end MUST NOT 复活已作废卡片；
  - `selected`（整卡禁用）：历史回放的 selected 为权威，或本页已点选（乐观置位）；selected 值不在 options 的 value 集合内（脏数据）时整卡禁用，不误标。
- **跨轮作废**：新 `clarify` 事件、或新一轮 `turn_start` 到达时，此前所有"未 selected 且仍可点"的 live 卡片整体置 disabled（过期态，作废置位幂等）——覆盖"停止生成后立即重发，旧轮 producer 迟到的 clarify/turn_end 经 `/stream` 到达"竞态。restored 历史卡片不做此推断（回放无事件顺序语义）：未 selected 的旧回放卡片保持可点（续聊场景），点选会被后端忽略并回落文本闭环。
- **点选载荷**：点击先乐观置 selected+disabled（先于发请求，避免 streaming 推进期间仍可点），点击回调入口有 `streaming` 守卫——轮次进行中点选直接忽略，防止 send 被守卫拦截后卡片"假已选"；随后发起下一轮 `POST /chat`：`message` 填所选项 label（照常显示为用户气泡），body **非空才挂** `clarify_selection: { value }`；手打输入不携带该字段。乐观 selected 与后端不一致时，以后续历史回放的 selected 覆盖。
- **历史还原**：`historyToMessages` 把历史 DTO 的 options/selected 映射到 `clarify`（有 selected 即整卡禁用，无 selected 保持可点）；restored 用户行无 turnId、assistant 行带持久化 turnId（与 broker 事件 turnId 同源，是 §3.6 对账依据）。

### 3.6 GET /stream 持久通道与双通道对账

- **连接生命周期**：仅在存在当前会话时挂载 `SessionStream`，按 `new EventSource('/stream?sessionId=...')` 建连；随 sessionId 变化 cleanup `close()` 后重建。新建会话（current=null）不建连，等 `turn_start` 带回 sessionId、setCurrent 后补挂载。cookie 携带、断线重连、`Last-Event-ID` 头全部由浏览器原生承担，前端不做应用层退避；React 18 StrictMode dev 双挂载靠 effect cleanup 收敛，不残留双连接。
- **按名监听，不用 onmessage**：后端每类事件都带 SSE `event:` 行，浏览器只把无 event 字段的默认事件投递给 onmessage；前端对七个事件名显式 `addEventListener`（turn_start/turn_delta/turn_end/tool_start/tool_end/clarify/notification），统一解析 `MessageEvent.data` 后汇入事件入口。
- **业务 error 与连接 error 同名分流**：业务事件恰好名为 `error`，在浏览器里同样进入 onerror。处理器规则：onerror 收到 MessageEvent 且 data 为可解析 JSON → 业务 error 事件（恰好投递一次，进事件应用层；不再对 "error" 按名 addEventListener，避免双发）；无 data 的普通 Event → 连接错误：`readyState=CLOSED`（服务端返回非可接受状态、浏览器放弃重连）→ `close()` + 熔断降级为**仅 POST 即时流**（仅 console.warn，不阻断发送框；浏览器不暴露状态码，401 由下一次 REST 请求的既有流程收敛）；`readyState=CONNECTING`（网络抖动）→ 浏览器自动重连，仅 console.warn。
- **双通道幂等合并**：POST 即时流（post）与 `/stream`（stream）汇入同一个事件入口，先过**会话级** eventGate（gate 按 sessionId 存于 Map，切会话不共享）：以有界 **LRU**（Map 插入序淘汰，容量 **2000**，命中不 refresh）记录已应用事件键 `"${turnId ?? ""}#${eventSeq}"`，重复事件（双通道重复、ring 重放重复）跳过——文本不重复拼接、卡片状态不重复翻转、toolCalls 不重复插入；notification 无 turnId 也占 seq（空串前缀保证键不塌缩）。无 eventSeq 的防御分支：post 通道放行但不记忆（无法构造稳定键），stream 通道一律忽略，两路均不推进高水位。
- **会话级高水位与缺口检测**：gate 维护 `maxSeenSeq`，所有带有限 seq 的事件共同推进（含前端不渲染的 notification——它同样经 broker.distribute 占 seq；heartbeat 是无 id 的 SSE 注释行，不计）。**缺口（`seq > maxSeenSeq+1`）只由 `channel==="stream"` 的事件产生**——POST 即时流服务端已按 turnId 过滤，其 seq 跳号不携带信息，故 post 通道只推进高水位与去重、永不报缺口；高水位仍由两通道共同推进，保证 stream 真正缺口可判。乱序迟到（seq ≤ 水位）只记忆不报；**首次连接只建基线不报缺口**（首连 ring 全量重放与历史的重复由 provenance 对账解决）。
- **缺口静默重拉合并**：活动轮（streaming）中检测到缺口仅挂账 `pendingGapRef`，在该轮 turn_end/error 收尾后执行；空闲缺口立即执行。执行体独立于 openSession（不 abort 即时流、不整体替换 messages、不 setCurrent、不清输入框）：重拉该会话历史映射为 restored，拼接"restored + turnId 尚未落库的 live 气泡"（已落库的旧 live 轮按 turnId 被 restored 替换、转正，selected 接受历史权威）；在途守卫保证连续缺口只重拉一次；顶部一次性内联提示"事件已过期，已刷新"，**合并成功后约 3 秒（`GAP_NOTICE_TTL_MS=3000`）自动消失**（切会话/新一轮发送的既有清除逻辑保留，timer 在卸载时清理）。`/stream` 熔断/不可用时 POST 即时流与澄清卡片照常工作，缺口能力随之缺失、靠刷新兜底。
- **provenance 对账（applyEvent 纯函数）**：双通道事件去重后汇入唯一纯函数 `applyEvent(messages, ctx, ev)`。除 turn_start 外，所有带 turnId 的事件仅在命中 **live 助手气泡**时才推进：命中 restored 消息 → 整事件忽略（历史是权威快照，不拼文本/不插卡/不翻转状态）；不命中任何消息（未知 turnId：首连 ring 重放的更早轮次、他标签页轮次）→ 忽略且 MUST NOT 新建气泡；无 turnId 的 notification → 不进消息模型（仅在外层推进高水位）。`turn_start` 经 ChatPage 持有的"当前活动轮气泡 id"绑定到无 turnId 的 live 乐观气泡（单活动轮不变式，沿用 send 的 `if (streaming) return` 守卫），绑定同时作废更早的未选 live 卡片；建会话/侧边栏插入/setStreaming 等 React 副作用留在 ChatPage 回调，不进纯函数。
- **停止生成的交互**：停止仍只 abort POST 即时流，EventSource 不受影响；同轮后续 turn_delta/clarify/turn_end 经 `/stream` 继续到达，经 gate 幂等后自然补齐（stopped 是截断时刻的本地事实，不阻止补齐），turn_end 后按现状收尾。取舍：刷新时刻正在运行、assistant 行尚未落库的轮次，其重放事件是未知 turnId 被忽略，本页不自动恢复，落库后重新打开会话可见。

### 3.7 POST /chat SSE 帧兼容

- 手写解析器按 SSE 规范兼容三种空行分帧：CRLF（sse-starlette 默认 `sep="\r\n"`，真实帧为 `id: N\r\nevent: x\r\ndata: {...}\r\n\r\n`）、LF（`\n\n`）、CR（`\r\r`）——事件分隔用 `/\r\n\r\n|\r\r|\n\n/`，块内行再按 `/\r\n|\r|\n/` 切。修复前仅以 `indexOf("\n\n")` 分块：CRLF 帧在流式期间永不切分，流结束后整段 dispatch 又因多 data 行 JSON.parse 失败被静默丢弃（clarify 与既有事件共用解析器，故为本 change 前置修复）。
- **跨 chunk 缓冲**：reader 循环保留残余 buffer 循环切分，支持多帧合并在同一 chunk、单帧跨多个 chunk；先以 `TextDecoder("utf-8", { stream: true })` 解码再入 buffer，CJK 多字节字符跨 chunk 边界安全（不在字节态切分字符串）。
- `id:` 行解析后挂为事件顶层 `id` 字段；序号以 `data.eventSeq` 为准（后端打 seq 时同时写 SSE id 行与 data.eventSeq）；多个 `data:` 行按 `\n` 拼接后 JSON.parse，注释行（`: heartbeat`）忽略，畸形帧静默跳过不影响后续事件。

## 四、验证方案

### 后端（pytest，全部不依赖外部服务）

1. 旧测试保持通过：`conftest.py` 已强制 `AUTH_MODE=disabled`，x-* 头链路不受影响。
2. 新增 `tests/test_auth_web.py`（FakeUserStore/FakeChatSessionStore 内存替身 + TestClient）：
   - 注册 → 200 + Set-Cookie；重复注册 → 409；
   - 未登录 `GET /auth/me`、`POST /chat`、`GET /sessions` → 401；
   - 登录 → me → 带 cookie POST /chat（stub LLM）收到完整 turn_start…turn_end，turn_start 含 sessionId；
   - 会话列表/消息回放/重命名/删除闭环；
   - 用户 A 不能访问用户 B 的 sessionId → 404；
   - 错误密码 → 401；登出后 cookie 失效 → 401；
   - 密码哈希格式与 verify 正确性单测。
3. 命令：`.venv\Scripts\python -m pytest`。

### 前端

1. `npm install && npm run build`（含 `tsc` 类型检查）通过；
2. `npm test`（`vitest run`，jsdom + @testing-library/react，配置见 `vitest.config.ts`，用例与源码同目录 `*.test.ts(x)`）通过，覆盖四层：SSE 字节流夹具（CRLF/LF/CR 三种分帧、多帧单 chunk、单帧跨 chunk、CJK 多字节跨 chunk，驱动真实 `streamChat` 而非 mock 事件对象）、纯函数 applyEvent/eventGate/history（状态机、双通道幂等、缺口高水位、无 seq 防御、provenance 对账、旧卡片作废）、连接薄封装 stream.ts（fake EventSource 按名派发、MessageEvent/普通 Event 两类 error 区分、畸形/非对象帧、CLOSED 熔断）、组件与 ChatPage 集成（ClarifyOptions/MessageBubble 禁用已选态与点击载荷；ChatPage 缺口静默重拉、在途挂账收口、活动轮延后、澄清点选闭环、StrictMode 双挂载）；
3. 联调：后端 stub LLM 模式（`.venv\Scripts\python -m general_agent.stub_llm` + 主服务），浏览器走通 注册→对话（流式+工具卡片+澄清卡片点选）→刷新历史（卡片/已选态还原）→停止生成后 /stream 补齐→重命名/删除→登出。

## 五、对长对话/记忆/会话管理的影响（Cookie+Session 方案）

- **长对话**：流建立后仅鉴权一次；cookie 滑动续期（7 天 idle），挂机后发消息/EventSource 重连均自动带 cookie，无 token 过期重试、无重复发消息风险。
- **记忆管理**：消息仍落 MySQL，隔离维度由 x-user 头变为 uid；改密/登出/踢下线即时生效，无身份滞后窗口。
- **会话管理**：多会话由 `agent_chat_session` 支撑，标题/时间/归属完整；删除会话连带清理消息。
