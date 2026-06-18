# Web 与 CLI 功能对等矩阵

两端共享同一评分内核（`backend/app`），按形态定位分工：**Web=协作/多用户/可视化**，**CLI=本地/批处理/隐私离线**。

| 能力 | Web（联网） | CLI（本地离线） | 备注 |
|---|---|---|---|
| 建/导入模板（Excel+Word 批注） | ✅ `/rubrics`、导入页 | ✅ `pgs import` | 导入返回 warnings + template_summary 作预检反馈 |
| 发布/克隆模板 | ✅ | ✅ `pgs publish` | draft→publish 冻结 |
| 上传/批量打分 | ✅ 上传页 + `/batches/{id}/score` | ✅ `pgs score`（多文件/目录/`--workers`） | 共用 score_paper 内核 |
| 零落库打分 | — | ✅ `pgs score --no-db --rubric-file` | CLI 专属：从文件直接评分、不建库 |
| 查看评分明细 | ✅ 复核页 | ✅ `pgs show <run_id>` | 含证据/置信/篇章·格式发现 |
| 人工复核闭环 | ✅ 复核页改分+提交 | ✅ `pgs review --set CODE=分 --submit` | 共用复核内核，写 ReviewLog + 重算 |
| HTML 报告 | ✅ `/scoring-runs/{id}/report` | ✅ `pgs report <run_id>` | |
| **结构化 JSON 导出** | ✅ `/scoring-runs/{id}/export.json` | ✅ `pgs report <run_id> --format json` | 下游二次处理 |
| Excel 导出 | ✅ `/batches/{id}/export.xlsx` | ✅ `pgs export <batch_id>` | 离线可用 |
| Google Sheets 写入 | ✅ `/scoring-runs/{id}/write-sheet` | ✅（需联网） | **离线模式禁用**（OFFLINE_MODE） |
| 相对排名 / 漂移 / 抽样复核 | ✅ `/batches/{id}/ranking·drift·review-sample` | ⏸（可经 API/脚本） | 分析面板在 Web |
| L2 校准锚点管理 | ✅ 评分标准页 / `/calibration/anchors` | 脚本 `build_anchors`（本地策展，PII 不入库） | |
| QWK 评估 / 回归门禁 | — | ✅ `pgs eval` + `scripts/run_qwk_eval.py` | CLI/CI 侧 |
| LLM 选择（云/本地） | 只读状态卡 + 自检（配置走 env） | ✅ `--provider/--model/--base-url` 旗标 | §4 决策：Web 不做密钥编辑 UI |
| 连通/离线自检 | `/system/llm-check`、`/system/integrations`（含 network/offline_ready） | `pgs check`、`pgs doctor` | |
| 鉴权/多用户 | 单租户简单登录（P4.3，规划中） | 本地单用户豁免 | |

> ⏸ = 暂未在该端暴露 UI（能力在内核/API/脚本里，按需补）。
