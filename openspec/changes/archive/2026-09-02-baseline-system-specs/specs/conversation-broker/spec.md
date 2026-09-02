## Purpose

提供会话级事件中枢与长任务稳定性保障：解耦 Agent 执行与 SSE 推送，通过心跳保活、eventSeq 环形缓冲续传、订阅扇出与异步通知多路复用，确保长时任务不掉单、断线可续传，并串行化同一会话的并发轮次。

## ADDED Requirements

### Requirement: 会话事件中枢

系统 SHALL 为每个会话维护一个事件中枢（Broker），所有事件（对话轮次事件与异步通知）经统一入口分配单调递增 `eventSeq`、写入该会话的环形缓冲（ring buffer，容量可配，默认 256）并扇出（fan-out）给当前所有订阅者队列。`POST /chat` 与 `GET /stream` SHALL 均为订阅者，区别仅在过滤策略。

环形缓冲 SHALL 保留最近 N 条事件作为续传窗口；系统 SHALL 支持按游标重放 `eventSeq > after_seq` 的事件。

#### Scenario: 事件统一编号并扇出

- **WHEN** 某会话同时存在 `POST /chat` 与 `GET /stream` 两个订阅者，且一轮对话产出事件
- **THEN** 每个事件被分配递增 `eventSeq`、写入 ring buffer，并投递给两个订阅者

#### Scenario: 按游标重放续传窗口

- **WHEN** 请求重放游标 `after_seq` 之后的事件
- **THEN** 系统返回 ring buffer 中所有 `eventSeq > after_seq` 的事件；窗口之外的事件不可重放

### Requirement: 背压不阻塞执行

订阅者队列满时，系统 SHALL 采用非阻塞投递：丢弃该订阅者的本次事件（环形缓冲仍全量保留），MUST NOT 阻塞事件生产方导致整轮卡死；丢弃事件可由该订阅者经 `Last-Event-ID` 重放补齐。

#### Scenario: 慢订阅者不拖累执行

- **WHEN** 某订阅者队列已满（容量可配，默认 1024）而新事件到达
- **THEN** 系统丢弃该订阅者的本次事件并记录，事件生产与其他订阅者不受影响，被丢事件仍保留在 ring buffer

### Requirement: POST /chat 生产消费解耦与心跳

`POST /chat` SHALL 在独立任务中运行 Agent 轮次（producer），与 SSE 推送（consumer）解耦；producer MUST NOT 随 SSE 客户端断开而取消——即使前端断开，轮次仍须跑完并持久化，事件入 ring buffer 供续传。

consumer 在无事件可发、超过心跳间隔（默认 15s）时 SHALL 下发 SSE 注释行心跳（`: heartbeat`），以刷新链路 idle 计时。`POST /chat` SHALL 仅转发当前 `turnId` 的事件并在 `turn_end`/`error` 后结束该 SSE 响应。

系统 SHALL 同步持有 producer 任务引用直到其结束，避免任务被垃圾回收。

#### Scenario: 客户端断开后轮次仍完成

- **WHEN** 客户端在轮次进行中断开 SSE 连接
- **THEN** producer 继续运行至轮次结束并持久化消息，事件写入 ring buffer，客户端后续可经 `GET /stream` 续传获取

#### Scenario: 空闲时下发心跳

- **WHEN** 一轮对话中长时间无事件（如工具执行期间）
- **THEN** consumer 按心跳间隔下发 SSE 注释行，连接不被链路当作空闲掐断

### Requirement: GET /stream 持久通道与重放

`GET /stream` SHALL 提供按 `sessionId` 的持久 SSE 通道，下发该会话全部事件（对话轮次 + 通知）；连接建立时 SHALL 先重放 ring buffer 中 `eventSeq > Last-Event-ID` 的历史事件，再转入实时事件，并去重重放边界。

`Last-Event-ID` SHALL 优先取查询参数 `lastEventId`，否则取标准 `Last-Event-ID` 请求头；格式非法时 SHALL 返回 400。

#### Scenario: 重连后先补历史再收实时

- **WHEN** 客户端携 `Last-Event-ID: N` 连接 `GET /stream`
- **THEN** 系统先下发 ring 中 `eventSeq > N` 的事件，再无缝转发后续实时事件，且不重复下发已重放的序号

### Requirement: 异步任务通知接入

系统 SHALL 提供 `POST /internal/notify` 接收业务系统异步任务完成通知（消息体含 `sessionId`、`taskId`、`status`、可选 `message`/`traceId`），并将其作为 `notification` 事件经事件中枢扇出、写入 ring buffer；该端点 SHALL 经服务间鉴权（API Key）保护。

#### Scenario: 离线完成的通知入缓冲

- **WHEN** 业务系统在无活跃订阅者时回调 `POST /internal/notify`
- **THEN** 通知作为 `notification` 事件分配 `eventSeq` 并写入 ring buffer，客户端下次重连时可重放获取

### Requirement: 同会话轮次串行化

系统 SHALL 按 `sessionId` 对并发轮次加互斥：同一会话的 producer SHALL 在锁内运行（后轮等待前轮持久化完成后才载入历史），不同会话互不阻塞，以避免消息行交错与工具消息跨轮次配对。锁注册表 SHALL 以引用计数方式回收，保证"锁释放瞬间仍有等待者"时不会误删锁导致互斥失效。

在 Web 登录模式下，会话行的创建 SHALL 在 producer 内、锁内执行，使"建会话 + 跑轮次"必定完成，消除请求极早断开留下空孤儿会话的窗口。

#### Scenario: 停止后立即重发不交错

- **WHEN** 用户在一轮进行中断开（停止生成）后立刻对同一会话发送新消息
- **THEN** 后轮在前轮 producer 跑完并落库后才载入历史，`agent_message` 行顺序正确、无跨轮次工具配对

#### Scenario: 极早断开不留空会话

- **WHEN** 新会话请求在 producer 运行前即断开
- **THEN** 因会话行由不随断开取消的 producer 在锁内创建，不会产生无任何消息的空会话记录
