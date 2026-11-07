# ASAP 2.0 外部基准评测方案

状态：**脚本已实现，只用假模型跑通**（2026-11-04；真实语料 300 篇假模型试跑 9 秒完成，流程无报错）。用真实模型跑需要维护者授权预算。

## 1. 目的与边界

用公开的 ASAP 2.0 语料检验“按评分标准判定、由代码算分”这套方法在公认基准上处于什么水平，并与已发表结果对照（直接整体打分 0.46、RULERS 0.71、单模型加 12 篇示例 0.72，均为 QWK）。

- **不能**验证第二阶段改造（`docs/论文评分第二阶段改造方案.md`）：作文平均 363 词、没有章节，预算本来就放得下，判定范围也用不上。第二阶段要用自有长论文验证。
- 只是外部对照，不是发布门禁；报告固定 `gating_eligible=false`。
- 不改评分代码、提示词与数据库结构。

## 2. 数据

- 文件：Kaggle `lburleigh/asap-2-0` 的 `ASAP2_train_sourcetexts.csv`（CC BY 4.0）。24,728 篇，列有 `essay_id`、`score`（1–6）、`full_text`、`assignment`、`prompt_name`、学生背景与最多 4 篇材料原文。文件名带 train，实际是全量，没有划分列。
- 数据与生成物一律放在仓库外（脚本拒绝仓库内的输出目录）；仓库根目录下的 CSV 已加进本机 `.git/info/exclude`。

## 3. 流程

```text
prepare  CSV → 按（分数, 题目）分层抽样（固定种子）→ 每篇生成 .docx（题目要求 + 作文，可选附材料）
         → scores.csv（文件名, 总分, HOLISTIC）+ manifest.json（ID、分数、题目、学生背景）
run      评分标准：一个评分项 HOLISTIC、6 个档位（S1–S6，分值 1–6），在进程内走
         建草稿 → 确认规则 → 提交审核 → 发布（与工作台同一组接口与守卫）
         → build_labeled_eval（导入 + Core 评分 + 比对，现有入口）
         → 在 1–6 原始分上算指标，写报告
```

档位描述从本机 JSON 读取（`--levels`）。官方标准是 PERSUADE 2.0 仓库里的 `sat_rubric_only_source_based.pdf`，源自 SAT 评分标准、有版权，不进仓库：由使用者把官方原文填进 JSON，`"official": true`。仓库里的 `backend/app/eval/asap2_levels.unofficial.json` 是自写的简短描述，只用于跑通流程，结论不能和文献比较。

## 4. 指标

- 主要：QWK（1–6 整数分，`quadratic_weighted_kappa` 固定 1..6）、MAE、完全一致率、相差不超过 1 分的比例、混淆矩阵、各分数段准确率。
- 公平性：按题目、经济状况、是否英语学习者、族裔、性别分组的平均偏差（系统分 − 人工分）。
- 运行：完成数、无总分数（判失败，或有评分项等人工定分——“证据不足”转人工落在这里）、评分政策的复核标记数。没有总分的样本不进指标，报告单列；不同组比较时取各组都有总分的交集。
- 评分政策的复核标记（总分过低、接近等级线）在 6 分制上会标出大量低分作文，它不改总分，不能当转人工率读。
- 报告记录三个第二阶段设置、模型身份、档位文件哈希与是否官方原文。

## 5. 对比组与费用

| 组 | 做法 |
|---|---|
| 基础 | 官方档位描述 |
| 示例 | 档位描述 + 每档 1–2 篇示例（从样本外抽，写进档位的正例） |
| 证据不足 | 基础 + `SCORING_INSUFFICIENT_EVIDENCE_MODE=review` |

每篇一次模型调用，约三千多输入 token；300 篇约 100 万 token，每组粗估不到 1 美元（未核实价格）。

## 6. 用法

```bash
.venv/bin/python -m backend.app.scripts.run_asap_eval prepare --csv ASAP2_train_sourcetexts.csv --out ~/pgs-eval/asap2 --size 300
```

```bash
LLM_PROVIDER=mock DATABASE_URL=sqlite+pysqlite:///$HOME/pgs-eval/asap2/eval.db .venv/bin/python -m backend.app.scripts.run_asap_eval run --data ~/pgs-eval/asap2 --levels backend/app/eval/asap2_levels.unofficial.json --label mock
```

真实模型运行前先 `alembic upgrade head` 建库，并配置与生产相同的模型连接（gemini-2.5-flash，思考关闭）。
