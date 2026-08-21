# 评估与校准（阶段 5 / 设计 §15）

回答"系统打得准不准、公不公平"——这是上线门槛。**复现一个错误的分数毫无意义**，所以必须用人工已评留出集对系统做一致性评估，并把 QWK 作为回归基线纳入 CI。

## 组成
- `metrics.py`：纯指标函数。**QWK（二次加权 Kappa）**为主；档位序数由注入的 GradeScale 动态构建，支持 percentage/raw_score 基准和非 100 总分，不再固定 60/70/80/90。
- `runner.py`：`evaluate(predictions, truths, scoring_policy=...)` 按 policy 舍入后产出总体、逐维度与动态 grade 混淆报告，同时输出 policy/GradeScale identity；`assert_no_regression()` 执行普通回归门禁。
- `run_eval.py`：`run_evaluation(db, dataset_path, scoring_policy=...)` 对数据集里**已在库**的 paper_id 评分，与真值比对，报告落 `storage/eval/`。
- `labeled_dataset.py`：`load_scores_table()` 解析教师成绩表；`build_labeled_eval()` 从「论文文件夹 + 成绩表」**自动导入+解析+评分+比对**（复用 `services/papers/ingestion.py` 与 `scoring.engine.score_paper`）。
- `scripts/run_qwk_eval.py`：命令行入口。普通模式生成明确不可用于发布的聚合基线；`--release-gate` 模式按 PGS-8 生成完整 identity candidate，并在人工批准后判定真实发布门禁。
- `gating.py`：构建伪名化私有 manifest、完整运行 identity、仓库安全 candidate、人工批准绑定和批准后回归检查。

## 留出集格式（JSON）
```json
[
  {"key": "<paper_id>", "human_total": 86, "human_items": {"C01": 13, "C02": 17}}
]
```
- `key`：库中已存在、已解析的 `paper_id`。
- `human_total`：教师总分。
- `human_items`：逐评分项人工分（按 Rubric 的 `code`），可选；用于逐维度 bias（>0 偏宽 / <0 偏严）。
- 分层抽样：覆盖各分数段与维度，避免只测中间档（§15.1）。

## 用法
```python
from backend.app.db.session import SessionLocal
from backend.app.eval.run_eval import run_evaluation

with SessionLocal() as db:
    report = run_evaluation(db, "backend/app/eval/dataset.json")
print(report["qwk"], report["mae"], report["exact_grade_agreement"])
```

## 推荐用法：论文文件夹 + 成绩表（一键 QWK）

你手头若是"一批真实 .docx 论文 + 一张教师成绩表"，无需自己整理 paper_id：

**成绩表（.xlsx 或 .csv）列约定**：文件名列（表头含 文件名/filename）+ 总分列（总分/total）+ 其余表头=评分项 `code` 的列（分项分，区分大小写，须与 Rubric 的 criterion code 一致）。例：

| 文件名 | 总分 | C01 | C02 |
|---|---|---|---|
| 张三.docx | 86 | 13 | 17 |

**命令行**（需真实 LLM + 教师用的同一份已发布 Rubric）：
```bash
LLM_PROVIDER=openai_compatible OPENAI_COMPATIBLE_API_KEY=... \
.venv/bin/python -m backend.app.scripts.run_qwk_eval \
    --rubric-id <rubric_id> --papers-dir /path/to/theses --scores /path/to/scores.xlsx
```
也可用 `pgs eval ... [--profile thesis]`。正式 rubric 会自动锁定唯一一致发布的 RubricVersion；如果缺失或有多个候选则 fail closed。评估从该版本读取 ScoringPolicy/GradeScale/舍入规则，并输出 RubricVersion、Profile、Policy、GradeScale、plan/document/runtime/model 身份。

输出 QWK/MAE/等级一致率/逐维度 bias、复核率、阻断率和 invalid evidence item rate，报告落 `storage/eval/`；首跑写 `baseline.json`，之后重跑做普通实验回归检查。invalid evidence item rate 定义为 `evidence_sufficient=false` 的评分项数除以全部已完成评分项数。该聚合基线固定标记 `gating_eligible=false`，不是发布证据。

真实 M1/M5/M8 发布门禁必须使用 `--release-gate`，详见 `docs/baselines/pgs-8-release-gate-runbook.md`。

M8 的真实 GATE-03 只有在 dataset/truth、唯一 published immutable RubricVersion、DocumentSnapshot manifest、不可变 model/provider artifact、policy、plan、prompt、anchors、checker 与 code revision 全部冻结，且 anchors/holdout 排除确认完成时才可能标记 `gating_eligible=true`。阈值和回退容差必须绑定 candidate hash 并取得维护者批准。任何 test-only 运行固定 `production_default_switch_authorized=false`，只能验证工具链，不能授权默认 Core。

## 作回归门禁（CI）
1. 普通实验可在一份**冻结的**留出集上跑一次，把聚合 `qwk/mae` 存为 `baseline.json`。
2. 改 prompt / 换模型 / 改 Rubric 编译后重跑，`assert_no_regression(report, baseline)` 返回非空即视为回退、CI 失败。
3. 默认容差：QWK 跌幅 ≤ 0.02、MAE 涨幅 ≤ 1.0，可调。

上述默认容差不适用于真实发布门禁。发布门禁的 QWK、MAE、RMSE、等级一致率、复核率、阻断率与 invalid evidence item rate 阈值及回退容差必须由维护者显式批准并绑定 candidate hash。

## 注意
- **必须用真实人工评分**作真值；mock 评分只能验证管线连通，不能当基线。
- 公平性（§15.3）：判分输入已移除学生身份；评估时另需检查是否对某类论文（长度/学科/语言风格）系统性偏向。
