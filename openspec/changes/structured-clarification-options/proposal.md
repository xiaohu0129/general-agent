## Why

后端 change `intent-routing-context-quality`（D11）已定稿并实现**结构化澄清选项协议**：路由低置信澄清时下发 `clarify` SSE 事件（含 `question` 与 `options:[{label,value}]`）、选项随断线续传（eventSeq ring buffer）与会话历史 API 回放、用户点选经 `POST /chat` 的 `clarify_selection:{value}` 回传后由后端确定性收窄。但前端目前只渲染文本 delta：

1. **硬闭环对用户不可达**：协议已下发 options，前端没有可点选项卡片，用户仍只能自由打字（"那个退款的吧"），确定性通道闲置。
2. **刷新后选项无法还原**：历史消息 API 已回放 options/已选 value，但前端消息模型无对应结构，刷新/换设备后澄清卡片退化为纯文本、已选项无标记。
3. **缺点击回发通道**：前端没有把点选编码为 `clarify_selection` 上行字段的逻辑。
4. **前端无持久通道消费**：`front/` 只消费 `POST /chat` 的单轮 fetch 流，`GET /stream` 持久通道（断线续传/事件重放）没有接入代码——断线或用户"停止生成"后，后端 producer 照常跑完产生的事件（含 `clarify`）前端收不到，只能靠刷新兜底（docs/02 已标注"本期未接入，作为后续增强点"）。不接入 /stream，`clarify` 事件"随断线续传重放"的协议能力对 Web 用户是空谈。
5. **既有 SSE 分块解析与真实帧不兼容（探索阶段实证）**：sse-starlette（约束 `>=2.2`，实测 3.4.8）默认输出 CRLF 帧（`...\r\n\r\n`），而 `front/src/api/chat.ts` 以 `indexOf("\n\n")` 分块——CRLF 帧流式期间永不切分、结束后整段 JSON.parse 失败被静默丢弃。clarify 与既有六事件共用该解析器，不前置修复，卡片通道在字节层即不可达；且 mock 事件对象的测试测不出此层缺陷。
6. **首连重放与历史快照必然重复**：EventSource 首连无 `Last-Event-ID`，后端按 `last=0` 全量 replay ring（≤256 条，含已完成历史轮次）；历史 DTO 不带 eventSeq，还原消息却带持久化 turnId。若按 turnId 直接归位，每次打开/刷新历史会话都会把重放的 delta/卡片再应用一遍（重复文本、重复卡片、工具卡重复、状态翻转）。

**依赖关系**：本 change 为纯前端，**depends-on `intent-routing-context-quality`**——只消费其定稿的 `clarify` 事件、options/selected 回放结构与 `clarify_selection` 回传字段，以及既有 `GET /stream` 持久通道（conversation-broker 已交付）；不定义协议、不改任何后端文件。后端协议 additive 上线后旧前端忽略即降级为纯文本澄清，故本 change 可独立排期、后随发版。

## What Changes

- **SSE 分块解析前置修复（D10）**：`streamChat` 的帧分隔兼容 SSE 规范的 CRLF/LF/CR（现状只认 `\n\n`，与 sse-starlette 默认 CRLF 帧不兼容）；补字节流级测试（三种分隔、多帧单 chunk、单帧跨 chunk、CJK 多字节跨 chunk），以真实分块而非 mock 事件对象驱动。
- **消息模型扩展**：前端会话/消息类型识别澄清选项——`clarify` 事件携带的 `options:[{label,value}]` 与历史回放消息中的 options/已选 `selected` 映射为前端消息结构（旧消息/无 options 消息不受影响）；消息同时标来源 `source: live|restored`（本页乐观气泡 vs 历史快照），作为重放对账依据。
- **澄清选项卡片渲染**：澄清消息渲染为可点选项按钮卡片（展示 label），与澄清文本（turn_delta）共存；手打输入框始终可用，不强制点选。
- **点击回发**：用户点击选项时，下一轮 `POST /chat` 请求以约定字段携带 `clarify_selection:{value}`（发送所选 value 而非 label 文本）；发送后该卡片标记为已选/禁用（乐观标记，历史回放的 selected 为权威）。
- **历史还原**：刷新/换设备加载历史时，澄清消息据回放的 options/selected 还原卡片，已选项标记已选/禁用，未选项可继续点选。
- **历史态与重放对账（D9）**：事件只应用到本页 live 消息；turnId 命中 restored 历史消息的事件整事件忽略，未知 turnId（首连重放的更早轮次、他标签页轮次）事件忽略且不新建气泡，无 turnId 的 notification 不进消息模型。接受取舍：刷新时刻正在运行、尚未落库的轮次不在本页自动恢复，落库后重开可见。
- **`GET /stream` 持久通道接入（D3）**：前端以 EventSource 按 sessionId 常开 `/stream`，Last-Event-ID 断线自动续传；按事件名 `addEventListener`（不能用 onmessage），业务 `error` 事件与连接错误按 MessageEvent/有无 data 区分，`readyState=CLOSED`（401/404 等致命错误）主动 close 并降级为仅即时流；与 `POST /chat` 单轮流并存，事件经 `(turnId,eventSeq)` **幂等应用**（两通道重复事件不重复渲染）；会话级 seq 高水位覆盖全部带 seq 事件（含 notification），ring 缺口检测只在非首次连接触发"静默重拉合并"（不 abort 活动轮、活动轮延后到收尾后执行，不整体替换消息）；用户"停止生成"或网络断开后，同轮后续事件（含澄清 `clarify`/`turn_end`）经 `/stream` 继续到达并应用，缓解"前端假状态"（气泡截断、工具卡片永久转圈）。
- **跨轮作废（D6）**：新 clarify 或新一轮 turn_start 到达即禁用此前所有未消费的旧澄清卡片；旧轮迟到的 turn_end 不复活已作废卡片（覆盖"停止后立即重发"竞态）。
- **优雅降级**：不识别 `clarify` 事件或消息无 options（含空数组）时，澄清照常以纯文本呈现（等价现状），不报错；`/stream` 致命/不可用时不阻断 `POST /chat` 即时流（回退现状行为）。

