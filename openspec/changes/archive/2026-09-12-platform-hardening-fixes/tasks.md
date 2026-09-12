# Tasks: platform-hardening-fixes

> 约定：后端测试 `.\.venv\Scripts\python -m pytest`（不依赖外部服务）；前端 `cd front && npm test -- --run && npx tsc --noEmit`。每组先红后绿（TDD）。

## 1. Agent 上下文 token 估算（D1）

- [x] 1.1 先写失败测试：`tests/test_context_trim.py`（或新增 test_token_estimate）覆盖——纯中文 4000 字估算 ≈4000（旧实现约 1000）、纯 ASCII 仍约 `len//4`、中英混合按字符类别分别计、tool_call args 计入；验证断言先失败
- [x] 1.2 修改 `runner.py` 的 `_est_tokens` 为 CJK 感知实现并修正 docstring/注释方向；验证 1.1 转绿、既有裁剪/孤儿清洗测试不破

## 2. Broker 与限流器的资源有界（D2）

- [x] 2.1 先写失败测试：Broker 在"无订阅者且空闲超过 `session_ttl`"后 seq/ring 状态被懒扫描清除、再来事件从 seq=1 重建；有订阅者/未超时不回收；验证：`tests` 新用例先失败
- [x] 2.2 在 `Broker` 增加每会话 `last_active` 与懒扫描回收、构造参数 `session_ttl`；`broker.ring_size` 默认 256→2048（`config.py` `BrokerSettings` + `config.yaml` 注释同步）；验证 2.1 转绿、既有 `test_m7_broker.py` 全绿
- [x] 2.3 先写失败测试：`TokenBucket` 长期不活跃 key 在后续判定中被清扫（key 数不随历史身份无限增长）；修改 `security.py` 实现懒清扫；验证测试转绿

## 3. 全鉴权模式会话归属（D3）

- [x] 3.1 先写失败测试（`ChatSessionStore` 内存 fake + 直连方法级）：`claim_if_absent` 首次登记成功且幂等、第二身份 claim 同 sessionId 返回不归属、`get_owned_scoped` 四元组匹配语义（同 uid 不同 service/env 不匹配）；验证先失败
- [x] 3.2 `chat_session_store.py` 实现 `claim_if_absent`（INSERT IGNORE + SELECT 归属）与 `get_owned_scoped`；fake store（tests/conftest 等）同步；验证 3.1 转绿
- [x] 3.3 先写失败测试（API 级，api_key/disabled 模式）：身份 A 首轮 POST 认领成功、身份 B POST 同 sessionId 返回 404 SESSION_NOT_FOUND 且不落消息；B GET /stream 返回 404；A 的 /stream 正常；升级前无 owner 行的会话被 A 首轮认领后历史可载入；验证先失败
- [x] 3.4 修改 `api/chat.py`（所有模式注入 chat_sessions、进入路由前 claim+归属校验、移除按模式置 None 分支、`session_key` 迁入本模块）与 `api/stream.py`（统一 `get_owned_scoped` 校验，session 模式行为不变）；验证 3.3 与既有 web/auth 测试全绿

## 4. 认证面硬化（D5、D6）

- [x] 4.1 先写失败测试：登录不存在用户时代码路径执行一次 PBKDF2（dummy hash，可用 spy/耗时下限或校验 `verify_password` 被调用的方式断言）且返回统一 401；实现 `auth.py` dummy 校验并接入 `auth_routes.py`；验证转绿
- [x] 4.2 先写失败测试：同用户名跨多个 IP 失败 5 次后新 IP 登录返回 429 LOGIN_LOCKED；成功登录清除计数；修改 `LoginSessionStore` 增加 username 维度计数与 `login_locked` 双判定；验证转绿
- [x] 4.3 删除 `LoginSessionStore.get` 中恒真的 compare_digest；验证全部既有 auth 测试仍绿
- [x] 4.4 先写失败测试：同 IP 连续注册超过突发上限返回 429 且不建用户；新增 `security.register_rate` 配置（enabled/rps/burst，默认 10/min、burst 5）、`app.state.register_limiter`，`/auth/register` 入口校验并审计；验证转绿

## 5. 半成品与死代码减面（D4）

- [x] 5.1 删除 `general_agent/redis_broker.py` 与 `tests/test_redis_broker.py`；删除 `general_agent/session.py` 及 `api/chat.py` 中 `SessionStore` 导入与 save_session 调用；全仓 grep 确认无残留引用；验证 `pytest` 收集与全量通过
- [x] 5.2 删除 `MessageStore.delete_older_than`、`UserStore.get_by_uid` 及两个 fake 中的同名冗余方法；验证 grep 无引用、全量测试绿
- [x] 5.3 lifespan shutdown 增加 `close_redis()`（包裹 best-effort，未配置不报错）；验证应用启停测试/现有 lifespan 测试通过

## 6. LLM/Embedding 适配硬化（D8）

