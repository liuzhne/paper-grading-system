# 评估基线归档规则

本目录只保存不含论文正文、学生身份或教师原始成绩的评估元数据。

M0 同时冻结两类证据：

1. `backend/app/tests/golden/m0/` 是 Mock LLM 下可重复生成的行为 golden，可作为回归门禁。
2. `m0-thesis-evaluation.json` 是 2026-06-16 A4 真实模型评估的**历史转录**。原始论文、教师分数、运行报告、数据库与 `/tmp` 基线均未留在仓库，因此它不可重算、不可作为自动门禁，也不能补造缺失 hash。`archive_record_version` 只版本化这份仓库内元数据记录，不是、也不能代替当时未记录的 dataset version。

新的可门禁评估基线必须在运行时一并保存：去标识化 dataset manifest/version/hash、真值 hash、RubricVersion snapshot/hash、DocumentSnapshot manifest hash、model/provider artifact identity、policy/plan/prompt/anchor/checker identity、代码 revision、完整精度指标与逐样本报告。任一必需 identity 缺失时，`gating_eligible` 必须为 `false`。

PGS-8 的正式流程见 [`pgs-8-release-gate-runbook.md`](pgs-8-release-gate-runbook.md)。发布门禁采用两阶段批准：

1. 有权限的维护者在仓库外运行真实留出集，生成私有 manifest、逐样本报告和仓库安全的 candidate。
2. 负责人完成隐私/锚点排除复核，并用 candidate 的精确 hash 批准基线、验收阈值和未来回退容差；对原 candidate 离线 finalize 后才可能得到 `gate_passed=true`。

普通 `pgs eval` / `run_qwk_eval` 首跑生成的聚合 `baseline.json` 会明确标记 `reproducible=false`、`gating_eligible=false`，只用于本地实验对比，不能替代上述发布门禁。

GATE-02 等仓库内 Mock/parity 记录属于 `test-only`，必须保持 `production_default_switch_authorized=false`。[`gate-03-test-only-rehearsal.json`](gate-03-test-only-rehearsal.json) 同样只证明 GATE-03 的数据库关系、完整归档合同和不可审批边界可执行；它使用合成真值与 test fixture 模型，不能替代真实门禁。

真实 GATE-03 还必须确认 anchors/holdout 排除，使用不可变真实模型与唯一 published RubricVersion，携带通过的 PGS-6 观测快照、获批基线与 M5 parity 比较，以及私有逐样本报告 hash。在所有门禁阈值和回退容差通过后取得维护者批准；最终记录必须同时为 `gating_eligible=true`、`gate_passed=true`、`production_default_switch_authorized=true` 才有资格进入 PGS-36 的默认切换决策。
