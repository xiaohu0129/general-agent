## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: 令牌桶限流

系统 SHALL 按身份维度（`user:env`）执行内存令牌桶限流（可配每秒补充速率与桶容量）；超过限流 SHALL 返回 429 `RATE_LIMIT`，并记录限流 metric 与审计日志。限流可通过配置关闭。

限流状态在内存中 SHALL 有界：系统 SHALL 周期性清扫长期无请求身份的计数状态（如随每次判定懒清理超过窗口未活跃的 key），MUST NOT 随身份数无限增长。

#### Scenario: 突发超限被限流

- **WHEN** 某身份在短时间内的请求超过桶容量且令牌未及补充
- **THEN** 系统返回 429 `RATE_LIMIT`，并记录一次限流命中

#### Scenario: 长期不活跃身份的限流状态被清扫

- **WHEN** 某限流 key 对应的身份在超过一个完整补充窗口以上没有任何请求
- **THEN** 其桶计数状态可被清除，进程内存不累积全部历史身份的限流条目
