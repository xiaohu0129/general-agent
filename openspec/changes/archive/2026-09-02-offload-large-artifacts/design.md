## Context

见 proposal.md - Why。现状：所有工具结果/助手输出全量内联写入 MySQL `agent_message.content`（MEDIUMTEXT，16MB 上限），无任何截断/外置（已核对 `runner.py`/`message_store.py`）；历史回放 `load_web_messages` 全量 `ORDER BY id ASC` 无分页。

约束（本项目现状）：

- 未上线、数据可重建——表结构可直接改 `CREATE TABLE`，无需 ALTER/迁移工具。
- 单实例部署——内存 Broker、内存登录态均不跨进程；本地磁盘 blob 与之一致，不引入新的跨实例能力。
- 已有可替换抽象先例：`Broker`(内存) / `RedisBroker`(多实例) 接口一致、drop-in 替换。BlobStore 照搬此模式。
- 事实源不变：`agent_message` 仍是转录权威；blob 是其"大字段外置"，向量库（Tier3）不在本期。

## Goals / Non-Goals

**Goals**

- 大工具产物/胖结果不进数据库行：MySQL 只存元数据 + 小内容 + 大内容的 head 摘要与引用。
- LLM 上下文构建不读取大对象；前端回放不内联大对象，改为按需下载。
- 历史回放分页，消除超长会话一次性全量返回。
- blob 存储以薄抽象封装，本地相对路径实现起步，预留 S3/OSS。
- 不破坏现有内联消息与 API（游标分页为新增可选参数；`content_ref` 为空即内联）。

**Non-Goals**

- 不做长期记忆/向量库（Milvus，Tier3）——另开 change。
- 不做用户文件"上传"链路（本项目尚无）。
- 不落地 S3/OSS 实现，仅留接口。
- 不引入 DB 迁移工具（未上线）。
- 不为 head 调用 LLM 做摘要（零额外 LLM 成本；智能摘要属未来 Tier3/summarize）。

## Decisions

### D1：三层存储定位

| 层 | 存什么 | 引擎 | 性质 |
|---|---|---|---|
| Tier1 元数据+小内容 | 消息行：角色/顺序/tool_calls/小 content/大内容的 head+ref | MySQL | 权威、有序、事务、可查 |
| Tier2 胖产物 | 超大工具结果/生成文件的完整字节 | 本地 blob 目录（本期）/ S3（未来） | 不可变、整块取、可由 ref 定位 |
| Tier3 语义记忆 | 偏好/事实/摘要向量 | Milvus（另案） | 派生、可重建 |

MySQL 与 blob 的分界按**字节阈值**，不按消息类型：聊天文本通常远低于阈值自然内联；胖产物超阈值外置。

**为什么不全放文件**：转录要有序分页、事务追加、按 uid/session 隔离过滤、级联删除——这些是关系库甜区、文件系统的短板。**为什么不全都放 MySQL**：大字段撑大备份/buffer pool、每轮被无谓 SELECT、超 16MB 存不下、无法"下载文件"。各取所长。

### D2：BlobStore 薄抽象 + 本地相对路径实现

```
class BlobStore(Protocol):
    async def put(self, scope: tuple[str,...], body: bytes, *, ext: str) -> str: ...  # 返回相对 key
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...                                    # best-effort
    def local_path(self, key: str) -> Path: ...                                      # 供 FileResponse 流式发送
```

- 本地实现 `LocalBlobStore`：根目录默认 `./artifacts/`（新增 `artifacts.dir` 配置项，相对路径基于服务工作目录）。
- key 全由服务端 id 构成：`{uid}/{session_id}/{turn_id}/{uuid4hex}.{ext}`；文件名用随机 uuid（不用 tool_call_id/message_id，避免同一作用域内多次写入碰撞），**不拼接任何用户输入**；路径段经白名单正则清洗 + `resolve()`/`parents` 越界校验，从根上杜绝路径穿越。目录按需创建。
- `content_ref` 存相对 key（非绝对路径、非 URL），便于迁移根目录/将来换 S3 时前缀可映射。
- 与 Broker/RedisBroker 同构：业务代码依赖抽象，未来加 `S3BlobStore` 即可 drop-in。
- 下载不引入 `open_stream`：首版 `get_artifact` 返回 blob 的本地路径 + kind，端点用 FastAPI `FileResponse(path, media_type, filename=...)` 由 ASGI 服务器分块流式发送，避免大文件全量读进内存；S3 后端未来可改为返回预签名 URL 或流式响应，端点签名不变。

**备选**：(a) 直接写文件不抽象——多实例/上云要返工；(b) 一上来接 S3——本地开发/单机部署增加依赖与配置。选薄抽象 + 本地实现，成本与 (a) 几乎相同，保留 (b) 的演进路径。

### D3：阈值分流 + 机械 head

- 写入时按序列化后字节数判断：`size <= artifacts.inline_threshold`（默认 **32KB**，可配）内联；否则外置。
- 外置时行内 `content` 存机械截断 head（前 N 字符，N 可配，默认约 2000 字符）+ 截断标记；`content_ref`/`content_size`/`content_kind` 记录元信息。
- head **不调 LLM**：零额外成本与延迟；智能摘要留给 summarize/Tier3。
- **写 blob 失败兜底**：外置写盘失败（磁盘满/目录不可写）时不中断落库——回退为内联全量内容；若全量仍超 `MEDIUMTEXT`（16MB）则退化为 head 截断，保证该轮消息可落库、`turn_end` 正常发出（与删除路径同为 best-effort 哲学）。
- **实时 SSE 与历史回放的差异**：外置只作用于持久化层。当轮 `tool_end` SSE 事件仍携带**全量**结果（实时路径不回填 `content_ref`/`messageId`，改动较大，留待另案）；刷新后经历史回放看到的是 head + 下载入口。即"实时全量展示、回放外置按需下载"。

