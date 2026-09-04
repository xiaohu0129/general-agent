# 工作流约定

本项目采用 **OpenSpec 管方案、superpowers 管执行** 的分工，避免两套流程重复。

## 一、默认姿态：Explore First

- 新会话开始时，若首条消息不是明确的实现/修复指令，先加载
  `openspec-explore` 技能，以探索姿态工作：只思考、画图、澄清，不写实现代码。
- 用户明确说"开始实现""退出 explore""直接改/修复"，或任务本身琐碎
  （改配置值、错别字、运行命令）时，不进入 explore。

## 二、正式变更（走 OpenSpec）

适用：新功能、架构调整、涉及 spec 的改动。

| 阶段   | 触发方式                         | 技能                                                                       |
|--------|----------------------------------|----------------------------------------------------------------------------|
| 探索   | /opsx-explore 或自动             | openspec-explore                                                           |
| 固化   | /opsx-propose                    | openspec-propose（产出 proposal/design/specs/tasks）                       |
| 修订   | 方案有变                         | openspec-update-change                                                     |
| 实现   | /opsx-apply-change 或"开始实现"  | openspec-apply-change 管进度 + subagent-driven-development 派活            |
| 校验   | 任务完成                         | verification-before-completion、requesting-code-review、openspec validate  |
| 归档   | 全部完成                         | openspec-archive-change、finishing-a-development-branch                    |

去重规则：
- 走 OpenSpec 的变更，**不触发** superpowers 的 brainstorming 和 writing-plans
  ——proposal/design/tasks 已覆盖其产出。
- 执行层技能照常使用：test-driven-development（子代理先测后码）、
  systematic-debugging（遇 bug）、dispatching-parallel-agents（独立任务并行）。

命名与文档约定：
- change 目录名用**英文 kebab-case slug**（CLI 参数/归档目录名，避免中文编码风险）；
  中文主题体现在工件正文。
- `/opsx-propose` 完成工件后，**同步更新 `README.md` 的"变更记录（OpenSpec Changes）"
  章节**：每个 change 一条（slug + 中文主题 + 主要作用），分"进行中/已归档"两组；
  change 归档后条目移入"已归档"组。

实现执行方式：以 subagent-driven-development 为主——tasks.md 的每个任务派
独立 subagent 按 TDD 实现，主会话负责审查与串联；仅当任务 trivial 或强耦合
无法拆分时，才由主线程直接实现。

## 三、小改动（不走 OpenSpec）

适用：不涉及 spec 的琐碎修改（配置值、错别字、临时脚本、一次性调试）。

- 走 superpowers 轻量流程：brainstorming（一句话确认意图）→
  test-driven-development（适用时）→ verification-before-completion。
- 不创建 OpenSpec change。

## 四、模式流转

explore/propose 是上下文指令而非系统状态，无状态栏标识，靠明确指令流转：
- "开始实现" / /opsx-apply-change → 解除 explore 禁写约束
- 不确定当前姿态时，直接询问确认
