# AGENT 实现设计

> 本文档为 agent 模块**实现级设计权威**，含 Bug 记录、多方案决策（D1–D9）、实现落点与验证矩阵。
> 上游架构/方案追问见 [00-AGENT综合设计.md](./00-AGENT综合设计.md)；对外 SSE 协议见 00 文档 §三，异步通知接入见 `api/notify.py`；Web 登录认证/多会话见 [02-Web前端与认证设计.md](./02-Web前端与认证设计.md)。
> 状态：核心能力（SSE 对话 / 断线续传 / 治理 / Skill 机制 / 可观测 / Web 登录认证）已实现并验证；测试套件全部通过。
> 迁移期踩坑（langchain-core 1.x 兼容、aiomysql 多语句、孤儿 tool_calls 清洗、jwt fail-closed、中断并发串行化等）统一记录于 [错题本.md](./错题本.md)（E1–E9），本文在相关章节交叉引用。

---

## 零、已发现 Bug 与迭代修复

| # | 位置 | 问题 | 严重度 | 归属 |
|---|------|------|--------|------|
| B1 | broker.py | 引用未定义的 events.with_seq/events.notification（孤儿），events.py 新增后 broker 直接 import 即用，无需包装 | 高 | M7 |
| B2 | runner.py | load_messages 无界载入全部历史，config.max_context_tokens/context_strategy/summarize_threshold 配置未落地 | 高 | 上下文治理 |
| B3 | chat.py | 无 GET /stream，缺 eventSeq/心跳/续传/通知端点 | 高 | M7 |
| B4 | 全局 | 无鉴权，任意请求带 x-service/x-env/x-user 即可访问 | 高 | M6 |
| B5 | 全局 | 无限流、无脱敏、无审计 | 高 | M6 |
| B6 | config.yaml 注释 | 误导性注释（M4 时代 trim/summarize 描述不符实现） | 低 | 配置 |
| B7 | Skill 入参 | Skill args_schema 字段命名若与下游业务 REST 不一致（如 schema 用 snake_case、下游用 camelCase），Skill.run 直接透传会脆弱；LLM 按 schema 传参，下游字段名不匹配时出错 | 中 | Skill 机制 |
| B8 | 多文件 | 上一轮用 PowerShell here-string 经 stdin 写文件时 `$OutputEncoding` 默认 ASCII，导致中文（含 Skill `description`）被替换成 `?`；docstring/注释/工具描述损坏，工具描述会发给 LLM 属**功能性损坏** | 高 | 编码 |
| B9 | chat.py | producer 任务引用仅在 `_produce` 内异步加入 `inflight`，`chat` 返回后局部引用即失效，存在 asyncio 任务被 GC 的理论窗口（官方建议同步持有引用） | 中 | M7 健壮性 |
| B10 | runner.py | `max_tool_rounds` 中断时最后一条 `AIMessage(tool_calls)` 已捕获但工具未执行（无对应 ToolMessage），仍落库；下轮载入出现"assistant(tool_calls) 后直接跟 human"，OpenAI 400，且该会话此后每轮报错（详见错题本 E7） | 高 | 上下文治理 |
| B11 | security.py | `auth_mode=jwt` 占位分支直接 `return` 静默放行（fails open），误配 jwt 会以为有鉴权实则全开放（详见错题本 E8） | 高 | M6 安全 |
| B12 | chat.py / turn_lock.py / 前端 | "停止生成"仅断开 SSE，producer 有意跑完落库，导致：① 停止后立即重发使两 producer 并发操作同 session，历史行交错/工具跨轮配对；② 新会话行在请求入口即建，极早断开留空孤儿会话；③ 前端中断后气泡/工具卡永久"执行中"（详见错题本 E9） | 高 | M7 健壮性/Web |

