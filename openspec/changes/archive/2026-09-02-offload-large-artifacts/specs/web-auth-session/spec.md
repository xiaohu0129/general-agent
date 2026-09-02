## MODIFIED Requirements

### Requirement: 会话管理与归属校验

系统 SHALL 提供会话列表、新建、历史消息查询、重命名、删除接口（均需登录）；所有会话操作 SHALL 按登录用户 `uid` 做归属校验，越权访问他人会话 SHALL 返回 404（不暴露资源存在性）。删除会话 SHALL 同时删除其消息，并 SHALL 级联删除这些消息外置的 blob 产物文件（见 `artifact-storage`）。

历史消息查询 SHALL 按时间**分页**返回（keyset 游标分页）：接受游标参数（如 `before=<messageId>`）与页大小（`limit`，有默认值与上限），按 `id` 倒序取一页后反转为升序返回，并在响应中给出是否还有更多（`hasMore`）与下一页游标（`nextCursor`）；不传游标时返回最新一页。系统 MUST NOT 在一次请求中返回会话的全部消息。

历史消息中的超大工具结果/产物 SHALL 以内联 head 摘要 + 产物标志（`contentRef`/`contentSize`/`contentKind`）形式返回，MUST NOT 内联完整 blob；完整产物经 `artifact-storage` 的下载端点按需获取。

#### Scenario: 越权访问他人会话返回 404

- **WHEN** 用户 A 请求访问属于用户 B 的 `sessionId`（会话详情、消息、重命名、删除、产物下载或订阅）
- **THEN** 系统返回 404 `SESSION_NOT_FOUND`，不确认该会话存在

#### Scenario: 会话生命周期闭环

- **WHEN** 用户新建会话、发送消息、查询历史、重命名、删除
- **THEN** 列表/标题/历史消息相应更新；删除后该会话、其消息及外置产物不再可见/可下载

#### Scenario: 历史消息分页加载

- **WHEN** 一个会话的消息数超过单页大小，客户端首次不传游标请求历史，随后携带返回的 `nextCursor` 请求下一页
- **THEN** 首次返回最新一页且 `hasMore=true`；后续每一页返回更早的消息，按时间升序拼接不重不漏，直至 `hasMore=false`

#### Scenario: 历史中的超大结果以摘要 + 引用返回

- **WHEN** 某条历史消息对应一个外置的大工具结果
- **THEN** 历史接口返回该消息的 head 摘要与 `contentRef`/`contentSize`/`contentKind` 标志，不内联完整内容；客户端可凭引用经下载端点获取完整产物
