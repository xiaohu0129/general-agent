## ADDED Requirements

### Requirement: 澄清选项卡片渲染与点击回发

Web 前端 SHALL 将澄清消息渲染为可点选项卡片：识别 `clarify` SSE 事件与历史回放消息中的 `options:[{label,value}]`，把澄清 assistant 消息渲染为"澄清文本 + 选项按钮列表"（按钮展示 label）；`value` 对前端为不透明字符串，前端 MUST NOT 解析或拼装它，仅原样暂存用于回传。澄清文本 SHALL 以 `turn_delta` 流式文本为准（`clarify` 事件的 `question` 字段与 turn_delta 同源，前端 MUST NOT 重复渲染）。用户点选选项时，前端 SHALL 在下一轮 `POST /chat` 请求中携带 `message`（填所选 label，照常展示为用户气泡）与 `clarify_selection:{value}`（机器值）；用户手打输入时 SHALL NOT 携带 `clarify_selection`。选项卡片在点选发送后 SHALL 标记已选/禁用（乐观显示；权威已选态以下一轮历史回放的 `selected` 为准，不一致时以回放覆盖）。点选交互 SHALL 等到该轮 `turn_end` 事件后才启用；该轮以 `error` 事件收尾、用户停止生成或连接断开且未恢复时，选项 SHALL 保持禁用（不出现可点但点了必然无效的状态）。同一会话后续澄清轮到达时，此前未消费的旧选项卡片 SHALL 整体禁用（其 value 已不在最新候选方向内，点击会被后端忽略）；**新一轮次的 `turn_start` 到达同样 SHALL 作废此前所有未 selected 的 live 澄清卡片**（用户停止后立即重发时，旧轮迟到的 `clarify`/`turn_end` 经 `/stream` 到达 MUST NOT 复活已作废卡片）。不识别 `clarify` 事件、消息无 options 或 options 为空数组的路径 SHALL 降级为纯文本澄清呈现，MUST NOT 报错、MUST NOT 渲染空卡片。

#### Scenario: 澄清消息渲染为可点选项卡片

- **WHEN** 路由产出澄清并下发 `clarify` 事件（含 question 与 options）
- **THEN** 前端渲染澄清文本与选项按钮列表（label 可读、value 不解析、文本不重复渲染），该轮不出现工具调用卡片

#### Scenario: 点击选项以约定字段回发

- **WHEN** 用户点击选项卡片中某一项（label「退款」、value `category:refund`）
- **THEN** 前端发起的下一轮请求 `message` 为该 label（照常作为用户气泡展示），并携带 `clarify_selection:{value:"category:refund"}`；发送后该卡片标记已选/禁用

#### Scenario: turn_end 前选项不可点

- **WHEN** 澄清轮仍在进行（已收到 `clarify` 事件但未收到 `turn_end`），用户尝试点选
- **THEN** 选项卡片保持禁用/loading 态不发起请求，点选交互在 `turn_end` 到达后才启用

#### Scenario: 异常收尾轮次选项保持禁用

- **WHEN** 澄清轮以 `error` 事件收尾、用户点"停止生成"、或连接断开且 `/stream` 未能恢复该轮事件
- **THEN** 该轮选项卡片保持禁用不发起请求；该轮 assistant 行已落库时，刷新后据历史回放还原（无 `selected` 时选项恢复可点）；error 轮不保证落库，未落库时刷新后历史中无该卡片（MUST NOT 报错）

#### Scenario: 停止后立即重发，旧轮迟到事件不复活旧卡片

- **WHEN** 用户在澄清轮点"停止生成"后立即发送新消息（新轮 `turn_start` 已到达），旧轮 producer 随后才经 `/stream` 补来迟到的 `clarify`/`turn_end`
- **THEN** 旧澄清卡片保持禁用/过期态不被迟到事件启用，仅最新一轮的卡片可交互；迟到事件不新建气泡、不改变新轮气泡

#### Scenario: 新澄清轮到达禁用旧卡片

- **WHEN** 用户对上一轮澄清回答"随便吧"仍无法裁决，系统再次产出澄清（新卡片到达）
- **THEN** 旧选项卡片整体禁用（提示已过期），仅最新卡片的选项可点

#### Scenario: 手打输入不携带回传字段

