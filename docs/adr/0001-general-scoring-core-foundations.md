# ADR-0001：通用评分 Core 的基础语义与迁移边界

- 状态：Accepted
- 决策日期：2026-07-18
- 决策范围：M0 / PR-00
- 决策所有者：项目维护者
- 需求来源：[通用评分内核架构方案](../通用评分内核架构方案.md)与[代码改造计划](../../代码改造计划.md)
- 权威关系：本 ADR 是 M0 已接受决策；对《通用评分内核架构方案》和《论文打分系统设计方案》中本文件覆盖的计分、证据、复核、身份、Profile 与 legacy 主题具有优先解释权；旧行为只由 `legacy_behavior_policy` 与 golden 记录，不是新 Core 的正确性口径

## 背景

现有系统的生产运行链仍以 `Paper → Rubric.criteria → score_paper()` 为核心，而目标架构要求把评分能力拆为通用 Core、业务 Profile 和 legacy Adapter。若在进入 M1/M2 前不冻结计分、证据、身份和迁移语义，后续 DTO、迁移和执行器会产生互不兼容的实现。

本 ADR 一次性接受 M0 所列的基础决策。修改任何已接受语义必须新建后继 ADR；不得通过修改默认值、迁移回填或兼容分支隐式改变这些决策。

## 规范术语

| 术语 | 唯一定义 | 不得混用 |
|---|---|---|
| Submission | 一次业务提交的领域/持久化实体，持有 `profile_key`、通用 metadata 和 artifact identity；字段 `profile_key` 的值就是下文的 `business_profile_key` | 解析后的正文、`Paper` ORM |
| SubmissionSnapshot | 进入 Core 的不可变提交身份 DTO | `DocumentSnapshot`、磁盘路径 |
| DocumentSnapshot | 对提交文档内容的不可变、内容寻址快照，含 sections、evidence units、metrics 和 parser diagnostics | `ParsedPaper`、`PaperChunk` 数据库身份 |
| RubricVersion | 由一次 compilation 产生的内容寻址版本实体；只有父来源图合法发布后才可执行 | `Rubric.version` 字符串、显示版本号 |
| legacy rubric version label | 旧 `Rubric.version` 的显示标签；兼容 API 可继续暴露 | 正式 `RubricVersion`、version hash |
| Criterion | 聚合评分项，保存 max score、weight 和展示分组 | `AtomicRule` |
| AtomicRule | Criterion 内最小、可独立判定并经授权的规则 | 旧自然语言 deduction、模型临时扣分点 |
| Profile | 业务扩展实现，例如 `ThesisProfile`、`TechnicalProposalProfile` | `workflow_profile` |
| business_profile_key | Submission 与 Rubric 的业务类型键，例如 `thesis`、`technical_proposal`；持久化/wire 字段可命名 `profile_key`，但两者是同一值域和语义，不得形成第三种 profile 概念 | 导入工作流 |
| workflow_profile | `template_driven`、`excel_only` 等导入/编译流程语义 | 业务 Profile |
| ParsedPaper | 当前论文解析器的 legacy 类型，仅作为 `LegacyPaperAdapter` 的输入 | `DocumentSnapshot` |

## 已接受决策

### D01 — Core/Profile 依赖方向

Core 只接收不可变合同和 ports，不依赖 FastAPI、SQLAlchemy Session、`Paper`、`ParsedPaper`、文件路径或论文模块。Profile 只能提供业务 metadata、解析解释、checker、prompt、Mock 和输出扩展；Profile 不得计算总分、等级或绕过证据验证。

依赖方向固定为：

    API / CLI / Legacy Adapter
                 ↓
          通用评分 Core
                 ↑
         业务 Profile ports

Checker 只返回 observation、verdict 和 evidence。API 路由不包含计分逻辑。运行中不重新读取可变 ORM 图。

### D02 — weight 只有两种互斥模式

`points` 模式要求所有 weight 为空，所有 criterion.max_score 之和等于 total_score，贡献为 awarded 分数本身。

