## Context

后端协议已由 `intent-routing-context-quality` 定稿交付：`clarify` SSE 事件（question/options）经 broker 编 `eventSeq` 入 ring（断线续传可重放）、澄清 meta（kind/categories/options/selected）落 `agent_message.meta`、`GET /sessions/{id}/messages` 回放透出 options/selected、`POST /chat` 识别 `clarify_selection:{value}` 确定性收窄（详见其 design D3/D11）。`GET /stream` 持久通道（Last-Event-ID 重放 + 去重）为 conversation-broker 既有交付，后端测试 `test_clarify_sse.py` 已钉住 clarify 事件断线重放。

前端现状（`front/`，React 18 + Vite，无状态库）：
- `api/chat.ts streamChat` 手动解析 `POST /chat` 的 SSE 帧，事件经 `ChatPage.onEvent` switch 直接 patch 本地 `ChatMessage[]`；
- 事件处理只认 turn_start/turn_delta/tool_start/tool_end/turn_end/error 六种，`clarify`/`notification` 落入"忽略未知事件"（降级已天然成立）；
- 无 `GET /stream` 消费代码（docs/02 标注"本期未接入"）；
- `front/package.json` 无任何测试 runner；
- 停止生成 = AbortController 断开即时流，气泡截断标记 stopped、工具卡片转圈→stopped（docs/02 §停止）。

探索阶段核对代码/实证补充的关键事实（本 change 必须处理）：

- **既有 SSE 解析与真实帧不兼容**：sse-starlette（`pyproject.toml` 约束 `>=2.2`，实测 3.4.8）`EventSourceResponse` 默认 `sep="\r\n"`，真实线上帧为 `id: N\r\nevent: x\r\ndata: {...}\r\n\r\n`（经 ASGI HTTP 实证：字节中只含 `\r\n\r\n`，不含 `\n\n`）。而 `chat.ts` 以 `buffer.indexOf("\n\n")` 分块——CRLF 帧在流式期间永不切分，流结束后整段 dispatch 又因多 data 行 JSON.parse 失败被静默丢弃。既有六事件与新 clarify 共用该解析器，**必须在本 change 前置修复（D10）**。
- **历史 DTO 不带 eventSeq**（`message_store.load_web_messages` 仅透出 options/selected），刷新后前端无从知道历史覆盖到哪个 seq；且还原消息带持久化 turnId，与 broker 重放事件的 turnId 同源。
- **EventSource 命名事件语义**：后端事件都带 `event:` 行，浏览器只把默认事件投递给 `onmessage`；必须按名 `addEventListener`。业务事件恰好名为 `error`，与连接失败的 `error` 事件同名，需按是否 MessageEvent/有无 data 区分。
- **notification 也占 seq 号**：`broker.publish_notification` 经同一 `distribute` 分配会话级 seq；前端虽不消费 notification，缺口高水位必须把它计入，否则相邻事件间夹一条 notification 即误报跳变。
- **clarify 轮事件时序固定**（runner 澄清分支）：`turn_start → turn_delta(整段 question) → clarify(若 options 非空) → 落库 assistant 行(meta) → turn_end`，无 tool_*；空 options 不发 clarify 事件（后端测试已钉）。
- **assistant 行在收尾阶段（turn_end 之前）才落库**：轮次进行中历史里没有该行；error 事件由 producer 兜底分发、不保证有 assistant 落库行。

本 change 为纯前端消费，"不改后端"边界不变。

## Goals / Non-Goals

**Goals:**

- 澄清消息渲染为可点选项卡片（label 展示、value 不透明暂存），点击回发 `clarify_selection:{value}` + label 作 message。
- 刷新/换设备后据历史回放还原卡片与已选态。
- 接入 `GET /stream` 持久通道：断线/停止后继续应用同轮事件，双通道按 eventSeq 幂等合并。
- 历史态与重放流对账：打开/刷新历史会话时，ring 全量重放 MUST NOT 造成重复文本/重复卡片/状态翻转（D9）。
- 修复既有 SSE 手写解析对 CRLF/CR 帧的不兼容，字节级夹具钉住（D10）——clarify 与既有六事件共用解析器，此为本 change 的前置修复。
- 全链路优雅降级：无 options/空 options/error 轮/`/stream` 致命错误，均回落现状纯文本行为，不报错。

**Non-Goals:**

