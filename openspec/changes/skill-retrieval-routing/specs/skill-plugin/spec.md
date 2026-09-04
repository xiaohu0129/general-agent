## MODIFIED Requirements

### Requirement: Skill 基类与元数据

系统 SHALL 提供 Skill 基类，业务子类 SHALL 声明名称（`name`）、功能描述（`description`）、参数 schema（`args_schema`，pydantic）、允许环境（`allowed_envs`，为空表示所有环境可用）、技能类别（`category`，域标签，用于向量检索的元数据过滤与兜底 LLM 选域）与示例话语（`examples`，2~5 条典型用户说法，作为向量检索的主要语义来源），并实现异步 `run(ctx, **kwargs)`。框架默认注册表 SHALL 为空（无业务 Skill 时退化为纯对话）。Skill 未提供 `examples` 时系统 SHALL 回退以 `description` 构建向量索引（兼容存量 Skill），并在路由可观测数据中标注该 Skill 无示例（检索质量降级可被识别）。

#### Scenario: 无注册 Skill 时纯对话

- **WHEN** 业务方未注册任何 Skill
- **THEN** Agent 工具集为空，对话正常进行（纯问答），不报错

#### Scenario: 示例话语作为检索语义来源

- **WHEN** 某 Skill 声明了 `examples`，且用户消息与其中某条示例话语语义相近
- **THEN** 路由向量检索可命中该 Skill（语义匹配基于示例话语而非仅干描述）

#### Scenario: 缺失示例回退描述并标注

- **WHEN** 某存量 Skill 未声明 `examples`
- **THEN** 系统以其 `description` 构建向量索引使其仍可被检索，路由记录中标注该 Skill 无示例，不因缺示例而注册失败
