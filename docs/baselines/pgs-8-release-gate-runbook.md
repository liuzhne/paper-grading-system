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

## 3. 导入、审核并发布 T01–T06

先把学校评分表导入门禁数据库。导入器可直接识别“打分项 / 评价内容 / 具  体  要  求”表头、跨行合并的评价内容，以及“指导教师成绩项1（20分）”中的 code/满分：

```bash
uv run pgs import /secure/pgs-eval/input/打分规则.xlsx \
  --name 指导教师评分标准 --version 2026-v1 \
  --db /secure/pgs-eval/runtime/eval.sqlite \
  --storage /secure/pgs-eval/runtime/storage
uv run pgs rubric-graph '<rubric-id>' --json \
  --db /secure/pgs-eval/runtime/eval.sqlite
```

原始学校表只给出评价要求和满分，没有授权扣分规则或分档，因此导入后会带 `MISSING_EXECUTABLE_SCORING_MODE` blocker。当前来源表的 T02“具体要求”本身为空，还会独立产生 `MISSING_CRITERION_DESCRIPTION`；配套 Word 模板的批注可以作为格式、结构和内容约束来源，但批注没有给出分值，不能代替 T01–T06 的授权评分映射。评分标准维护者必须补齐 T02 评分依据，并取得学校批准的补充规则，在来源 Excel 中明确分档或扣分规则后以新版本重新导入；`rule-edit` 只用于细化已导入的可执行规则，不能代替缺失的授权。不得由运行者臆造扣分或档位。随后逐条执行 `rule-submit` / `rule-approve`，确认所有模板映射，执行 `rubric-submit-review`，最后用活动 compilation id 执行 `publish`。发布操作必须由具备职责的人工完成。

发布门禁会在调用真实模型前 fail closed 地验证：

- Rubric 的评分项按顺序严格为 T01–T06，满分合计与 `total_score` 一致；
- 只有一个与 published 时间和内容 hash 一致的不可变 `RubricVersion`；
- 教师成绩每行都含 T01–T06，分项在合法范围内，分项和等于总分；
- 本次 QWK 临时 batch 显式锁定到该 `RubricVersion.id`。

在配置真实模型前，数据维护者和评分标准维护者可独立运行只读预检：

```bash
DATABASE_URL=sqlite+pysqlite:////secure/pgs-eval/runtime/eval.sqlite \
.venv/bin/python -m backend.app.scripts.run_qwk_eval \
  --release-gate --preflight-only \
  --dataset-id thesis-teacher-holdout \
  --dataset-version 2026-07-30-v1 \
  --rubric-id '<published-rubric-id>' \
  --papers-dir /secure/pgs-eval/input/papers \
  --scores /secure/pgs-eval/input/teacher-scores.xlsx
```

预检只校验仓库外输入、论文/教师成绩完整覆盖以及唯一已发布的 T01–T06
`RubricVersion`，不会调用 LLM、创建私有归档或生成 candidate。输出只含公开身份
hash，并固定标记 `gate_passed=false`、`gating_eligible=false`；预检成功仅表示可以
进入真实模型候选运行，不表示 GATE-01 已通过。若 Rubric 仍为草稿，错误只列出
经过白名单过滤的 `blocker code(criterion code)`，不会回显来源文本或 blocker message。

### 3.1 建立数据库门禁关系

生产运行不在代码中写死 T01–T06 映射、模型、数据集或阈值。具备职责的用户通过
`POST /api/release-gates/profiles` 建立不可变 profile，关联：

- `gate_key` 与唯一已发布的 `rubric_id` / `rubric_version_id`；
- 预检得到的 holdout 公共身份（版本、样本数及 manifest/truth hash）；
- 固定 provider revision 或本地模型制品 SHA-256 身份；
- 脱敏 anchor manifest 及与 holdout 不相交的确认；
- 经批准的验收阈值和回退容差。

profile、candidate run 与 approval 分表保存并以外键关联。profile 内容进入
`profile_hash`，没有更新或删除端点；candidate 也做内容寻址，approval 的复合外键
绑定同一 run 的精确 `candidate_sha256`。数据库仅保存上述安全身份、聚合指标和批准
元数据，不保存论文、教师逐样本真值、原文件名或私有路径。需要变更任一关系时建立
新 profile，而不是修改旧审计记录。

`anchors_identity.holdout_exclusion_proven=true` 是创建 profile 时由责任人作出的冻结
确认；最终批准仍必须再次提交 `privacy_review.anchors_exclude_holdout=true`。二者都存入
数据库审计链，但不会把私有 holdout 明细复制到 anchor 库。

