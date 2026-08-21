# Web 与 CLI 功能对等矩阵

两端共享同一评分内核（`backend/app`），按形态定位分工：**Web=协作/多用户/可视化**，**CLI=本地/批处理/隐私离线**。

| 能力 | Web（联网） | CLI（本地离线） | 备注 |
|---|---|---|---|
| 建/导入模板（Excel+Word 批注） | ✅ `/rubrics`、导入页 | ✅ `pgs import` | 导入返回 warnings + template_summary 作预检反馈 |
| Rubric 严格审核/发布 | ✅ 静态 Web 生命周期闭环 | ✅ `pgs rubric-submit-review` / `rule-approve` / `publish` | 两端共用 M4 lifecycle；Web 显示 import warnings/template_summary、活动 compilation、blockers、规则/映射签核，并显式提交 compilation_id 发布唯一 immutable RubricVersion |
| 论文兼容评分 | ✅ 上传页 + 持久化 `/batches/{id}/score-jobs` | ✅ `pgs score`（多文件/目录/`--workers`） | Web 保存观察策略、租约/逐论文检查点、尝试历史与门禁信号，支持 cancel/retry；不传 Profile 时默认 thesis，保持 v1 兼容 |
| 通用 Profile/Core 评分 | ✅ `/api/v2/submissions` + `/api/v2/submissions/{id}/score` | ✅ `pgs score --profile <key> [--profile-version <version>] --metadata-json ...` | 两端都从已发布 RubricVersion 解析用户建立的 Profile 关联，必须精确版本且不回退 |
| 零落库打分 | — | ✅ `pgs score --no-db --rubric-file --profile thesis` | CLI 专属：论文兼容 Profile 可直入 Core；其他 Profile 缺少已发布 DB 关联时 fail closed |
| 查看评分明细 | ✅ 复核页 | ✅ `pgs show <run_id>` | 含证据/置信/篇章·格式发现 |
| 人工复核闭环 | ✅ 复核页改分+提交 | ✅ `pgs review --set CODE=分 --submit` | 共用复核内核，写 ReviewLog + 重算 |
| HTML 报告 | ✅ v1 `/scoring-runs/{id}/report`；v2 `/api/v2/scoring-runs/{id}/report` | ✅ `pgs report <run_id>` | CLI 按 run 类型选择兼容 HTML 或通用 HTML |
| **结构化 JSON 导出** | ✅ v1 `/scoring-runs/{id}/export.json`；v2 `/api/v2/scoring-runs/{id}/export.json` | ✅ `pgs report <run_id> --format json` | Submission run 输出稳定 `grading-core/run-export@2` |
| Excel 导出 | ✅ `/batches/{id}/export.xlsx` | ✅ `pgs export <batch_id>` | 离线可用 |
| Google Sheets 写入 | ✅ `/scoring-runs/{id}/write-sheet` | ✅（需联网） | **离线模式禁用**（OFFLINE_MODE） |
| 相对排名 / 漂移 / 抽样复核 | ✅ `/batches/{id}/ranking·drift·review-sample` | ⏸（可经 API/脚本） | 分析面板在 Web |
| L2 校准锚点管理 | ✅ 评分标准页 / `/calibration/anchors` | 脚本 `build_anchors`（本地策展，PII 不入库） | |
| QWK 评估 / 回归门禁 | — | ✅ `pgs eval --profile thesis` + `scripts/run_qwk_eval.py` | 自动锁定唯一已发布 RubricVersion；GradeScale/ScoringPolicy/舍入来自冻结数据，不固定 60/70/80/90 |
| Core 切换 inventory 审计 | — | ✅ `pgs core-cutover-audit [--json]` | 只读枚举活跃 rubric/batch；blocker 时退出 1。通过只覆盖 inventory scope，不代表 GATE-03 或默认切换批准 |
| LLM 选择（云/本地） | 只读状态卡 + 自检（配置走 env） | ✅ `--provider/--model/--base-url` 旗标 | §4 决策：Web 不做密钥编辑 UI |
| 连通/离线自检 | `/system/llm-check`、`/system/integrations`（含 network/offline_ready） | `pgs check`、`pgs doctor` | |
| 鉴权/多用户 | 单租户简单登录（P4.3，规划中） | 本地单用户豁免 | |

> ⏸ = 暂未在该端暴露 UI（能力在内核/API/脚本里，按需补）。
