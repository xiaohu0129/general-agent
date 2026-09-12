## MODIFIED Requirements

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

## ADDED Requirements

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