> B7 本质是 Skill 入参与下游协议字段名的映射问题：业务 Skill 应用 pydantic `Field(alias=...)` + `ConfigDict(populate_by_name=True)` 做双名兼容，run 内用规范名、调下游时转下游命名（见 §四 Skill 落点）。
> B8 根因：PowerShell 管道默认用 ASCII 编码，中文转 `?`；**修复**：写文件时显式 `$env:PYTHONIOENCODING=utf-8` + `$OutputEncoding=UTF8`，或用 `apply_patch`/.NET `WriteAllText` 直写 UTF-8。本轮已清理全部源码与文档。**写文件规范**：含中文/emoji 时须 UTF-8 管道，或直写 UTF-8，禁止默认 ASCII stdin。
> B9 修复：`chat` 中 `asyncio.create_task` 后**同步** `app.state.inflight.add(producer)`，`_produce` 仅在 `finally` 移除。
> B10 修复：`runner.py` 新增 `_drop_unanswered_tool_calls`——按现有 ToolMessage 的 `tool_call_id` 集合过滤 `AIMessage.tool_calls`，清洗后既无内容也无工具调用的空 AIMessage 直接丢弃；在**载入历史后**（修复已污染旧数据）与**持久化前**（不写入新污染）两处应用。落库前清洗 + 读取时清洗双保险（错题本 E7）。
> B11 修复：`security._check_auth` 的 jwt 占位分支改为抛 `GovernanceError(500, "CONFIG", ...)`，宁失败勿放行；安全相关预留分支默认必须 fail-closed（错题本 E8）。
> B12 修复：① 后端按 `session_id` 加互斥锁，producer 在 `TurnLockRegistry`（`turn_lock.py`）锁内跑 `run_turn`，同会话轮次串行；注册表用**引用计数**回收（进入者在 await 前同步 `refs++`，仅 refs 归零且锁空闲才回收），避免 setdefault/pop 在"释放与唤醒之间"误删锁致互斥失效。② 会话行创建延后到 producer 内、锁内执行（`ChatSessionStore.create(..., session_id=预生成)`），producer 不随断开取消，消除孤儿会话窗口。③ 前端 abort 后气泡/工具卡标记 `stopped`，`finally` 统一 `refreshSessions()` 补回后端建好的会话（错题本 E9）。

---

## 一、M7 长任务稳定性设计

### 1.1 业界对照

长时 Agent 推送面临 SSE 空闲断开问题，业界主流均为 **结构化事件流 + 心跳 + 续传**：

| 厂商/SDK | 推送方式 | 续传 | 心跳 |
|-----------|----------|------|------|
| OpenAI Assistants/Responses | SSE typed events | 流式无原生 after 续传 | 内置 |
| Anthropic Messages | SSE typed events | - | ping 事件 |
| Vercel AI SDK | SSE（Data Stream Protocol） | 自定义 | 注释行 |
| 通用网关实践 | SSE/WebSocket | Last-Event-ID 重放 | 注释行 |

SSE 标准的 `id:` 行 + 浏览器 EventSource 自动重连携带 `Last-Event-ID` 是 W3C 标准能力，最契合断线续传。

### 1.2 事件中枢 Broker（中心化 hub + ring buffer + fan-out）

**决策 D1：Broker 中心化（弃每轮独立 queue 双路径）**

| 方案 | 机制 | 缺点 |
|------|------|------|
| A. 每轮独立 queue | POST /chat 内 producer->queue->consumer；GET /stream 另起 queue | 两套路径，turn 结束即销毁、续传/通知难复用 |
| **B. Broker 中心化** | run_turn(producer)->Broker.distribute->ring buffer + fan-out；POST /chat 与 GET /stream 均为订阅者 | **单一路径**，turn 事件与通知统一 pub/sub，续传天然支持 |

**选 B**：Broker 作为会话级事件 hub，所有事件经 distribute 打 seq + 入 ring + fan-out 订阅者；POST /chat 与 GET /stream 都是订阅者，区别仅在过滤策略。

**数据结构**：
- `seq: dict[session_id -> int]`：每会话单调递增 eventSeq。
- `ring: dict[session_id -> deque(maxlen=N)]`：每会话保留最近 N 条事件（续传窗口）。
- `subs: dict[session_id -> set[Queue]]`：每会话订阅者集合。

**关键方法**：
- `distribute(session_id, event)`：分配 seq（设 id 行 + data.eventSeq）-> append ring -> fan-out 订阅者。
- `subscribe/unsubscribe`：注册/注销 `asyncio.Queue`。
- `replay(session_id, after_seq)`：返回 ring 中 seq>after_seq 的事件，供续传重放。
- `publish_notification(...)`：notification 事件经 distribute，与 turn_* 多路复用。

