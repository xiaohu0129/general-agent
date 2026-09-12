# web-auth-session Specification

## Purpose
为浏览器终端用户提供登录使用层：基于 PBKDF2 密码哈希与 Cookie + 服务端 Session 的登录认证（即时吊销、滑动续期、登录失败限流），配套用户体系与多会话管理（列表/新建/历史消息/重命名/删除）及归属校验，使用户可注册登录并在多个对话窗口间管理历史，且无法越权访问他人数据。

## Requirements

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

登录时"用户不存在"与"密码错误" SHALL 统一返回 401 `INVALID_CREDENTIALS`（不区分），防止用户枚举。两种失败路径的响应 SHALL 时序不可分辨：用户不存在时系统 SHALL 仍执行一次等价的 PBKDF2 校验（dummy hash 比对），MUST NOT 因跳过密码哈希而产生可远程测量的响应时间差。

系统 SHALL 对登录失败计数并锁定：保留"客户端 IP + 用户名"维度（默认窗口 10 分钟内 5 次），**并 SHALL 同时按用户名跨 IP 累计失败次数**，同一阈值达到后即使攻击者更换 IP 也 SHALL 返回 429 `LOGIN_LOCKED`；登录成功后清除相关计数。按用户名锁定带来的"故意锁死某账号"DoS 面为已知接受的权衡（锁定有窗口、自动恢复，且锁定事件可审计）。

#### Scenario: 错误密码不暴露用户是否存在

- **WHEN** 对不存在的用户名或错误密码发起登录
- **THEN** 系统均返回 401 `INVALID_CREDENTIALS`，响应内容与处理耗时不可区分两种情况（用户不存在路径同样执行 PBKDF2）

#### Scenario: 连续失败触发锁定

- **WHEN** 同一 IP + 用户名在 10 分钟内连续登录失败达到 5 次
- **THEN** 后续登录尝试返回 429 `LOGIN_LOCKED`，即使密码正确也暂不受理

#### Scenario: 分布式撞库按用户名锁定

- **WHEN** 同一用户名在 10 分钟窗口内自多个不同 IP 累计登录失败达到阈值
- **THEN** 后续（即使来自新 IP）的登录尝试返回 429 `LOGIN_LOCKED`，窗口过后自动恢复

### Requirement: 用户与多会话数据模型

系统 SHALL 维护用户表（`uid` 为 uuid4 hex 身份键，用户名唯一）与对话会话表（一个用户多个会话，`session_id` 即消息流的 sessionId，含标题与更新时间）；表结构 SHALL 在服务启动时惰性创建。Web 链路写入的消息 SHALL 以 `uid` 作为用户隔离键、`service` 固定为配置的 web 服务维度。

#### Scenario: 一个用户拥有多个会话

- **WHEN** 同一登录用户发起多个对话
- **THEN** 每个对话有独立 `session_id` 与标题，消息按会话隔离存储

### Requirement: 会话管理与归属校验

系统 SHALL 提供会话列表、新建、历史消息查询、重命名、删除接口（均需登录）；所有会话操作 SHALL 按登录用户 `uid` 做归属校验，越权访问他人会话 SHALL 返回 404（不暴露资源存在性）。删除会话 SHALL 同时删除其消息，并 SHALL 级联删除这些消息外置的 blob 产物文件（见 `artifact-storage`）。

历史消息查询 SHALL 按时间**分页**返回（keyset 游标分页）：接受游标参数（如 `before=<messageId>`）与页大小（`limit`，有默认值与上限），按 `id` 倒序取一页后反转为升序返回，并在响应中给出是否还有更多（`hasMore`）与下一页游标（`nextCursor`）；不传游标时返回最新一页。系统 MUST NOT 在一次请求中返回会话的全部消息。

历史消息中的超大工具结果/产物 SHALL 以内联 head 摘要 + 产物标志（`contentRef`/`contentSize`/`contentKind`）形式返回，MUST NOT 内联完整 blob；完整产物经 `artifact-storage` 的下载端点按需获取。

