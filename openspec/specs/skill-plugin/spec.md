# skill-plugin Specification

## Purpose
定义可插拔的业务能力扩展机制：业务方以自研 Skill（基类 + 注册表）封装能力，框架按请求环境动态过滤并产出 LangGraph 工具，通过依赖注入上下文提供业务客户端，使框架本身不内置任何业务依赖，且工具错误被受控处理而不终止对话轮次。

## Requirements

### Requirement: Skill 基类与元数据

系统 SHALL 提供 Skill 基类，业务子类 SHALL 声明名称（`name`）、功能描述（`description`）、参数 schema（`args_schema`，pydantic）、允许环境（`allowed_envs`，为空表示所有环境可用），并实现异步 `run(ctx, **kwargs)`。框架默认注册表 SHALL 为空（无业务 Skill 时退化为纯对话）。

#### Scenario: 无注册 Skill 时纯对话

- **WHEN** 业务方未注册任何 Skill
- **THEN** Agent 工具集为空，对话正常进行（纯问答），不报错

### Requirement: 显式注册与按环境动态过滤

系统 SHALL 通过注册表显式注册 Skill（不自动扫描）；每次请求 SHALL 根据当前环境（env）过滤——仅 `allowed_envs` 命中（或不限制）的 Skill 被产出为工具并进入模型提示，某环境专属 Skill MUST NOT 出现在其他环境的工具集中。Agent SHALL 每请求重建以实现动态加载。

#### Scenario: 环境白名单过滤工具

- **WHEN** 某 Skill 声明 `allowed_envs=["prod"]` 而请求环境为 `dev`
- **THEN** 该 Skill 不被产出为工具、不进入模型提示，模型在 dev 环境无法调用它

### Requirement: 业务依赖注入

系统 SHALL 通过 `SkillContext` 向 Skill 提供 `env`、`user`、`session_id` 与 `services`（业务客户端字典）；业务方在应用组装时把客户端放入服务字典，Skill 内按键取用。框架 MUST NOT 内置任何业务客户端。

#### Scenario: Skill 取用注入的业务客户端

- **WHEN** 业务方将某 REST 客户端以键 `my_client` 注入服务字典，且 Skill 在 `run` 中访问 `ctx.services["my_client"]`
- **THEN** Skill 取得该客户端实例并可调用；框架自身不感知具体业务

### Requirement: 工具埋点与错误受控

每个 Skill 产出的工具 SHALL 自带调用 span 与工具 metric（调用次数、耗时、成功/错误、错误码）。工具抛出的异常 SHALL 被捕获并转为 `status=error` 的工具结果返回给模型（不终止轮次）；异常携带 `code` 属性时 SHALL 作为 `errorCode`，否则为 `INTERNAL`。

#### Scenario: 工具异常转为错误工具结果

- **WHEN** Skill 的 `run` 抛出带 `code="NOT_FOUND"` 的异常
- **THEN** 该次工具调用产生 `tool_end{status:"error", errorCode:"NOT_FOUND"}` 与错误工具消息，模型可据此继续，轮次不崩溃，且错误 metric/span 被记录

### Requirement: 入参命名兼容

系统 SHALL 支持业务 Skill 用 pydantic 字段别名（alias）兼容下游系统的字段命名（如 camelCase）：参数 schema 声明别名并开启按名填充后，模型按 schema 命名或别名传参均 SHALL 被接受；框架本身 MUST NOT 规定业务字段。

#### Scenario: 别名双名传参

- **WHEN** Skill 参数以 `Field(alias="taskId")` 声明并开启按名填充
- **THEN** 模型传 snake_case 或 camelCase 名均可被正确解析，Skill 内部以规范名处理