**背压策略 D2：put_nowait + QueueFull 丢弃该订阅者（ring 全量保留）**

| 方案 | 机制 | 缺点 |
|------|------|------|
| A. await q.put 阻塞 | 满则等订阅者消费 | 慢消费者阻塞 producer -> 整轮卡死/雪崩 |
| **B. put_nowait + QueueFull 丢弃** | 满即丢该订阅者，ring 全量保留 | 该订阅者漏事件（可 Last-Event-ID 重放 ring 补） |

**选 B**：单轮事件量远小于 maxsize=1024，正常不触发；触发说明订阅者异常慢，丢弃优于阻塞 producer。丢的事件仍在 ring，慢订阅者可经 Last-Event-ID 重放补齐；drop 计 metric。**绝不阻塞** producer，保证执行推进。

### 1.3 POST /chat：producer/consumer 解耦

```
POST /chat:
  subscribe broker(session) -> q
  producer = create_task(_produce: async for ev in run_turn: broker.distribute(session, ev))
  app.state.inflight.add(producer)          # 同步持有引用（B9 修复）
  consumer loop:
    ev = await wait_for(q.get(), heartbeat)
    -> on timeout: yield {"comment":"heartbeat"}
    -> on turn_end/error(for this turnId): yield ev; break
    -> else: yield ev（仅转发当前 turnId 事件，过滤 notification/他轮）
  (client disconnect -> consumer cancelled -> unsubscribe; producer 继续跑完，事件入 ring)
```

- producer 为独立 `asyncio.create_task`，**不随 SSE 断开取消**：即便前端断开，轮次仍跑完并落库，事件入 ring 供 GET /stream 续传。
- consumer 按 turnId 过滤，仅本轮 turn_* 收尾 SSE；notification 无 turnId 被 POST /chat 过滤，走 GET /stream。
- producer 任何异常转 error 事件分发，保证前端收到完整语义（turn_start -> error）。引用持有由 `chat` 同步加入 `inflight`，`_produce` 仅 `finally` 移除（B9）。
- 轮次隔离：POST /chat 只推当前轮 turn_*；GET /stream 推全部会话事件（对应综合设计 §四 持久通道多路复用）。并发多轮按 turnId 互不串扰。

> 注意：POST /chat 收尾后 SSE 关闭；GET /stream 为持久通道。前端若要持续接收通知，须连 GET /stream。

### 1.4 多实例扩展路径（当前单实例，接口不变）

| 维度 | 当前单实例 | 多实例扩展 |
|------|----------------|----------------|
| eventSeq | Broker 内存 next_seq | Redis INCR（SessionStore.next_event_seq 已预留） |
| ring buffer | Broker 内存 deque | Redis List + TTL 滑动窗口 |
| 通知广播 | 进程内 publish_notification | Redis Pub/Sub（频道 `general:agent:notify:{sessionId}`），各实例订阅后本地 distribute |

Broker 接口（distribute/subscribe/replay/publish_notification）不变，仅替换 seq/ring/notify 的存储后端。当前单实例内存实现为零序列化零网络、延迟最低；多实例时切 Redis。

### 1.5 事件构造（events.py）

修复 B1，补齐：
- `notification(task_id, status, message?, trace_id?)` -> `event: notification`，无 turnId。
- `with_seq(raw_event, seq)` -> 设 `id=str(seq)` + `data.eventSeq=seq`。
- `heartbeat()` -> sse-starlette `{"comment":"heartbeat"}`，渲染为 SSE 注释行。

SSE id 行由 sse-starlette dict `{"id":..,"event":..,"data":..}` 驱动。

---

## 二、M6 治理与安全设计

### 2.1 鉴权

**决策 D3：可配置鉴权 + API Key + Cookie/Session + 网关信任模式**

| 方案 | 优点 | 缺点 |
|------|------|------|
| A. API Key（X-Api-Key） | 简单、网关卸载/轮换易 | 无用户级身份 |
| B. JWT | 携带 claims，用户级 | 签发/校验/过期管理复杂 |
| C. 网关信任（mTLS+头透传） | 内网最强隔离 | 依赖网关部署 |
| D. Cookie+服务端 Session | Web 终端用户原生（EventSource 自动带 cookie/重连/Last-Event-ID）、即时吊销 | 需服务端登录态（单实例内存，多实例需 Redis） |
| **E. A+C+D 组合可配置** | dev 放行 / api_key 校验 / session Web 登录 / 预留 jwt | - |

