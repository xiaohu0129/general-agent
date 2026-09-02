## Purpose

为浏览器终端用户提供登录使用层：基于 PBKDF2 密码哈希与 Cookie + 服务端 Session 的登录认证（即时吊销、滑动续期、登录失败限流），配套用户体系与多会话管理（列表/新建/历史消息/重命名/删除）及归属校验，使用户可注册登录并在多个对话窗口间管理历史，且无法越权访问他人数据。

## ADDED Requirements

### Requirement: 密码安全存储

系统 SHALL 以 PBKDF2-HMAC-SHA256（随机 salt、200000 次迭代，标准库实现）存储密码，MUST NOT 存储明文；校验 SHALL 使用常量时间比较。用户名 SHALL 满足 `^[A-Za-z0-9_\\u4e00-\\u9fa5]{2,32}$`，密码长度 8–64。

#### Scenario: 注册存储密码哈希

- **WHEN** 用户以合法用户名与 ≥8 位密码注册
- **THEN** 系统存储形如 `pbkdf2$<iterations>$<salt>$<hash>` 的哈希，不存明文

#### Scenario: 非法凭据格式

- **WHEN** 注册时用户名或密码不符合规则
- **THEN** 系统返回 400 `VALIDATION` 并给出可读原因，不创建用户

### Requirement: Cookie + 服务端 Session 登录

`auth_mode=session` 时，系统 SHALL 在登录/注册成功后创建服务端登录态并通过 HttpOnly、`SameSite=Lax`、`Path=/` 的 cookie 下发不透明 token（token 由密码学随机源生成，仅经 cookie 传输，JS 不可读）。受保护端点 SHALL 从 cookie 解析登录态，无效/过期返回 401 `UNAUTHORIZED`。

登录态 SHALL 支持滑动续期（剩余寿命不足一半时重写 cookie Max-Age，默认有效期 168 小时）与即时吊销（登出/踢下线删除服务端记录即生效）。

#### Scenario: 登录成功下发 cookie

- **WHEN** 用户以正确用户名/密码登录
- **THEN** 系统返回用户信息并设置 HttpOnly + SameSite=Lax 的会话 cookie

#### Scenario: 未登录访问受保护接口

- **WHEN** 请求未携带或携带无效/过期会话 cookie 访问 `/chat`、`/sessions`、`/auth/me` 等
- **THEN** 系统返回 401 `UNAUTHORIZED`，前端据此跳转登录页

#### Scenario: 登出即时生效

- **WHEN** 用户登出
- **THEN** 服务端登录态被吊销、cookie 被清除，此后携带该 cookie 的请求返回 401

### Requirement: 防用户枚举与登录失败限流

登录时"用户不存在"与"密码错误" SHALL 统一返回 401 `INVALID_CREDENTIALS`（不区分），防止用户枚举。系统 SHALL 按"客户端 IP + 用户名"对登录失败计数，在窗口内（默认 5 次 / 10 分钟）超限后返回 429 `LOGIN_LOCKED`；登录成功后清除该计数。

#### Scenario: 错误密码不暴露用户是否存在

- **WHEN** 对不存在的用户名或错误密码发起登录
- **THEN** 系统均返回 401 `INVALID_CREDENTIALS`，响应不可区分两种情况

#### Scenario: 连续失败触发锁定

- **WHEN** 同一 IP + 用户名在 10 分钟内连续登录失败达到 5 次
- **THEN** 后续登录尝试返回 429 `LOGIN_LOCKED`，即使密码正确也暂不受理

### Requirement: 用户与多会话数据模型

系统 SHALL 维护用户表（`uid` 为 uuid4 hex 身份键，用户名唯一）与对话会话表（一个用户多个会话，`session_id` 即消息流的 sessionId，含标题与更新时间）；表结构 SHALL 在服务启动时惰性创建。Web 链路写入的消息 SHALL 以 `uid` 作为用户隔离键、`service` 固定为配置的 web 服务维度。

#### Scenario: 一个用户拥有多个会话

- **WHEN** 同一登录用户发起多个对话
- **THEN** 每个对话有独立 `session_id` 与标题，消息按会话隔离存储

### Requirement: 会话管理与归属校验

系统 SHALL 提供会话列表、新建、历史消息查询、重命名、删除接口（均需登录）；所有会话操作 SHALL 按登录用户 `uid` 做归属校验，越权访问他人会话 SHALL 返回 404（不暴露资源存在性）。删除会话 SHALL 同时删除其消息。

历史消息查询 SHALL 返回按时间升序的消息（含角色、内容、工具调用、turnId 等），供前端刷新后回放。

#### Scenario: 越权访问他人会话返回 404

- **WHEN** 用户 A 请求访问属于用户 B 的 `sessionId`（会话详情、消息、重命名、删除或订阅）
- **THEN** 系统返回 404 `SESSION_NOT_FOUND`，不确认该会话存在

#### Scenario: 会话生命周期闭环

- **WHEN** 用户新建会话、发送消息、查询历史、重命名、删除
- **THEN** 列表/标题/历史消息相应更新；删除后该会话及其消息不再可见

### Requirement: 跨域与 CSRF 防护

session 模式下系统 SHALL 配置明确的 CORS 来源白名单并允许凭证（`allow_credentials`），MUST NOT 允许通配源 `["*"]`（启动时校验报错）。CSRF 防护 SHALL 由 `SameSite=Lax` cookie、仅接受 `Content-Type: application/json`（触发预检）与 Origin 白名单共同提供。

#### Scenario: session 模式配置通配源被拒

- **WHEN** `auth_mode=session` 且 `cors_origins` 含 `*`
- **THEN** 应用启动即报错，拒绝以不安全配置运行
