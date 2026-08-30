# 模板中心 AI 规则起草与生命周期重构计划

## 1. 目标

本次重构解决模板中心第 2 步无法保存、第 3 步展示旧校验结果、阻断项无法定位、接口错误未呈现在前端，以及用户需要手工填写过多扣分细则的问题。

产品原则：

> AI 优先起草，用户负责确认；用户已有输入优先，AI 只解释和补全缺失部分。只有用户直接提供或明确确认的规则才能进入可执行评分版本。

## 2. 改动边界

允许修改：

- `frontend/web/index.html`
- `frontend/web/assets/app.js`
- `frontend/web/assets/styles.css`
- 对应的 `public/` 静态构建产物
- `backend/app/api/routes/rubrics.py`
- `backend/app/schemas/rubric.py`
- `backend/app/services/rubric_import/compiler.py`
- `backend/app/services/rubric_import/pipeline.py`
- `backend/app/services/rubrics/lifecycle.py`
- 新增 `backend/app/services/rubric_import/ai_rule_drafter.py`
- 模板导入、生命周期和 Web 相关测试

明确不修改：

- 数据库模型与 Alembic 迁移
- 评分执行引擎、批量评分、报告和 QWK
- 用户、组织、权限和 AI Provider 底层
- 已发布 `RubricVersion` 的不可变约束

## 3. P0 设计

### P0-1 用户输入识别

自然语言扣分说明必须先按原始输入分类，不能把正则未匹配误判为用户未输入：

- `absent`：没有原始输入；允许 AI 根据评分项、Profile 和模板上下文完整起草。
- `parsed`：全部解析成功；保留确定性解析结果。
- `partial`：部分成功；保留成功部分，AI 只处理未解析片段。
- `unparsed`：存在原始输入但全部失败；AI 必须解释用户原文，不得忽略原文重新生成。

分析结果包含 `raw_segments`、`parsed_rules`、`unresolved_segments`、`needs_ai_draft`、`needs_severity_expansion` 和 `source_refs`。

### P0-2 AI 扣分规则起草

新增只读建议接口 `POST /rubrics/{rubric_id}/draft-deduction-rules`。接口不修改 Rubric、执行草稿或评分规则，只返回待确认建议。

支持单评分项和批量评分项。模型上下文仅包含规则设计所需信息。模型选择复用现有真实 AI 连接；Mock LLM 不得生成可发布的评分政策。没有真实连接时返回结构化的 `AI_DRAFT_CONNECTION_MISSING`。

### P0-3 封闭结构化输出

AI 输出必须符合 `ai-deduction-draft@1`，包含：

- 评分项和输入判断
- 规则组、问题类型、互斥组和扣分上限
- 每个严重程度的固定触发条件、固定扣分、原因、重复策略和来源
- 模型、Prompt 版本和生成指纹
- `requires_confirmation=true`

后端确定性约束：

- 不得修改评分项编码、名称、满分和计分方式。
- 扣分必须大于零且不超过评分项满分。
- 同一问题严重程度分值单调递增。
- 同一严重程度组互斥，规则组有累计上限。
- 不得产生奖励分，不得使评分项低于零。
- 每条规则必须有来源引用。
- Schema 或业务校验失败时整项拒绝，不静默采用异常子集。

### P0-4 严重程度拆分

“扣 2 到 6 分”等范围不再静默取上限。AI 将其拆为轻微、中等、严重等固定扣分规则，保留用户给定的上下限，中间分值标记为 AI 建议。相同问题使用同一 `mutex_group`，评分时最多命中一个严重程度。

### P0-5 批量生成与确认

第 2 步显示缺失分析摘要，提供“一键生成全部缺失细则”和单项重新生成。AI 建议以规则组和严重程度表格呈现，支持“确认并应用全部”、展开检查、排除和修改。用户修改后的建议标记为 `user_edited_ai_draft`。

确认信息包含确认人、确认时间、模型、模型版本、Prompt 版本和生成指纹。用户不需要复制或编辑高级 JSON 才能完成基本流程。

### P0-6 通过重新编译保存

“保存并重新校验”必须调用 `POST /rubrics/{rubric_id}/recompile`，并携带当前活动执行草稿 ID 作为 `supersedes_compilation_id`。

后端从前置执行草稿继承 Profile、workflow、global policy 和格式配置。只有用户直接提供、确定性解析或用户已确认的 AI 规则可以进入新的可执行图。旧执行草稿标记为 `superseded`，新执行草稿成为唯一活动版本。

### P0-7 错误反馈闭环

模板中心接口统一返回结构化错误：

- `code`
- `message`
- `user_action`
- `severity`
- `retryable`
- `criterion_code`
- `field_path`
- `rule_index`
- 非敏感 `context`

前端按错误范围显示在字段、评分项、第 2 步或第 3 步。业务错误不得只显示短暂 Toast；必须提供“去修改”“重新生成”“检查 AI 连接”“保留修改并刷新”等可执行动作。失败时保留用户输入、解析结果、AI 建议和确认状态。

### P0-8 测试先行

先编写失败测试，再编写生产代码。覆盖：

1. `absent`、`parsed`、`partial`、`unparsed` 分类。
2. 正则规则保留、AI 只补全未解析片段。
3. 严重程度互斥、分值递增和扣分上限。
4. Mock/缺少真实连接、超时和无效模型输出。
5. 未确认 AI 规则不能进入有效执行草稿。
6. `/recompile` 使用精确 predecessor，旧版本被替代且保留审计。
7. 导入、修改、确认、重新编译、审核和发布完整流程。
8. 错误能呈现在对应前端位置，保存失败不丢失编辑内容。

## 4. P1 设计

### P1-1 审核阻断

活动执行草稿不是 `validated` 或仍有 blocker 时，禁用“提交模板审核”，显示“请先处理 N 个阻断项”。后端同步拒绝非法状态转换。

### P1-2 阻断定位

blocker 增加评分项、字段和规则行定位信息。前端提供“去修改 T02”，自动切换到第 2 步、滚动、聚焦并高亮对应控件。历史 blocker 按错误码兼容映射。

### P1-3 编辑状态

模板编辑状态包括 `clean`、`dirty`、`generating`、`ai_draft_pending`、`saving` 和 `save_failed`。第 3 步明确显示当前校验结果来自哪个执行草稿，以及是否未包含第 2 步修改。

### P1-4 减少重复确认

第 2 步确认负责内容授权，第 3 步负责治理审核。提供批量提交和批量批准入口，只展开存在差异、低置信度或被修改的规则；不降低组织要求的双人审核约束。

### P1-5 用户术语

用户界面使用“评分规则”“执行草稿”“存在阻断”“校验通过”“已被新草稿替代”“复制为新版本后编辑”。内部模型、API 路径和数据库字段保持不变。所有错误都说明发生了什么以及下一步怎么做。

## 5. 完成标准

- 用户不打开开发者工具即可理解并处理模板中心错误。
- 用户输入不会因正则未匹配而丢失。
- 缺失或不完整规则由真实 LLM 起草，用户以批量确认为主。
- 未确认 AI 规则永远不能成为正式评分政策。
- 第 2 步保存生成新的可审计执行草稿，第 3 步只审核已校验内容。
- blocked 状态有明确定位和恢复路径。
- 定向测试和全套测试全部通过。
