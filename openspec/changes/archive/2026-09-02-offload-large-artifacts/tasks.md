## 1. 配置与 BlobStore 基础设施

- [x] 1.1 `config.py`/`config.yaml` 新增 `artifacts` 配置块：`dir`（默认 `./artifacts/`）、`inline_threshold`（默认 32KB）、`head_chars`（默认 2000）
- [x] 1.2 新增 `general_agent/blob_store.py`：`BlobStore` 抽象（`put`/`get`/`delete`/`local_path`）+ `LocalBlobStore`（相对路径、根目录可配、目录按需创建）
- [x] 1.3 blob key 全由服务端 id 生成（`{uid}/{session}/{turn}/{uuid}.{ext}`），不拼接用户输入；路径段清洗 + resolve/parents 穿越防护
- [x] 1.4 `app.py` 构建并注入 `app.state.blob_store`，并传入 `MessageStore`

## 2. 表结构

- [x] 2.1 `mysql_client.SCHEMA_DDL` 的 `agent_message` 新增 `content_ref VARCHAR(512) NULL`、`content_size BIGINT NULL`、`content_kind VARCHAR(32) NULL`
- [ ] 2.2 本地 drop 表/删库重建验证新表（未上线，不做 ALTER/迁移工具）——需本地 MySQL 环境手动验证，自动化测试用 FakePool 不触达真实 DDL

## 3. 写入分流

- [x] 3.1 `message_store.append_message` 按 `inline_threshold` 判断：小内容内联；超阈值写 blob、行内存 head + content_ref/content_size/content_kind；写 blob 失败 best-effort 回退内联全量（超 MEDIUMTEXT 再退化为 head），不中断落库
- [x] 3.2 head 为机械截断前 `head_chars` 字符 + 截断标记（不调 LLM）
- [x] 3.3 `runner.py` 持久化 assistant/tool 消息统一走 `append_message`（自动分流）；判定用 UTF-8 字节数
- [x] 3.4 内联消息 `content_ref` 为空；外置/内联两形态共存

## 4. 读取分流

- [x] 4.1 `load_messages`（喂 LLM）仅 SELECT 行内 content（即 head），不拉 blob
- [x] 4.2 `load_web_messages` 改 keyset 分页：`?before=<id>&limit=`，`ORDER BY id DESC LIMIT n+1` 取一页反转为升序；响应 `{messages, nextCursor, hasMore}`；limit 默认 50、硬上限 200
- [x] 4.3 回放消息对超大结果返回 head + `contentRef`/`contentSize`/`contentKind` 标志，不内联 blob
- [x] 4.4 `api/sessions.py` 历史端点暴露 `before`/`limit` 查询参数

## 5. 产物下载端点

- [x] 5.1 新增 `GET /sessions/{sessionId}/artifacts/{messageId}`，经 `governance_dep` + `_owned_session` 归属校验（越权 404）+ 校验消息归属该会话
- [x] 5.2 经 `message_store.get_artifact` 返回 blob 本地路径 + kind，`FileResponse(path, media_type, filename=...)` 由 ASGI 流式发送（不把全量字节读进内存），透传 Content-Type + Content-Disposition；未登录 401；不接受任意路径输入（路径只含会话/消息 id，blob key 由服务端 id 生成）
- [x] 5.3 Nginx 反代覆盖该路径：`/sessions/...` 已被现有 `^/(auth|sessions|...)` 块代理（普通短请求，120s 超时足够），无需新增 location

## 6. 级联删除

- [x] 6.1 删会话时经 `message_store.list_artifact_refs` 收集 `content_ref` 后 best-effort 删除 blob（失败仅 `logger.warning` 不阻断）；实现在 `api/sessions.py` 删除端点（跨 message_store + blob_store，便于测试替身注入）

## 7. 前端（front/）

- [x] 7.1 工具卡片对 `offloaded` 工具结果、助手气泡对外置正文显示 head 摘要 + "下载完整产物/回复"链接（`artifactDownloadUrl`，`a[download]` 新标签）+ 完整产物大小提示
- [x] 7.2 历史消息按 `nextCursor`/`hasMore` 分页，提供"加载更早的消息"按钮向前拼接
- [x] 7.3 `api/types.ts`（`HistoryMessage` 加 contentRef/contentSize/contentKind/messageId、`MessagePage`）、`chat/model.ts`（`ToolCall` 加 offloaded/artifactUrl/artifactSize/artifactKind）类型补齐

## 8. 部署与忽略

- [x] 8.1 `.gitignore`、`.dockerignore` 加 `artifacts/`
- [x] 8.2 `docker-compose.yml` 挂命名卷 `agent-artifacts` 到 `/app/artifacts`；`docs/03` 补"本地 blob 单实例约束 + 卷挂载 + 备份"
- [x] 8.3 `README` 同步外置产物说明（阈值、目录、下载端点、卷挂载）

## 9. 测试（全部不依赖外部服务）

- [x] 9.1 写入分流：小内容内联无 blob；超阈值外置且 head/ref/size/kind 正确（`test_artifact_offload.py`）
- [x] 9.2 LLM 上下文：外置消息 `load_messages` 只取 head、不读 blob（`test_load_context_does_not_fetch_blob`）
- [x] 9.3 回放分页：多页游标升序拼接不重不漏、hasMore/nextCursor 正确（`test_message_pagination.py`）
- [x] 9.4 下载端点：本人可下载、越权 404、未登录 401（`test_artifact_download.py`）；路径穿越在 `test_blob_store.py` 覆盖
- [x] 9.5 级联删除：删会话清理 blob（`test_artifact_cascade.py`）；blob 删除失败 best-effort 不阻断（删除端点 try/except）
- [x] 9.6 新旧形态共存：`content_ref` 为空的内联消息回放/上下文正常（FakeStore 与分流测试共存）