`weighted_normalized` 模式要求所有 weight 均存在且大于零，贡献计算固定为：

    total_score × weight / Σweight × awarded / max_score

两种模式都要求 `total_score > 0` 且每个参与聚合的 `criterion.max_score > 0`；纯展示分组不是 Criterion，不得用 `max_score=0` 规避校验。禁止混填 weight、零/负 weight、静默量化或发布后改变聚合语义。v1 weight 必须能由现有 `Numeric(6,2)` 精确表达，即 `0.01..9999.99` 且最多两位小数。初评、人工改单项和提交复核使用同一冻结 policy，并保存 raw、max、weight 与 contribution。

### D03 — 语义引用与确定性观察分型

`SourceQuoteEvidence` 必须有 trim 后非空的 quote、有效 `evidence_unit_id`，且归一化 quote 逐字存在于该 evidence unit。`source-quote-normalization-v1` 对 quote 与 unit text 同时执行 Unicode NFC、CRLF/CR→LF、全部 Unicode whitespace 连续段→单个 ASCII 空格并 trim；不做大小写、标点、全半角或同义替换。location 不能代替 quote；`evidence_sufficient` 由系统推导。

`DeterministicObservation` 必须记录 namespaced `checker_key`、不可变 `checker_version`、locator、observation code、measured/expected value，并可从 `DocumentSnapshot` 重放；它不要求伪造原文 quote。

`PaperChunk.id/chunk_id` 只允许作为 legacy Adapter 的非权威 source reference，不参与 Core 验证、PromptEnvelope 或缓存身份。

### D04 — 缺失事实必须有范围覆盖证明

“未找到”不能直接推出“全文缺失”。`ScopedAbsenceEvidence` / `CoverageEvidence` 必须记录：

- `document_snapshot_hash`；
- policy 预先声明的 scope selector，以及由 `DocumentSnapshot` 确定性解析出的 expected section/evidence-unit IDs；
- Checker 实际检查的 section/evidence-unit IDs；
- 查找目标、expected/checked 集合的 canonical hash 与 coverage completeness；
- 可选邻近原文；
- 允许 absence effect 的 evidence policy/version。

`coverage_completeness` 不接受模型或 Checker 自报：Core 先解析 expected 集合，验证 checked 是其子集，再以二者集合完全相等推导 `complete`；缺少、重复或范围外 ID 均为 `invalid`。若 selector 解析为空，除非 policy 明确声明该目标检查的是完整文档结构索引，否则不得形成 absence effect。section scope 只有在 policy 声明它足以判定该 target 时才可视为完整；否则必须覆盖全文。只有 policy 明确允许 absence evidence 且 coverage 为 `complete` 时，缺失事实才能触发自动 score effect。普通检索无结果只产生 validation/review 信息。

### D05 — 模型和 Checker 不能授权分值

模型或 Checker 只能返回已发布 `rule_code`/`level_code`、verdict、occurrence 和 evidence。`AtomicRule.max_points` 或 `RuleLevel.points` 是唯一分值授权来源。

未知、跨 criterion、跨 RubricVersion、未发布或证据无效的规则均为零 score effect，并形成 `ValidationIssue`；“零 effect”不等于回退满分。若因此导致 required rule 没有合法 decision，criterion auto/final score 与 run.final_total 均为空；optional effect 才可丢弃，并由冻结 policy 决定是否复核。原始模型输出保留审计，但不成为最终计分依据。

### D06 — RuleDecision、执行顺序与合法矩阵

`RuleDecision.status` 只允许：

- `triggered`
- `not_triggered`
- `not_applicable`
- `invalid`
- `skipped`

这五个值是一次 run 的判定状态，不得与 `AtomicRule.status` 混用。后者是内容审核生命周期：`draft → review → approved`，另允许 `review → rejected` 或 `review → draft`；只有 `approved` 可进入发布候选。approve/reject 必须保存 `reviewed_by`、`reviewed_at` 及不可变审核事件（change ID、rule code、action、before/after、actor、time、reason），已发布图不可再改。

