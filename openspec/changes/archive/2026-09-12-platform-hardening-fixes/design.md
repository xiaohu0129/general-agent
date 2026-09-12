## Context

本 change 是一次跨模块缺陷硬化，见 proposal.md 的 21 项清单。当前形态约束：

- 内存 Broker 是唯一装配实现（`app.py:185`），`RedisBroker` 写了但**从未被装配**，且与 `stream.py` 的同步 `replay` 调用签名不兼容；docs/03 已明确单实例约束。
- 会话归属只在 `auth_mode=session` 校验（`chat.py:192`、`stream.py:35`）；api_key/disabled 模式仅靠共享凭证 + 自报 `x-*` 头，`agent_chat_session` 表在非 session 模式不写入。
- 全部测试用 FakeStore/MockTransport，不连真实 MySQL/Redis，长跑资源问题与跨组件时序问题无测试守护。
- 配置层 `llm.temperature` 存在但构造模型时未传入；`_est_tokens` 注释方向写反。

## Goals / Non-Goals

**Goals:**

- 消除会随运行时间/中文长会话必然发作的资源与正确性缺陷。
- 关闭认证面三个缺口（注册滥用、用户枚举时序、跨租户订阅）。
- 减面：移除从未接线且不兼容的半成品分布式 Broker 与只写不读的 SessionStore。
- 所有新行为可在"无外部服务"测试约束下验证（FakeStore/内存 MySQL mock/httpx MockTransport）。

**Non-Goals:**

- 不实现真正的多实例事件中枢与 Redis 登录态（独立 change，deployment spec 已登记前置条件）。
- 不引入 tiktoken 等真实分词依赖；不做分布式令牌桶。
- 不做人机验证/验证码；不改 SSE 事件协议；不清理已声明的 summarize 等预留配置。
- 不回填存量消息的工具状态（仅影响历史回放展示，新写入数据天然正确）。

## Decisions

### D1：CJK 感知的 token 粗估（缺陷 1）

`_est_tokens` 改为逐字符：CJK 统一表意范围（含中文标点 `\u3000-\u303f`、`\u4e00-\u9fff`、全角`\uff00-\uffef`）每字计 1，其余字符 `len//4`；tool_call args 用同一函数计入。

- 为什么：主流分词器中文约 0.6~1 token/字，旧 `//4`（0.25）系统性低估；宁可轻微高估（更早裁剪）也不超窗 400。
- 备选：接 tiktoken——拒绝（新增二进制依赖、与国产模型分词不一致、离线环境约束）；统一 `len//2`——对西文过高估、裁剪过激，拒绝。
- 裁剪/孤儿清洗算法不动，仅换估算函数。

### D2：Broker 会话状态空闲 TTL 回收 + ring 容量上调（缺陷 2、11）

Broker 内为每会话记录 `last_active`（每次 distribute 更新）。回收触发：懒扫描——在 `distribute`/`subscribe` 入口以低成本条件（如会话数超过扫描阈值时）扫描一次，清理"`_subs` 为空且 `now-last_active > session_ttl`"的会话条目（seq/ring/last_active）。不引入后台 task（单事件循环下懒扫描无竞态、测试可控）。

- ring 默认 256→2048（token 级 delta 下一条长回复数百事件；单事件不大，2048 × 平均事件体积的内存量经 TTL 回收有界）。
- seq 纪元重置：回收后再来事件从 1 开始；断线续传本就只承诺 ring/TTL 窗口，旧 Last-Event-ID 大于新纪元时 replay 返回空，live 正常投递（spec 已写明）。
- 备选：后台定时清扫 task——拒绝（生命周期/测试复杂度，懒扫描足够）；LRU 按条数淘汰会话——拒绝（TTL 语义对"长期不活跃"更直观可配）。
- TokenBucket 同构处理：每 key 记最后访问时间，`allow()` 中顺带按比例清扫超过补充窗口未活跃的 key（缺陷 2 的限流器部分）。

### D3：全鉴权模式统一会话归属，owner 落 MySQL（缺陷 5）

`agent_chat_session` 表已含 `service/env/uid` 列，作为唯一 owner 权威：