#### Scenario: 越权访问他人会话返回 404

- **WHEN** 用户 A 请求访问属于用户 B 的 `sessionId`（会话详情、消息、重命名、删除、产物下载或订阅）
- **THEN** 系统返回 404 `SESSION_NOT_FOUND`，不确认该会话存在

#### Scenario: 会话生命周期闭环

- **WHEN** 用户新建会话、发送消息、查询历史、重命名、删除
- **THEN** 列表/标题/历史消息相应更新；删除后该会话、其消息及外置产物不再可见/可下载

#### Scenario: 历史消息分页加载

- **WHEN** 一个会话的消息数超过单页大小，客户端首次不传游标请求历史，随后携带返回的 `nextCursor` 请求下一页
- **THEN** 首次返回最新一页且 `hasMore=true`；后续每一页返回更早的消息，按时间升序拼接不重不漏，直至 `hasMore=false`

#### Scenario: 历史中的超大结果以摘要 + 引用返回

- **WHEN** 某条历史消息对应一个外置的大工具结果
- **THEN** 历史接口返回该消息的 head 摘要与 `contentRef`/`contentSize`/`contentKind` 标志，不内联完整内容；客户端可凭引用经下载端点获取完整产物

### Requirement: 跨域与 CSRF 防护

session 模式下系统 SHALL 配置明确的 CORS 来源白名单并允许凭证（`allow_credentials`），MUST NOT 允许通配源 `["*"]`（启动时校验报错）。CSRF 防护 SHALL 由 `SameSite=Lax` cookie、仅接受 `Content-Type: application/json`（触发预检）与 Origin 白名单共同提供。

#### Scenario: session 模式配置通配源被拒

- **WHEN** `auth_mode=session` 且 `cors_origins` 含 `*`
- **THEN** 应用启动即报错，拒绝以不安全配置运行

### Requirement: 澄清选项持久化与历史回放

系统 SHALL 将澄清轮的结构化选项与用户选择随会话消息持久化：澄清消息落库时 SHALL 在消息的通用 `meta` 结构中记录 `kind:"clarify"`、候选方向 `categories`、选项 `options:[{label,value}]`，以及用户经选项闭环后被选中的 `selected` value（若有）。其中 `options`/`selected` 的 value 为带 `category:`/`skill:` 前缀的可路由值，持久化与回放原样保留（不做语义改写），`label` 为人类可读文案。该 meta 复用消息表的通用 JSON 扩展列，MUST NOT 为选项单独建表；存量无 meta（NULL）的历史消息 SHALL 视为普通消息，回放时不报错。

会话历史消息查询（`/sessions/{id}/messages`，含分页）SHALL 在消息 DTO 中透出澄清消息的 options 与已选 value（经现有 meta 透传通道），使前端刷新/换设备后可还原澄清选项卡片并标记已选项为已选/禁用。历史回放 MUST NOT 内联超出既有大小限制的内容，选项结构为小尺寸结构化字段、随消息头返回。持久化与回放 SHALL 按登录用户 `uid` 做既有归属校验，澄清选项数据不跨会话/跨用户泄露。

#### Scenario: 澄清选项随消息持久化

- **WHEN** 路由产出澄清（含 options）并结束该轮
- **THEN** 澄清 assistant 消息落库时 meta 含 `kind:"clarify"`、`categories` 与 `options:[{label,value}]`；用户下一轮点选且确定性收窄成功后，系统 SHALL 按会话归属键与该澄清消息的 `turn_id` 定位回上一轮澄清行，将其 `meta.selected` 回写为所选带前缀 value（合并写入、不覆盖已有 options），供历史回放标记已选

#### Scenario: 历史消息回放透出澄清选项

- **WHEN** 客户端请求某会话历史消息且其中包含澄清轮
- **THEN** 返回的澄清消息 DTO 含 options 与已选 value（若有），前端可据此还原选项卡片并标记已选/禁用；分页边界上选项不重不漏

