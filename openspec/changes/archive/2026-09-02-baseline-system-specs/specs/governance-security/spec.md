## Purpose

为服务提供多租户治理与安全护栏：可配置的鉴权模式、环境白名单隔离、令牌桶限流、凭证脱敏与审计留痕，对受保护端点统一执行"鉴权 -> 环境校验 -> 限流"，并以统一 JSON 错误响应失败，防止未授权访问、跨环境误调与凭证泄露。

## ADDED Requirements

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

### Requirement: 令牌桶限流

系统 SHALL 按身份维度（`user:env`）执行内存令牌桶限流（可配每秒补充速率与桶容量）；超过限流 SHALL 返回 429 `RATE_LIMIT`，并记录限流 metric 与审计日志。限流可通过配置关闭。

#### Scenario: 突发超限被限流

- **WHEN** 某身份在短时间内的请求超过桶容量且令牌未及补充
- **THEN** 系统返回 429 `RATE_LIMIT`，并记录一次限流命中

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