- 不做选项之外的富交互（表单/下拉级联/多选）；不做后端改动；不承诺多标签页实时同步（他端点选经 `/stream` 到达本端属顺带收益，不作为验收，未知 turnId 事件直接忽略）；不做消息 API 之外的轮次补拉（eventSeq 缺口走整页历史刷新兜底）；不做 vitest 之外的重型测试基建（无 Playwright e2e）。
- **不恢复"刷新时刻正在运行、尚未落库的轮次"**：该轮 assistant 行尚不在历史中，重放事件对前端是未知 turnId（D9 忽略），待轮次落库后手动刷新/重新打开会话可见。
- 不消费 `notification` 事件（但其 eventSeq 计入缺口高水位，见 D2/D4）；不细分 EventSource 致命错误的 HTTP 状态码（浏览器不暴露，401 统一由下一次 REST 请求的既有 401 流程收敛）。

## Decisions

### D1：澄清状态归属——卡片状态内聚在 ChatMessage，不建全局 store

`ChatMessage` 增 `clarify?: { options: {label,value}[]; selected?: string; disabled?: boolean }` 与 `source: "live" | "restored"`（来源标记，对账语义见 D9）。状态推进：
`clarify 事件` → disabled=true（等 turn_end）→ `turn_end` 且该轮未被更新轮次作废 → disabled=false → `点击发送` → selected=value（乐观，disabled=true）→ **刷新后历史回放的 selected 为权威**（覆盖乐观值）。
"可点"条件：live 卡片需 `!disabled && !selected` 且所属轮次未被更新轮次作废（过期/被作废由 D6 置位，迟到的 turn_end MUST NOT 复活，见 D6）；restored 卡片只要无 selected/无脏数据即保持可点（续聊场景，旧卡片点选被后端忽略是 D6 已接受的安全回落）。
理由：与既有 toolCalls/streaming/stopped 字段同构，全部状态可由 ChatPage 的 patchAssistant 闭包推进，零新依赖。备选（zustand 全局 store）被否：单页应用无跨组件状态共享需求，引入状态库违背"无新增运行时依赖"。

### D2：双通道幂等——(turnId, eventSeq) 去重，会话级高水位，事件应用函数唯一

即时流（`streamChat` onEvent）与 `/stream`（EventSource 监听，见 D3）汇入**同一个** `applyEvent(state, ev)` 纯分发函数（messages→messages；游标状态在外层 reducer 持有）。两层序号语义必须分开：

1. **去重游标**：按 `(turnId, eventSeq)` 记录已应用事件（量小直接 `Set<turnId#seq>`），重复（含双通道重复、重放重复）跳过——文本不重复拼接、卡片状态不重复翻转、toolCalls 不重复插入。
2. **缺口高水位**：`maxSeenSeq` 是**会话级**（不是 per-turn），**所有带 seq 的事件都推进**，包括前端不渲染的 `notification`（它同样经 `broker.distribute` 占 seq 号，不推进就会在相邻 turn 事件间造成伪跳变）；heartbeat 是 SSE 注释行、无 id/seq，不参与。缺口检测见 D4。

无 eventSeq 的防御分支：不做内容哈希（不可靠），按"无 seq 只从即时流应用、/stream 端忽略无 seq 事件"处理（后端所有经 broker 的事件均带 seq，此为纯防御）。
游标生命周期：状态按 sessionId 分别持有（`Map<sessionId, …>`），切会话不共享；回到旧会话时 EventSource 从 0 全量重放，重复/错乱由 D9 的"restored 忽略 + 未知 turnId 忽略"兜底，不依赖跨挂载记忆游标。
理由：broker.distribute 对同一会话所有通道用同一 seq 序列（`broker.py next_seq`），seq 即会话级去重键，无需自造幂等键。备选（每通道独立渲染、UI 层 diff 去重）被否：文本拼接类副作用无法靠 render diff 幂等。

### D3：/stream 生命周期——随 currentSessionId 建连，EventSource 原生续传，按名监听

`useEffect([current?.sessionId])`：会话切换/新建时 close 旧 EventSource、按 `new EventSource(\`/stream?sessionId=...\`)` 建新连接；断线重连与 `Last-Event-ID` 头由浏览器原生承担（服务端 `stream.py` 已支持头/参数两形态）。新建会话（current=null）不建连，等 `turn_start` 带回 sessionId 再挂。cleanup 必须 close——React 18 StrictMode dev 双挂载会短暂建两条连接，靠 cleanup 收敛。实现形态：ChatPage 条件挂载子组件 `SessionStream`（sid 非空才挂载，effect 依赖 `[sid,dispatch]`），与内联 effect 语义等价，且让 StrictMode setup→cleanup→setup 双挂载真实发生。