**明确不做**：不改后端/SSE 协议/回传字段（已在 `intent-routing-context-quality` 定稿）；不做通用表单/多字段槽位卡片（缺参追问的参数收集仍走后端模型对话式半槽位 D6）；不做选项之外的富交互（图片/下拉级连）；不改鉴权与多租户模型；不承诺多标签页完美同步（`/stream` 全量事件为顺带收益，他端轮次事件按未知 turnId 忽略，历史轮次加载仍靠消息 API 分页）；**不恢复刷新时刻在跑且未落库的轮次**（assistant 行收尾时才落库，见 design D9）；不消费 notification 事件（其 seq 仅计入缺口高水位）；不细分 EventSource 致命错误状态码（浏览器不暴露，401 由下次 REST 请求收敛）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `web-auth-session`（前端部分）：前端消息模型支持澄清选项结构（options/selected）；澄清消息渲染为可点选项卡片、点击经 `clarify_selection` 回发、刷新后据历史回放还原卡片与已选态；`GET /stream` 持久通道的前端接入与事件幂等应用（断线续传、停止后继续跟踪轮次）。后端持久化/回放协议不在本 change（见 `intent-routing-context-quality` 的 web-auth-session delta）。

## Impact

- **代码（仅前端 `front/`）**：
  - **SSE 解析修复（前置）**：`api/chat.ts` 帧分隔兼容 CRLF/LF/CR（规范化后按空行切分），解析 `id:` 行。
  - SSE 事件解析/消息 store：识别 `clarify` 事件，落为含 options 的澄清消息；历史消息映射 options/selected；消息带 `source` 标记；事件应用改为 `(turnId,eventSeq)` 幂等（去重游标）+ 会话级 seq 高水位（含 notification 等忽略事件）。
  - 澄清选项卡片组件（按钮列表 + 已选/禁用态 + turn_end 前禁用 + 被更新轮次作废态）。
  - 发送逻辑：点选时构造携带 `clarify_selection:{value}` 的 `/chat` 请求；手打输入框保持原路径（不携带该字段）。
  - 历史还原：消息列表渲染时对澄清卡片据 selected 标记已选。
  - 事件应用层：纯函数 `applyEvent(messages, ctx, ev)`，provenance 对账（restored 命中忽略、未知 turnId 忽略不建气泡、无 turnId 不进模型）；React 副作用（建会话/侧边栏/setStreaming）留在 ChatPage。
  - `/stream` 接入模块：EventSource 连接管理（按 currentSessionId 建连/切换/断开、StrictMode cleanup）、按名 addEventListener、业务/连接 error 区分、readyState CLOSED 熔断降级、Last-Event-ID 自动续传（浏览器原生）；缺口"静默重拉合并"（活动轮延后，不 abort、不整体替换）。
- **代码（后端）**：无改动（`clarify` 事件、`GET /stream`、eventSeq、options/selected 回放、`clarify_selection` 识别均由既有 change 交付；CRLF 为 sse-starlette 既有帧格式，前端适配即可）。
- **协议/文档**：`docs/02` 前端章节补选项卡片渲染与回发、`/stream` 接入（替换"本期未接入，作为后续增强点"的表述）、SSE 帧 CRLF 兼容说明；README 前端说明。协议字段以 `intent-routing-context-quality` 的 `docs/00` 为准，本 change 不重复定义。
- **依赖**：无新增运行时依赖；新增 dev 依赖（vitest + @testing-library/react + jsdom）补前端测试基建。
- **兼容性**：协议字段均可选；旧后端（无 clarify 事件）或旧消息（无 options）时前端降级为纯文本澄清，不报错；`/stream` 连接失败不阻断对话主流程；解析修复只影响当前对 CRLF 帧失效的路径，不改变既有事件语义。
- **测试**：① SSE 字节流夹具（CRLF/LF/CR、多帧单 chunk、跨 chunk、CJK 跨 chunk）；② 前端组件/事件解析单测（mock SSE 事件与历史消息载荷），覆盖选项卡片渲染、点击回发载荷含 `clarify_selection.value`、手打不携带、历史还原已选态/脏数据全禁用、空 options 降级纯文本、eventSeq 幂等去重（双通道重复事件只应用一次、乱序到达）、无 seq 防御分支、**restored 轮事件全忽略、未知 turnId 忽略不建气泡、notification 不进模型但推进高水位**、**停止后立即重发时旧轮迟到 turn_end 不复活旧卡片**；③ `/stream` 集成：fake 连接模拟 EventSource 按名派发与 MessageEvent/ErrorEvent 区分、CLOSED 熔断、断线重连后事件补齐、缺口检测（首次连接不误报、notification 占 seq 不误报、活动轮延后静默重拉一次）。