- [x] 6.1 先写失败测试：构造 `OpenAICompatibleModel(temperature=0.7)` 时非流式/流式请求体均携带 0.7（MockTransport 捕获）；`app.create_app` 按 `llm.temperature` 传参；验证先失败后转绿
- [x] 6.2 先写失败测试：流式响应中夹一个损坏 JSON 的 `data:` 分片时，该分片被跳过（告警），前后正常分片继续、流正常结束；修改 `llm.py._astream` 逐分片 try；验证转绿
- [x] 6.3 重构：模型与 EmbeddingClient 支持注入共享 `httpx.AsyncClient/Client`，`create_app`/lifespan 创建并在 shutdown 关闭；测试继续用 MockTransport 注入。验证新增"两次调用复用同一 client"的测试与全部既有 LLM/embedding 测试绿

## 7. MySQL 连接保活与配置（D9、D14）

- [x] 7.1 `mysql_client.create_pool` 增加 `pool_recycle=cfg.pool_recycle`（`MysqlSettings` 新增，默认 1800）；`config.yaml` 增加注释项；验证：单测断言 create_pool 调用参数含 pool_recycle=1800（默认）与配置覆盖值

## 8. 入参边界（D12）

- [x] 8.1 先写失败测试：POST /chat 空串/纯空白/8001 字符返回 400 VALIDATION 且不写消息不调 LLM（FakeStore 无 append、模型无调用）；`clarify_selection.value` 超 200 返回 400；实现后转绿
- [x] 8.2 先写失败测试：POST /internal/notify 的 status 超 32、message 超 2000、sessionId/taskId/status 缺失返回 400 且不产生事件；`NotifyBody` 加 Field 约束后转绿

## 9. Skill 注册校验（D13）

- [x] 9.1 先写失败测试：注册空名/含空格/中文/超过 64 字符/重名均抛错，合法名与既有内置注册成功；修改 `SkillRegistry.register`；验证转绿且 `build_registry()` 组装期不报错

## 10. 健康检查聚合（D11）

- [x] 10.1 先写失败测试：Redis 已配置但 ping 异常时 `/health` HTTP 200 且 `status="degraded"`；`routing_status.mode=="degraded"` 时 degraded；全部正常（含主动 stub 的 rule+keyword）为 ok；端点自身不抛异常；修改 `api/health.py` 后转绿

## 11. 前端：历史状态、缺口检测、有界去重（D7、D10）

- [x] 11.1 先写失败测试：`historyToMessages` 对 tool 行 DTO 映射真实 `status`（error/success），error 工具卡刷新后显示失败；扩展 `types.ts` HistoryMessage（tool 行/status），后端 `load_web_messages` 输出 tool 行并派生 status（content JSON 含 errorCode → error），后端先补单测（FakeStore 构造含错误工具行的页面）
- [x] 11.2 先写失败测试：`eventGate.ingest` 中 post 通道 seq 跳号永不返回 gap=true（仍推进高水位/去重），stream 通道跳号仍报 gap；实现通道判定
- [x] 11.3 先写失败测试：`seen` 改 LRU（容量 2000），灌入超量事件后集合大小封顶、最旧键淘汰但近期键去重仍生效；实现后转绿
- [x] 11.4 先写失败测试（ChatPage 集成）：stream gap 触发重拉成功后提示条出现并在 3 秒后自动消失（fake timer），切会话/卸载清理 timer；实现后转绿
- [x] 11.5 验证 `cd front && npm test -- --run` 全绿且 `npx tsc --noEmit` 无错误

## 12. 文档与配置同步

- [x] 12.1 `config.yaml` 同步：`broker.ring_size: 2048`、`broker.session_ttl`、`security.register_rate`、`mysql.pool_recycle`，注释与默认值一致
- [x] 12.2 更新 `docs/03-部署方案.md`：删除"RedisBroker 接口已预留"表述，改为多实例 backlog（重新交付的五项前置：replay 签名一致、不自发自收双投、监听重连、turn 事件跨实例、key TTL）；补 `pool_recycle`；补非 session 模式会话归属升级说明
- [x] 12.3 更新 `docs/02`：历史工具错误状态回放、缺口检测仅 /stream 通道与提示自动消失；更新 `docs/01` 错题/决策记录（中文 token 估算反向错误、RedisBroker 减面、归属模型）
- [x] 12.4 更新 `README.md` 变更记录："进行中"组增加本 change 条目（slug + 中文主题 + 主要作用）

## 13. 全量验证

- [x] 13.1 后端：`.\.venv\Scripts\python -m pytest` 全绿；前端：`npm test -- --run`、`npx tsc --noEmit`、`npm run build` 全绿
- [x] 13.2 `openspec validate platform-hardening-fixes --type change --strict` 通过；按 traceability 表人工核对每个 D 有场景或显式 Non-Goal/Open Question
- [x] 13.3 代码与文档一致性走查：grep 确认 256 默认值、RedisBroker、SessionStore、delete_older_than 等旧表述无残留（CHANGELOG/错题历史记录除外）