发布图先按依赖关系做确定性拓扑排序（同层按 `rule_code` 升序）；自依赖、未知依赖或环在发布时失败。`depends_on_rule_codes` 是 AND：仅当全部依赖 decision 都有效且为 `triggered` 时才执行当前规则，否则当前规则为 `skipped`。运行时先执行并验证可执行的 decisions，再按 criterion 计算分数。

band criterion 在 v1 **恰好包含一个** `direction=band, effect_type=score` 的 AtomicRule；该规则包含至少两个 `level_code` 唯一的 RuleLevel，并且 `max_points`、`repeat_policy`、`cap_points`、`mutex_group` 全为空。每个 RuleLevel.points 必须位于 `0..criterion.max_score`。一次判定必须且只能选择一个已发布 level；零个、多个或未知 level 均为 `invalid`。因此 RuleLevel 之间不使用 AtomicRule 的 mutex 字段，消除了“单规则内 levels”与“规则间 mutex group”的建模冲突。band criterion 不得再含 deduct 规则，但可含不计分的 `direction=none` review/block/report 规则。

非 band 的**扣分制** criterion 以 `criterion.max_score` 起算，并至少包含一个 deduct AtomicRule。deduct AtomicRule 不得包含 RuleLevel，发布时要求 `0 < max_points <= criterion.max_score`；`once`、`per_occurrence` 的 `cap_points` 必须为空，`capped` 要求 `0 < cap_points <= criterion.max_score`。唯一 occurrence 去重后，扣分固定为：`once = min(n, 1) × max_points`、`per_occurrence = n × max_points`、`capped = min(n × max_points, cap_points)`。`cap_points` 不必是 `max_points` 的整倍数；为得到稳定明细，按 `occurrence_id` 升序分配，每条最多分配 `max_points`，最后一条可只分配剩余 cap。per-occurrence 等合法规则在本次运行产生总扣分大于 criterion.max_score 时，该 criterion outcome 为 `invalid`，不把静态规则图误标无效，也不做静默 clamp。

`review_only` 是第三种 criterion assessment mode，不是 direction。它仍有正的 max_score/weight 并参与最终聚合，但不得包含 score-effect rule，且至少包含一个 `direction=none, effect_type=review` 的规则；其 auto score 始终为空，绝不能从 max_score 起算或自动送满分。具有 `score.review.override` 的人工必须给出 `0..max_score` 的 final score 和理由；在此之前该 criterion final score 与 run.final_total 均为空。纯 `report_only`/`block_submission` 规则应挂在 band、deduct 或 review_only criterion 上，不创建零分 Criterion。

mutex group 只作用于同一 criterion 内的多个非 band AtomicRule，组内至少两个成员；跨 criterion、单成员组或 band 上的 mutex 在发布时失败。组内零个规则触发合法，恰好一个触发合法，多个触发则相关 criterion fail closed。最后由 policy 计算 contribution，并聚合未舍入 contribution。

`occurrence_id` 是 Core 按 `occurrence-id-v1` 计算的 SHA-256 小写十六进制：输入使用 `core-canonical-json-v1`，固定含 scheme、document snapshot hash、rubric hash scheme+hash、criterion code、rule code、可空（JSON `null`）finding code、排序去重后的稳定 evidence-unit IDs 与规范化 locator；模型/Checker 提供的 occurrence ID 仅作原始审计字段。`locator-v1` 是有 discriminator 的封闭联合，只允许 `text_span(evidence_unit_id,start,end)`、`section(section_path)`、`document_structure(structure_code,ordinal)`、`page_region(page_index,bbox)` 或 `metadata(field_path)`；offset 基于归一化文本的 Unicode code point，path segment 做 NFC+trim，bbox Decimal 用无指数十进制字符串，禁止自由文本、数据库 ID 和未声明字段。run 必须保存 canonical occurrence payload；同一 digest 对应不同 payload 视为 hash collision 并 fail closed。相同 occurrence 必须去重。所有 level/score 必须位于 `0..criterion.max_score`，越界为 `invalid`，不得用 clamp 掩盖规则错误。

direction/effect 合法矩阵固定为：