- 新增 `ChatSessionStore.claim_if_absent(session_id, service, env, uid, title)`：`INSERT ... ON DUPLICATE KEY UPDATE` 后按四元组查归属（或先 INSERT IGNORE 再 SELECT），幂等；返回归属是否属于当前身份。
- 新增 `get_owned_scoped(session_id, service, env, uid)`；session 模式的 `get_owned(session_id, uid)` 保留（web 固定 service/env，等价）。
- `POST /chat`：所有模式下进入路由前先 claim+校验；无主会话由首次写入者认领（兼容升级前历史会话，见 Migration）。
- `GET /stream`：所有模式先 `get_owned_scoped`，不匹配/无记录 → 404。
- `api_key/disabled` 模式下 `chat_sessions` 不再按模式置 None（`chat.py:189` 的分支调整为始终注入）；`/sessions` 管理接口仍仅 session 模式使用（api_key 调用方用自管 sessionId）。
- `session_key(service,env,user)` 保留为无 sessionId 时的稳定隐式 ID，函数从将被删除的 `session.py` 迁到 `chat.py` 内部（或小工具位置）。
- 备选：owner 存 Redis——拒绝（Redis 是可选项，归属必须强一致，且已有 MySQL 表）；在消息表上推断 owner——拒绝（竞态、无法区分无消息的新会话）。

### D4：移除 RedisBroker 与 SessionStore（缺陷 6、20、21）

- 删除 `redis_broker.py` + `tests/test_redis_broker.py`：五个硬伤（replay async 与 stream.py 同步调用不兼容、notification 自发自收双投、listener 无重连、turn 事件不走 Pub/Sub、key 无 TTL）不是小修能救，且从未接线；多实例重新设计时以独立 change 交付（deployment delta 已把"不得附带未装配半成品"写入规格）。
- 删除 `session.py` 整个模块：唯一调用是 `chat.py` 每轮 `SessionStore().save_session` 只写不读（owner 权威归 MySQL 后更无意义；实例每次现建也没有复用价值）。
- 删除 `MessageStore.delete_older_than`（无调用）、`UserStore.get_by_uid` 及两个 fake 中的同名冗余方法（保留 fake 对真实接口的实现）。
- docs/03 多实例段落改写为"backlog + 前置条件"，删除"接口已预留"表述；README 同步。
- `close_redis()` 接入 lifespan shutdown（当前从未被调用）。

### D5：登录时序拉平（缺陷 4）

`auth.py` 增加模块级 `_DUMMY_HASH = hash_password("...")`（固定随机 salt 启动期生成一次即可，或常量）。登录路径用户不存在时执行 `verify_password(password, _DUMMY_HASH)` 后再返回统一 401，使两条路径都执行一次 200k PBKDF2。

- 备选：随机延时——拒绝（不可靠且引入噪声）。

### D6：登录锁定双维度 + 注册限流（缺陷 3、13、19）

- `LoginSessionStore` 增加 username 维度失败计数（与 ip|username 同窗口、同阈值 5/10min）；`login_locked` 任一维度命中即锁。接受锁号 DoS 权衡：窗口短（10min）自动恢复，且审计日志可见；攻击者原本就能用 ip|username 维度锁定已知账号，新增维度主要防"多 IP 各试 4 次"撞库。
- 删除 `get()` 中恒真的 `compare_digest(sess.token, token)`（dict 已按键命中）。
- 注册限流：`app.state.register_limiter = TokenBucket(...)`，配置 `security.register_rate.{enabled,rps,burst}`，默认 10 次/分钟（rps≈0.167）、突发 5；`/auth/register` 入口直接校验，超限 429 `RATE_LIMIT`。
- 备选：注册用独立登录失败同款固定窗口——也可行；实现复用 TokenBucket 更一致。

### D7：历史工具状态回放（缺陷 7）