**选 E**：`security.auth_mode: disabled|api_key|jwt(预留)|session`。
- `disabled`（dev）：放行，按 `x-service/x-env/x-user` 头解析身份（API 调用方）。
- `api_key`：常量时间比较校验 `security.api_keys`（可配多 key）；失败 401 `AUTH`。
- `session`（Web 登录）：`governance_dep` 分流到 `_web_session_identity`，从 HttpOnly cookie 解析服务端登录态 -> `Identity(user=uid)`；滑动续期、即时吊销；配套用户体系/多会话/登录失败限流，详见 [02](./02-Web前端与认证设计.md)。
- `jwt`（预留）：**fail-closed**——占位分支抛 `GovernanceError(500, "CONFIG", ...)`，MUST NOT 静默放行（B11/错题本 E8）。

网关信任模式：鉴权通过后信任 `x-service/x-env/x-user` 头（**网关负责真实身份**），agent 不自建用户体系；生产前置网关 + mTLS，纵深防御。
- 失败 -> 401 `{"code":"AUTH","message":...}`（SSE/HTTP 统一）。
- 登录失败限流：`LoginSessionStore` 按 "IP+用户名" 计数，5 次/10 分钟锁定 -> 429 `LOGIN_LOCKED`（见 `auth.py`）。

### 2.2 env 白名单与多租户隔离

**决策 D4：白名单 + 配置级硬隔离**

沿用综合设计 §10.5：白名单 + 配置级硬隔离。
- `security.allowed_envs: list[str]`，如 `["dev","staging","prod"]`。
- 非法 env -> 400 `{"code":"VALIDATION","message":"env not allowed"}`。
- 叠加 Skill `allowed_envs`：Registry 过滤阶段即剔除（不进 prompt）+ env 白名单运行时校验，纵深防御。

### 2.3 限流

**决策 D5：内存令牌桶 + Redis 可换**

| 方案 | 优点 | 缺点 |
|------|------|------|
| A. 固定窗口 | 简单 | 临界突发不平滑 |
| **B. 令牌桶** | 允许突发、平滑 | 单机内存 |
| C. Redis 分布式限流 | 跨实例精确 | 多一跳 Redis |

**选 B（内存令牌桶）**：单实例足够，多实例可换 Redis（Lua 原子脚本）`RateLimiter.allow(key, cost)`，接口不变。
- 维度 `(user, env)`。
- `security.rate_limit.rps`（每秒补充）/`burst`（桶容量）。
- 超限 -> 429 `{"code":"RATE_LIMIT"}`，记 `agent.rate_limit.hits` metric + 审计。

### 2.4 脱敏

**决策 D6：structlog 处理器 + 递归掩码**

- `logging_setup.redact_processor`：敏感键（api_key/token/password/authorization/secret/api_keys/cookie/x-api-key 等）值掩码（保留首尾 2 字符 + `***`），递归处理 dict/event_dict。
- OTel httpx instrumentation 默认**不记请求体**（仅 URL/method/status/duration）；X-Api-Key/Authorization 不入 span，必要时 request_hook 清理。
- `OpenAICompatibleModel` 的 api_key（`llm.api_key`）仅存内存，不落日志。

### 2.5 审计

**决策 D7：独立 audit logger + 关键动作留痕**

- 独立 audit logger（structlog），可配独立 sink，`event="audit"`。
- 鉴权成功/失败、限流命中、通知接收等关键动作留痕（含 taskId、env 等）。
- 字段：actor(user)/action/resource/env/traceId/ts。
- 后续可对接企业统一审计中心（跨模块，本模块仅产日志）。

### 2.6 依赖组装

FastAPI 依赖链 `governance_dep`（按 `auth_mode` 分流：`session` -> cookie 登录态 `_web_session_identity`；其余 -> x-* 头 `resolve_identity` + `_check_auth`；随后 env 白名单 -> 限流）供 /chat、/stream、/sessions；`service_auth_dep`（api_key）供 /internal/notify；`/auth/*` 不走 governance_dep（未登录），`/auth/me` 自行校验 cookie；/health 免鉴权。`GovernanceError` 经 app 统一 handler 转 `{"code","message"}` JSON。

