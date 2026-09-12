## ADDED Requirements

### Requirement: MySQL 连接保活

系统 SHALL 以短于 MySQL 服务端 `wait_timeout` 的回收周期重建连接池中的连接（`mysql.pool_recycle` 可配，默认 1800 秒），避免长时间空闲后拿到被服务端静默关闭的死连接，导致隔夜/长空闲后的首个请求失败。

#### Scenario: 空闲超过服务端超时后首请求不失败

- **WHEN** 服务长时间无流量（超过 MySQL `wait_timeout`）后收到首个请求
- **THEN** 连接池提供的是回收周期内建立的存活连接，请求正常执行而不出现连接失效错误

## MODIFIED Requirements

### Requirement: 多实例与 HTTPS 演进前置

启用多实例高可用前 SHALL 配置 Redis 以支撑跨实例事件中枢，并 MUST 另行实现 Redis 版登录态存储（当前登录态仅内存实现）；跨实例事件中枢与 Redis 登录态 SHALL 经独立变更交付并补齐集成测试，当前版本 MUST NOT 附带未装配、接口不兼容的半成品分布式实现（曾存在的未接线 RedisBroker 已移除，其五个已知缺陷随移除消除）。上 HTTPS 后 SHALL 将会话 cookie 的 `Secure` 位置为 true（cookie 仅经 TLS 传输）。

#### Scenario: 升级多实例的前置条件

- **WHEN** 部署者需要多副本高可用
- **THEN** 须先经独立变更交付 Redis 事件中枢与 Redis 版登录态（含 replay 同步/异步签名一致、跨实例事件不重复投递、监听断线重连、连接关闭与 key TTL），之后方可水平扩展后端并在 Nginx 配置多后端 upstream

#### Scenario: 启用 HTTPS 后加固 cookie

- **WHEN** 站点通过 HTTPS 提供服务
- **THEN** 会话 cookie `Secure` 置为 true，cookie 不经过明文 HTTP 传输