- **WHEN** 澄清轮结束后用户不点选、直接在输入框手打消息发送
- **THEN** 请求仅含 `message`（用户原话），不携带 `clarify_selection`；新轮 `turn_start` 到达后旧澄清卡片标记过期禁用（新轮若再次产出澄清，则以新卡片为准）

#### Scenario: 空 options 降级纯文本

- **WHEN** 澄清消息的 options 缺失或为空数组（旧后端、clarify_options 关闭、或无候选方向）
- **THEN** 前端按纯文本 assistant 气泡渲染，不报错、不出空卡片

### Requirement: 澄清选项历史还原

Web 前端 SHALL 在加载会话历史时还原澄清选项卡片：历史消息 DTO 中带非空 options 的澄清消息渲染为选项卡片，已带 `selected`（已选 value）的 SHALL 标记对应项为已选/禁用（不可重复点选）；`selected` 缺失的澄清消息选项保持可点（续聊场景）。`selected` 值不在当前 options 列表内（脏数据）时 SHALL 将该卡片全部选项禁用（MUST NOT 崩溃）。存量无 options/meta 的历史消息 SHALL 按普通文本消息渲染，MUST NOT 报错。

#### Scenario: 刷新后还原选项卡片与已选态

- **WHEN** 用户刷新页面或换设备加载含澄清轮的历史消息，其中某澄清消息带 options 且 `selected=category:refund`
- **THEN** 前端还原该澄清卡片并标记「退款」项已选/禁用，其余项同样禁用；未 selected 的澄清消息选项保持可点

#### Scenario: selected 脏数据全禁用

- **WHEN** 历史回放的澄清消息 `selected` 值不在其 options 的 value 集合内
- **THEN** 前端将该卡片全部选项禁用，不崩溃、不误标某项为已选

#### Scenario: 存量历史消息正常渲染

- **WHEN** 历史消息中存在改动前的无 options 澄清消息（或普通消息）
- **THEN** 前端按普通文本气泡渲染，不因缺少 options/meta 字段报错

### Requirement: GET /stream 持久通道前端接入

Web 前端 SHALL 为当前会话维持一条 `GET /stream` 持久 SSE 连接（EventSource，携带 sessionId）：连接按会话切换重建，effect cleanup MUST 关闭旧连接（开发态 StrictMode 双挂载不得残留双连接）。因后端每类事件都带 SSE `event:` 字段，前端 SHALL 对各事件名显式 `addEventListener`（turn_start/turn_delta/turn_end/tool_start/tool_end/clarify/error/notification），MUST NOT 仅依赖 `onmessage`（命名事件不会投递到它）。浏览器 SHALL 经 `Last-Event-ID` 头自动续传（服务端已支持重放）。业务 `error` 事件（MessageEvent，含 data）与连接错误（无 data，伴随 readyState 变化）同名，前端 SHALL 区分且 MUST NOT 把连接错误送入事件应用层；`onerror` 中 readyState 为 `CLOSED`（服务端返回非可接受状态、浏览器放弃重连）时 SHALL 主动关闭并降级为仅 `POST /chat` 即时流（不报错、不阻断发送框，401 由下一次 REST 请求的既有流程收敛），readyState 为 `CONNECTING` 时交由浏览器原生重试（仅记日志）。

`/stream` 事件 SHALL 与 `POST /chat` 即时流汇入同一事件应用函数，按 `(turnId, eventSeq)` 幂等应用——同一事件 MUST NOT 被重复应用（文本不重复拼接、卡片状态不重复翻转、工具卡不重复插入）；`/stream` 到达但即时流未见过的事件（用户停止生成或断线后由后端 producer 继续产生）SHALL 被继续应用到对应消息。事件应用 SHALL 做来源对账：历史快照还原的消息（restored）对其 turnId 的全部事件整事件忽略；不命中任何现有消息的未知 turnId 事件 SHALL 忽略且 MUST NOT 新建气泡（首连 ring 全量重放的更早轮次、他标签页轮次均落入此分支）；无 turnId 的 notification 事件 SHALL NOT 进入消息模型。

前端 SHALL 维护**会话级** seq 高水位：所有带 eventSeq 的事件（含被忽略不渲染的 notification——它同样占用 broker seq）都推进高水位；无 id 的心跳注释行不参与。重放/接收事件出现 seq 严格跳变（`seq > maxSeenSeq+1`）且此前有过事件时，SHALL 判定 ring 缺口并触发一次会话历史静默重拉合并（不 abort 活动轮、不整体替换消息、保留本页 live 气泡），活动轮进行中 SHALL 延后到该轮收尾后执行，MUST NOT 静默错乱、MUST NOT 在首次连接（高水位初始）误报。`/stream` 连接失败或断开 SHALL NOT 阻断 `POST /chat` 即时流与发送框。

