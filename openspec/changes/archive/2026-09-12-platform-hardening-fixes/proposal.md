## Why

一次全项目缺陷探测（319 后端测试 + 110 前端测试全绿前提下的代码走查）发现 21 个测试覆盖不到的缺陷，集中在四类：**生产长跑必发的资源问题**（Broker/限流器/前端去重集合无界增长、MySQL 死连接）、**安全面缺口**（注册无限流、登录可时序枚举用户、api_key 模式 `/stream` 跨租户窃听）、**配置与估算失真**（中文 token 低估 2~4 倍导致超长会话 400、`llm.temperature` 配置被静默忽略）、**半成品与死代码**（未装配且签名不兼容的 `RedisBroker`、多个从未调用的存储方法）。本 change 统一收口这批硬化修复，避免逐条小改造成规格与文档长期漂移。

## What Changes

- **中文感知的上下文预算（修缺陷 1）**：`runner._est_tokens` 改为按字符类别估算——CJK 字符每字约 1 token、其余沿用 `len//4`，修正"CJK 偏高估"的反向注释；超预算裁剪与孤儿消息清洗逻辑不变。
- **会话事件状态资源回收（修缺陷 2、11）**：内存 Broker 的 `seq/ring` 增加空闲 TTL 懒淘汰（新增 `broker.session_ttl`，默认 24h，计时基于最后 distribute 时间），最后一个订阅者退出且空闲超时后清除；ring 默认容量 256→2048（token 级 delta 下 256 不足一个长回复）；令牌桶状态按周期清扫过期 key；前端事件门 `seen` 去重集改为有界 LRU。
- **注册限流与登录时序拉平（修缺陷 3、4、13、19）**：`/auth/register` 增加按 IP 的限流（独立于对话限流，新增 `security.register_rate` 配置）；登录路径在用户不存在时执行一次 dummy PBKDF2 校验，使"用户存在/不存在"响应时序不可分辨；登录失败锁定增加 username 维度计数（多 IP 分布式撞库时按用户名锁定，接受由此带来的锁号 DoS 面，见 design D8）；移除 `LoginSessionStore.get` 中恒真的常量时间 token 比较（dict 按键命中后比较自己）。
- **统一会话归属校验（修缺陷 5）**：**BREAKING**（仅对 api_key/disabled 模式的异常用法）——所有鉴权模式下 `/chat`、`/stream`、`/sessions` 都校验 `sessionId` 与调用身份 `(service,env,user)` 的归属：`POST /chat` 对无 owner 行的会话执行首次写入者 `INSERT IGNORE` 认领（兼容升级前的历史会话），`GET /stream` 对无 owner 行或归属不符的会话返回 404；归属以 MySQL `agent_chat_session` 为唯一权威，不依赖可选 Redis。
- **移除未接线的 RedisBroker（修缺陷 6）**：**BREAKING**（仅对直接 import 该模块的假想调用方，生产从未装配）——删除 `redis_broker.py` 与其单测（存在 replay 同步/异步签名不兼容、notification 自发自收双投、listener 无重连、turn 事件不走 Pub/Sub、key 无 TTL 五个硬伤）；多实例事件中枢作为未来独立 change 重新设计，docs/03 改写为 backlog 说明。
- **历史工具状态真实回放（修缺陷 7）**：历史消息 DTO 中 `tool` 行携带从内容 JSON `errorCode` 派生的 `status`（error/success），前端历史还原不再把所有工具卡硬编码为 success。
- **LLM 适配硬化（修缺陷 8、15、16）**：构造模型时传入 `llm.temperature`（配置生效）；模型与 embedding 客户端复用共享 `httpx.AsyncClient/Client`（lifespan 关闭），不再每次调用新建连接；流式解析单个坏 chunk 跳过并告警，不再因一个畸形 JSON 杀掉整轮流式响应。
- **MySQL 连接保活（修缺陷 9）**：连接池增加 `pool_recycle`（新增 `mysql.pool_recycle`，默认 1800s），规避服务端 `wait_timeout` 断连后隔夜首请求失败。
- **消除前端缺口误报（修缺陷 10、18）**：seq 缺口检测只由 `GET /stream` 通道产生（POST 通道服务端已按 turnId 过滤、其 seq 跳号不携带信息）；"事件已过期，已刷新"提示在合并成功后定时自动消失；前端去重集有界化（与资源回收项合并）。
- **Skill 注册 fail-fast（修缺陷 12）**：注册时校验 `name` 非空、匹配 `^[A-Za-z0-9_-]{1,64}$`、全注册表唯一，违规直接抛错（启动期暴露配置问题，替代静默覆盖）。
- **入参边界（修缺陷 14）**：`POST /chat` 的 `message` 约束为去除空白后 1~8000 字符；`/internal/notify` 的 `status` 限长 32、`message` 限长 2000，越界返回 400 VALIDATION。
- **健康检查语义化（修缺陷 17）**：`/health` 的 `status` 在"配置了 Redis 但 ping 失败"或"路由启用但索引未就绪"时为 `degraded`（HTTP 仍 200，存活与就绪可区分）。
- **资源关闭与死代码清理（修缺陷 20、21）**：lifespan 关闭流程调用 `close_redis()`；删除只写不读的 `SessionStore` 整个模块（`session.py`）与 `chat.py` 中的 owner 落 Redis 调用——归属权威改为 MySQL 会话表后该模块无存在意义；删除 `MessageStore.delete_older_than`、`UserStore.get_by_uid` 及 fake/测试引用；未使用的预留配置不在本次删除（summarize 等预留项保留，避免扩大面）。
- **文档与错题本**：README/文档同步以上行为差异；新增错题记录（token 估算方向错误、半成品模块减面决策）。

