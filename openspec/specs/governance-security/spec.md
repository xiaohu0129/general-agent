# governance-security Specification

## Purpose
为服务提供多租户治理与安全护栏：可配置的鉴权模式、环境白名单隔离、令牌桶限流、凭证脱敏与审计留痕，对受保护端点统一执行"鉴权 -> 环境校验 -> 限流"，并以统一 JSON 错误响应失败，防止未授权访问、跨环境误调与凭证泄露。

## Requirements

### Requirement: 可配置鉴权模式

系统 SHALL 支持鉴权模式 `disabled`（放行）、`api_key`（校验 `X-Api-Key`）、`session`（Web Cookie 登录态，见 `web-auth-session`）。`jwt` 模式为预留但 MUST NOT 静默放行——配置为未实现模式时 SHALL 返回 500 `CONFIG` 错误（fail-closed）。

`api_key` 模式 SHALL 以常量时间比较校验请求 `X-Api-Key` 是否命中配置的合法密钥列表；失败 SHALL 返回 401 `AUTH`。

#### Scenario: api_key 校验

- **WHEN** `auth_mode=api_key` 且请求未携带或携带错误的 `X-Api-Key`
- **THEN** 系统返回 401，响应体为 `{"code":"AUTH", "message":...}`

#### Scenario: 未实现的 jwt 模式拒绝放行

- **WHEN** `auth_mode=jwt`（尚未实现）
- **THEN** 系统返回 500 `CONFIG` 错误提示该模式未实现，MUST NOT 放行请求

#### Scenario: disabled 模式放行

- **WHEN** `auth_mode=disabled`
- **THEN** 系统不做鉴权，请求依据 `x-service/x-env/x-user` 头解析身份

### Requirement: 环境白名单隔离

系统 SHALL 支持配置 `allowed_envs` 环境白名单；当白名单非空时，请求身份的环境不在白名单内 SHALL 返回 400 `VALIDATION`。

#### Scenario: 非法环境被拒

- **WHEN** `allowed_envs=["dev","prod"]` 而请求环境为 `staging`
- **THEN** 系统返回 400 `VALIDATION`，请求不进入 Agent

### Requirement: 会话归属统一校验

所有鉴权模式（含 `disabled`/`api_key`/`session`）下，携带 `sessionId` 的对话与订阅端点（`POST /chat`、`GET /stream`）SHALL 校验该会话归属的调用身份与请求身份一致：归属身份为 `(service, env, user)` 四元组中的三维身份键，以持久化会话所有权记录为唯一权威（MUST NOT 依赖可选的 Redis）。

- `POST /chat`：当该 `sessionId` 尚无所有权记录时，SHALL 以当前调用身份登记所有权（首次写入者先到先得，重复登记幂等）；所有权已属于其他身份时 SHALL 返回 404 `SESSION_NOT_FOUND`，MUST NOT 读取其历史或向其写入消息。
- `GET /stream`：无所有权记录或归属身份与请求身份不符时 SHALL 返回 404 `SESSION_NOT_FOUND`（不区分"不存在"与"越权"，不暴露存在性）。
- `session` 模式继续以登录 `uid` 归属（web 固定 service/env），行为不变。

升级前已存在、但没有所有权记录的历史会话，经其合法身份首次 `POST /chat` 时 SHALL 能被认领并继续使用。

#### Scenario: api_key 模式跨租户订阅被拒

- **WHEN** 身份 A（service/env/user 任一维度不同）已知某会话属于身份 B，A 携有效 API Key 请求 `GET /stream?sessionId=...`
- **THEN** 系统返回 404 `SESSION_NOT_FOUND`，A 收不到该会话任何事件，不确认会话存在

#### Scenario: 跨身份向他会话发消息被拒

- **WHEN** 身份 A 对已归属身份 B 的 `sessionId` 调用 `POST /chat`
- **THEN** 系统返回 404 `SESSION_NOT_FOUND`，不读取 B 的历史、不写入 A 的消息、不触发轮次

