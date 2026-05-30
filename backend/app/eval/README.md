# 评估与校准（阶段 5 / 设计 §15）

回答"系统打得准不准、公不公平"——这是上线门槛。**复现一个错误的分数毫无意义**，所以必须用人工已评留出集对系统做一致性评估，并把 QWK 作为回归基线纳入 CI。

## 组成
- `metrics.py`：纯指标函数。**QWK（二次加权 Kappa）**为主（按等级序数 不及格0…优秀4），辅以总分 MAE/RMSE、同档/相邻档一致率、同档混淆矩阵。
- `runner.py`：`evaluate(predictions, truths)` 配对后产出报告（总体 + 逐维度 bias/MAE + grade 混淆）；`assert_no_regression(report, baseline)` 回归门禁；`load_dataset()` 读留出集。
- `run_eval.py`：`run_evaluation(db, dataset_path)` 对数据集里**已在库**的 paper_id 评分，与真值比对，报告落 `storage/eval/`。
- `labeled_dataset.py`：`load_scores_table()` 解析教师成绩表；`build_labeled_eval()` 从「论文文件夹 + 成绩表」**自动导入+解析+评分+比对**（复用 `services/papers/ingestion.py` 与 `scoring.engine.score_paper`）。
- `scripts/run_qwk_eval.py`：命令行入口，跑评估 + 首跑定基线 + 回归门禁（回退则非零退出，供 CI/cron）。

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
输出 QWK/MAE/等级一致率/逐维度 bias，报告落 `storage/eval/`；首跑写 `baseline.json`，之后重跑做回归门禁。

## 作回归门禁（CI）
1. 在一份**冻结的**留出集上跑一次，把 `qwk/mae` 存为 `baseline.json`。
2. 改 prompt / 换模型 / 改 Rubric 编译后重跑，`assert_no_regression(report, baseline)` 返回非空即视为回退、CI 失败。
3. 默认容差：QWK 跌幅 ≤ 0.02、MAE 涨幅 ≤ 1.0，可调。

## 注意
- **必须用真实人工评分**作真值；mock 评分只能验证管线连通，不能当基线。
- 公平性（§15.3）：判分输入已移除学生身份；评估时另需检查是否对某类论文（长度/学科/语言风格）系统性偏向。