#### Scenario: 双通道事件幂等应用

- **WHEN** 同一 `turn_delta` 事件既经 `POST /chat` 即时流到达、又经 `/stream` 到达（相同 turnId + eventSeq）
- **THEN** 前端只应用一次，澄清文本不重复拼接，乱序到达（/stream 先到）结果相同

#### Scenario: 停止生成后经 /stream 补齐轮次

- **WHEN** 用户点"停止生成"中断 `POST /chat` 即时流，后端 producer 继续跑完该轮并产生后续 `turn_delta`/`clarify`/`turn_end`
- **THEN** 前端经 `/stream` 继续接收并应用这些事件，气泡文本补齐、澄清卡片渲染、`turn_end` 后选项恢复可点

#### Scenario: 打开历史会话时 ring 重放不重复渲染

- **WHEN** 打开/刷新一个已有会话：历史消息先按快照还原（含 turnId=T1 的气泡与澄清卡片），EventSource 首连无 Last-Event-ID，后端 replay ring 中 T1 的 turn_delta/clarify/turn_end
- **THEN** T1 的重放事件因命中 restored 消息被整事件忽略，文本不重复拼接、不出现第二张卡片、工具卡不重复；当前页面新发起轮次（live）的事件仍正常应用

#### Scenario: 未知 turnId 事件忽略且不建气泡

- **WHEN** `/stream` 到达的事件 turnId 在当前消息列表中不存在（ring 中更早轮次、其他标签页发起的轮次）
- **THEN** 前端忽略该事件、不新建消息气泡、不报错；其 eventSeq 仍推进会话高水位

#### Scenario: 命名事件监听与连接错误区分

- **WHEN** 后端经命名 SSE 事件（`event: clarify`）下发澄清，随后连接断开触发连接级 error
- **THEN** clarify 经按名监听到达并渲染；连接 error（无 data）不进入事件应用、不渲染为错误气泡；readyState 为 CLOSED 时连接关闭并降级为仅即时流，发送与即时流对话可用

#### Scenario: notification 占用 seq 不造成伪缺口

- **WHEN** 相邻两个 turn 事件（seq=10、12）之间夹一条不渲染的 notification（seq=11）
- **THEN** 高水位随 notification 推进到 11，收到 seq=12 时不误判缺口、不触发历史重拉

#### Scenario: /stream 不可用不阻断对话

- **WHEN** `/stream` 连接失败（后端不可用/网络断开）
- **THEN** `POST /chat` 发送与即时流不受影响；澄清卡片照常经即时流渲染（断线续传能力缺失，靠刷新兜底），不报错

#### Scenario: ring buffer 溢出触发静默历史重拉

- **WHEN** 断线时间过长（非首次连接），`/stream` 重连重放的事件 `eventSeq` 相比会话高水位出现跳变（缺口超出 ring 容量）
- **THEN** 前端检测到缺口并触发一次该会话历史静默重拉合并（不 abort 进行中的即时流、不清空 live 气泡），UI 不出现事件错乱；若活动轮进行中，重拉延后到该轮收尾后执行

### Requirement: POST /chat SSE 帧解析兼容标准分隔符

Web 前端的 `POST /chat` fetch 流解析 SHALL 兼容 SSE 规范的全部空行分隔形式（CRLF `\r\n\r\n`、LF `\n\n`、CR `\r\r`；sse-starlette 服务端默认输出 CRLF），MUST NOT 仅按 `\n\n` 切分；解析 SHALL 正确处理单帧跨多个网络 chunk（含多字节 UTF-8 字符跨 chunk 边界）与多帧合并在同一 chunk 到达。

#### Scenario: CRLF 帧在流式期间逐帧派发

- **WHEN** 后端以默认 CRLF 分帧的 SSE 字节（`id:`/`event:`/`data:` 行以 `\r\n` 结束、事件间 `\r\n\r\n`）分多个 chunk 到达
- **THEN** 每个事件在到达时即被解析派发（不等待流结束），event/data 与 LF 帧结果一致，eventSeq（id 行或 data 字段）可用于幂等