## 4. 候选运行

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
  --gate-profile-id '<release-gate-profile-id>' \
  --evaluation-id pgs-8-m1-2026-07-30 \
  --papers-dir /secure/pgs-eval/input/papers \
  --scores /secure/pgs-eval/input/teacher-scores.xlsx \
  --artifact-dir /secure/pgs-eval/archive/pgs-8-2026-07-30 \
  --model-artifact /secure/pgs-eval/input/model.gguf \
  --public-record-output /tmp/pgs-8-candidate.json
```

命令会拒绝以下情况：

- 输入、模型、运行 storage 或私有归档位于 Git 仓库内；
- 成绩表引用非 PDF/DOCX，或论文目录中有 PDF/DOCX 未列入教师成绩表；
- Mock provider、允许 Mock fallback、开启 LLM 调试日志；
- 非命令级 Core 模式；
- RubricVersion、DocumentSnapshot、policy、plan、prompt、anchor、checker、代码或依赖身份缺失；
- Git 工作树不干净；
- 任一样本失败、阻断、漏配或未进入指标。

候选还必须包含 `invalid_evidence_rate`：分子为持久化评分项中
`evidence_sufficient=false` 的数量，分母为所有已完成运行的评分项数量。该指标、
`maximum_invalid_evidence_rate` 和 `invalid_evidence_rate_rise` 均由 profile/批准记录
显式冻结，代码不提供发布默认值。

成功生成候选后，命令把安全记录自动写入 profile 关联的
`release_gate_runs`，以状态码 3 暂停并输出 `candidate_sha256` 与
`database_gate_run_id`。这表示等待批准，不表示门禁失败或通过。

## 5. 人工批准

数据库 profile 流程中，批准人向
`POST /api/release-gates/runs/{database_gate_run_id}/approve` 提交四项隐私复核：

```json
{
  "privacy_review": {
    "dataset_deidentified": true,
    "repository_scan_passed": true,
    "anchors_exclude_holdout": true,
    "teacher_truth_external_only": true
  }
}
```

服务端从精确 candidate 复制聚合指标，并从不可变 profile 读取阈值和回退容差；调用
与离线流程相同的 fail-closed 判定器后，原子写入 approval 和 final record。客户端不能
借批准请求替换指标、阈值、容差或 candidate hash。重复批准返回冲突。

需要保留离线签核文件时，仍可使用下述兼容流程。

复制 [`pgs-8-approval.example.json`](pgs-8-approval.example.json)，并完成：

1. 把 `candidate_sha256` 替换为命令输出的精确值。
2. 逐项确认去标识化、Git 扫描、教师真值仅在外部、锚点已排除留出集。
3. 把 candidate 的完整精度聚合指标原样填入 `accepted_baseline.metrics`。
4. 批准适用于当前产品定位的验收阈值与未来回退容差。

阈值属于发布决策，代码不猜测默认值。当前结果即使身份完整，指标未达批准阈值时仍会得到 `gate_passed=false`。

GATE-02 Core 候选相对已批准基线运行时，还应提供逐评分项差异说明：

```bash
  --regression-reference /external/gate-baseline-final.json \
  --difference-explanations /external/gate-02-differences.json
```

差异说明使用公开安全的 `criterion_code`、`reason_code`、`summary` 三字段；示例见
[`gate-02-difference-explanations.example.json`](gate-02-difference-explanations.example.json)。
生成的 `gate_regression.json` 同时归档基线/候选指标、指标 delta、逐维 MAE/bias
delta 与说明。数据库 GATE-02 profile 下使用回退参考时若缺少说明，CLI 会 fail closed。

### 5.1 GATE-03 完整证据

数据库 GATE-03 profile 的正式候选还必须传入 `--gate03-evidence`。该公开安全 JSON
由责任人从冻结的 PGS-6 观测、获批真实基线和 M5 parity 记录生成，至少包含：

- 完整 observation policy/hash、operational metrics 及逐信号 observation report；
- invalid evidence、unauthorized rule、人工复核、cache、checker/LLM/retry、p50/p95 latency 和 paired legacy/Core delta；
- 获批基线与 M5 parity 的引用 hash、指标 delta、显著差异和逐项解释；
- 仓库外私有逐样本报告的 SHA-256。

缺少任一字段、观测未通过或报告不能由冻结策略重算时，数据库拒绝登记 candidate。
正式 candidate 在精确 hash 人工批准前固定
`production_default_switch_authorized=false`；只有完整 GATE-03 最终记录通过后该值才为
`true`。仓库内合成数据只能调用 test-only rehearsal 端点，直接保存为终态
`ineligible`，没有批准转换。

```bash
  --gate03-evidence /secure/pgs-eval/input/gate03-evidence.json
```

## 6. 最终判定

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

## 7. 重复回归触发

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
