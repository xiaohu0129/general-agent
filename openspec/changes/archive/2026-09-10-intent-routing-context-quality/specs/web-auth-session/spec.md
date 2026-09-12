## ADDED Requirements

### Requirement: 澄清选项持久化与历史回放

系统 SHALL 将澄清轮的结构化选项与用户选择随会话消息持久化：澄清消息落库时 SHALL 在消息的通用 `meta` 结构中记录 `kind:"clarify"`、候选方向 `categories`、选项 `options:[{label,value}]`，以及用户经选项闭环后被选中的 `selected` value（若有）。其中 `options`/`selected` 的 value 为带 `category:`/`skill:` 前缀的可路由值，持久化与回放原样保留（不做语义改写），`label` 为人类可读文案。该 meta 复用消息表的通用 JSON 扩展列，MUST NOT 为选项单独建表；存量无 meta（NULL）的历史消息 SHALL 视为普通消息，回放时不报错。

会话历史消息查询（`/sessions/{id}/messages`，含分页）SHALL 在消息 DTO 中透出澄清消息的 options 与已选 value（经现有 meta 透传通道），使前端刷新/换设备后可还原澄清选项卡片并标记已选项为已选/禁用。历史回放 MUST NOT 内联超出既有大小限制的内容，选项结构为小尺寸结构化字段、随消息头返回。持久化与回放 SHALL 按登录用户 `uid` 做既有归属校验，澄清选项数据不跨会话/跨用户泄露。

#### Scenario: 澄清选项随消息持久化

- **WHEN** 路由产出澄清（含 options）并结束该轮
- **THEN** 澄清 assistant 消息落库时 meta 含 `kind:"clarify"`、`categories` 与 `options:[{label,value}]`；用户下一轮点选且确定性收窄成功后，系统 SHALL 按会话归属键与该澄清消息的 `turn_id` 定位回上一轮澄清行，将其 `meta.selected` 回写为所选带前缀 value（合并写入、不覆盖已有 options），供历史回放标记已选

#### Scenario: 历史消息回放透出澄清选项

- **WHEN** 客户端请求某会话历史消息且其中包含澄清轮
- **THEN** 返回的澄清消息 DTO 含 options 与已选 value（若有），前端可据此还原选项卡片并标记已选/禁用；分页边界上选项不重不漏

#### Scenario: 存量无 meta 消息回放不报错

- **WHEN** 会话历史中存在改动前写入的、meta 为 NULL 的消息
- **THEN** 历史回放正常返回这些消息（按普通文本消息处理），不因缺少 options/meta 字段报错

#### Scenario: 澄清选项遵循会话归属校验

- **WHEN** 用户请求非本人会话的历史消息
- **THEN** 系统按既有归属校验返回 404，澄清选项数据不泄露给无归属用户