`load_web_messages` 输出中纳入 `role="tool"` 行（当前查出但被丢弃）：`{messageId, role:"tool", toolCallId, status, content(head), contentRef/Size/Kind}`。`status` 由 Python 侧派生：content 去除空白后能解析为 JSON 对象且含非空 `errorCode` → `error`，否则 `success`。外置（offloaded）的错误工具结果无法从头摘要判定（错误 JSON 很小、实际不会超 32KB，接受该边界）。前端 `history.ts` 的 toolResults 携带 status 映射到卡片，不再硬编码 success；"stopped" 是前端瞬时态、不落库，历史无需表达。

### D8：LLM/Embedding 客户端复用与配置生效（缺陷 8、15、16）

- `OpenAICompatibleModel` 构造增加可选共享 `httpx.AsyncClient`/`httpx.Client` 注入（保留现有 `transport` 注入供测试）；app lifespan 创建带配置 timeout 的共享 client 并在关闭时 `aclose`；`EmbeddingClient` 同构（仅 async client）。
- `create_app` 构造模型时传入 `temperature=llm_cfg.temperature`（路由 LLM 复用同一模型实例，temperature 一并生效）。
- `_astream` 中每个 payload 的 `json.loads` 包 try：失败记录 warning（含原始分片前缀，脱敏由日志层保证）并 `continue`；`[DONE]` 与正常分片语义不变。

### D9：MySQL 连接保活（缺陷 9）

`aiomysql.create_pool(pool_recycle=cfg.pool_recycle)`，新增 `mysql.pool_recycle` 默认 1800s（MySQL 默认 wait_timeout 28800s，留出充裕余量）。

### D10：前端缺口检测收窄 + 提示自动消失 + 去重有界（缺陷 10、18）

- `eventGate.ingest(ev, channel)`：仅 `channel==="stream"` 时允许产出 `gap=true`；post 通道只做去重、不推进/不报告缺口（服务端已按轮过滤，seq 跳号无信息）。注意高水位仍由两通道共同推进，保证 stream 真正缺口可判。
- ChatPage 的 gapNotice 展示后 `setTimeout(3000)` 清除（新一轮发送/切会话的既有清除逻辑保留，timer 在卸载时清理）。
- gate 内部 `seen` 由 Set 改为基于 Map 的 LRU，容量默认 2000（插入序淘汰最旧）；高水位 seq 保留。

### D11：健康检查聚合状态（缺陷 17）

`/health` 聚合：Redis 已配置但 ping 失败 → degraded；`routing_status.mode=="degraded"` → degraded；否则 ok。HTTP 恒 200。主动配置 stub（mode=rule+keyword）不判降级。

### D12：入参边界（缺陷 14）

`ChatRequest` 增加路由内校验（Pydantic Field 限制不住"strip 后非空"，用 `model_validator` 或入口显式校验）：strip 后 1~8000；`clarify_selection.value` ≤200。`NotifyBody`：`sessionId/taskId/status` 非空、status≤32、message≤2000。越界 400 `VALIDATION`。

附注（实现期决策）：为满足"notify 缺字段/越界返回 400 而非 FastAPI 默认 422"，app.py 注册全局 `RequestValidationError` 处理器，统一映射为 400 `VALIDATION`（响应体与 GovernanceError 同形 `{code, message}`）。影响面为全站请求校验错误（含 query/path/header 校验，如 `/stream` 的 `lastEventId`），实现时已核对全量测试无 422 依赖。

### D13：Skill 注册 fail-fast（缺陷 12）

`SkillRegistry.register` 校验 `name` 正则 `^[A-Za-z0-9_-]{1,64}$` 与全局唯一，违规抛 `ValueError`；`build_registry()` 在应用组装期运行，错误立即暴露。

### D14：配置项与默认值变更汇总

新增：`broker.session_ttl`（86400）、`security.register_rate.{enabled,rps,burst}`、`mysql.pool_recycle`（1800）；变更默认：`broker.ring_size` 256→2048。`config.yaml` 注释同步。

## 决策-规格追溯表

