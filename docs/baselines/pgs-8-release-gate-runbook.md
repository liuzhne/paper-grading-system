# PGS-8｜M1 真实教师留出集发布门禁

本流程把真实论文、教师真值和逐样本结果留在仓库外；Git 中只允许保存聚合指标、不可逆内容身份、批准信息和私有产物 hash。它不会修改生产默认 `SCORING_ENGINE_MODE=legacy`。

## 1. 角色与人工确认

正式运行前必须明确以下责任人：

- 数据维护者：确认教师留出集已去标识化、版本冻结，论文与成绩表都在仓库外。
- 模型维护者：确认本地模型制品 SHA-256，或云模型使用不可变 revision 而非滚动 alias。
- 评分标准维护者：确认使用已发布并锁定的 RubricVersion，锚点样本不属于留出集。
- 发布批准人：查看候选结果，批准当前基线、验收阈值和未来允许回退容差。

以上任何一项缺失时，可以做普通实验，但不能宣称 M1 门禁通过。

## 2. 冻结仓库外资产

建议目录结构：

```text
/secure/pgs-eval/
  input/
    papers/
    teacher-scores.xlsx
    model.gguf                 # 本地模型时
  runtime/
    eval.sqlite
    storage/
  archive/
    pgs-8-2026-07-30/
```

数据集必须有稳定的 `dataset-id` 和只增不改的 `dataset-version`。代码会计算：

- 不含原文件名的论文 artifact manifest hash；
- 由 artifact hash 派生的逐样本伪名；
- 教师真值 hash；
- dataset manifest hash。

私有 manifest 仍含教师分数，只能留在外部归档目录。

## 3. 候选运行

示例使用本地固定模型；云模型把 `--model-artifact` 换成厂商提供的不可变 `--immutable-model-revision`：

```bash
DATABASE_URL=sqlite+pysqlite:////secure/pgs-eval/runtime/eval.sqlite \
STORAGE_ROOT=/secure/pgs-eval/runtime/storage \
LLM_PROVIDER=local \
LLM_FALLBACK_TO_MOCK=false \
LLM_DEBUG_LOG_ENABLED=false \
SCORING_ENGINE_MODE=core \
.venv/bin/python -m backend.app.scripts.run_qwk_eval \
  --release-gate \
  --evaluation-id pgs-8-m1-2026-07-30 \
  --dataset-id thesis-teacher-holdout \
  --dataset-version 2026-07-30-v1 \
  --rubric-id '<published-rubric-id>' \
  --papers-dir /secure/pgs-eval/input/papers \
  --scores /secure/pgs-eval/input/teacher-scores.xlsx \
  --artifact-dir /secure/pgs-eval/archive/pgs-8-2026-07-30 \
  --model-artifact /secure/pgs-eval/input/model.gguf \
  --public-record-output /tmp/pgs-8-candidate.json
```

命令会拒绝以下情况：

- 输入、模型、运行 storage 或私有归档位于 Git 仓库内；
- Mock provider、允许 Mock fallback、开启 LLM 调试日志；
- 非命令级 Core 模式；
- RubricVersion、DocumentSnapshot、policy、plan、prompt、anchor、checker、代码或依赖身份缺失；
- Git 工作树不干净；
- 任一样本失败、阻断、漏配或未进入指标。

成功生成候选后，命令以状态码 3 暂停，输出 `candidate_sha256`。这表示等待批准，不表示门禁失败或通过。

## 4. 人工批准

复制 [`pgs-8-approval.example.json`](pgs-8-approval.example.json)，并完成：

1. 把 `candidate_sha256` 替换为命令输出的精确值。
2. 逐项确认去标识化、Git 扫描、教师真值仅在外部、锚点已排除留出集。
3. 把 candidate 的完整精度聚合指标原样填入 `accepted_baseline.metrics`。
4. 批准适用于当前产品定位的验收阈值与未来回退容差。

阈值属于发布决策，代码不猜测默认值。当前结果即使身份完整，指标未达批准阈值时仍会得到 `gate_passed=false`。

## 5. 最终判定

对已经审阅的原 candidate 做离线 finalize。此步骤不会重新运行模型，也不会读取真实论文或教师成绩：

```bash
.venv/bin/python -m backend.app.scripts.run_qwk_eval \
  --finalize-candidate /secure/pgs-eval/archive/pgs-8-2026-07-30/gate_candidate.json \
  --approval /secure/pgs-eval/approval/pgs-8-approval.json \
  --public-record-output docs/baselines/pgs-8-m1-approved.json
```

批准文件绑定候选的精确 hash；不能拿批准文件批准“重跑后看起来相同”的另一个候选。输入、结果或身份发生任何变化都必须重新生成并重新批准。只有最终记录同时满足：

- `reproducible=true`
- `gating_eligible=true`
- `gate_passed=true`
- `status=passed`

才可作为 M1 真实发布门禁证据。即使通过，本命令也不会切换生产默认 Core。

## 6. 重复回归触发

以下任一变化必须在同一冻结留出集上重跑：

| 变化 | 必须冻结/比较的身份 |
|---|---|
| 评分逻辑或代码 | code revision、engine、policy、plan、checker |
| prompt 或输入构造 | prompt version/source manifest、plan |
| 模型/provider/采样 | model artifact identity、provider revision |
| Rubric/Checker | RubricVersion、snapshot、policy、plan、checker manifest |
| anchors | anchor manifest，并重新证明与留出集无交集 |
| 文档解析 | DocumentSnapshot manifest、scored source manifest |

回归必须使用批准记录中的容差，不允许运行者临时放宽。需要改变阈值时，必须形成新的人工批准记录。

执行时复用候选运行命令，并增加：

```bash
  --regression-reference docs/baselines/pgs-8-m1-approved.json \
  --public-record-output /tmp/pgs-8-regression.json
```

同一 dataset/truth、Rubric 和 anchors 下，代码、prompt、模型、checker、policy、plan 或解析身份可以变化，命令会按批准容差判定指标。dataset/truth、Rubric 或 anchors 发生变化时，命令拒绝复用旧基线并要求重新人工批准。
