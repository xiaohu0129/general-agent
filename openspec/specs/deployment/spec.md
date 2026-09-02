# deployment Specification

## Purpose
定义 general-agent 的单机生产部署形态与运维约束：单实例后端 + Nginx 托管前端 + 外部 MySQL + 真实 LLM 的 Docker Compose 部署，约定配置覆盖优先级、SSE 反向代理要点、数据持久化边界，以及向 HTTPS 与多实例高可用演进的前置条件，保证部署后 SSE 长连接、登录态与数据持久化正确工作。

## Requirements

### Requirement: 单机 Docker Compose 部署形态

系统 SHALL 提供单机部署编排：后端服务（运行 Agent，端口 9093，仅内部暴露）+ 前端服务（Nginx 托管 SPA 静态资源并反向代理 API/SSE），依赖外部 MySQL（用户/会话/消息三表惰性自建）与外部 OpenAI 兼容 LLM。浏览器经 Nginx 同源访问，登录 cookie 为第一方。

#### Scenario: 一键启动并健康检查

- **WHEN** 配置好环境变量（LLM 端点、MySQL 连接）后执行 `docker compose up -d --build`
- **THEN** 后端与前端启动，经 Nginx 访问 `/health` 返回 `status:ok`，浏览器可注册、登录并对话

### Requirement: 配置覆盖优先级

系统配置 SHALL 按"环境变量 > .env 文件 > config.yaml > 内置默认值"优先级合并；环境变量使用 `AGENT_` 前缀与 `__` 嵌套分隔符（如 `AGENT_MYSQL__HOST`、`AGENT_SECURITY__SESSION__TTL_HOURS`）。LLM `base_url` MUST NOT 含 `/v1` 后缀。

#### Scenario: 环境变量覆盖配置文件

- **WHEN** compose 中设置 `AGENT_SECURITY__SESSION__TTL_HOURS=168` 而 config.yaml 为其他值
- **THEN** 运行时生效值为环境变量的 168

### Requirement: SSE 反向代理要点

反向代理对 `/chat`、`/stream` SHALL 关闭响应缓冲（`proxy_buffering off`）、使用 HTTP/1.1、清空 `Connection` 头并设置长于心跳间隔（15s）的读取超时（如 3600s），否则流式输出会被缓冲或长连接被掐断。Nginx SHALL 对 SPA 路由做 `try_files` 回退到 `index.html`。

#### Scenario: 长连接不被代理掐断

- **WHEN** 浏览器经 Nginx 建立 `/stream` 长连接且期间仅有 15s 心跳
- **THEN** 连接保持不被代理 idle 超时关闭，事件实时送达

### Requirement: 单实例约束

当前部署 MUST 为单实例后端（单 uvicorn 进程、不得多 worker/多副本）：内存事件中枢与内存登录态不跨进程共享，多实例会导致 `/stream` 收不到事件、登录态不共享。业务数据 SHALL 全部持久化于外部 MySQL，容器无状态；登录态在内存中，后端重启后用户需重新登录。

#### Scenario: 多副本导致功能异常的约束说明

- **WHEN** 部署者尝试运行多个后端副本/多 worker
- **THEN** 该配置不被支持（内存 Broker/登录态不共享）；文档明确要求单实例，数据本身在外部 MySQL 不丢失

### Requirement: 多实例与 HTTPS 演进前置

启用多实例高可用前 SHALL 配置 Redis 以支撑跨实例事件中枢，且 MUST 补充 Redis 版登录态存储（当前登录态仅内存实现，接口已预留 create/get/touch/revoke）。上 HTTPS 后 SHALL 将会话 cookie 的 `Secure` 位置为 true（cookie 仅经 TLS 传输）。

#### Scenario: 升级多实例的前置条件

- **WHEN** 部署者需要多副本高可用
- **THEN** 须先接入 Redis（事件中枢）并实现 Redis 版登录态，之后方可水平扩展后端并在 Nginx 配置多后端 upstream

#### Scenario: 启用 HTTPS 后加固 cookie

- **WHEN** 站点通过 HTTPS 提供服务
- **THEN** 会话 cookie `Secure` 置为 true，cookie 不经过明文 HTTP 传输
