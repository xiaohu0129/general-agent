## MODIFIED Requirements

### Requirement: 上下文 token 预算裁剪

系统 SHALL 在送入模型前按 token 预算（`max_context_tokens`，默认 24000）裁剪历史，保留 system 消息与最近消息；裁剪 MUST 保持工具调用配对——若裁剪后首条是缺少对应 assistant 工具调用的孤儿 ToolMessage，SHALL 向后跳过，避免工具协议错乱。

token 估算 SHALL 按字符类别分别计算：**CJK 统一表意文字（含中文标点）按每字约 1 token 计**，其余文本沿用每 4 字符约 1 token（`len//4`）；对中文内容 MUST NOT 再以 `//4` 低估（旧实现对中文低估 2~4 倍，长会话实际 token 数超出模型窗口触发 context-length 400）。工具调用参数 SHALL 计入估算。

#### Scenario: 超预算裁剪不产生孤儿工具消息

- **WHEN** 历史消息估算 token 超过预算
- **THEN** 系统保留最近消息且裁剪结果不以无配对的 ToolMessage 开头，模型输入的工具调用/工具消息始终成对

#### Scenario: 中文长会话实际 token 不超窗

- **WHEN** 历史由约 6000 个汉字组成（旧 `//4` 估算约 1500 token、新估算约 6000 token）且预算为 24000
- **THEN** 送入模型的估算值接近实际分词结果（误差不导致超窗 400），仍在预算内时正常发送；若新估算超预算则按既有规则裁剪
