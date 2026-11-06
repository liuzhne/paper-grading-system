# CLI 参考（`pgs`）

> 从 README 移来（2026-11-06）。快速试用见 [README](../README.md#快速开始)；初始化与隔离实验见 [RUNBOOK §2.4](../RUNBOOK.md)；Web 与 CLI 的功能对照见 [web-cli-功能对等.md](web-cli-功能对等.md)。

设计的双前端之一：与 Web 共享同一套评分内核，**自带本地 sqlite + storage，不起服务即可跑完整流程**。`uv sync` 后即有 `pgs` 命令（或 `uv run pgs` / `python -m backend.app.cli.main`）。数据默认落在 `~/.paper-grading/`，`--db` / `--storage` 可改。

```bash
uv run pgs init --seed                                   # 建本地库 + 默认评分标准/批次
uv run pgs check [--mock]                                # LLM 连通自检（--mock 离线、不联网）
uv run pgs import 规则.xlsx --name 校级标准 --template 模板.docx   # 导入 M4 provenance 草稿图
uv run pgs rubrics                                       # 列出评分标准（--json 机器可读）
uv run pgs rubric-graph <rubric_id> --json               # 查看活动 compilation/version/rule/link 状态
uv run pgs rule-submit <rubric_id> <rule_code> --reason "送审"
uv run pgs rule-approve <rubric_id> <rule_code> --reason "核对通过"  # 逐条规则人工签核
uv run pgs template-link-review <rubric_id> <link_id> --decision confirmed --reason "映射无误"
uv run pgs rubric-submit-review <rubric_id>               # rubric draft → review
uv run pgs publish <rubric_id> --compilation-id <id>     # 严格校验后发布冻结版本
uv run pgs score 论文.docx 目录/ --rubric "校级标准" [--mock] [--workers N] [--report-dir out/]   # 评分（多文件/目录递归）
uv run pgs score 方案.docx --rubric <id> --profile <key> [--profile-version <version>] --metadata-json '{...}' --mock --json   # v2 Profile/Core
uv run pgs core-cutover-audit --json                    # 只读盘点活跃 Rubric；有 blocker 退出 1
uv run pgs score 论文.docx --no-db --rubric-file 规则.xlsx [--template 模板.docx] [--mock]   # 无状态评分：不建库、不落任何库
uv run pgs runs [--batch <id>]                           # 列出评分任务（拿 run_id）
uv run pgs show <run_id>                                 # 逐项明细 + 篇章一致性/格式问题
uv run pgs review <run_id> --set C01=18 [--note "理由"] [--submit]   # 人工复核：按 code 改单项分/提交
uv run pgs batches                                       # 列出批次（拿 batch_id）
uv run pgs report <run_id> -o 报告.html                  # 生成 HTML 报告
uv run pgs export <batch_id> -o 成绩.xlsx                # 导出批次 Excel
uv run pgs scores-template <rubric_id> -o 成绩表模板.xlsx   # 生成教师成绩表模板（QWK 填写用）
uv run pgs eval --rubric <id> --papers-dir 论文夹/ --scores 成绩表.xlsx [--profile thesis]   # 版本锁定的 QWK + 基线/回归门禁
```

- LLM 由环境变量驱动（与 Web 一致，读 `.env`）；`--mock` 强制本地 Mock、不联网。
- 批量：`score --workers N` 并发评分（适合真实 LLM 批量）+ 进度条 + 等级分布汇总。LLM 计算已脱离 DB 事务（6.1 解耦：collect 读 → compute 纯算 → persist 短写），多 worker 在本地 sqlite 也能并行（compute 无锁，仅末尾短写经 busy_timeout 串行）。
- `score` 任一篇解析/评分失败即**非零退出**（便于脚本/CI）；`--json` 在 `score`/`rubrics`/`runs`/`show`/`batches`/`check` 输出机器可读结果。
- M4 发布不会自动批准规则、确认模板映射或清除 blocker。驳回规则用 `rule-reopen`，审核中标准用 `rubric-return-draft`，旧的未版本化草稿须先显式执行 `rubric-upgrade --reason ...`。
- 静态 Web 的“Rubric 严格审核与发布”面板与 CLI 共用上述 M4 lifecycle：导入后展示 warnings/template_summary 和 blocker，逐条完成规则/模板映射签核，并显式绑定活动 `compilation_id` 发布。
- 正式标准的 batch 会锁定唯一已发布 `rubric_version_id`，评分时必然使用 AtomicRule Core；全局 `SCORING_ENGINE_MODE` 在 M8 前仍默认 `legacy`，只影响未版本化兼容路径。
- 显式 `--profile` 走 v2 Submission→不可变 DocumentSnapshot→Core，Profile key/version 必须与数据库中唯一已发布 RubricVersion 的关联一致；用户在生产中建立关联，CLI 不猜测、不硬编码非论文默认。
- 内置生产 Profile 包括 `technical_proposal / technical-proposal-profile@1`、`general_document / general-document-profile@1` 与 `thesis / thesis-legacy-profile@1`。工作台与评分助手支持通用文档与学术论文评估，类型在导入评分标准时选择（见 `docs/通用文档评分改造方案.md`）；通用文档身份字段为文件名与标题。技术方案元数据严格为 `proposal_id`、`vendor_name`、`project_name`，其章节/checker/等级/扣分仍由用户审核发布的 RubricVersion 和 ScoringPolicy 绑定，不随代码预置业务评分数据。
- `pgs eval` 对正式标准自动锁定唯一一致发布的 RubricVersion，按其 ScoringPolicy 中的 GradeScale、总分和舍入规则计算动态 QWK 档位，并输出 Profile/Policy/GradeScale 可复现身份。
- `pgs core-cutover-audit` 对活跃 GradingBatch/EvaluationBatch 稳定去重，重算 published provenance hash、加载 snapshot 并用生产 registry 构建 plan；legacy 只接受显式 direct/composite compatibility。报告通过只代表 Rubric inventory scope，`production_default_switch_authorized` 始终为 false，不能替代 GATE-03 与维护者发布批准。
- 闭环：`score` → 拿 run_id（或 `pgs runs`）→ `pgs show <run_id>` 看逐项明细 → `pgs review` 改分/提交（写 ReviewLog、重算总分）→ `pgs report` 出报告。
- 无状态：`score --no-db --rubric-file 规则.xlsx` 从文件直接评分，**不建 sqlite、不落任何库**（隐私/一次性/脚本友好，由 6.1 纯 Core 支撑）；`--json` 取逐项明细；需 `--rubric-file`，不支持 `--report-dir`。
