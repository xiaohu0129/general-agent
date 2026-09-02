# artifact-storage Specification

## Purpose

为对话中产生的大体积工具结果与胖产物（大 JSON、报表、生成文件、长文档等）提供外置存储与受控访问：小内容内联数据库，超阈值产物以相对路径存放在本服务工作目录下的 blob 存储中，数据库行仅保留摘要与引用；并提供经登录与归属校验的下载端点，使数据库不承载大对象、LLM 上下文不拉取全量产物、前端可按需查看/下载。

## Requirements

### Requirement: 大产物阈值外置分流

持久化消息时，系统 SHALL 按可配置阈值（默认 32KB）决定内容去向：不超过阈值的内容内联存入消息行；超过阈值的工具结果或超长助手文本 MUST 外置——完整内容写入 blob 存储，消息行内仅保留机械截断的 head 摘要（不调用 LLM 生成摘要）以及引用信息。

外置消息行 SHALL 记录：`content_ref`（blob 的相对 key/path）、`content_size`（原始字节数）、`content_kind`（内容类型，如 json/text/file）；内联消息 `content_ref` 为空。读取时系统 SHALL 以 `content_ref` 是否为空判定内联/外置，两种形态共存且互不影响。

#### Scenario: 小内容内联

- **WHEN** 一条工具结果/助手文本的大小不超过阈值
- **THEN** 完整内容写入消息行 `content`，不产生 blob 文件，`content_ref` 为空

#### Scenario: 超大工具结果外置

- **WHEN** 一条工具结果大小超过阈值
- **THEN** 完整内容写入 blob 存储，消息行 `content` 为截断 head，且记录 `content_ref`（相对路径）、`content_size`（原始大小）、`content_kind`

#### Scenario: 新旧消息形态共存

- **WHEN** 历史中同时存在外置消息（`content_ref` 非空）与内联消息（`content_ref` 为空）
- **THEN** 读取/回放/上下文构建对两种形态均正确处理，不要求回填旧数据

#### Scenario: 外置写入失败回退内联

- **WHEN** 一条超大内容外置写 blob 失败（如磁盘满、目录不可写）
- **THEN** 系统不中断消息落库：回退为将完整内容内联写入消息行（`content_ref` 为空）；若完整内容仍超过数据库列上限，则退化为 head 截断，保证该轮消息可持久化

### Requirement: 本地 blob 存储与相对路径

系统 SHALL 提供 blob 存储，首版将产物以**相对路径**存放在本服务工作目录下（默认 `./artifacts/`，根目录可配置）。blob 的 key/path MUST 完全由服务端生成的标识（uid/session/turn/消息或工具调用 id）构成，MUST NOT 拼接任何用户输入，以杜绝路径穿越；存放目录 SHALL 在需要时自动创建。

#### Scenario: 产物写入服务目录

- **WHEN** 一条外置产物被保存
- **THEN** 系统在工作目录下按服务端生成的相对路径写入文件，并返回该相对 key；同一会话/轮次的产物路径可预测但不含用户可控片段

#### Scenario: 拒绝路径穿越

- **WHEN** 任何外部输入试图影响 blob 路径（如包含 `..`、绝对路径）
- **THEN** 系统不使用该输入构造路径，产物路径仅由服务端 id 生成

### Requirement: BlobStore 抽象与后端可替换

系统 SHALL 以薄抽象（BlobStore：写入、读取、删除、定位本地路径）封装 blob 存储，业务代码（消息持久化、下载端点）仅依赖该抽象；首版提供本地磁盘实现，下载端点经本地路径以流式文件响应发送（不将全量字节读入内存）。接口 SHALL 预留对象存储（S3/OSS）实现以支撑多实例/云部署，替换后端时业务代码不改动（与现有 Broker/RedisBroker 的可替换模式一致）。

#### Scenario: 切换 blob 后端不影响业务

- **WHEN** 未来由本地磁盘实现切换为对象存储实现
- **THEN** 消息写入与下载端点代码不变，仅 BlobStore 的实现被替换

### Requirement: 产物下载端点与访问控制

系统 SHALL 提供产物下载端点，返回指定消息/工具调用的完整 blob 内容（流式、透传 Content-Type，支持在前端内联展示或下载）。该端点 MUST 经过登录鉴权，并 MUST 校验：① 会话属于当前用户（越权返回 404，不暴露存在性）；② 该 blob 归属于该会话/消息。端点 MUST NOT 接受任意路径输入，只允许按服务端标识定位 blob。

#### Scenario: 已登录用户下载自己会话的产物

- **WHEN** 已登录用户对自己拥有的会话中的某条外置消息请求下载
- **THEN** 系统流式返回完整 blob 内容，Content-Type 与产物类型一致

#### Scenario: 越权下载他人产物返回 404

- **WHEN** 用户请求下载属于其他用户会话的产物
- **THEN** 系统返回 404，不确认该产物或会话存在

#### Scenario: 未登录下载被拒

- **WHEN** 未携带有效登录态请求产物下载端点
- **THEN** 系统返回 401 `UNAUTHORIZED`

### Requirement: 会话删除级联清理产物

删除会话时，系统 SHALL 同时删除该会话下所有消息引用的外置 blob 文件（best-effort），再删除消息与会话行；内联消息无 blob 可删。blob 删除失败 SHALL 不阻断会话删除（记录告警），避免悬挂存储导致会话无法删除。

#### Scenario: 删除会话清理产物文件

- **WHEN** 用户删除一个含外置产物的会话
- **THEN** 该会话引用的 blob 文件被清理，消息与会话行被删除，后续下载该产物返回不存在