---

## 三、上下文治理（修复 B2）

**决策 D8：trim 按 token 预算保配对，summarize 预留**

| 方案 | 优点 | 缺点 |
|------|------|------|
| A. 不裁剪 | 简单 | 历史膨胀/超限 |
| **B. trim（按 token 预算保最近 N 条）** | 简单可控，无需额外 LLM | 丢早期上下文 |
| C. trim_then_summarize | 保留摘要 | 多一次 LLM 调用，成本/延迟 |

**选 B（当前）**；C 在 `context_strategy=summarize` 时启用（预留，需额外 LLM 调用，暂不实现）。
- token 估算：无 tokenizer 时用 `len(content)//4`（约 4 char/token，CJK 偏高估），config 可调。
- 裁剪保 system 不动 + 最近优先，**保 tool_call/tool_message 配对**：裁剪后若首条是孤儿 ToolMessage（前导 AIMessage(tool_calls) 已被裁掉），则向后跳过，避免无对应 tool_call 的 ToolMessage 喂给 LLM 致工具协议错乱。
- load_messages 带 DB `limit`（硬上限 500）+ runner 侧 max_context_tokens 裁剪，双层防护。

---

## 四、Skill 入参兼容（B7 经验）

**决策 D9：pydantic alias 双名（字段命名兼容）**

业务 Skill 的 args_schema 若需对接下游 REST 的 camelCase 等命名，用 `Field(alias="xxx")` + `model_config = ConfigDict(populate_by_name=True)`：LLM 按 schema 默认命名（snake_case）传参、或传 alias 名均可；`run` 内用规范名，调下游时再转下游命名，消除两侧命名不一致的脆弱性。框架本身不内置业务字段，业务 Skill 自行处理。

---

## 五、M5 / M8 实现要点

### 5.1 Skill 机制落地（文件级）

- `general_agent/skills/base.py`：`Skill`/`SkillContext`/`SkillRegistry`；`SkillContext` 携带 `env`/`user`/`session_id`/`services`（业务客户端注入点）；`Skill.to_tool()` 产出带 span+metric 的 `StructuredTool`，异常统一记 error（取异常 `code` 属性，缺省 INTERNAL）后 re-raise。
- `general_agent/skills/__init__.py`：`build_registry()`（显式注册，非自动扫描）；框架默认返回空注册表（纯对话），业务方在此 register 自己的 Skill。
- `general_agent/agent.py`：`build_agent` 构造 `ToolNode(tools, handle_tool_errors=_tool_error_handler)` 传入 `create_react_agent`；tools 为空时退化为纯对话；`_tool_error_handler` 把异常转 JSON（errorCode/message）作错误 ToolMessage 内容；系统提示词由 `agent.system_prompt` 配置。
- `general_agent/runner.py`：`on_tool_error` 分支（产 `tool_end{error,errorCode}`）+ tools 节点 `on_chain_end` 捕获错误 ToolMessage 持久化（按 `tool_call_id` 去重，成功/错误均持久化）。
- `general_agent/app.py`：`skill_registry` + `services`（业务客户端 dict）进 `app.state`（不预建 agent）。
- `general_agent/api/chat.py`：每请求构造 `SkillContext(services=app.state.services, ...)` -> `get_tools` -> `build_agent`。
- `general_agent/llm.py`：`OpenAICompatibleModel`（langchain-core `BaseChatModel` 适配 OpenAI 兼容 `/v1/chat/completions`，支持 `bind_tools`/流式 tool_calls，可注入 transport 测试）。
- `general_agent/stub_llm.py`（:9094）：OpenAI 协议本地 stub，供无真实 LLM 端点时联调。
- 业务接入：实现 `Skill` 子类 -> `build_registry()` 注册 -> 所需客户端放入 `app.state.services`；下游 REST 错误建议抛出带 `code` 属性的异常，以便映射 errorCode。

### 5.2 M8 可观测性落地（文件级）