| direction | 允许的 effect_type | v1 规则 |
|---|---|---|
| `band` | `score` | 必须引用已发布 `RuleLevel`；计分字段按上文为空 |
| `deduct` | `score` | once/per_occurrence/capped 均需正的 max_points；capped 另需正的 cap_points |
| `none` | `review`、`block_submission`、`report_only` | 不产生 score effect，levels/max_points/repeat/cap 均为空 |
| `bonus` | 无 | v1 不可执行、不可发布；启用前必须用新 ADR 定义 base score 与 ceiling |

### D07 — dependency/mutex 采用 fail-closed

依赖规则只有在所有依赖 decision 已有效执行且为 `triggered` 时满足，否则当前 decision 为 `skipped`。依赖必须引用同一 RubricVersion 中的 rule code；未知依赖、依赖环、mutex 多规则同时触发、required evidence invalid 或 `block_submission` 均使相关 criterion 自动最终分为空，并使 run.final_total 为空。invalid/blocked 通过依赖边传播为 `skipped`，但原始 invalid/block issue 必须保留，不能因传播而降级。

这些状态不得按“未扣分”处理，也不得自动回退成满分。

### D08 — human override 不改写自动审计事实

AI 输出、RuleDecision、证据验证结果和 calculated score 不可变。授权人工可在 `0..criterion.max_score` 设置 final criterion score，但必须以独立 override 记录 actor、当时生效的 capability 与授权来源、reason、before/after、policy hash、时间与 resolution type。

权限不依赖硬编码角色名，固定为 capability 检查：

- `score.review.override`：只允许 `ordinary_override`，不能改变 issue 状态；
- `score.review.resolve_validation`：只允许解决冻结 policy 将 issue code 标为 `human_resolvable` 的 required-evidence/validation issue，必须附新证据或明确 waiver；
- `score.review.resolve_block`：只允许解决冻结 policy 将 block code 标为 `human_resolvable` 的业务阻断；
- `score.run.rescore`：创建新的 `rescore_generation`，不能改写旧 run。

未知 Checker、execution-plan invalid、未发布/错配 RubricVersion、hash/identity 冲突和依赖环属于结构性问题，永远不能通过 human resolution 清除，只能修复输入/规则后重评分。resolution 追加在原 issue 后，不删除或改写原事实；Core 必须重新校验 capability、policy 白名单、证据/waiver 和 final score 完整性。只有全部 required/block issue 均有有效 resolution，且每个必需 criterion 均有 final score 时，才可重算 final_total。

### D09 — 运行身份由分离的 hash 合同组成

新 Core run 必须冻结并保存：

- submission/source artifact identity；
- normalized content 与 `document_snapshot_hash`；
- `rubric_source_kind`、`rubric_snapshot_hash`；
- published version 的 `rubric_version_id`、version hash 与 scheme；
- policy snapshot/hash/scheme；
- execution plan snapshot/hash/scheme；
- business Profile 与版本；
- checker manifest；
- engine、prompt、provider、model、sampling 与 anchor identity；
- `rescore_generation` 和数据库唯一的 `idempotency_key`。

文档身份边界固定如下：

- `source_artifact_hash`（`source-artifact-sha256-v1`）只对上传原始字节做 SHA-256；文件名、路径和上传时间不进入。
- `normalized_content_hash`（即 `DocumentSnapshot.content_hash`，scheme=`normalized-content-v1`）对 normalizer version、按文档顺序排列的 section path/heading/normalized text，以及按 section/ordinal 排列但**尚不含 evidence_unit_id** 的 unit normalized text 做 canonical hash；原始字节、存储引用、parser diagnostics 和业务 metadata 不进入。
- evidence_unit_id 由上述 content hash、section path、稳定 ordinal 与 unit text hash 派生，避免循环定义。
- `document_snapshot_hash`（`document-snapshot-v1`）对 schema version、business profile key/version、parser/normalizer version、content hash、section 层级与 unit IDs/locator、确定性 metrics、format facts、parse quality、schema 声明的 scoring-relevant `profile_extensions`，以及会影响评分/复核的规范化 diagnostics 做 canonical hash；storage ref、文件名、时间戳、数据库/legacy chunk ID 和非评分 provenance 不进入。
- SubmissionSnapshot 另存 business metadata 与 source artifact identity；这些字段不得偷偷混入 document hash。run 同时固定 submission、artifact、content 和 document snapshot identity，不能用其中一个代替另一个。

