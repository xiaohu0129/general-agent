## ADDED Requirements

### Requirement: 对话请求消息体约束

`POST /chat` 的请求体 SHALL 对用户输入设界：`message` MUST 为字符串，去除首尾空白后长度 SHALL 在 1~8000 字符之间；空消息（含纯空白）或超长消息 SHALL 返回 400 `VALIDATION`，MUST NOT 触发路由、LLM 调用或消息落库。可选字段 `clarify_selection.value` 长度 MUST NOT 超过 200 字符，越界按 400 `VALIDATION` 拒绝。

#### Scenario: 空消息被拒绝

- **WHEN** 客户端 POST `/chat` 且 `message` 为空串或仅含空白字符
- **THEN** 系统返回 400 `VALIDATION`，不产生任何 SSE 事件、不写入消息、不调用 LLM

#### Scenario: 超长消息被拒绝

- **WHEN** `message` 去除空白后超过 8000 字符
- **THEN** 系统返回 400 `VALIDATION`，请求不进入路由与 Agent 执行