#### Scenario: 首次写入者认领无主会话

- **WHEN** 某 `sessionId` 尚无所有权记录，身份 A 对其发起首轮 `POST /chat`
- **THEN** 系统以 A 的身份登记所有权并正常执行轮次；此后身份 B 访问该会话返回 404

#### Scenario: 升级前历史会话被合法身份认领

- **WHEN** 升级前产生的会话没有所有权记录，其原使用身份（与历史消息行的 service/env/user 一致）首次对其 `POST /chat`
- **THEN** 认领成功且历史消息可正常载入，会话连续可用

#### Scenario: 并发首轮只有一个 owner

- **WHEN** 两个不同身份几乎同时对同一个无主 `sessionId` 发起首轮 `POST /chat`
- **THEN** 所有权登记的原子性保证只有一个身份认领成功并执行轮次，另一身份收到 404，消息不交错、不产生双 owner

### Requirement: 令牌桶限流

系统 SHALL 按身份维度（`user:env`）执行内存令牌桶限流（可配每秒补充速率与桶容量）；超过限流 SHALL 返回 429 `RATE_LIMIT`，并记录限流 metric 与审计日志。限流可通过配置关闭。

限流状态在内存中 SHALL 有界：系统 SHALL 周期性清扫长期无请求身份的计数状态（如随每次判定懒清理超过窗口未活跃的 key），MUST NOT 随身份数无限增长。

#### Scenario: 突发超限被限流

- **WHEN** 某身份在短时间内的请求超过桶容量且令牌未及补充
- **THEN** 系统返回 429 `RATE_LIMIT`，并记录一次限流命中

#### Scenario: 长期不活跃身份的限流状态被清扫

- **WHEN** 某限流 key 对应的身份在超过一个完整补充窗口以上没有任何请求
- **THEN** 其桶计数状态可被清除，进程内存不累积全部历史身份的限流条目

### Requirement: 凭证脱敏

系统 SHALL 在结构化日志中对敏感键（如 api_key、token、password、authorization、secret、cookie、x-api-key 等）的值掩码处理（递归处理嵌套结构）；LLM API Key 仅保存在内存，MUST NOT 落日志；出站 HTTP 自动埋点 MUST NOT 记录请求体中的凭证。

#### Scenario: 日志中密钥被掩码

- **WHEN** 一条日志事件包含 `api_key`/`password` 等敏感字段
- **THEN** 输出日志中这些字段值被掩码（保留首尾少量字符），不泄露完整凭证

### Requirement: 审计留痕

系统 SHALL 对关键动作（鉴权成功/失败、限流命中、登录/注册、通知接收等）输出独立审计日志，含操作者、动作、环境、资源、traceId 与时间。

#### Scenario: 鉴权与限流可审计

- **WHEN** 发生一次成功鉴权或一次限流命中
- **THEN** 审计日志中存在对应 `audit` 记录，可据此追溯操作者与环境

### Requirement: 统一治理错误响应

治理失败 SHALL 经统一异常处理返回 JSON `{"code": <错误码>, "message": <可读信息>}` 与对应 HTTP 状态码，MUST NOT 暴露内部堆栈。受保护端点（`/chat`、`/stream`、`/sessions` 等）SHALL 经过治理依赖链；`/health` SHALL 免鉴权。

#### Scenario: 治理错误统一为 JSON

- **WHEN** 任一治理检查失败（鉴权/环境/限流）
- **THEN** 响应为 JSON `{"code","message"}` 且状态码与错误类型匹配；`GET /health` 始终可匿名访问

### Requirement: 服务间通知鉴权

`POST /internal/notify` SHALL 经服务间鉴权：`disabled` 模式放行，其余模式校验 `X-Api-Key`，失败返回 401 `AUTH`。

#### Scenario: 通知端点缺少服务凭证

- **WHEN** 非 disabled 模式下回调 `/internal/notify` 未携带有效 `X-Api-Key`
- **THEN** 系统返回 401，通知不被处理
