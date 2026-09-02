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
  - 内存实现：`dict[token, LoginSession]` + 懒过期清理；`create(uid, username, ttl) -> token`（token = `secrets.token_urlsafe(32)`）、`get(token)`（过期返回 None 并删除）、`touch(session)`（滑动续期，剩余寿命 < ttl/2 时重置 expires）、`revoke(token)`、`revoke_all(uid)`（踢下线，预留）。
  - Redis 实现预留（`RedisLoginSessionStore`，key `general:agent:loginsession:<token>`，TTL），配置 redis 时启用；本期实现内存版，接口对齐。
- Cookie 辅助：`set_session_cookie(resp, token, ttl, secure)` / `clear_session_cookie(resp)`；属性 `HttpOnly; SameSite=Lax; Path=/; Max-Age=<ttl>`，`Secure` 按配置。

#### `general_agent/user_store.py` — UserStore（MySQL）

- `create_user(username, password) -> uid`：用户名冲突抛 `AuthError(409, "USER_EXISTS")`；用户名规则 `^[a-zA-Z0-9_\u4e00-\u9fa5]{2,32}$`，密码长度 8–64。
- `get_by_username(username)` / `get_by_uid(uid)`：返回行或 None。
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
| `POST /auth/register` | `{username, password}` | 校验规则 → 建用户 → 自动登录（建 session + Set-Cookie） | `{uid, username}` + Set-Cookie |
| `POST /auth/login` | `{username, password}` | 查用户 → verify_password（用户不存在与密码错误统一报 401 `INVALID_CREDENTIALS`，防用户枚举） | `{uid, username}` + Set-Cookie |
| `POST /auth/logout` | — | revoke 服务端 session + 清 cookie | `{ok: true}` |
| `GET /auth/me` | — | 需登录 | `{uid, username}` |

错误均走现有 `GovernanceError`（JSON `{code, message}`）：401 `UNAUTHORIZED` / 409 `USER_EXISTS` / 400 `VALIDATION`。

#### `general_agent/api/sessions.py`（均需登录）

| 方法/路径 | 行为 |
|---|---|
| `GET /sessions` | 当前用户会话列表 `[{sessionId, title, updatedAt}]` |
| `POST /sessions` | 显式新建会话 `{title?}` → `{sessionId, title}`（也可不建，/chat 自动建） |
| `GET /sessions/{sessionId}/messages` | 历史消息（归属校验）：`[{role, content, toolCalls, toolCallId, turnId, createdAt}]`，按 id ASC |
| `PATCH /sessions/{sessionId}` | `{title}` 重命名 |
| `DELETE /sessions/{sessionId}` | 删除会话及其消息 |

### 2.4 现有链路改造

#### `security.py`

- `auth_mode` 增加 `"session"`：新增依赖 `web_session_dep(request) -> Identity`：
  1. 读 cookie token → `LoginSessionStore.get()`；无效/过期 → 401 `UNAUTHORIZED`；
  2. `touch()` 滑动续期（并重写 cookie Max-Age）；
  3. 返回 `Identity(service=settings.security.web.service, env=settings.security.web.env, user=session.uid)`。
- `governance_dep` 按 mode 分流：`session` → cookie 登录态；`disabled/api_key/jwt` → 现有 x-* 头逻辑（API 调用方不受影响）。
- token 校验要点（**重点**）：
  - token 仅经 cookie 传输（HttpOnly，JS 不可读，XSS 无法窃取）；
  - 服务端查找后 `hmac.compare_digest` 比对；
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
- 登录限流：同 IP + 同用户名失败计数（内存，5 次/10 分钟锁定，常量时间比较继续保留）——复用 TokenBucket 思路，失败计数器放 `LoginSessionStore` 同模块。
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
  package.json / tsconfig*.json / vite.config.ts
  src/
    main.tsx
    App.tsx                     # 路由：未登录 → /login；已登录 → /
    api/
      client.ts                 # fetch 封装：credentials:'include'、JSON、错误归一化
      types.ts                  # API/事件 TypeScript 类型定义
      auth.ts                   # register/login/logout/me
      sessions.ts               # 会话 CRUD
      chat.ts                   # POST /chat 的 fetch+ReadableStream SSE 解析
    chat/
      model.ts                  # 对话领域模型：消息/工具调用/会话状态的前端建模与归并
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
      ToolCallCard.tsx          # tool_start/tool_end 折叠卡片（工具名/参数/结果/错误）
      Markdown.tsx              # react-markdown 包装
      Composer.tsx              # 底部输入框（自适应高度、发送、停止生成）
    styles/global.css           # 豆包风格设计 token 与基础样式
```

### 3.3 交互与数据流

- **引导**：App 启动 `GET /auth/me`（credentials: include）→ 401 则登录页。
- **对话**：
  - `POST /chat` 用 `fetch`（EventSource 不支持 POST）+ `ReadableStream` 手动解析 SSE 帧（`event:`/`data:`/`id:`），`credentials:'include'`；
  - 事件处理：`turn_start`（取 sessionId → 必要时新建侧边栏条目/更新 URL）→ `turn_delta`（追加打字）→ `tool_start/tool_end`（工具卡片）→ `turn_end`（收尾，finishReason 提示）→ `error`（错误条）；
  - **停止生成**：`AbortController.abort()`（后端 producer 仍跑完落库，与现有设计一致）；
  - 刷新/切换会话后：`GET /sessions/{id}/messages` 回放历史（已完成的轮次内容不丢）；
  - 后端另有持久通道 `GET /stream`（cookie 下原生 EventSource 可自动带 cookie/重连/`Last-Event-ID`），
    用于"进行中轮次在断线后续传"，前端本期未接入（刷新后靠历史回放），作为后续增强点。
- **会话管理**：侧边栏列表（`GET /sessions`）、新建（本地空态 + 首条消息时后端自动建）、重命名、删除（二次确认）、切换（拉历史消息）。
- **401 处理**：任意接口 401 → 清登录态 → 跳登录页（登录页提交后回到原路径）。

### 3.4 美术风格（参考豆包）

- 浅色为主：背景 `#f7f8fa`，侧边栏白/微灰，对话区居中最大宽度 768px；
- 圆角 12–16px，气泡：用户侧品牌色（蓝紫渐变 `#4d6bfe→#7b61ff`）白字，助手侧白底浅边框；
- 底部输入框：大圆角卡片、阴影、聚焦描边；发送按钮圆形品牌色；
- 工具卡片：浅灰底、左侧图标、可折叠，进行中转圈动画、成功/失败色态；
- 字体：系统字体栈；Markdown 代码块深色等宽。

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
2. 联调：后端 stub LLM 模式（`.venv\Scripts\python -m general_agent.stub_llm` + 主服务），浏览器走通 注册→对话（流式+工具卡片）→刷新历史→重命名/删除→登出。

## 五、对长对话/记忆/会话管理的影响（Cookie+Session 方案）

- **长对话**：流建立后仅鉴权一次；cookie 滑动续期（7 天 idle），挂机后发消息/EventSource 重连均自动带 cookie，无 token 过期重试、无重复发消息风险。
- **记忆管理**：消息仍落 MySQL，隔离维度由 x-user 头变为 uid；改密/登出/踢下线即时生效，无身份滞后窗口。
- **会话管理**：多会话由 `agent_chat_session` 支撑，标题/时间/归属完整；删除会话连带清理消息。