**按名监听，不能用 onmessage**：后端每类事件都带 `event:` 行，浏览器仅把无 event 字段的默认事件投递给 `onmessage`；必须对七个业务类型显式 `addEventListener`（turn_start/turn_delta/turn_end/tool_start/tool_end/clarify/notification），处理器统一解析 `MessageEvent.data`（且必须是非空对象，畸形/非对象帧静默忽略）后汇入 applyEvent。MUST NOT 再按名监听 `error`——见下条，业务 error 在 `onerror` 内分流，否则双发。

**业务 error 与连接 error 同名区分**：后端业务错误事件名也是 `error`，与 EventSource 连接失败事件撞名，两类都进 `es.onerror`。业务事件是 `MessageEvent`（有非空字符串 `data` 且可解析为对象）→ 恰好一次投递 `{event:"error",data}` 进 applyEvent；其余（普通 Event 无 data、非 MessageEvent、data 不可解析或非对象）一律按连接错误处理，MUST NOT 进 applyEvent。

**连接错误不做应用层退避，但做致命熔断**：`onerror` 时读 `readyState`——`CONNECTING`（网络抖动，浏览器将自动重试）仅记日志；`CLOSED`（服务端返回非可接受状态，如 401/404，浏览器放弃）则主动 `close()` 并降级为仅 POST 即时流（不阻断对话），可给一次性轻提示。浏览器不暴露响应状态码，不区分 401/404：401 由下一次 REST 请求的既有 401 流程统一登出。
理由：EventSource 的自动重连 + Last-Event-ID 是为该协议设计的原生机制，自研重连循环是负收益；但原生无限重试对致命状态是坑，需 readyState 熔断。备选（fetch + 手动 ReadableStream 解析，复用 streamChat 基建）被否：要自管重连、重放请求与 id 行解析，且失去 Last-Event-ID 原生能力，复杂度不对等。

### D4：eventSeq 缺口检测——会话级高水位，跳变触发"静默重拉合并"，活动轮延后

`/stream` 通道维护会话级 `maxSeenSeq`（口径同 D2：含 notification 等被忽略事件，无 seq 的 heartbeat 不计）；新到事件 `seq <= maxSeenSeq` 由去重覆盖，`seq > maxSeenSeq + 1` 且此前有过事件（说明 ring 重放起点跳过了缺口，如 ring 256 条被冲掉）→ 判定溢出。

**不能直接调 `openSession`**：它首行 `abort()` 即时流并整体替换 messages；若缺口发生在活动轮期间，乐观气泡尚未落库，会被杀掉且历史里没有它。处理：

1. 活动轮（streaming）期间仅置 `pendingGapReload=true`，在 turn_end/error 收尾后执行；
2. 执行体是**静默重拉合并**（新函数，非 openSession）：重拉该会话历史，以 turnId 为键替换 restored 消息、保留当前 live 乐观气泡与输入态、不 abort 任何连接；同时按 D9 把已落库的旧 live 轮转为 restored（selected 接受历史权威覆盖）；
3. toast 提示"事件已过期，已刷新"（一次性，合并多次缺口只重拉一次）。

首次连接（maxSeenSeq=0）不触发——首连 ring 全量重放与历史的重复由 D9 对账解决，不是缺口。
理由：ring 容量 256 条（`broker.ring_size`），长断线必溢出；静默错乱（漏 turn_delta/clarify）比显式刷新体验差。备选（按缺口精确补拉事件 API）被否：后端无按 seq 区间查询端点，MUST NOT 为此加后端。

### D5：文本与卡片分离——turn_delta 是文本唯一来源

`clarify` 事件只取 `options`（question 与 turn_delta 同源，不重复渲染）；卡片组件 `ClarifyOptions` 独立于 Markdown 气泡，挂在气泡下方。
理由：协议已定 question==direct_reply（events.clarify 调用点），重复渲染是确定性 bug 源头。

### D6：旧卡片禁用——新 clarify 或新轮 turn_start 到达即作废此前所有未 selected 卡片