| 决策 | 规格需求 | 关键 Scenario |
|---|---|---|
| D1 | agent-loop《上下文 token 预算裁剪》 | 中文长会话实际 token 不超窗 |
| D2 | conversation-broker《会话事件中枢》 | 空闲无订阅回收 / 活跃不被回收；governance《令牌桶限流》长期不活跃被清扫 |
| D3 | governance-security《会话归属统一校验》 | 跨租户订阅被拒 / 跨身份写入被拒 / 首次写入认领 / 历史会话认领 |
| D4 | deployment《多实例与 HTTPS 演进前置》 | 升级多实例前置条件（含五项缺陷消除）；无对应行为场景的删除项为纯减面，不立新场景 |
| D5 | web-auth-session《防用户枚举与登录失败限流》 | 响应内容与耗时不可区分 |
| D6 | 同上 + 《注册接口频率限制》 | 分布式撞库按用户名锁定 / 批量注册被限流 |
| D7 | web-auth-session《历史工具调用状态回放》 | 失败工具刷新后仍失败 / 成功回放成功 |
| D8 | llm-adapter《确定性采样参数》《流式分片协议兼容》 | 显式 temperature 生效 / 畸形分片不中断流 |
| D9 | deployment《MySQL 连接保活》 | 长空闲后首请求不失败 |
| D10 | web-auth-session《前端事件缺口检测的通道边界与去重有界》 | POST seq 跳号不报缺口 / 提示自动消失 / 游标有界 |
| D11 | observability《观测可配置与健康检查暴露》 | Redis 不可达 degraded / 索引构建失败 degraded |
| D12 | chat-sse-protocol《对话请求消息体约束》、conversation-broker《异步任务通知接入》 | 空/超长消息 400 / 超长通知字段 400 |
| D13 | skill-plugin《显式注册与按环境动态过滤》 | 非法名/重名注册即失败 |
| D14 | 无独立场景 | 配置默认值由 D2 等的场景隐含覆盖 |

纯内部、无外部可观察行为变化的项（D8 的连接复用、D4 的死代码删除、D2 懒扫描实现方式、D6 删除恒真比较）不立新场景，在 tasks 中以单测/代码审查验证。

## Risks / Trade-offs

- **[BREAKING：非 session 模式归属收紧]** 共享 API Key 的多个不同身份若过去混用同一 sessionId（A 写 B 订阅），升级后 B 收到 404。→ 缓解：首次写入者认领使合法单身份会话零迁移；该混用本身是跨租户泄漏路径。部署说明需在 docs/03 标注。
- **[认领竞态]** 两个身份同时首轮 POST 同一无主 sessionId：INSERT IGNORE 保证只有一个 owner，另一身份收到 404，不产生消息交错。
- **[token 估算变化使裁剪提前]** 中文会话上下文保留量变少（高估方向）。→ 预算可配；这是用"少一点上下文"换"不 400"，方向正确。
- **[按用户名锁定的 DoS 面]** 攻击者可对已知用户名制造 10 分钟锁定。→ 窗口自动恢复、审计可见；与既有 ip|username 锁定的 DoS 面同量级。
- **[ring 2048 内存上升]** 单会话缓冲增大 8 倍。→ TTL 回收保证总量 = 活跃会话 × 有界窗口，且事件体小。
- **[health degraded 噪声]** 本地 stub 开发若被误判降级会造成困惑。→ 仅 `mode=="degraded"`（构建失败）判降级，主动 stub 的 rule+keyword 不判。
- **[删除 RedisBroker 影响外部引用]** 若有部署直接 import 该模块会断。→ 全仓 grep 确认仅自身测试引用；它从未进入 app 装配，非公共 API。

## Migration Plan

1. 先发布代码（配置新键均有默认值，无需强制配置）。
2. 非 session 模式部署：无需数据迁移；首次 `POST /chat` 自动给历史会话补 owner 行（标题用默认值，仅首次）。如需预置 owner，可运维手动 INSERT。
3. 回滚：代码回退即可；新增 owner 行不影响旧代码（旧代码非 session 模式不读该表）；新配置键被旧版本忽略。
4. 删除模块（redis_broker/session）随同版本发布，无灰度依赖（均未接线）。

## Open Questions

- 前端去重 LRU 容量（暂定 2000）与缺口提示停留时长（暂定 3s）的具体数值，可在实现期据测试手感微调，不影响规格与方案。
