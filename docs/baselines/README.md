# 评估基线归档规则

本目录只保存不含论文正文、学生身份或教师原始成绩的评估元数据。

M0 同时冻结两类证据：

1. `backend/app/tests/golden/m0/` 是 Mock LLM 下可重复生成的行为 golden，可作为回归门禁。
2. `m0-thesis-evaluation.json` 是 2026-06-16 A4 真实模型评估的**历史转录**。原始论文、教师分数、运行报告、数据库与 `/tmp` 基线均未留在仓库，因此它不可重算、不可作为自动门禁，也不能补造缺失 hash。`archive_record_version` 只版本化这份仓库内元数据记录，不是、也不能代替当时未记录的 dataset version。

新的可门禁评估基线必须在运行时一并保存：去标识化 dataset manifest/version/hash、真值 hash、RubricVersion snapshot/hash、DocumentSnapshot manifest hash、model/provider artifact identity、policy/plan/prompt/anchor/checker identity、代码 revision、完整精度指标与逐样本报告。任一必需 identity 缺失时，`gating_eligible` 必须为 `false`。
