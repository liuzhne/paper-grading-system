# 评估与校准（阶段 5 / 设计 §15）

回答"系统打得准不准、公不公平"——这是上线门槛。**复现一个错误的分数毫无意义**，所以必须用人工已评留出集对系统做一致性评估，并把 QWK 作为回归基线纳入 CI。

## 组成
- `metrics.py`：纯指标函数。**QWK（二次加权 Kappa）**为主（按等级序数 不及格0…优秀4），辅以总分 MAE/RMSE、同档/相邻档一致率、同档混淆矩阵。
- `runner.py`：`evaluate(predictions, truths)` 配对后产出报告（总体 + 逐维度 bias/MAE + grade 混淆）；`assert_no_regression(report, baseline)` 回归门禁；`load_dataset()` 读留出集。
- `run_eval.py`：`run_evaluation(db, dataset_path)` 对数据集里的论文用系统评分，与真值比对，报告落 `storage/eval/`。

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

## 作回归门禁（CI）
1. 在一份**冻结的**留出集上跑一次，把 `qwk/mae` 存为 `baseline.json`。
2. 改 prompt / 换模型 / 改 Rubric 编译后重跑，`assert_no_regression(report, baseline)` 返回非空即视为回退、CI 失败。
3. 默认容差：QWK 跌幅 ≤ 0.02、MAE 涨幅 ≤ 1.0，可调。

## 注意
- **必须用真实人工评分**作真值；mock 评分只能验证管线连通，不能当基线。
- 公平性（§15.3）：判分输入已移除学生身份；评估时另需检查是否对某类论文（长度/学科/语言风格）系统性偏向。