**备选**：外置时调 LLM 生成摘要——质量高但每大产物多一次 LLM 调用、慢且贵、失败要兜底。本期用机械截断。

### D4：表结构（直接改 CREATE，不迁移）

`agent_message` 新增三列（改 `mysql_client.SCHEMA_DDL`）：

```
content_ref   VARCHAR(512) NULL   -- 外置 blob 的相对 key；NULL=内联
content_size  BIGINT NULL         -- 原始字节数
content_kind  VARCHAR(32) NULL    -- json/text/file ...
```

`content` 保持 `MEDIUMTEXT NOT NULL`（外置时存 head）。因未上线，部署/开发时 drop 表或删库让其按新 DDL 重建，不做 ALTER、不引 alembic。

### D5：读路径三处区别对待

| 消费者 | 方法 | 对外置消息的处理 |
|---|---|---|
| LLM 上下文 | `load_messages` | 只取行内 head；**绝不拉 blob**（反正超 token 预算会被 trim） |
| 前端回放 | `load_web_messages` | 返回 head + `contentRef`/`contentSize`/`contentKind` 标志，不内联 blob |
| 完整产物 | 新增下载端点 | 流式返回 blob，透传 Content-Type |

下载端点：`GET /sessions/{sessionId}/artifacts/{messageId}`，经 `governance_dep` 登录 + 会话归属校验（`_owned_session`，越权 404）+ SQL 以 `(service,env,user,session,id)` 定位且 `content_ref IS NOT NULL`，再由 `message_store.get_artifact` 返回 blob 本地路径，用 `FileResponse(path, media_type, filename=...)` 流式发送（Content-Type 按 kind 透传：json→application/json、text→text/plain，其余 octet-stream；带 `Content-Disposition` 文件名）。不接受任意路径参数，`message_id` 为整型路径段，blob 路径只由服务端 id 定位。

### D6：历史回放 keyset 分页

- `GET /sessions/{id}/messages?before=<messageId>&limit=<n>`：`WHERE ... AND id < before ORDER BY id DESC LIMIT n`，取到后反转为升序；`before` 缺省从最新开始。
- 响应：`{messages:[...升序...], nextCursor, hasMore}`；`nextCursor` = 本页最小 id，`hasMore` = 本页满额。
- keyset 而非 OFFSET：深分页性能稳定、不跳项（OFFSET 在新消息插入时会漂移）。
- `limit` 有默认值与硬上限（如默认 50、上限 200）。

### D7：级联删除

删除会话时，先查出该会话消息的 `content_ref` 集合，best-effort 删除 blob 文件（失败仅告警、不阻断），再删消息行与会话行（沿用现有 `chat_session_store.delete` 顺序）。

## Risks / Trade-offs

- **[外置后 LLM 只见 head]** 超大工具结果外置后，模型上下文里只有截断 head，无法回读全量（本期无"读产物"工具）。缓解：32KB 阈值覆盖绝大多数工具结果；Skill 作者应让工具返回精炼结论、把大数据外置并在返回值里带摘要（本就是好实践）；未来若需要，可提供受控的"按偏移读取产物"工具或让 Skill 自行摘要。**这是明确的行为取舍**，需在实现时让 head 截断标记清晰可辨。
- **[本地 blob 不跨实例]** 与内存 Broker/登录态同属单实例约束：多副本下产物可能落在另一实例。缓解：写入部署文档（单实例）；BlobStore 抽象使上 S3 即可解；多实例前置条件与 Redis 登录态并列。
- **[容器重建丢产物]** 容器文件系统易失。缓解：`artifacts.dir` 挂持久卷；部署文档说明；blob 可由业务侧重新生成（非权威事实源，事实在 MySQL/下游）。
- **[机械截断 JSON 截在中间]** head 可能是非法 JSON 片段。缓解：head 仅用于展示/上下文提示，不作为可解析数据；完整内容始终在 blob。
- **[目录增长/备份]** `artifacts/` 需进 `.gitignore`、`.dockerignore`，备份策略与 MySQL 分开考虑；暂无 TTL/GC（可后续加）。

## Migration Plan

1. 改 `SCHEMA_DDL` 加三列；开发/部署环境 drop `agent_message` 表（或删库）重启，服务惰性建新表。未上线，无数据保留需求。
2. 新增 `artifacts` 配置块（`dir`/`inline_threshold`/`head_chars`）；默认 `./artifacts/`、32KB、2000 字符。
3. 实现 `LocalBlobStore` 并在 `app.state` 注入；`message_store` 写入分流 + head + blob 读写；`runner` 持久化/上下文走新逻辑。
4. 新增下载端点 + 归属校验；`chat_session_store.delete` 级联 blob。
5. `load_web_messages` 改 keyset 分页；`api/sessions.py` 暴露游标参数。
6. 前端：工具卡片对 `contentRef` 消息显示"查看完整/下载"；历史按 `nextCursor` 分页加载。
7. `.gitignore`/`.dockerignore` 加 `artifacts/`；部署文档补单实例 blob 约束与卷挂载。
8. 回滚：新列/新目录对旧代码无害（旧代码忽略新列）；如需回滚，恢复旧代码即可，外置消息的完整内容在 blob（旧代码不读，但不影响内联消息）。

## Open Questions

- 阈值默认 32KB、head 默认 2000 字符是否合适？（均可配，先按此实现，压测后调。）
- 下载端点最终路径与是否支持 `?download=1` 强制附件下载 vs 浏览器内联预览（图片/PDF）——倾向透传 Content-Type 并支持内联，下载由前端 `a[download]` 控制。
- 是否需要 blob TTL/垃圾回收（孤立产物清理）？本期不做，列入后续。
