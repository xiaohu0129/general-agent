> 实现纪律：前端任务按 TDD（先写失败测试 → 跑红 → 实现 → 跑绿）。测试命令：`cd front && npm test`（vitest run，基建见任务 1.1）。不依赖真实后端（EventSource/fetch 均 mock 或注入 fake）。
> 顺序约束：**任务 1.2（SSE 分块解析修复）必须先于 1.3（applyEvent 迁移）**——既有解析器对 sse-starlette 默认 CRLF 帧失效，mock 事件对象测不出该层；"与迁移前行为一致"仅指六事件语义推进一致，不得保留解析缺陷（design D10）。

## 1. 测试基建与 SSE 解析前置修复

- [x] 1.1 搭建 vitest 基建并验证可运行：`front/package.json` devDependencies 增 vitest/@testing-library/react/@testing-library/jest-dom/jsdom，新增 `test` script（`vitest run`）与 `vitest.config.ts`（environment jsdom、复用 vite react 插件）；验证：写一个最小快测试（渲染 Welcome 组件）跑绿，`npm run build` 不受影响
- [x] 1.2 先写失败测试（红）：以**字节流夹具**（经 `ReadableStream` 喂给 `streamChat`，不是 mock 事件对象）覆盖——CRLF（`\r\n\r\n`，sse-starlette 默认）/LF（`\n\n`）/CR（`\r\r`）三种空行分隔均逐帧派发；多帧合并在同一 chunk；单帧跨多个 chunk；CJK 多字节字符跨 chunk 边界；`id:` 行可读；验证：CRLF 用例在现状（`indexOf("\n\n")`）下跑红
- [x] 1.3 实现解析修复（绿）：`front/src/api/chat.ts` 规范化分隔（先统一换行再按空行切，或等效正则），解析 `id:` 行；上述夹具全绿
- [x] 1.4 事件应用层单测先红后绿：新建 `front/src/chat/applyEvent.ts`（**纯函数** `(messages, ctx, ev) => messages`，输入当前 messages + 活动轮绑定上下文 + 事件，输出新 messages），把现 ChatPage 内联 switch 的六事件**语义推进**迁移进去；建会话/setCurrent/侧边栏插入/setStreaming 等 React 副作用 MUST NOT 进纯函数，留在 ChatPage 事件回调；验证：turn_start/turn_delta/tool_start/tool_end/turn_end/error 状态推进断言全绿

## 2. 消息模型、来源标记与历史还原

- [x] 2.1 先写失败测试（红）：`ChatMessage` 增 `clarify?: {options: {label,value}[]; selected?: string; disabled?: boolean}` 与 `source: "live" | "restored"`；`historyToMessages` 把 HistoryMessage 的非空 `options`/`selected` 映射进 clarify（空数组/缺失不生成 clarify 字段）、产出消息一律 `source:"restored"`；断言：带 selected 的卡片还原为已选禁用、selected 不在 options 内全禁用、无 options 消息无 clarify 字段且仍正常渲染、restored 标记存在；`front/src/api/types.ts` 的 HistoryMessage 增 `options?: {label,value}[] | null`、`selected?: string | null`
- [x] 2.2 实现（绿）：更新 model.ts/types.ts/historyToMessages 及上述测试全绿

## 3. 澄清卡片组件与点击回发

- [x] 3.1 先写失败测试（红）：`ClarifyOptions` 组件渲染选项按钮列表（label），`disabled=true` 或消息 streaming 时全部禁用，`selected` 匹配项高亮且全部禁用；点击回调携带所选项的 value（不解析）；验证：组件测试覆盖 渲染/禁用态/已选态/点击载荷 四断言
- [x] 3.2 实现（绿）：组件 + MessageBubble 挂载（气泡下方，clarify 存在时渲染）；MessageList/MessageBubble 快照不因无 clarify 消息变化
- [x] 3.3 先写失败测试（红）：`streamChat` 增可选 `clarifySelection?: string` 参数，请求体携带 `clarify_selection: {value}`；ChatPage `send` 增重载 `send(text, clarifySelection?)`——点选路径 `message=label, clarify_selection={value}` 且点击后卡片乐观置 selected、乐观气泡 `source:"live"`；手打路径不携带字段；验证：mock fetch 断言两种路径的请求体与卡片状态
- [x] 3.4 实现（绿）：上述接线与测试全绿

## 4. applyEvent 扩展：clarify 状态机、跨轮作废与来源对账

- [x] 4.1 先写失败测试（红）：applyEvent 处理 `clarify` 事件（取 options、忽略 question 不重复渲染文本；同会话既有未 selected 的 **live** 澄清卡片全部置 disabled 过期态；本卡片初始 disabled=true）；`turn_end` 仅在"该 turnId 仍是当前最新 live 轮"时把其 clarify.disabled 置 false（可点），旧轮迟到 turn_end 不复活已作废卡片；新一轮 `turn_start` 到达即作废此前所有未 selected 的 live 卡片；`error` 事件保持 disabled=true（永不启用）；验证：单测覆盖 正常收尾/异常收尾/停止后立即重发旧轮迟到 × 卡片状态 转移断言
- [x] 4.2 实现（绿）：applyEvent 扩展 + types.ts 增 ClarifyData 事件类型；测试全绿
- [x] 4.3 先写失败测试（红，design D9 对账）：事件 turnId 命中 `source:"restored"` 消息时整事件忽略（delta 不拼接、不插卡片、状态不翻转）；未知 turnId 事件忽略且不新建气泡；无 turnId 的 notification 形态事件不进消息模型；验证：构造"历史还原 + ring 全量重放"消息序列，断言文本不重复、卡片不重复、toolCalls 不重复插入
- [x] 4.4 实现（绿）：applyEvent 入口 provenance 分流；测试全绿

