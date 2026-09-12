## MODIFIED Requirements

### Requirement: 观测可配置与健康检查暴露

系统 SHALL 支持配置观测开关、服务名、部署环境、console/OTLP 导出与采样率；未配置 OTLP 端点时仅 console 导出、不导出 OTLP。`GET /health` SHALL 返回观测状态（是否启用、OTLP 端点、是否已初始化）。观测关闭时 MUST NOT 干扰正常请求与流式传输。

`GET /health` SHALL 给出聚合状态字段 `status`：

- 全部检查通过时为 `"ok"`；
- 当"配置了 Redis 但连通性探测失败"或"路由处于故障降级状态（索引构建失败的 `degraded` 模式；主动配置 stub 走 rule+keyword 不算降级）"时为 `"degraded"`；
- 两种情况下 HTTP 状态码 SHALL 均为 200（该端点表达存活与配置状态，不因降级返回 5xx）；就绪探针可据 `status=="ok"` 判定是否接流量。

健康检查自身的探测失败 MUST NOT 抛出异常或拖垮端点（探测异常按对应检查项降级展示）。

#### Scenario: 健康检查报告观测状态

- **WHEN** 调用 `GET /health`
- **THEN** 响应含 `observability.{enabled, otlp_endpoint, initialized}` 字段

#### Scenario: 关闭观测不影响流式

- **WHEN** `observability.enabled=false`
- **THEN** 应用不安装 instrumentation，SSE 流式对话与测试传输正常工作

#### Scenario: 已配置 Redis 不可达时降级

- **WHEN** 配置了 Redis 地址但健康探测 ping 失败
- **THEN** `GET /health` 仍返回 HTTP 200 且 `status="degraded"`，响应体标明 Redis 检查为 error

#### Scenario: 路由索引构建失败时降级

- **WHEN** `routing.enabled=true` 但语义索引构建失败、路由处于 `degraded` 模式（主动使用 stub 关键词路不算）
- **THEN** `GET /health` 返回 `status="degraded"` 且 routing 状态可被识别为降级；全部正常（含主动使用 stub）时为 `"ok"`
