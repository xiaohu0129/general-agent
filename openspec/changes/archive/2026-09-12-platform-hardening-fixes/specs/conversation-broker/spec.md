## MODIFIED Requirements

### Requirement: 会话事件中枢

系统 SHALL 为每个会话维护一个事件中枢（Broker），所有事件（对话轮次事件与异步通知）经统一入口分配单调递增 `eventSeq`、写入该会话的环形缓冲（ring buffer，容量可配，**默认 2048**——token 级 `turn_delta` 下旧默认 256 不足一个长回复的续传窗口）并扇出（fan-out）给当前所有订阅者队列。`POST /chat` 与 `GET /stream` SHALL 均为订阅者，区别仅在过滤策略。

环形缓冲 SHALL 保留最近 N 条事件作为续传窗口；系统 SHALL 支持按游标重放 `eventSeq > after_seq` 的事件。

会话级事件状态（序号计数、ring buffer、订阅集合）SHALL 被有界回收：当某会话**超过可配置空闲时长（`broker.session_ttl`，默认 86400 秒）没有新事件且当前无订阅者**时，系统 SHALL 清除其内存状态；该会话再次产生事件时按新会话从 seq=1 重新开始（持久化的消息历史不受影响，断线续传仅承诺 TTL/ring 容量内的窗口）。存在活跃订阅者或空闲计时未超时的会话 MUST NOT 被回收。

#### Scenario: 事件统一编号并扇出

- **WHEN** 某会话同时存在 `POST /chat` 与 `GET /stream` 两个订阅者，且一轮对话产出事件
- **THEN** 每个事件被分配递增 `eventSeq`、写入 ring buffer，并投递给两个订阅者

#### Scenario: 按游标重放续传窗口

- **WHEN** 请求重放游标 `after_seq` 之后的事件
- **THEN** 系统返回 ring buffer 中所有 `eventSeq > after_seq` 的事件；窗口之外的事件不可重放

#### Scenario: 空闲无订阅的会话状态被回收

- **WHEN** 某会话最后一个订阅者退出后，在超过 `session_ttl` 的时间内没有新事件
- **THEN** 该会话的 ring/序号内存状态被清除，长期运行的进程内存不随历史会话数无限增长；该会话之后再来事件时正常重建状态

#### Scenario: 活跃会话不被回收

- **WHEN** 某会话仍有订阅者，或距上次事件未超过空闲时长
- **THEN** 其 ring buffer 与订阅集合保持不变，续传窗口不丢失

### Requirement: 异步任务通知接入

系统 SHALL 提供 `POST /internal/notify` 接收业务系统异步任务完成通知（消息体含 `sessionId`、`taskId`、`status`、可选 `message`/`traceId`），并将其作为 `notification` 事件经事件中枢扇出、写入 ring buffer；该端点 SHALL 经服务间鉴权（API Key）保护。请求字段 SHALL 有界：`sessionId`/`taskId`/`status` SHALL 为非空字符串，`status` 长度 MUST NOT 超过 32，`message` 长度 MUST NOT 超过 2000；越界 SHALL 返回 400 `VALIDATION` 且通知不入缓冲。

#### Scenario: 离线完成的通知入缓冲

- **WHEN** 业务系统在无活跃订阅者时回调 `POST /internal/notify`
- **THEN** 通知作为 `notification` 事件分配 `eventSeq` 并写入 ring buffer，客户端下次重连时可重放获取

#### Scenario: 超长通知字段被拒

- **WHEN** 回调的 `status` 超过 32 字符或 `message` 超过 2000 字符
- **THEN** 系统返回 400 `VALIDATION`，不产生事件、不写 ring buffer