`applyEvent` 在两种触发点遍历当前 messages，把既有 `clarify && !selected` 的 live 卡片置 disabled（过期态）：① 新的 `clarify` 事件（原语义：新澄清=旧候选失效）；② **新轮次的 `turn_start` 到达**（覆盖"停止生成后立即重发"竞态：旧轮 producer 仍在跑，其迟到的 clarify/turn_end 经 /stream 到达时不得复活旧卡片——作废置位幂等，迟到 turn_end 的"启用本卡"逻辑只对未被作废的最新轮生效）。规则落到实现：turn_end 启用卡片前检查"该 turnId 仍是当前最新 live 轮"，否则保持 disabled。
历史还原路径不做此推断（回放无事件顺序语义，按"仅最新一张可点"处理太魔法；旧回放卡片全部可点是可接受现状——点击会被后端忽略回落文本闭环，安全）。
理由：live 路径有明确"新澄清/新轮=旧候选失效"语义；restore 路径无该语义，宁可少做不可做错。

### D7：测试基建——vitest + @testing-library/react + jsdom

`package.json` devDependencies 增 `vitest`、`@testing-library/react`、`@testing-library/jest-dom`、`jsdom`；新增 `test` script（`vitest run`）与 `vitest.config.ts`（复用 vite react 插件配置）。覆盖：事件解析纯函数（applyEvent 幂等/缺口检测）、ClarifyOptions 组件（状态机渲染/点击回调载荷）、ChatPage 集成（mock EventSource + fetch 流）。
理由：Vite 系原生 runner，零额外转译链；jsdom 够用（无 canvas/布局依赖）。备选（jest）被否：与 Vite 双配置漂移。

### D8：停止生成与 /stream 的交互——停止只断即时流，/stream 不受影响

现状 stop=abort fetch；接入 /stream 后同轮事件继续从 /stream 到达，气泡经 D2 幂等合并自然补齐，`turn_end` 到达后按现状收尾（streaming=false）。stopped 标记保留（截断时刻的本地事实），后续 /stream 补齐的内容直接追加。
理由：/stream 存在的意义即"断开不丢事件"；把 stopped 当终态会阻止补齐，与 D3 目标冲突。

### D9：历史态与重放流对账——provenance 分流，restored/未知 turn 一律不应用

EventSource 首连没有 Last-Event-ID，后端以 `last=0` replay ring 中**全部**事件（最多 256 条，含已完成历史轮次）；历史 DTO 又不带 eventSeq。若直接按 turnId 归位，历史气泡会被重放的 turn_delta 再拼一遍、toolCalls 再插一套、clarify 卡片再渲染一张——**每次打开/刷新历史会话必现**。对账规则：

1. `ChatMessage` 标 `source`：`historyToMessages` 产出 `restored`（历史权威），send 的乐观气泡为 `live`；D4 静默重拉合并后，已落库的旧 live 轮按 turnId 转为 restored。
2. `applyEvent` 只对 **turnId 命中 live 消息**的事件做状态推进：
   - 命中 restored 消息 → 整事件忽略（不拼文本、不插卡、不翻转状态；历史是权威快照）；
   - 不命中任何消息（未知 turnId，含首连重放的更早轮次、他标签页轮次）→ 忽略，**MUST NOT 新建气泡**；
   - 无 turnId 事件（notification）→ 不进消息模型（仅经 D2 外层推进高水位）。
3. **live 气泡绑定**：send 乐观气泡没有 turnId，`turn_start` 经 ChatPage 持有的"当前活动轮 assistantId"绑定（单活动轮不变式，沿用 send 的 `if (streaming) return` 守卫）；绑定关系作为 applyEvent 的上下文入参传入，纯函数自身不建消息。
4. **副作用留在 ChatPage，不进纯函数**：建会话/setCurrent/侧边栏插入/setStreaming 等 React 副作用留在事件回调；applyEvent 只做 `(messages, ctx, ev) -> messages`（tasks 1.3 迁移时遵守，避免把闭包副作用误搬进纯函数）。
5. 取舍（列 Non-Goals）：刷新页面时若轮次正在运行，assistant 行尚未落库，重放事件为未知 turnId 被忽略，该轮气泡不会在本页自动出现；落库后重新打开会话可见。

理由：事件流只表达"发生了什么"，无法自证"前端是否已通过历史快照拥有它"；provenance 是唯一可靠的对账依据。备选（靠 per-turn 内容 diff）被否：delta 拼接、toolCalls 插入、卡片渲染均为不可逆副作用，无幂等 diff。

