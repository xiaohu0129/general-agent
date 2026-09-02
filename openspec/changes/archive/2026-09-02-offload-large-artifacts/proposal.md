## Why

当前所有工具结果与助手输出都**全量内联**写入 MySQL `agent_message.content`（MEDIUMTEXT，上限 16MB）。一次多轮、含大量工具调用的会话会产生体量大的中间产物（大 JSON 报表、生成文件、长文档、base64 等），它们：① 撑大数据库/备份/buffer pool；② `load_messages` 每轮把它们 SELECT 出来（随即又被上下文 trim 丢弃，纯浪费 IO）；③ 超过 MEDIUMTEXT 上限存不下；④ 前端回放无法"下载完整产物"。

这类"胖产物"的两个消费者（LLM、前端）都不需要内联拿到全量：LLM 受 token 预算限制只会看摘要，前端工具卡片应折叠 + 按需查看/下载。因此引入**消息存储分层**：小内容内联 MySQL，大产物外置到本服务目录下的 blob 存储，MySQL 行只存摘要 + 相对路径引用。同时补齐相邻的真实缺口：前端历史回放当前全量无分页（`load_web_messages` 无 LIMIT）。

## What Changes

- **新增 blob 产物存储**：定义薄抽象 `BlobStore`（put/get/open_stream），首版提供**本地磁盘实现**——产物以相对路径存放在本服务工作目录下（默认 `./artifacts/`，可配置），key 全由服务端 id 生成（防路径穿越）；接口预留 S3/OSS 实现（对齐现有 Broker/RedisBroker 的"单机实现 + 多实例可换"模式）。
- **写入分流**：持久化消息时按可配置阈值（默认 32KB）判断——小内容内联 `agent_message.content`；超阈值的 tool 结果/超长助手文本外置：完整内容写 blob，行内 `content` 存机械截断 head（不调 LLM），并记录 `content_ref`（相对 key）、`content_size`、`content_kind`。
- **表结构**：`agent_message` 新增 `content_ref`/`content_size`/`content_kind` 三列。项目未上线、数据可重建，**直接改建表语句**（`CREATE TABLE`），不做 ALTER 迁移、不引迁移工具。
- **读取分流**：喂 LLM 的 `load_messages` 遇外置消息只取 head/摘要，**绝不拉 blob 全量**（与现有 token trim 协同）；前端 `load_web_messages` 返回 head + 产物标志（`contentRef`/`contentSize`/`contentKind`），不内联 blob。
- **产物下载**：新增受登录与归属校验保护的下载端点，后端流式代理返回 blob（透传 Content-Type），越权返回 404；路径由服务端 id 构成、拒绝任意路径输入。
- **历史回放分页**：`GET /sessions/{id}/messages` 改为 keyset 分页（`?before=<messageId>&limit=`，`ORDER BY id DESC` 取后反转为升序），响应带 `nextCursor`/`hasMore`，默认页大小可配置；消除超长会话一次性全量返回。
- **级联删除**：删除会话时连带删除其外置 blob 文件。
- **非目标（明确排除）**：长期记忆/向量库（Milvus，Tier3）另开 change；用户文件"上传"链路本项目尚无，本期不做；S3/OSS 实现仅预留接口不落地。

## Capabilities

### New Capabilities
- `artifact-storage`: 大工具产物/胖结果的外置存储与访问——阈值分流（内联 vs 外置）、BlobStore 抽象与本地相对路径实现、产物下载端点与归属/路径安全、会话删除级联清理。

### Modified Capabilities
- `web-auth-session`: 历史消息查询由"全量升序返回"改为"keyset 分页 + 产物标志"（消息体对超大结果返回 head 与 contentRef 而非内联全量）；归属校验与其余行为不变。
- `agent-loop`: 载入历史构建模型上下文时，被外置的消息 SHALL 仅以内联 head/摘要进入上下文，MUST NOT 拉取 blob 全量。

## Impact

- 代码（实现阶段，本期仅定稿）：新增 `general_agent/blob_store.py`（BlobStore + LocalBlobStore）；`mysql_client.py`（SCHEMA_DDL 加三列）；`message_store.py`（写入分流、head、blob 读写、keyset 分页、级联删）；`runner.py`（持久化走分流、上下文用 head）；`config.py`/`config.yaml`（新增 `artifacts` 配置块）；`app.py`（注入 BlobStore）；新增/改 `api/sessions.py` 或新 `api/artifacts.py`（下载端点）；`chat_session_store.delete` 级联 blob；前端 `front/`（工具卡片"查看完整/下载"、历史分页加载）。
- 数据：`agent_message` 加三列；新增本地 `artifacts/` 目录（需进 `.gitignore`、`.dockerignore`，部署卷挂载）。
- 部署：本地 blob 不跨实例共享，与"内存 Broker/登录态"同属单实例约束（写进部署文档）；容器需挂载持久卷否则重建丢产物。
- 测试：新增外置分流/下载鉴权/分页/级联删除测试；现有测试不依赖外部服务。
- 无 API 破坏性变更（分页游标为新增可选参数；旧内联消息 `content_ref IS NULL` 自然兼容）。