新 Core 的 snapshot/policy/plan hash 使用 `core-canonical-json-v1`：schema 先拒绝 float、NaN、Infinity 和未声明字段；Decimal 以去除多余尾零的无指数十进制字符串表示（零固定为 `0`，禁止负零），object key 按 Unicode code point 排序，UTF-8、`ensure_ascii=false`、无多余空白，array 保持 schema 顺序，只有声明为集合的数组才先按其 canonical JSON 排序。字符串不隐式做 Unicode/空白归一化，所需文本归一化必须在上游 versioned normalizer 中完成。

孤立 hash 没有对应不可变 snapshot 时不得声明为可重放。idempotency 冲突后必须逐项核对输入 identity；不一致视为合同冲突并 fail closed。

### D10 — hash scheme 和 Checker 版本不可原地改写

现有已发布内容 hash 冻结为 `rubric-content-v1`。它沿用当前 `_canonical_version_hash` 的 canonicalization：Decimal=`format(value, "f")`、datetime=`isoformat(timespec="microseconds")`、dict key 字符串化后排序、普通 list 保序、图中的 row collection 按其 compact/sorted-key JSON 排序，最终 `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 的 UTF-8 字节取 SHA-256。白名单固定为：

| 图节点 | `rubric-content-v1` 唯一纳入字段 |
|---|---|
| Rubric | total_score, description, format_spec |
| RubricCriterion | code, name, max_score, weight, description, evidence_hints, deduction_rules, display_order, criterion_type, scoring_mode, applies_to, rubric_levels, sub_checks, dimension, deduction_rules_structured |
| RubricCompilation | parser_version, compiler_version, model_provider, model_name, sampling_params, prompt_version, raw_parse_output, raw_model_output, validation_result, blockers, warnings |
| SourceArtifact | artifact_type, file_name, file_hash, file_size_bytes |
| SourceRule | source_rule_code, sheet_name, row_number, cell_locator, raw_text |
| RubricVersion | workflow_profile, global_policy |
| TemplateItem | item_code, kind, section_path, raw_text, normalized_constraint, strictness, source_locator, source_hash, parse_confidence |
| AtomicRule | rule_code, name, rule_text, direction, effect_type, max_points, repeat_policy, cap_points, judge_type, checker_key, checker_params, evidence_policy, positive_example, negative_example, boundary_example, strictness, applies_to, mutex_group, depends_on_rule_codes, creation_method |
| RuleLevel | level_code, points, descriptor, positive_example, negative_example, display_order |
| RuleTemplateLink | relationship_type, match_method, match_confidence, rationale，以及其所链 TemplateItem 内容 |

规则用 `criterion_code` 取代 criterion FK；来源/模板 link 以内嵌内容取代 ID。除此表以外的字段（特别是所有 ID/FK、`Rubric.name`、`Rubric.version`、各状态/审核身份/时间、human_changes/final hashes）均不进入 v1。ORM 新列不得自动进入旧 hash；M1/M3 在增加列前必须把当前动态遍历实现改为上述显式白名单，并以既有 fixture digest 证明逐字节兼容。新增 business Profile 等字段使用 `rubric-content-v2`。已发布 v1 hash 永不重写。

snapshot、policy、plan 使用各自的 `schema_version` 与 `hash_scheme`，不得复用 rubric version 算法。hash scheme 升级必须保留旧 loader/白名单并通过新 ADR 或版本化合同发布。

CheckerRegistry 只接受显式、namespaced、不可变版本的 key。条目冻结 checker version、implementation hash、params schema、支持的 DocumentSnapshot/Profile 与 observation schema。`checker-package-sha256-v1` 的 implementation hash 输入是 canonical JSON：scheme、checker key/version、entrypoint、runtime contract version、runtime dependency/build manifest SHA-256，以及按 POSIX 相对路径排序的显式 artifact manifest（每项为 path、raw-byte SHA-256）。dependency/build manifest 必须冻结影响行为的解释器/ABI、外部包版本与可获得的 wheel/container digest；artifact manifest 必须列出 checker 模块及所有会影响行为的本地代码/静态配置。重复/绝对/父级穿越路径、缺文件、依赖身份缺失或运行时 digest 不匹配均 fail closed。相同 key+version 再注册时 manifest 或 hash 不同视为合同冲突；行为修复必须注册新版本，禁止覆盖旧实现或从数据库动态 import。

### D11 — business Profile 与导入 workflow 分离

`business_profile_key` 表示评分业务，Submission、Rubric 和 Checker 支持范围必须一致。`workflow_profile` 只表示来源如何导入/编译，两者分别持久化和校验。

第二个业务 Profile 固定采用 `technical_proposal`，实现名为 `TechnicalProposalProfile`。它使用与论文不同的 metadata、章节语义、grade policy 和输出扩展，用于证明 Core 没有 Thesis 泄漏。

### D12 — legacy 数据不伪回填

完全没有 provenance 图的历史 Rubric 只能标记为 `legacy_unversioned`，由 `LegacyRubricAdapter` 生成内容 snapshot/hash；正式 version ID/hash 保持空。“存在 provenance”的判定是该 rubric 存在任一 `RubricCompilation`；其子图即使残缺也必须 fail closed，不能退回 legacy。

只要存在 provenance/version 图但没有合法发布版本，就必须 fail closed，不能伪装 legacy。历史 ScoringRun 的权威字段永不回填；推断关联只能作为独立、非权威分析数据，不参与评分、复核、报告或导出身份。

旧 `Rubric.version` 仅是 legacy label。`llm_direct` 与 `hybrid` 仅能存在于显式 legacy compatibility node；新 RubricVersion 发布前必须转换为 band/deduct/review-only 和独立叶子 criterion。

### D13 — 改造期能力入口与 UI 范围

Core 改造期间，新增论文业务能力只能进入 `ThesisProfile` 或现有论文模块的明确过渡 facade；不得继续给 `backend/app/services/scoring/engine.py` 增加论文专用分支。

Streamlit 泛化在本轮冻结。静态 Web 继续作为论文控制台；通用 schema-driven Web 属于独立后续里程碑，不阻塞 Core/API/CLI。

## 结果与取舍

- M1 可以先修 policy、weight、evidence 和缓存身份，而不等待完整 Core。
- M2 的合同和 import boundary 有明确边界，不需要在实现时重新解释术语。
- 严格 evidence 与 fail-closed 会提高人工复核率；这是安全结果，不得用静默给分规避。
- 正确性修复可能使新运行偏离 legacy golden；历史运行不重写，差异通过 compare artifact 解释。
- `technical_proposal` 的差异化合同会增加 Profile 实现量，但它是验证通用性的必要成本。

## 被拒绝的方案

- 让模型直接返回任意 points 或自报最终总分。
- 把所有 evidence 都伪装成原文 quote。
- 以检索不到内容证明全文缺失。
- 部分 criterion 使用 weight、其余按 points。
- 在同一 criterion 混用 band 与 deduct。
- 把 legacy label 或推断结果回填成正式 RubricVersion identity。
- 用可变中文 criterion.name 调度 Checker。
- 在生产 compare 中双倍调用真实模型并产生两个权威结果。
- 在 M8 前把旧论文入口默认切换到 Core。

## 合规与验收

- M0 characterization 与 golden：`backend/app/tests/test_m0_characterization.py`、`backend/app/tests/golden/m0/`。
- M0 评估基线：`docs/baselines/m0-thesis-evaluation.json`。
- M0 ADR/术语契约：`backend/app/tests/test_m0_artifacts.py`。
- 后续实现必须把本 ADR 的决策转成 Core DTO、policy/evidence 属性测试、registry/import-boundary 测试和迁移约束。

本 ADR 不包含影响 M1/M2 实施路径的开放语义。