### D10：既有 SSE 分块解析修复——按规范兼容 CRLF/LF/CR，字节夹具先行

sse-starlette 默认 `sep="\r\n"`（3.4.8 实证真实帧为 `id: N\r\nevent: x\r\ndata: {...}\r\n\r\n`，约束 `>=2.2` 同默认），而 `chat.ts` 用 `buffer.indexOf("\n\n")` 分块：CRLF 帧在流式期间永不切分，流结束整段 dispatch 又因多 data 行 JSON.parse 失败被静默丢弃。修复（仅前端解析器，不改协议）：

- 先规范化再分块：buffer 经 `/\r\n/g → "\n"`、`/\r/g → "\n"` 后按 `\n\n` 切（或直接用 `/\r?\n\r?\n/` 分块；单 `\r\r` 罕见，规范化方案更稳）；
- 行解析已有 `trim()`，CRLF 残尾天然兼容；`id:` 行解析进 data 同源字段（data.eventSeq 已存在，id 行作为交叉校验/即时流 seq 来源，二者一致时无额外分支）；
- 测试以**字节流夹具**驱动 `streamChat`（不是 mock 事件对象）：CRLF/LF/CR 三种空行分隔、多帧合并在一个 chunk、单帧跨多个 chunk（含 CJK 多字节字符跨 chunk，钉 `TextDecoder(stream:true)` 路径）；
- 任务排序上先修解析、再迁移 applyEvent；迁移任务的"与迁移前行为一致"只指六事件的**语义推进**一致，MUST NOT 把解析缺陷一并保留。

理由：mock 事件对象绕过了字节层，正是该缺陷长期未被发现的原因；解析器是 clarify 与既有事件的共同入口，必须前置修复。

## Risks / Trade-offs

- [双通道事件竞态（/stream 先于即时流到达）] → D2 幂等以 eventSeq 为准，先到先应用、后到跳过，与到达顺序无关。
- [EventSource 无法自定义 header（如鉴权）] → 现状 session 鉴权走 Cookie（自动携带），无 header 需求；`/stream` 已在治理链内（stream.py governance_dep），无新增风险。
- [ring 溢出误报（重连窗口抖动）] → D4 仅在"有过事件且 seq 严格跳变"才刷新，首次连接（maxSeenSeq=0）不触发。
- [乐观 selected 与后端忽略不一致] → 回放权威覆盖（D1）；轮内不一致窗口用 toast 提示"已按文本理解"可选，v1 仅静默回落（后端 clarify_selection_ignored 已有审计）。
- [测试对 EventSource 的 mock 成本] → 抽象 `createStreamConnection(sessionId, handlers)` 薄层，单测注入 fake；fake MUST 模拟按名派发语义（命名事件不走 onmessage）与 MessageEvent/ErrorEvent 区分，否则测不出 D3 的两个陷阱；不引 msw。
- [首连 ring 全量重放与历史重复] → D9 provenance 对账：restored 轮忽略、未知 turnId 不建气泡。
- [notification 占 seq 导致伪缺口] → D2/D4 高水位会话级、含所有带 seq 事件。
- [缺口刷新杀掉活动轮乐观气泡] → D4 活动轮延后 + 静默重拉合并（不 abort、不整体替换）。
- [停止后立即重发，旧轮迟到事件复活旧卡片] → D6 新 turn_start 即作废旧卡，turn_end 只启用最新 live 轮。
- [EventSource 致命错误无限重连] → D3 readyState CLOSED 熔断 close，降级仅即时流。
- [StrictMode dev 双挂载双连接] → effect cleanup 必须 close，集成测试覆盖挂载/卸载。
- [SSE 解析修复影响既有六事件行为] → D10 字节夹具（CRLF/LF/CR、跨 chunk、CJK）钉住，语义不变、仅分块修正。

## Migration Plan

纯前端增量，随前端发版生效；后端无变更、无数据迁移。回滚 = 前端回退到上一版本（旧前端忽略 clarify 事件，协议 additive 天然兼容；D10 解析修复回滚仅影响本就失效的分块路径，无行为回退风险）。灰度点：先随版修 D10 解析与卡片（仅依赖即时流），再验证 `/stream`（接入失败不影响即时流，D3 已隔离）。