#### Scenario: 存量无 meta 消息回放不报错

- **WHEN** 会话历史中存在改动前写入的、meta 为 NULL 的消息
- **THEN** 历史回放正常返回这些消息（按普通文本消息处理），不因缺少 options/meta 字段报错

#### Scenario: 澄清选项遵循会话归属校验

- **WHEN** 用户请求非本人会话的历史消息
- **THEN** 系统按既有归属校验返回 404，澄清选项数据不泄露给无归属用户

### Requirement: 注册接口频率限制

`POST /auth/register` SHALL 按客户端 IP 独立限流（与对话令牌桶分离，速率/突发与开关可配置，默认每分钟不超过 10 次、突发 5 次）；超限 SHALL 返回 429 `RATE_LIMIT`，MUST NOT 创建用户。登录失败锁定与注册限流均 MUST NOT 依赖外部服务（内存实现即可，多实例由独立变更统一分布式化）。

#### Scenario: 批量注册被限流

- **WHEN** 同一 IP 在短时间内连续调用注册接口超过突发上限
- **THEN** 超出部分返回 429 `RATE_LIMIT`，用户表中只有限流前成功创建的账号

### Requirement: 历史工具调用状态回放

会话历史消息查询（`/sessions/{id}/messages`）SHALL 对 `tool` 角色消息透出其调用状态 `status`：内容为结构化 JSON 且含错误标记（如 `errorCode`）时为 `error`，否则为 `success`（停止生成导致的未落库工具结果不产生历史行，无需"stopped"态）。前端据历史回放渲染工具卡片时 MUST NOT 将全部工具卡片硬编码为成功态。

#### Scenario: 失败的工具调用刷新后仍显示失败

- **WHEN** 某轮中工具返回了 `errorCode` 的错误结果并落库，用户刷新页面重新加载历史
- **THEN** 该工具消息 DTO 携带 `status:"error"`，前端工具卡片渲染为失败态（而非成功）

#### Scenario: 成功工具调用回放为成功

- **WHEN** 历史中的工具消息无错误标记
- **THEN** DTO `status` 为 `success`，前端卡片显示成功并可展示结果/产物引用

### Requirement: 前端事件缺口检测的通道边界与去重有界

前端双通道（`POST /chat` 即时流与 `GET /stream` 持久通道）的会话级 seq 缺口检测 SHALL 仅以 `/stream` 通道事件为依据：`POST /chat` 通道经服务端按当前轮次过滤，其观察到的 seq 不连续（如 notification 等其他轮次事件穿插）MUST NOT 判定为缺口、MUST NOT 触发静默历史重拉或用户提示。

静默重拉合并成功后展示的"事件已过期，已刷新"类提示 SHALL 在短时间后（默认 3 秒）自动消失，MUST NOT 永久驻留。

双通道幂等去重所用的 `(turnId, eventSeq)` 游标集合 SHALL 有界（LRU 淘汰），MUST NOT 随会话事件总量无限增长而造成长跑页面内存泄漏。

#### Scenario: POST 通道 seq 跳号不误报缺口

- **WHEN** 一轮对话进行中有 `notification`（或他轮事件）分配了会话 seq，POST 通道仅收到本轮事件而表现为 seq 不连续
- **THEN** 前端不触发历史重拉、不展示缺口提示，仅 `/stream` 通道的真正缺口才触发

#### Scenario: 缺口提示自动消失

- **WHEN** `/stream` 通道检测到缺口并静默重拉合并成功，提示条展示
- **THEN** 提示在约 3 秒后自动消失，无需用户手动关闭

#### Scenario: 长会话去重游标不无限增长

- **WHEN** 同一会话页面长时间运行、累计处理远超内存上限数量的事件
- **THEN** 去重游标集合维持在固定容量上限（LRU 淘汰最旧条目），内存占用有界
