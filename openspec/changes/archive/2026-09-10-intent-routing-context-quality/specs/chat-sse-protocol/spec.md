## ADDED Requirements

### Requirement: 澄清选项事件与结构化回传

系统 SHALL 在路由产出澄清选项时，于 SSE 事件流中下发结构化选项，使前端可渲染可点选项并形成确定性闭环：在既有事件类型之外新增 `clarify` 事件，其 `data` 除公共字段 `turnId`/`traceId`/`eventSeq` 外 SHALL 含澄清文本 `question` 与选项数组 `options: [{label, value}]`。`label` 为人类可读展示文案；`value` 为确定性可路由值，MUST 带类型前缀以消歧：`category:<名>`（收窄到该技能域全部工具）或 `skill:<名>`（收窄到单个 Skill），`<名>` 均为当前 env 候选集内合法标识。`value` 对前端为不透明字符串，前端 MUST NOT 解析或拼装它，只原样回传。澄清轮 SHALL 仍以 `turn_start .. turn_delta(澄清文本) .. clarify(选项) .. turn_end{finishReason:"stop"}` 收尾，MUST NOT 构建 ReAct 图或调用业务工具。`clarify` 事件 SHALL 携带单调递增 `eventSeq` 并写入断线续传 ring buffer，客户端以 `Last-Event-ID` 重连时 SHALL 随缺失事件一并重放（含 options）。

系统 SHALL 定义选项回传的上行约定：用户点选选项时，前端在下一轮 `POST /chat` 请求中 SHALL 同时携带——① 必填 `message` 字段填所选项的 **label（人类可读文案）**（与手打路径同构，照常作为用户消息落库并进入模型上下文）；② 专用可选字段 `clarify_selection: {value}` 填所选项的带前缀机器值（原样回传，MUST NOT 用 label 代替、MUST NOT 进入 message）。未点选、手打输入时 `message` 为用户原话且 MUST NOT 携带 `clarify_selection`。该字段为可选新增，不识别 options 的旧客户端不携带时系统回落文本澄清闭环。协议字段 MUST 全部向后兼容：旧前端忽略未知 `clarify` 事件类型与 options 字段时，仍能凭 `turn_delta` 文本呈现纯文本澄清（等价改动前行为）。

#### Scenario: 澄清事件携带结构化选项

- **WHEN** 路由低置信产出澄清且澄清选项开启
- **THEN** 系统在澄清轮下发 `clarify` 事件，data 含 `question` 文本与 `options:[{label,value}]` 及公共 `turnId`/`traceId`/`eventSeq`，该轮不出现工具调用事件并以 `turn_end{finishReason:"stop"}` 收尾

#### Scenario: 断线重连重放澄清选项事件

- **WHEN** 客户端在收到含 `clarify` 事件（`eventSeq=N`）后断开，并以 `Last-Event-ID: N-1` 重连持久通道
- **THEN** 系统重放窗口内 `eventSeq > N-1` 的事件时包含该 `clarify` 事件及其 options，前端可据此还原选项卡片

#### Scenario: 旧前端忽略选项降级为纯文本澄清

- **WHEN** 不识别 `clarify` 事件/options 字段的旧客户端接收澄清轮
- **THEN** 客户端忽略未知事件，仍凭 `turn_delta` 澄清文本正常展示，轮次正常 `turn_end`，不报错；其后续消息不携带回传字段，系统按文本闭环处理

#### Scenario: 点选选项以专用字段回传

- **WHEN** 用户点击澄清选项卡片中某一项（label「退款」、value `category:refund`）
- **THEN** 前端在下一轮请求中 `message` 填该 label（"退款"，照常落库/进模型上下文），并携带 `clarify_selection:{value:"category:refund"}`；value 原样回传、不进 message；用户手打时 `message` 为原话且不携带该字段