- `general_agent/observability.py`：`XTraceIdPropagator`（复合 `TraceContext`+`Baggage`+`XTraceId`）；providers（`ParentBased(ALWAYS_ON)` + OTLP/console exporter）；7 项 metric instruments；`record_turn/llm/tool` + `record_span_error`；`severity_of` 映射；`setup_observability`/`instrument_app`/`shutdown_observability`。
- `general_agent/logging_setup.py`：`_add_context` 处理器注入 OTel trace/span + 请求级 contextvar（`env`/`user`/`session_id`/`turn_id`）。
- `general_agent/config.py` + `config.yaml`：新增 `observability` 配置块（enabled/service_name/deployment_environment/console/otlp/traces/metrics）。
- `general_agent/app.py`：`create_app` 起 `setup_observability`（创建 httpx 客户端前）+ lifespan shutdown；构建 app 后 `instrument_app`。
- `general_agent/api/chat.py`：`_derive_trace_id` 从 OTel span 派生（未启用回退 `X-Trace-Id`/生成）；`bind_request_context`；error 事件透传 `code`。
- `general_agent/runner.py`：手动 span（run_turn/load_history/agent_graph/append_messages）+ `record_turn`；保留 `max_tool_rounds` 兜底。
- `general_agent/llm.py`：`llm_call` span（model）+ `record_llm`（duration/tokens/errors）；`LLMError`（携带 code）+ `_classify_error`（httpx 状态码->error_code）。
- `general_agent/skills/base.py`：`Skill.to_tool()` 自带 `tool_call` span + `record_tool`（每 Skill 自带 M8 埋点；原 `tools.py` 的 `@instrumented_tool` 已并入）。
- `general_agent/api/health.py`：报告 `observability.{enabled,otlp_endpoint,initialized}`。
- `pyproject.toml`：加 `opentelemetry-api/sdk/exporter-otlp-proto-http/instrumentation-fastapi/instrumentation-httpx`。

---

## 六、实现落点

### 6.1 M7
- `events.py`：补 `notification`/`with_seq`/`heartbeat`。
- `broker.py`：重构为事件中枢（seq 内置 distribute、ring、fan-out、put_nowait 背压、replay、publish_notification）。
- `runner.py`：run_turn 不变，事件经 yield 后由 broker.distribute 打 seq。
- `api/chat.py`：POST /chat 改 producer/consumer 解耦 + 心跳 + eventSeq；producer 独立 task 同步持有引用（B9）。
- `turn_lock.py`（新，B12/错题本 E9）：`TurnLockRegistry` 按 `session_id` 提供 asyncio.Lock，**引用计数**回收（进入者在 await 前同步 `refs++`，仅 refs 归零且锁空闲才 pop），避免"释放与唤醒之间误删锁致互斥失效"；producer 在锁内跑 `run_turn`，同会话轮次串行。
- `api/chat.py`（session 模式）：会话行创建延后到 producer 内、锁内（`chat_sessions.create(..., session_id=预生成)`），producer 不随断开取消，消除极早断开的空孤儿会话；`turn_start` 携带 `sessionId`。
- `api/stream.py`（新）：GET /stream 续传 + 通知多路复用。
- `api/notify.py`（新）：POST /internal/notify 通知接入。
- `app.py`：`app.state.broker` 注入；`app.state.inflight` producer 引用集；`app.state.turn_locks = TurnLockRegistry()`。
- `config.py`/`config.yaml`：`heartbeat_interval`/`ring_size`/`sub_queue_size`。
- 跨模块契约：业务异步任务完成后回调 `POST /internal/notify`（见 `api/notify.py`，服务间 api_key 鉴权），消息体 `{sessionId, taskId, status, message?, traceId?}`。