**明确不做**：不重新实现多实例事件中枢（独立 change）；不引入真正的中文分词 tokenizer（粗估即可，D1 说明）；不做注册验证码/人机校验（仅限流）；不改 SSE 事件协议字段；不动 summarize 等已声明的预留配置项；不做令牌桶的 Redis 分布式实现（多实例 change 一并考虑）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `agent-loop`：上下文 token 预算估算改为 CJK 感知，预算语义与裁剪边界不变。
- `conversation-broker`：会话事件状态空闲回收与 ring 默认容量调整；通知入参长度约束。
- `chat-sse-protocol`：新增 `/chat` 请求消息长度约束。
- `governance-security`：全鉴权模式统一会话归属校验；限流状态有界；注册限流（与 web-auth-session 协同）。
- `web-auth-session`：注册限流、登录时序拉平与锁定维度；历史工具调用状态回放；前端缺口检测通道收窄与去重集有界。
- `llm-adapter`：temperature 配置生效、HTTP 客户端复用、流式坏 chunk 容错。
- `skill-plugin`：Skill 名称合法性与唯一性注册校验。
- `observability`：健康检查区分 ok/degraded。
- `deployment`：MySQL 连接池保活配置；多实例交付物现状（RedisBroker 移除）的文档化约束。

## Impact

- **后端代码**：`runner.py`、`broker.py`、`api/chat.py`、`api/stream.py`、`api/notify.py`、`api/auth_routes.py`、`api/health.py`、`security.py`、`auth.py`、`app.py`、`llm.py`、`embedding.py`、`mysql_client.py`、`config.py`、`skills/base.py`、`message_store.py`、`chat_session_store.py`、`user_store.py`；删除 `session.py`（SessionStore）、`redis_broker.py` 与 `tests/test_redis_broker.py`。
- **前端代码**：`chat/history.ts`（工具状态映射）、`chat/eventGate.ts`（有界 LRU、post 通道不产 gap）、`pages/ChatPage.tsx`（提示自动消失）、`api/types.ts`（历史 DTO status）。
- **配置**：新增 `broker.session_ttl`、`security.register_rate.*`、`mysql.pool_recycle`；`broker.ring_size` 默认值变更；`config.yaml` 示例同步。
- **API 行为**：api_key/disabled 模式新增会话归属 404（BREAKING，含首次写入认领兼容）；`/chat` 空/超长消息 400；`/health` 可能返回 `degraded`；`/internal/notify` 超长字段 400。
- **存储**：`agent_chat_session` 在非 session 模式也会写入 owner 行（表结构不变，已有 service/env 列）。
- **文档**：README 变更记录、`docs/01`（错题/决策）、`docs/03`（多实例 backlog、pool_recycle）、`docs/02`（历史工具状态、缺口提示行为）。
- **测试**：新增 token 估算 CJK 用例、Broker TTL 回收用例、归属校验跨模式用例（含历史会话认领）、注册限流/登录时序用例、坏 chunk 跳过、健康降级、Skill 名校验、入参 400、前端 LRU/post 不报 gap/提示消失；删除 RedisBroker 单测；全部测试继续不依赖外部服务。