## 5. /stream 持久通道接入、幂等合并与缺口处理

- [x] 5.1 先写失败测试（红）：applyEvent 外层幂等——同一 (turnId, eventSeq) 只应用一次（delta 文本不重复拼接、卡片状态不重复翻转、toolCalls 不重复插入）；即时流与 /stream 两来源乱序到达结果一致；无 eventSeq 防御分支（无 seq 仅即时流应用、/stream 端忽略）；验证：单测覆盖 双通道重复/乱序/无 seq
- [x] 5.2 实现（绿）：去重游标 Set<turnId#seq>（按 sessionId 持有，切会话不共享），applyEvent 入口去重；测试全绿
- [x] 5.3 先写失败测试（红）：`createStreamConnection(sessionId, handlers)` 薄封装（真实实现用 EventSource，测试注入 **fake 须模拟 EventSource 真实派发语义**：命名事件仅经按名注册的 listener 收到、不走 onmessage；业务 error 为 MessageEvent 带 data、连接 error 为无 data 事件）+ ChatPage useEffect 随 currentSessionId 建连/切换重建/null 不建连（turn_start 拿到 sessionId 后补建）、cleanup 必 close（含 StrictMode 双挂载用例）；八类事件名全部 addEventListener；连接错误 `readyState=CONNECTING` 仅记日志，`CLOSED` 主动 close 并降级（不抛到 UI、不阻断发送）；/stream 事件经同一 applyEvent；验证：集成测试用 fake connection 断言 ①停止生成后 /stream 补齐同轮事件（delta 追加、clarify 渲染、turn_end 收尾）②命名事件不经 onmessage 也能收到 ③两类 error 正确区分 ④CLOSED 后 POST 路径仍可用
- [x] 5.4 实现（绿）：stream 连接模块 + ChatPage 接线；测试全绿
- [x] 5.5 先写失败测试（红）：**会话级**高水位缺口检测——所有带 seq 事件（含不渲染的 notification）推进 maxSeenSeq，相邻 turn 事件夹 notification 不误报；新到 seq > maxSeenSeq+1 且此前有事件时触发**一次**静默历史重拉合并（新函数，非 openSession：不 abort 即时流、不整体替换 messages、保留 live 气泡，按 turnId 用历史替换/转正 restored，selected 接受历史权威）；活动轮（streaming）中检测到缺口仅标记 pendingGapReload，turn_end/error 收尾后执行；首次连接（高水位初始）不触发；验证：单测注入跳变 seq/notification 序列断言 重拉一次、活动轮延后、首连不误报、live 气泡不丢
- [x] 5.6 实现（绿）：缺口检测接入 applyEvent 外层 + 静默重拉合并函数；测试全绿

## 6. 文档同步

- [x] 6.1 更新 `docs/02-Web前端与认证设计.md`：补澄清选项卡片（状态机：disabled→clickable→selected；新轮 turn_start/新 clarify 作废旧卡；异常轮保持禁用）、点击回发载荷、历史还原 selected 权威语义、**消息 provenance（live/restored）与 ring 重放对账规则**；`/stream` 接入章节替换"本期未接入，作为后续增强点"为实际接入方案（按名 addEventListener/两类 error 区分/CLOSED 熔断/Last-Event-ID/幂等合并/会话级高水位/缺口静默重拉）；补 **SSE 帧 CRLF/LF/CR 兼容**说明（sse-starlette 默认 CRLF）；验证：文档与 specs delta、design D1-D10 一致性人工核对通过
- [x] 6.2 更新 `README.md`：变更记录该 change 条目置于"进行中"组（状态：已实现待归档）并补 /stream 接入、SSE CRLF 解析修复与前端测试基建一句；验证：与 proposal What Changes 一致

## 7. 收尾验证

- [x] 7.1 全量验证：`cd front && npm run build && npm test` 全绿；后端测试不受影响（`$env:PYTHONPATH="."; python -m pytest` 仍全绿，本 change 不改后端代码）；`openspec validate structured-clarification-options --type change` 通过
- [ ] 7.2 联调冒烟（**必做至少一次**，mock 测过字节层后仍需真实帧实证）：stub LLM 起后端，浏览器走通 ①真实 CRLF SSE 帧流式逐字到达（非流结束才整段出现）②低置信触发澄清→点选→收窄回复→刷新还原已选 ③打开含历史轮次的会话，ring 重放不产生重复文本/重复卡片 ④停止生成后 /stream 补齐 ⑤手动断网重连恢复、致命错误（停后端）降级仅即时流