### 6.2 M6
- `security.py`：`governance_dep`（按 auth_mode 分流 session/x-* 头）/`service_auth_dep`/`TokenBucket`/审计 helper/`GovernanceError`；jwt 占位 fail-closed（B11）。
- `auth.py`（新，Web）：PBKDF2 密码哈希/校验、`LoginSession`/`LoginSessionStore`（内存 TTL + 滑动续期 + 即时吊销 + 登录失败限流 5 次/10 分钟）。
- `user_store.py`（新）：`UserStore`（MySQL `agent_user` 表，建用户/按名/按 uid 查询，冲突抛 `UserExistsError`）。
- `chat_session_store.py`（新）：`ChatSessionStore`（MySQL `agent_chat_session` 表，多会话 CRUD + uid 归属校验 + touch）。
- `api/auth_routes.py`（新）：`POST /auth/register|login|logout`、`GET /auth/me`；防用户枚举（统一 401 INVALID_CREDENTIALS）+ 登录限流（429 LOGIN_LOCKED）。
- `api/sessions.py`（新）：`GET/POST /sessions`、`GET /sessions/{id}/messages`、`PATCH/DELETE /sessions/{id}`，越权 404。
- `logging_setup.py`：`redact_processor`。
- `api/chat.py`/`api/stream.py`：接入 `governance_dep`；session 模式做会话归属校验。
- `config.py`/`config.yaml`：`security.*`（含 `session`/`web` 子块）。
- `observability.py`：`agent.rate_limit.hits` metric。
- 服务间鉴权：`POST /internal/notify` 经 `service_auth_dep`（X-Api-Key）；详见 `security.py`。Web 认证完整设计见 [02](./02-Web前端与认证设计.md)。

### 6.3 上下文治理 / Skill 机制
- `runner.py`：token 预算裁剪（`_trim_history`，保 tool 配对不孤儿）+ **`_drop_unanswered_tool_calls`（B10/错题本 E7）**：载入历史后与持久化前两处剔除无 ToolMessage 的孤儿 tool_calls，空 AIMessage 丢弃，防 OpenAI 400 污染。
- `turn_lock.py`（新）：`TurnLockRegistry` 同会话轮次串行（引用计数回收），见 §6.1。
- `skills/base.py`：`SkillContext.services` 业务注入点；异常统一取 `code` 属性映射 errorCode。
- `message_store.py`：load_messages limit 支持、`load_web_messages` 历史回放。

### 6.4 测试
- `tests/test_m7_broker.py`：seq/ring/replay/fan-out/背压/通知缓冲/会话隔离。
- `tests/test_m7_chat_stream.py`：POST /chat 心跳+eventSeq+续传；GET /stream 真实服务器 replay/notification。
- `tests/test_m6_security.py`：鉴权/env 白名单/限流/脱敏/审计。
- `tests/test_m6_integration.py`：api_key 拒纳、env 拒纳、限流 429、/internal/notify 服务间鉴权、/health 免鉴权。
- `tests/test_context_trim.py`：保 tool 配对不孤儿。
- `tests/test_auth_web.py`：Web 注册/登录/登出/me、未登录 401、cookie 对话、会话 CRUD/归属 404、登录限流。
- `tests/test_turn_lock.py`：同会话串行、引用计数回收不互斥失效。
- `tests/test_redis_broker.py`：RedisBroker 序列/环形缓冲/Pub-Sub（多实例）。
- 基建：MockTransport/TestClient/uvicorn 真实服务器/FakeStore/FakeUserStore/FakeChatSessionStore。

---

## 七、验证矩阵

| 模块 | 验证手段 | 结果 |
|------|----------|------|
| M1 骨架 | 配置加载 | env/yaml/默认三级合并 |
| M2/M4 核心 | POST /chat 端到端 | turn_start..turn_end，含 id/eventSeq/心跳/续传，roles 持久化 user->assistant->tool->assistant |
| M3 会话 | SessionStore 单元 | next_event_seq 预留多实例；当前 Broker 内存 seq |
| Skill 机制 | 工具链路端到端 | 内联 DemoSkill 驱动；成功流/错误流端到端，工具异常不杀轮次 |
| M6 治理 | 集成测试 | 鉴权/env/限流/脱敏/审计全通过 |
| M7 稳定 | 单元+真实服务器 | Broker 单元 + GET /stream replay/心跳/notification |
| M8 可观测 | metric/rate_limit span | instrument 禁用时门控正确；InMemory exporter 验证 span 树/traceId/metric |

> 端到端 SSE 序列：turn_start/turn_delta/tool_*/turn_end/error，事件带 id/eventSeq；持久化角色 user->assistant->tool->assistant 顺序正确。

