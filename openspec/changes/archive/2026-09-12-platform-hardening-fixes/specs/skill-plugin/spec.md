## MODIFIED Requirements

### Requirement: 显式注册与按环境动态过滤

系统 SHALL 通过注册表显式注册 Skill（不自动扫描）；注册时 SHALL 立即校验技能名：非空、匹配 `^[A-Za-z0-9_-]{1,64}$`（与 OpenAI function name 约束兼容）且在整个注册表内唯一；违规（空名、非法字符、重名）SHALL 直接抛出错误（fail-fast，使组装期配置问题立即暴露），MUST NOT 以静默覆盖方式继续（重名会导致向量/BM25 索引段互相覆盖、路由静默丢失技能）。每次请求 SHALL 根据当前环境（env）过滤——仅 `allowed_envs` 命中（或不限制）的 Skill 被产出为工具并进入模型提示，某环境专属 Skill MUST NOT 出现在其他环境的工具集中。Agent SHALL 每请求重建以实现动态加载。

#### Scenario: 环境白名单过滤工具

- **WHEN** 某 Skill 声明 `allowed_envs=["prod"]` 而请求环境为 `dev`
- **THEN** 该 Skill 不被产出为工具、不进入模型提示，模型在 dev 环境无法调用它

#### Scenario: 非法技能名注册即失败

- **WHEN** 业务方注册的 Skill `name` 含空格/点号/中文等非法字符或为空
- **THEN** 注册调用直接抛出错误，应用不以该注册表继续启动

#### Scenario: 重名注册被拒绝

- **WHEN** 两个不同 Skill 类使用相同 `name` 注册
- **THEN** 第二次注册直接抛出错误（而非后者覆盖前者），避免索引段覆盖导致的路由静默丢技能