### Skill 端到端验证（覆盖 `app.state`：MockTransport 注入 OpenAI SSE LLM + FakeStore，内联 DemoSkill）
- **成功流**：LLM 发起工具调用 -> `tool_start` -> `tool_end{success, result:{...}}` -> `turn_delta` -> `turn_end{stop}`；持久化角色 `user->assistant(tool_calls)->tool->assistant`。
- **错误流**：工具抛带 `code` 异常（DemoSkill 对 `task_id=bad` 抛 NOT_FOUND）-> `tool_end{error, errorCode:NOT_FOUND}` -> **LLM 收到错误 ToolMessage 继续反应** -> `turn_delta` -> `turn_end{stop}`（轮次未崩溃，「错误不杀轮次」确认）；错误 ToolMessage 已持久化。

### M8 端到端验证（InMemory exporter + httpx MockTransport + TestClient）
- **Span 树**：`POST /chat`(server) -> `run_turn` -> `load_history` / `agent_graph` -> `llm_call`×2(含 httpx client span) / `tool_call` / `append_messages`，结构完整。
- **traceId 一致性**：`X-Trace-Id` 头与 `traceparent` 头的 trace_id 均正确传播到全部 span 与 SSE 事件；无头时后端生成。
- **Metric/死循环兜底**：7 项 metric 记录正确；`max_tool_rounds` 触发受控 `turn_end` 不算 ERROR span。

---

## 八、关键实现坑与解法

- **sse-starlette dict 驱动渲染**：`EventSourceResponse` 接收 dict 经 `ServerSentEvent(**data)`：`{"comment":"heartbeat"}` -> `: heartbeat` 注释行；`{"id":..,"event":..,"data":..}` -> 产出 `id:` 行。续传 id 行靠此 dict 驱动。
- **TestClient 无法读无限 SSE 流**：TestClient/ASGITransport 会 `await` app 完成才返回 response.start，无法读取无限 SSE 流。**解法**：GET /stream e2e 改用后台 uvicorn 真实服务器 + httpx 验证 replay/心跳/notification。
- **instrument_app 门控**：原用 `if not _initialized`（禁用时仍为 True）会令无 provider 的 instrumentation 干扰流式 test transport。**解法**：改用 `is_enabled()`，禁用时不 instrument。
- **ToolMessage.content 为 JSON 字符串**：常为 JSON 串，直接塞 SSE 不结构化。**解法**：`_maybe_json` 解析为 dict 供 SSE 结构化 result（对齐 `result:{taskId,PENDING}`）。
- **turn_start 前置**：run_turn 原 load_history 在 `yield turn_start` 前 -> DB 故障时前端只收 error 无 turn_start。**解法**：前置 `turn_start`，DB 故障仍发 turn_start -> error。
- **create_react_agent ToolNode 默认错误处理不足**：`create_react_agent` 默认 `ToolNode` 的 `_default_handle_tool_errors` 仅捕 `ToolInvocationError`、其余 re-raise（会崩整轮）。**解法**：显式传 `ToolNode(handle_tool_errors=callable)`（`create_react_agent` 接受 `ToolNode` 实例，工具仍正确绑定模型）。开启后 `on_tool_error` 触发（带原异常 `.code`）、`on_tool_end` 不触发；错误 ToolMessage 仅从 tools 节点 `on_chain_end` 的 `output["messages"]` 可得。

---

## 九、后续迭代修复

- **B8 编码损坏**：上一轮写文件经 PowerShell stdin（ASCII）致中文变 `?`，含 Skill `description`（发给 LLM，功能性损坏）。本轮显式 UTF-8 写入，清理全部源码 docstring/注释/工具描述与本文档及通知契约，并固化写文件规范（见 §零）。
- **B9 producer 引用**：`chat` 同步 `inflight.add(producer)` 持有引用，消除 asyncio 任务 GC 理论窗口。
- ✅ **多实例 Redis 升级**：已实现 `RedisBroker`（Redis INCR 序列、Redis List 环形缓冲、Redis Pub/Sub 跨实例通知），与 `Broker` 接口一致，9 项单测通过。
- **真实 LLM 端点切换**：代码已就绪（`llm.base_url`/`llm.api_key`/`llm.model` 配置），属于运维切换，无需新代码；本地联调可用 `stub_llm.py`（:9094）。
- **服务间鉴权方案**：业务系统→agent 的异步通知经 `X-Api-Key` 鉴权（`service_auth_dep` + `security.api_keys`）；agent→LLM 端点经 `llm.api_key`（Bearer）。生产可在网关层正式化双向 TLS/mTLS。