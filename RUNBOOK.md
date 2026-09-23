# 开发、排错与发布 Runbook

> 当前操作基线：2026-09-20；Python 3.10+，推荐/CI 为 3.12；Alembic head `0031_rubric_import_sessions`；默认 `SCORING_ENGINE_MODE=legacy`。以下命令默认在仓库根目录执行，不要把真实 Secret、论文原文或学生 PII 写入终端记录、Git、CI artifact 或工单。

## 1. 先判断运行形态

| 目的 | 数据库/存储 | 推荐入口 |
|---|---|---|
| 快速开发或离线冒烟 | 临时 SQLite + `storage/` 或临时目录 | FastAPI 或 `.venv/bin/pgs` |
| CLI 单机评分 | `~/.paper-grading/cli.db` + 本地 storage | `.venv/bin/pgs`，无需启动 Web |
| 完整本地开发 | PostgreSQL 16 + 本地 storage | FastAPI + 静态 Web |
| 内网试点 | Compose：Caddy + FastAPI + PostgreSQL 16 + volumes | `docker compose` |
| Vercel 生产 | Vercel API/静态资源 + PostgreSQL/Supabase | 只允许 GitHub Actions 门禁发布 |

Web SQLite 与 CLI 默认 SQLite 是两套库。排错前先确认当前命令的 `DATABASE_URL`、`STORAGE_ROOT`、CLI `--db/--storage` 指向同一目标。

## 2. 安装与初始化

### 2.1 依赖

优先使用锁文件和 Python 3.12：

```bash
uv sync --locked --python 3.12
```

若仓库已有 `.venv`，命令可直接使用 `.venv/bin/python`、`.venv/bin/alembic`、`.venv/bin/uvicorn`、`.venv/bin/pgs`。本文同时给出这种形式，避免依赖全局 `uv` 命令。

### 2.2 临时 SQLite 启动 Web

```bash
export DATABASE_URL=sqlite+pysqlite:////private/tmp/paper_grading_dev.db
export STORAGE_ROOT=/private/tmp/paper_grading_storage
.venv/bin/alembic upgrade head
.venv/bin/python -m backend.app.scripts.seed_dev
.venv/bin/uvicorn backend.app.main:app --reload --port 8000
```

访问：

- Web：`http://localhost:8000/`
- OpenAPI：`http://localhost:8000/docs`
- 集成状态：`http://localhost:8000/api/system/integrations`
- LLM 自检：`http://localhost:8000/api/system/llm-check`

也可在全局 `uv` 可用时运行 `./scripts/start-web.sh`；该脚本默认使用 `/tmp/dev.db` 并自动迁移、seed、启动。

### 2.3 PostgreSQL 本地启动

准备安全的本地环境变量后启动数据库：

```bash
docker compose --env-file .env.intranet up -d db
export DATABASE_URL=postgresql+psycopg://paper:<本地密码>@localhost:5432/paper_grading
.venv/bin/alembic upgrade head
.venv/bin/python -m backend.app.scripts.seed_dev
.venv/bin/uvicorn backend.app.main:app --reload --port 8000
```

不要在承载真实数据的生产库运行 `seed_dev`。

### 2.4 CLI 初始化

```bash
.venv/bin/pgs init --seed
.venv/bin/pgs check --mock
.venv/bin/pgs rubrics
```

需要隔离实验时显式指定：

```bash
.venv/bin/pgs init --seed --db /private/tmp/pgs-cli.db --storage /private/tmp/pgs-cli-storage
```

## 3. 常用开发操作

### 3.1 测试

全套测试自包含，使用内存 SQLite + Mock LLM，无需 Postgres、API Key 或联网：

```bash
.venv/bin/python -m pytest -q
```

按风险选择更小的反馈环：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_m8_documentation_contract.py
.venv/bin/python -m pytest -q backend/app/tests/test_migrations.py
.venv/bin/python -m pytest -q backend/app/tests/test_api_core_flow.py
.venv/bin/python -m pytest -q backend/app/tests/test_m6_v2_submissions_api.py
```

注意：普通 pytest fixture 多用 `create_all` 建表，不能替代真实 Alembic 链。迁移、约束和恢复的生产证据以 CI 的 PostgreSQL 16 job 为准。

### 3.2 静态 Web 构建

修改 `frontend/web/` 后生成 Vercel `public/` 指纹资源并运行契约测试：

```bash
.venv/bin/python scripts/build_web_static.py
.venv/bin/python -m pytest -q backend/app/tests/test_static_web_build.py backend/app/tests/test_m8_web_rubric_lifecycle.py
```

### 3.3 评分标准生命周期

```bash
.venv/bin/pgs import 规则.xlsx --name 校级标准 --template 模板.docx
.venv/bin/pgs rubric-graph <rubric_id> --json
.venv/bin/pgs rule-submit <rubric_id> <rule_code> --reason "送审"
.venv/bin/pgs rule-approve <rubric_id> <rule_code> --reason "核对通过"
.venv/bin/pgs template-link-review <rubric_id> <link_id> --decision confirmed --reason "映射无误"
.venv/bin/pgs rubric-submit-review <rubric_id>
.venv/bin/pgs publish <rubric_id> --compilation-id <compilation_id>
```

发布失败时先看 `rubric-graph --json` 的 blocker。导入不会自动批准、确认映射或创造缺失的评分授权；不要通过改代码绕过业务审核。

### 3.4 评分、复核与导出

v1 论文兼容：

```bash
.venv/bin/pgs score 论文.docx --rubric <rubric_id> --mock --json
.venv/bin/pgs show <run_id>
.venv/bin/pgs review <run_id> --set C01=18 --note "人工复核理由" --submit
.venv/bin/pgs report <run_id> -o /private/tmp/报告.html
.venv/bin/pgs export <batch_id> -o /private/tmp/成绩.xlsx
```

v2 通用 Profile：

```bash
.venv/bin/pgs score 方案.docx --rubric <rubric_id> \
  --profile technical_proposal \
  --profile-version technical-proposal-profile@1 \
  --metadata-json '{"proposal_id":"P-001","vendor_name":"示例供应商","project_name":"示例项目"}' \
  --mock --json
```

正式数据不要使用示例元数据，不要把输出写进仓库；需要隐私隔离的一次性评分可用 `score --no-db --rubric-file ...`。

## 4. 配置与安全检查

### 4.1 LLM

- 流程测试：`LLM_PROVIDER=mock`，不联网。
- 本地模型：`LLM_PROVIDER=local`，配置 `LOCAL_LLM_BASE_URL` 和 `LOCAL_LLM_MODEL`。
- 云模型：`openai` 或 `openai_compatible`，仅在已批准数据边界下配置 Key。
- 真实评估/门禁：`LLM_FALLBACK_TO_MOCK=false`、`LLM_DEBUG_LOG_ENABLED=false`，使用不可变模型身份。
- 多用户受保护部署：平台共享模型默认关闭；优先使用用户私有 BYOK，并配置独立 `BYOK_MASTER_KEY`。

自检：

```bash
.venv/bin/pgs check --mock
curl --fail http://localhost:8000/api/system/integrations
curl --fail http://localhost:8000/api/system/llm-check
```

### 4.2 鉴权

开发默认可关闭鉴权；公网/内网受保护部署必须至少满足：

- `AUTH_ENABLED=true`
- `AUTH_PASSWORD` 至少 12 位、不是默认值或用户名
- `AUTH_SECRET` 至少 32 位且足够随机
- `LLM_DEBUG_LOG_ENABLED=false`
- Cookie 经 HTTPS 使用 Secure、HttpOnly、SameSite

不满足时设置加载会 fail closed；不要为了启动临时放宽生产校验。

### 4.3 离线模式

`OFFLINE_MODE=true` 硬禁止外呼。在线写表、云 LLM 或 Supabase 操作会被拒绝；离线交付优先用本地模型、HTML/JSON/Excel 文件。

### 4.4 Langfuse LLM 可观测性

默认关闭。准备好独立自托管 Langfuse v4 后，通过部署平台 Secret/环境变量配置：

```bash
export LLM_OBSERVABILITY_ENABLED=true
export LLM_OBSERVABILITY_EXPORTER=langfuse
export LLM_OBSERVABILITY_CONTENT_MODE=metadata_only
export LLM_OBSERVABILITY_SUCCESS_SAMPLE_RATE=0.2
export LANGFUSE_BASE_URL=https://<approved-langfuse-host>
export LANGFUSE_PUBLIC_KEY=<managed-public-key>
export LANGFUSE_SECRET_KEY=<managed-secret-key>
export LANGFUSE_ENVIRONMENT=production
export LANGFUSE_RELEASE=<deployment-commit>
```

不要把真实值写入 `.env`、文档、仓库或 CI 日志。生产默认保持 `metadata_only`；只有完成数据边界审批后才能使用 `redacted`。`GET /api/system/integrations` 的 `llm_observability` 只返回开关/配置状态，不返回凭据。

验证：

1. 用合成材料触发一次 Core 评分。
2. 在 Langfuse 中按 `scoring_request_id` 定位 `scoring_run -> rule_scoring_task -> llm_generation -> retry_attempt` Trace。
3. 确认只有 hash/字符数、运行身份、Token、延迟和脱敏错误，没有论文原文、学生 PII、Authorization 或 Key。
4. 临时停止 Langfuse 或使 endpoint 不可达，确认评分仍按业务合同完成，仅内部观测 logger 告警。

回滚只需设置 `LLM_OBSERVABILITY_ENABLED=false`。不删除业务库中的评分、证据和审计记录，不修改历史分数。

## 5. 诊断顺序

遇到故障先收集非敏感事实：

1. 记录 commit、Python 版本、入口（Web/CLI）、数据库方言和 Alembic revision。
2. 请求 `/api/system/integrations`；已登录时请求 `/api/system/ops-readiness`。
3. 查看 HTTP 状态、`Server-Timing`、批任务状态/heartbeat 和错误 code；不要复制论文正文或 raw LLM payload。
4. 用 Mock + 最小合成 DOCX/PDF 判断问题属于解析/业务编排还是真实 provider。
5. 运行最小相关测试，再运行全套测试；涉及数据库时补真实 Postgres/Alembic 验证。
6. 形成修复方案时，同步更新 `ARCHITECTURE.md`、`DECISIONS.md`、本文件的维护记录和具体步骤。

## 6. 常见故障

| 症状 | 优先检查 | 处理 |
|---|---|---|
| API 503“数据库暂不可用” | Postgres/SQLite 路径、容器 health、`DATABASE_URL` | 启动数据库或切到明确的临时 SQLite；不要让 Web 与 CLI 指向不同库后误判数据丢失 |
| API 503“表尚未初始化/结构不匹配” | `.venv/bin/alembic current`、`heads` | 先备份，再执行 `alembic upgrade head`；生产不要用 `create_all` 修补 |
| SQLite `database is locked` / `BUSY_SNAPSHOT` | 是否在自定义代码里跨 LLM 调用持事务；是否绕过 collect-compute-persist | 缩短写事务，复用 `score_paper()`/服务层；不要用全局串行化掩盖长事务 |
| 应用开启鉴权后拒绝启动 | `deployment_security_issues` 对应的 issue code | 更换强密码/Secret，关闭原文 debug；不要降低校验 |
| 登录后资源 404 | 当前 organization、member role、资源 organization_id | 以组织隔离为准核对身份；不要改成 403 泄露资源存在性，也不要移除过滤 |
| LLM 自检失败 | provider、base URL、模型名、Key、timeout、离线模式、BYOK snapshot | 先用 Mock 确认管线；本地模型检查进程；云模型检查批准的 endpoint。受保护批次连接变更后重建任务 |
| 规则显示 `CONTEXT_LENGTH_EXCEEDED` / `TOKEN_BUDGET_UNSATISFIABLE` | PromptEnvelope schema、selection/budget identity、context window、输出预留、完整 EvidenceUnit 大小 | 保持 V4，检查规则 coverage 与证据块粒度；不要退回发送全文或截断引用。必需规则转人工复核 |
| 多个规则出现 429/498/5xx 或 `PROVIDER_CIRCUIT_OPEN` | AI connection、service tier、批并发、熔断阈值、Provider 状态 | 暂停新批次并降低外层并发；遵守 Retry-After。401/403 先重认证连接；实例重启只清进程内 circuit，不代表故障已恢复 |
| 评分返回部分结果但总分/等级为空 | `RuleScoringTask` failed/review_required 与 open/claimed blocking `ManualReviewTask` | 这是 fail-closed 预期；由授权教师领取并依据冻结原文解决，不要用普通整体复核强行发布总分 |
| 结果意外来自 Mock | `LLM_PROVIDER`、`LLM_FALLBACK_TO_MOCK`、集成状态 | 正式评估设 fallback=false；检查日志中的 provider identity，不要把 Mock 结果当质量证据 |
| 缓存结果疑似陈旧 | `PROMPT_VERSION`、模型/采样/RubricVersion/anchor identity | 修复输入构造时 bump Core 与 cache 的 prompt 版本；不要只删除用户数据或扩大 cache key 复用 |
| 上传失败/413 | Caddy 限制、`MAX_UPLOAD_SIZE_MB`、直传上限、Supabase CORS/TUS | 对齐批准的限制；serverless 大文件使用私有直传，不要改公开桶或暴露 service key |
| Paper 长时间 `parsing` | `PAPER_PARSE_LEASE_SECONDS`、对象是否存在/大小一致、worker 中断 | 租约未过期时禁止并发接管；超时后用“重新解析”，仅在新解析成功后替换 chunks |
| Submission 422 | 文件仅支持 DOCX/文本 PDF、Profile 元数据、解析质量、Profile identity | 修正输入或选择正确 Profile；不要让通用 Core 猜业务元数据 |
| Rubric 无法发布 | compilation、rule review、template link、total/policy、provenance blocker | 用 execution draft/graph 定位并由授权人补齐；不要自动批准或清 blocker |
| 正式版本却走 legacy | batch 的 `rubric_version_id` 与一致发布状态 | 这是严重回归：锁定正式版本必须强制 Core；保持全局默认 legacy 不能成为绕过理由 |
| 批任务不推进 | job status、heartbeat、runner lease、item attempts | 活跃 lease 不允许第二 runner；确认旧 runner 失效后按 API retry，只恢复失败/取消项 |
| 报告端点不匹配 | run 属于 Paper 还是 Submission | v1 用 `/api/scoring-runs/...`，v2 用 `/api/v2/scoring-runs/...`，不要交叉投影 |
| OPS ready 但不能切 Core | GATE-03 candidate/approval、`gating_eligible` | 这是预期：OPS 只表示必要条件，必须另有真实门禁和维护者批准 |
| Langfuse 无 Trace | `llm_observability` 集成状态、endpoint/Key、采样率、Langfuse health | 先保证评分权威结果完整；再检查自托管服务和 Exporter 告警。不得为了出 Trace 打开原文 debug |
| Langfuse 出现敏感内容 | content mode、脱敏回归、有效凭据和访问日志 | 立即关闭 Exporter，按安全事故处理并轮换可能暴露的凭据；清理观测副本前保留受控审计证据 |

## 7. 批任务运行与恢复

1. 创建任务前由授权用户提供完整 observation policy；代码不提供生产业务阈值。
2. `POST /api/batches/{batch_id}/score-jobs` 创建/幂等返回任务。
3. `POST /api/batch-scoring-jobs/{job_id}/run` 获取 runner lease 并执行。
4. 用 `GET /api/batch-scoring-jobs/{job_id}` 查看 counts、heartbeat、attempt 和 metrics；不要只看进程是否存在。
5. 取消用 `/cancel`；终态为 failed/canceled/completed_with_errors 后，`/retry` 只重置可重试 item。
6. heartbeat 超时前不要强行接管。确认原 runner 已失效后再按持久化状态恢复。

观察报告永远不自动设置 `production_default_switch_authorized=true`。

### 7.1 规则检查点与人工复核

先按运行读取规则状态：

```bash
curl --fail --cookie <受控会话文件> \
  http://localhost:8000/api/v2/scoring-runs/<run_id>/rule-tasks
curl --fail --cookie <受控会话文件> \
  'http://localhost:8000/api/v2/manual-review-tasks?status=open'
```

处理 blocking 任务的顺序是：

1. `POST /api/v2/manual-review-tasks/<id>/claim`，body 为当前 `{"version": n}`。
2. 在冻结 DocumentSnapshot/评分明细中核对权威 EvidenceUnit；不要从外部可变文件复制未验证证据。
3. `POST .../resolve`，提交新版本、`final_score`、理由和至少一条 `evidence_unit_id + quote`。
4. 409 表示任务已被其他人更新，重新读取后再决定；422 表示证据不属于冻结快照，不得绕过。
5. 所有 blocking 任务 resolved 后再核对 run 总分/等级和 ReviewLog；自动失败投影必须保留。

当前规则检查点在 Core 计算结束后持久化。若进程在该事务之前中断，按原 submission 的幂等身份重试整次运行；不得声称能从单规则断点恢复。尚无自动 rule retry API，Provider 恢复后仍需按批准流程重新评分或人工解决。

## 8. 备份、恢复与数据库迁移

创建同时覆盖数据库和 storage 的备份：

```bash
.venv/bin/python -m backend.app.scripts.ops_backup create \
  --storage-root <storage-root> \
  --destination <仓库外备份目录> \
  --migration-head 0023_rule_scoring_review_tasks \
  --revision <部署commit>
```

先校验：

```bash
.venv/bin/python -m backend.app.scripts.ops_backup verify <package>
```

恢复必须使用隔离数据库与不存在/空的 storage 目标，并精确确认数据库名：

```bash
.venv/bin/python -m backend.app.scripts.ops_backup restore <package> \
  --database-url postgresql+psycopg://.../<recovery_db> \
  --storage-target <空目录> \
  --confirm-database <recovery_db>
```

恢复后运行 `backend.app.scripts.verify_postgres_ops` 并核对 migration head、关键表计数、对象引用和 manifest。0017/0022/0023 含真实状态时降级会 fail closed；不要绕过有损 downgrade 拒绝。

0023 发布前必须在生产外验证：

```bash
.venv/bin/alembic upgrade 0023_rule_scoring_review_tasks
.venv/bin/alembic current
.venv/bin/python -m pytest -q backend/app/tests/test_migrations.py
```

0023 新表含审计数据后禁止 downgrade。应用回滚优先部署兼容旧 revision 并保留新表；只有确认两表为空且已有验证备份时，才可在隔离环境演练退到 0022。

Supabase 生产若存在最小权限角色 `pgs_app`，0023 会在同一迁移内为两张新表授予 SELECT/INSERT/UPDATE/DELETE、启用 RLS 并创建与现有基线一致的 `pgs_app_dml` policy。迁移后必须以运行角色执行 `verify_postgres_ops`；不要把手工 SQL Editor 操作当作未归档的永久配置。

## 9. 发布流程

### 9.1 发布前

1. 明确 revision、发布/数据库/模型/值班责任人。
2. 完成代码、迁移、测试和三文档同步；涉及 prompt/评分逻辑时重新锚定仓库外真实 QWK 基线。
3. 按 `docs/上线清单.md` 记录生产配置、日志安全、监控阈值、备份恢复和回退证据。
4. 新迁移先在生产外完成备份、verify、隔离恢复演练和 Postgres 16 逐版本验证。

### 9.2 权威 Vercel 流程

1. 功能分支按保护规则合入 `main`，不从本地工作区发布。
2. `.github/workflows/ci.yml` 运行：
   - `unit-and-sqlite-migrations`
   - `postgres-migrations-constraints-and-restore`
   - `docker-compose-smoke`
3. 三项成功后，`deploy-vercel-production` 才进入 GitHub `production` Environment，运行 `vercel pull`、`vercel build --prod`、`vercel deploy --prebuilt --prod`。
4. 保存 CI URL、三类 artifact 和 deployment URL；验证 `/`、`/api/system/integrations` 及关键登录/上传/评分/报告路径。

Vercel Git 直部署保持关闭。CLI 当前固定 `vercel@58.4.0`；不要删除 `.vercelignore` 规避上游 prebuilt 回归。

### 9.3 内网 Compose

```bash
cp .env.intranet.example .env.intranet
# 编辑强密码、Secret、站点、LLM 和 OPS 阈值
docker compose --env-file .env.intranet up -d --build
docker compose --env-file .env.intranet ps
docker compose --env-file .env.intranet logs -f app caddy
```

验证 HTTPS、`/api/system/integrations`、登录和合成数据冒烟，并确认容器内 `pg_dump --version` 为 16.x。

## 10. 回滚

- 门禁失败：不部署，修复后提交新 revision；只有 Secret 配置错误且代码未变时才重跑失败 job。
- Vercel 部署后冒烟失败：回滚到上一 Ready deployment，记录失败 revision/URL/原因；不要改写历史评分。
- 应用变更含 schema：先判断迁移是否 backward-compatible。禁止用有损 Alembic downgrade 作为默认回滚；必要时恢复已验证备份到隔离环境，确认后按批准的灾备步骤切换。
- Core 观察触发回退：只把未来未版本化入口退回 legacy compatibility；已发布 RubricVersion 仍走 Core，历史 run 不重写。
- LLM provider 故障：停止新批次；按数据边界选择已批准 provider/本地模型或 Mock 做流程诊断。Mock 不能成为正式评分或发布门禁结果。

## 11. 修复交付清单

- [ ] 有最小复现或明确症状，且未记录敏感原文。
- [ ] 根因和受影响边界已确认，不只处理表面异常。
- [ ] 修复包含回归测试；涉及 schema 有 Alembic；涉及 prompt 已 bump version。
- [ ] `ARCHITECTURE.md` 更新了修复后的边界/调用链/数据流或明确记录边界不变。
- [ ] `DECISIONS.md` 更新了选择、原因、拒绝方案和代价，或明确复用既有决策。
- [ ] `RUNBOOK.md` 更新了症状、诊断、验证、发布和回滚操作。
- [ ] 三份文档的维护记录使用同一日期/主题。
- [ ] 最小测试和全套测试按风险通过；生产变更取得 Postgres/CI/恢复证据。

### 11.1 前端 v2 计划审查与后续验证

2026-09-07 完成计划审查及用户确认后的修订，未实施前端或后端改造。初稿的问题包括正式 Core 证据无法直接关联 chunk、阻塞复核缺少操作入口、直传恢复遗漏、Excel 历史日志回填错误，以及本地/Docker 与 Vercel 部署方案不完整。修订后的 `docs/前端v2改造计划.md` §13 提供 R1–R8 闭环表，§12 提供 V01–V13 浏览器与接口验收矩阵。

在仓库根目录复查现有合同：

```bash
rg -n 'evidence_unit_id|confidence=None' backend/app/services/scoring/adapters/persistence.py backend/app/services/scoring/core/engine.py
rg -n 'manual-review-tasks|claim|resolve' backend/app/api/routes/submissions_v2.py
rg -n 'direct-upload-intents|complete-upload|uploadViaTus' frontend/web/assets/app.js
rg -n 'target_type=' backend/app/services/spreadsheet
.venv/bin/python -m pytest -q backend/app/tests/test_p3_manual_review_tasks.py backend/app/tests/test_paper_direct_upload.py backend/app/tests/test_api_core_flow.py backend/app/tests/test_m4_rule_executor.py::test_authorized_quote_is_bound_to_validated_unit_and_canonical_locator
```

审查时上述定向测试为 `15 passed`，只确认现有行为，不能证明 v2 计划已实现。计划修订使用下列现有文档核验入口；Vite/Playwright/Vitest 和前端 npm 脚本在阶段 0 实现后再补实际操作命令，不能提前记为已通过。

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_m8_documentation_contract.py
git diff --check
```

后续实现的验收需覆盖：

- 正式 RubricVersion 的 Core 证据可追溯至运行快照；无置信度/无页码/非引用证据有明确展示；阻塞任务领取冲突、证据校验和解决前总分为空均保留。
- 批量采纳的 run 范围、并发改分和重复提交有明确结果；不能覆盖已修改分数或隐式解决阻塞项。
- 大文件签名直传、TUS 恢复、归档确认与解析重试可用；组织切换时清理旧数据和在途请求；运维角色与组织范围由服务端验证。
- 导出迁移保留四种已有通道，说明批次导出与逐 run 日志的关系；状态机覆盖取消、失败、重试和重评，并禁止客户端任意设置终态。
- 浏览器完成登录/邀请/密码重置、创建批次、上传、正式评分、复核和导出；Vercel 与本地/Docker 均验证资源、深链接刷新、新旧页跳转和旧页回退。现有 Python 静态源码断言不能替代 Vue 页面行为验收。

后续计划执行顺序为：阶段 0 完成三部署、权限和浏览器门禁；阶段 1 建立状态及最小冻结正文查看器；阶段 2 完成普通/阻塞复核及原因；阶段 3～5 交付工作区、上传和模板/账户/运维；阶段 6A 扩展导出事件，6B 幂等补录历史并切入口。每阶段按计划退出条件保存证据，当前不得执行计划编号的迁移或调用计划新增端点。

本轮无需部署或数据库回滚。后续发布沿用 §9 的 main/CI/production 流程，新增前端 job 必须进入 deploy 的依赖；新增迁移需 PostgreSQL 证据，prompt 改动需 QWK 重锚。应用回退按 §10 保留数据库审计状态，使用已适配权限/复核守卫的兼容版本与归档静态产物；旧日志表首版保留，回退期产生的旧日志在重新前进时幂等补录，不默认执行有损 downgrade。已接受的设计见 D-019。

### 11.2 阶段 0 已交付部分与操作命令

已实施：Vite + Vue 3 脚手架（`frontend/workbench/`）、`/workbench/*` 双入口托管、`GET /api/system/capabilities`、运维与门禁端点的角色收紧、CI 前端 job。

浏览器验收与 IBM Plex Mono 自托管已于阶段 6B 落地，见 §11.3。验收覆盖 §12.1 矩阵的 V03–V07、V09、V11、V12；**V01、V02、V08、V10、V13 仍无自动化用例**（需真实 Supabase、多身份会话或 `AUTH_ENABLED=true` 的多组织数据，一次性验收后端不具备），不得记为已通过。

```bash
cd frontend/workbench && npm ci && npm run test:unit && npm run typecheck && npm run build
```

**API 合同门禁**（后端改了路由/模型而前端类型没跟上，会显示成 `schema.d.ts` 的
diff）：

```bash
cd frontend/workbench && npm run api:dump && npm run api:check
```

`openapi.json` 从代码现导出、不入库；`src/api/schema.d.ts` 入库，比对的就是它。
另有 `backend/app/tests/test_frontend_api_contract.py` 把前端源码里的调用路径逐条
比对真实 OpenAPI——前端是纯 JS，调错路径没有编译期报错，只在浏览器里变成一个
404，而 404 常被页面当成「暂无数据」渲染掉。

产物组装与漂移核验（Vercel 的 build hook 不带 `--with-workbench`，部署用的是仓库里已提交的 `public/workbench`，因此源码改动后必须重建并提交，否则 CI 的 `frontend-workbench` job 会拦截）：

```bash
.venv/bin/python scripts/build_web_static.py --with-workbench && git status --porcelain -- public/
```

本地起服务查看新页（`/workbench` 深链接刷新回 HTML，缺失资源 404，`/api` 不被 SPA 回退吞掉）：

```bash
DATABASE_URL=sqlite+pysqlite:////tmp/dev.db AUTH_ENABLED=false .venv/bin/python -m uvicorn backend.app.main:app --port 8000
```

权限收紧的定向验证（这些用例在 `AUTH_ENABLED=True` 下运行；仓库默认的开发模式会放行守卫，覆盖不到真实判定）：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_ops_permissions.py backend/app/tests/test_system_capabilities.py backend/app/tests/test_workbench_hosting.py
```

角色边界现状：`/system/ops-readiness` 与 `/system/llm-check` 限平台管理员；`/release-gates/*` 由路由级守卫限平台管理员，**只收紧角色，不替代**服务原有的候选身份、门禁条件与人工批准约束；`/system/organization-readiness` 限 org_admin 或已选组织的平台管理员，先按组织过滤再聚合，且不下发磁盘/数据库/部署安全等宿主事实。缺少组织上下文时直接拒绝，不回落为全局查询。

### 11.3 阶段 6B：导出补录与默认入口切换

**旧导出日志补录**（可重入，重复执行不产生重复事件）：

```bash
DATABASE_URL='<目标库>' .venv/bin/python -m backend.app.scripts.backfill_export_events --dry-run
```

**必须显式给出 `DATABASE_URL`。** `settings` 会加载 `.env.local`，开发机上它通常指向
生产库——不带 URL 直接跑，等于按文档执行一次就连上生产。脚本对非本地目标默认拒绝，
需要 `--i-know-this-is-not-local` 显式确认（与 `ops_backup restore` 的 `--confirm-database`
同一条约定）。`--dry-run` 同样被拦：它一样建立连接、一样按 `.env.local` 解析目标，
「只读所以没关系」正是让人在生产上养成随手执行习惯的那句话。

确认统计无误后去掉 `--dry-run` 正式执行。它是独立入口而非迁移的一部分，因为
`alembic upgrade head` 不会重跑已完成的迁移：回退窗口内旧应用只写
`spreadsheet_write_logs`，重新前进时必须能再执行一次把这段补上。

遇到未登记的 `target_type` 会**整批终止**并打印该值，不写入猜测的映射。此时先
确认该通道应映射到哪个展示通道，补进 `services/batches/exports.LEGACY_CHANNELS`
后重新执行。

**默认入口**：根路径直接是 v2 工作台，旧 SPA 已下线（用户决定，2026-09-08）。
`WORKBENCH_DEFAULT_ENTRY` 开关与常驻 `/legacy/` 一并取消。

`/login`、`/register`、`/reset-password` 由工作台承接。**这三条不能只剩 404**：
邮件里已经发出去的邀请与重置链接指向后两条，收件人不会重新拿到新链接。

**浏览器验收**（V01–V13）：

```bash
cd frontend/workbench && npx playwright install --with-deps chromium && npx playwright test
```

跑的是完整 Chromium 的新版 headless（配置里的 `channel: "chromium"`），不是默认的
headless shell——验收要证明的是真实浏览器里的渲染与交互。webServer 每次都新起一个
一次性后端，不复用已在跑的进程：验收里有写操作（批量采纳），复用会让第二次运行在
一个已被跑空的队列上假失败。

Playwright 需要与 Node 版本匹配的构建，1.49 在 Node 26 上会在 ESM loader 注册后静默
挂起——没有任何输出，看起来像用例卡住。遇到长时间无输出先核对版本，不要去改用例。

被测对象是 `scripts/build_web_static.py --with-workbench` 的统一组装产物经真实
FastAPI 托管的结果，不是 Vite dev server——深链接回退、缺失资源必须 404、
`/api` 不被 SPA 吞掉这几条只在生产托管路径上才会出问题。数据为合成文档与
Mock LLM，库与存储建在临时目录，因此截图与 trace 可安全归档。

### 11.4 新增迁移时必须同步的两处清单

`backend/app/services/deployment/postgres_verifier.MIGRATION_SEQUENCE` 的**末项被
当作预期 head**。加了迁移不更新它，PostgreSQL 门禁会在建 fixture **之前**抛
「unexpected alembic head」，于是后面那条「有数据时拒绝 lossy downgrade」拿到一个
空库、守卫没数据可拒，最终报成「lossy downgrade unexpectedly succeeded」——
**报错点离真正的缺陷隔了两步**，照着报错去查降级逻辑只会白费时间。

同步两处，缺一不可：

```
backend/app/services/deployment/postgres_verifier.py   # MIGRATION_SEQUENCE
.github/workflows/ci.yml                               # 逐版本升级的 revision 列表
```

`test_migration_sequence_is_current.py` 对着 Alembic 本身验，本地 pytest 就会拦下。
另有一条冻结契约测试（`test_m8_ops_readiness.py`）让改动必须是有意的，但它拿自己的
硬编码副本比对，**两边同时过期时不会报警**——这正是 0024–0028 漏登被放过去的原因。

### 11.5 一次性环境不得继承 `.env.local`

`Settings` 的 `env_file` 含 `.env.local`，开发机上那份带的是**生产**数据库地址、
口令与 `AUTH_SECRET`。任何一次性进程（浏览器验收、离线脚本）都必须先置
`PGS_DISABLE_ENV_FILE=1` 再导入 backend，并自带一次性凭据。

不这么做时**本地一直是绿的**——因为它悄悄用上了生产密钥，只有在没有这个文件的
机器（CI）上才暴露。这与「测试打到生产库」是同一个根。

### 11.6 「本部署尚未配置平台模型」

受保护部署下调用 LLM 而既没绑 BYOK 连接、平台也没配模型时，服务端抛：

```
本部署尚未配置平台模型。请绑定你自己的 AI 连接，或联系平台管理员在运维页配置平台默认模型。
```

**这是期望行为，不是故障**（D-027/D-028）。修复前的行为才是问题：`LLM_PROVIDER`
默认 `mock`，于是没绑连接的评分**悄悄返回 Mock 假分**，界面上没有任何提示。

两条出路，任选其一：

- 用户自己在「账户与连接」配一个 BYOK 连接；
- 平台管理员在运维页配置平台默认模型（`platform_llm_config` 单例）。

**改 `LLM_PROVIDER` 或 `PLATFORM_MANAGED_LLM_ENABLED` 都没有用**——受保护部署不再从
环境变量取模型。`AUTH_ENABLED=false` 的本地开发仍走 env，所以本机跑不出这个错。

### 11.7 浏览器验收：spec 与后端要对上

验收现在有**三个后端**，各自的 spec 由 project 的 `testMatch` / `testIgnore` 决定：

| project | 后端 | 覆盖 |
|---|---|---|
| `chromium` / `mobile` | `AUTH_ENABLED=false` | 主流程、窄屏 |
| `auth` | `--auth`（已配平台模型） | V01/V02/V10 |
| `llm-setup` | `--auth --no-platform-model` | 未配模型时的引导 |

**新增 spec 必须同时加进默认 project 的 `testIgnore`**，否则它会被 `chromium` 也捡走、
跑在错误的后端上——症状是同一个用例单跑通过、全跑失败，很容易被误读成不稳定。

改完前端后**必须重建产物再跑验收**（`build_web_static.py --with-workbench`）：验收托管的
是 `public/`，不是 Vite dev server。忘了重建同样表现为「代码明明改了却不生效」。

### 11.8 「验可见」不等于「验可用」

覆盖层之下的元素照样 `visible`，`toBeVisible()` 会通过而用户点不到。模型配置弹窗
的遮罩 `inset: 0` 铺满视口，配置表单一直是可见的——那条验收因此一路绿灯，直到线上
才发现表单点不了。

**要验的是能不能用**：直接 `click()` / `fill()` 一次，Playwright 的可操作性检查会
因遮挡失败。同理，「按钮存在」不等于「按钮可点」。

### 11.9 验收失败的三种常见误判

同一条用例单跑通过、全跑失败时，先按这个顺序排查，**不要直接当成不稳定**：

1. **产物没重建**。改完前端必须跑 `build_web_static.py --with-workbench` 再跑验收——
   验收托管的是 `public/`，不是 Vite dev server。症状是「代码明明改了却不生效」。
2. **spec 被错误的 project 捡走**。新增 spec 要同时加进默认 project 的 `testIgnore`
   （见 §11.7），否则它会跑在没有鉴权或没有平台模型的后端上。
3. **选择器命中多个元素**。新加的文案与既有文案重复时，`getByText` 会报
   strict mode violation——报错说的是「找不到」，实际是「找到太多」。

排除这三条之后仍然只在全跑时失败，才考虑用例间的状态串扰。

### 11.10 「本地绿是因为用了环境里的东西」

这个根出过两次，两次都只在 CI 暴露：

| 症状 | 真因 |
|---|---|
| 鉴权验收本地通过、CI 报口令强度不足 | 验收进程继承了 `.env.local` 的**生产** `AUTH_SECRET` |
| 加密相关用例本地通过、CI 报 `BYOK master key is not configured` | 同一份 `.env.local` 里有 `BYOK_MASTER_KEY` |

`Settings` 的 `env_file` 含 `.env.local`，而开发机上那份带的是生产凭据。**测试与一次性
进程都必须自带凭据**，不能借用环境里的那把。

- 测试：`conftest.py` 的 `tests_bring_their_own_byok_master_key` 固定注入测试密钥；
  要验证「没有密钥时应当失败」的用例自己 monkeypatch 成空值。
- 一次性进程（验收、离线脚本）：先置 `PGS_DISABLE_ENV_FILE=1` 再导入 backend。

自查命令（**在提交前跑一次，比等 CI 快**）：

```bash
PGS_DISABLE_ENV_FILE=1 DATABASE_URL="sqlite+pysqlite:///:memory:" .venv/bin/python -m pytest -q
```

`test_config_loading.py` 会因为这个开关失败——它验证的正是 env 文件加载，属于预期。

### 11.11 `.vue` 里漏导入 `ref`：构建与 typecheck 都拦不住

`<script setup>` 里用了没导入的 `ref`，`vite build` 与 `vue-tsc` 都**不报错**——
它在运行时才抛，表现为整个页面空白。浏览器验收会失败，但报错说的是「找不到某个
元素」，指向的是页面结构，不是缺失的导入。

判断方法：某个页面的**全部**用例同时失败（而不是零星几条），先看它的 `import`。

### 11.12 含迁移的发布：顺序不能反

新代码启动即读新表时，**先部署代码后迁移**会让应用在那段时间里报「配置读不出来」。
本仓库的 `platform_llm_config` 就是这种：`_platform_runtime()` 按设计不回落 Mock，
读表失败等于全站 LLM 动作不可用。

顺序仍应为：**迁移 → 验证运行角色 → 再部署代码**。

不过顺序反了也不会整站崩：读 `platform_llm_config` 的两处（`/system/capabilities`
与 `/system/platform-llm`）都对「表不存在」做了容错，读不到就当作「没有平台模型」。
**这是防线，不是许可**——`capabilities` 是前端启动就要读的，它 500 会让整个工作台
起不来，故障面远大于「平台模型读不到」。

`pgs-production-migrate` 的最后一步用 `pgs_app` 复验，不是用 owner——owner 什么都读
得到，证明不了应用能不能工作。0026/0027 的新表缺授权正是靠这一步才暴露（见 0029）。

### 11.13 验收用例互相抢同一行种子数据

**报错指向**：某个按钮 `locator.click` 等到超时，页面快照里那一行根本不存在。看起来
像功能没做出来，或者选择器写错了。

**真正的原因**：浏览器验收 `fullyParallel: false`、`workers: 1`、**共用一个后端**，
按声明顺序跑，而且**不在用例之间重置数据库**。前面的用例把那行数据消费掉了，后面的
用例面对的是一个已经被改过的库。

复核队列尤其容易踩：种子里可采纳项本来只有一条，「批量采纳」和「逐项确认」都要消掉
一条。谁先跑谁拿到。

**处理**：
- 需要独占一行数据的用例，在种子里给它留自己的那一行（`e2e_server/seed.py`）；
- 顺序相关的地方写进注释，说明为什么这条必须排在那条之前；
- 断言「少一行」之前**先等目标行出现**再数总数。队列加载完成前表里只有空态占位行，
  那时数到的是 1，后面的比较就是跟一个假的起点比——而它有时会碰巧通过。

### 11.14 改种子数据要顺带查依赖计数的断言

改 `e2e_server/seed.py` 时，`grep` 一遍 spec 里的数字断言（`（1）`、`已采纳 N 项`、
`toHaveCount`、`N / M 项已确认`）。种子改动不会让这些用例编译失败，只会让它们在
另一个页面上失败，而失败信息完全不提种子。

本轮把第二份材料的一项改成待确认（`need_manual_review=True`），刻意选了**翻转已有项
的标记**而不是新增一项：新增会改变 run 总分、分布图柱数与工作台 KPI，牵连面大得多。

### 11.15 「点了 AI 起草，什么都没发生」

**报错指向**：没有报错。按钮转完圈、恢复原状，阻断项数字一个没变，用户以为起草失败了。

**真正的原因**：`POST /rubrics/{id}/draft-deduction-rules` 是 **non-persistent** 的
（后端 docstring 写明）。它返回建议，不写库。上一版前端拿到结果后还去重新加载完整度
——读回来的当然是同一批旧数字。

**现在的行为**：建议由确认面板呈现，点「确认并应用」才经 `recompile` 落库，之后完整度
才会变。如果面板没出现，看这几处：

- 起草是否真的成功（`draftError` 会显示）。Mock 模型会被显式拒绝：`draft_deduction_rules`
  对 `provider == "mock"` 直接抛 `AI_DRAFT_CONNECTION_MISSING`。
- `store.lastDraft.items` 是否为空。应用成功后会被主动清空，避免同一批建议叠加两次。
- 「确认并应用」灰着，通常是这批建议被全部排除了。

**验收测不到这一段**：Mock LLM 起不了草，所以浏览器验收只能覆盖到「起草入口要先选
连接」。面板与合并逻辑靠单测（`lib/ai-draft.test.js`、`components/AiRuleDraftPanel.test.js`）。

### 11.16 界面把用户指向已经下线的入口

**报错指向**：没有报错。链接合法，页面渲染正常。

**真正的原因**：文案写在旧 SPA 下线之前。`retired-entry.test.js` 用源码契约扫
`<template>` 里的「旧版模板中心」「旧版界面」「旧 SPA」「/legacy/」。只扫模板不扫
`<script>`——注释里解释历史是正当的。

删页面时顺手 `grep` 一遍引用：`components/StageStub.vue` 已经没人 import，却还在模板里
挂着一个指向旧版工作台的链接，谁都没发现。

### 11.17 「扣了分但看不到依据」

评分时按格式或篇章问题扣了分，工作区右栏只显示结果，看不到依据——那是因为发现项
此前只进报告与导出，不进界面。现在在中栏的「篇章结构 / 格式发现」两个页签里。

排查顺序：

- 页签上的条数为 0：这一份确实没有发现。非 docx 提交无法判定格式，属正常。
- 有发现但「计入扣分」列显示**未计入**：该发现没有被任何启用的评分项消费
  （`deducted_by` 为空）。它是提示，不是已扣的分——**不要在复核时再扣一次**。
- 页签存在但内容为空：run 上这两列为 null（历史 run 常见）。前端 `|| []` 兜住，
  不会报错，但也补不出当初没算的东西。

### 11.18 视觉契约「跳过」比「失败」更难发现

`button-centering.spec.js` 第一版在 `h1` 可见之后就数 `a.btn`，数到 0 就 `test.skip`。
结果**复核页被静默跳过**——那一页的「查看原文」是表格数据回来之后才渲染的。用例报告是
绿的，实际什么都没检查。

凡是「找不到就跳过」的用例，都要先确认「找不到」是不是等待不够。这里加了
`waitForLoadState("networkidle")`，跳过数从 4 降到 3（剩下三页确实没有链接型按钮）。

同一个陷阱的另一种形态见 §11.13：断言「少一行」之前先等目标行出现。

### 11.19 同一个视觉问题修了三次

`.btn` 用在 `<a>` 上文字偏上，先后在弹窗按钮、复核行内动作、「新建评分任务」上各出现
一次，前两次都是在视图里补局部规则。

**判断依据**：修一个视觉问题之前，先 `grep` 这个类在多少个地方被用。用在三处以上就修
在类上，不要修在视图里。局部补丁的代价不是这一次的工作量，是下一次还会遇到。

验收也要跟着改：断言 `getComputedStyle` 的 `display` 锁死了实现方式，却证明不了文字
真的居中。改为用 `Range` 量文字矩形与元素矩形的中心偏差。

### 11.20 「点任何页面都被弹回配置页，但没有提示」

**报错指向**：没有报错。用户点导航，页面跳到「账户与连接」，屏幕上什么都不说。看起来
像路由坏了。

**真正的原因**：拦截是对的（没有可用模型），缺的是**解释**。旧版弹窗用组件级的
`dismissed` 标记，关掉一次整个会话不再出现，于是只剩下静默重定向。

**现在的行为**：每次被拦都会重新弹窗，并说出用户本来想去哪一页。排查顺序：

- 弹窗不出现但确实被重定向了 → `pendingBlock` 没被写入。检查守卫里
  `noteBlockedAttempt(to)` 是否在 `return` 重定向**之前**执行。
- 弹窗一直不消失 → `clearBlockedAttempt()` 没接上关闭按钮与操作链接。
- 主动走到配置页也弹 → 说明有一条陈旧的 `pendingBlock` 没清掉。配置页在白名单里，
  不该产生新记录。

**单测注意**：`pendingBlock` 是模块级 ref，跨用例共享。测试必须在 `beforeEach` 里
`clearBlockedAttempt()`，否则前一个用例留下的记录会让后一个用例看到一个不存在的弹窗。

### 11.21 「配好了 API Key 还是进不去」

配置成功后拦截应当**立即**解除，不需要刷新页面。做不到通常是漏了
`session.loadCapabilities()`：

| 位置 | 动作 | 必须刷新 |
|---|---|---|
| 运维与质量 | 保存 / 测试 / 停用平台默认模型 | ✓ |
| 账户与连接 | 新建 AI 连接 | ✓ |
| 账户与连接 | 停用 / 删除连接 | ✓（反向） |
| 账户与连接 | 换 Key（rotate） | ✗ 不改可用性 |

**反向的洞更隐蔽**：停用最后一个连接后 `can_use_llm` 已变 false，若不刷新，前端仍以为
可用、放人进功能页，然后每个动作在后端失败。症状是「能进去，但什么都做不成」，与
§11.20 完全不同，别搞混。

判据在后端 `/system/capabilities`：`platform_model_available or has_own_connection`。
开发模式（`AUTH_ENABLED=false`）恒为 true——本地复现不了这一整套，要用
`playwright test --project=llm-setup`。

### 评分标准条款确认与原型还原（2026-09-13，已部署，待登录验收）

症状：导入后有“待确认”但无正文/确认入口；完整度无缺失仍可能有编译阻断。
诊断时分别检查当前执行草稿的 blockers 与规则审核状态，不将完整度计数当作发布许可。
仅用合成 Excel/Word 验证：导入、核对来源和条款、单条确认、当前评分项批量确认、
刷新后状态保留、旧内容拒绝确认、失败停止。确认不得发布或更改其它条款。
保存前若提示满分合计与总分不一致，在“基本信息与评分项”核对总分及各项满分；仅按授权模板修正。保存失败应保留输入，重试成功后重新核对当前草稿条款。起草版本与缓存版本已更新，首次评分可能重新调用模型而不命中旧缓存。

验证入口：`.venv/bin/python -m pytest -q`（包含迁移自检）；前端在
`frontend/workbench` 运行 `npm run test:unit`、`npm run typecheck` 与 `npm run test:e2e`。
发布仍由 main push 的 GitHub CI 全部门禁及 production Environment 执行。
本次未新增数据库结构，head 仍为 `0030_platform_llm_config`；不得进行空迁移或数据降级。
发布后核对健康接口、静态资源和合成模板确认流程；发生异常停止后续写操作，按既有
生产回滚流程恢复先前部署，保留确认审计。此节不构成测试或发布已完成的证据。

发布证据：[主线 CI 34746064881](https://github.com/liuzhne/paper-grading-system/actions/runs/34746064881)
对提交 `95d189f95ed079d2f90787965859441ab5ba4e0e` 的后端、前端、Postgres、Docker 与
production 部署均成功。线上 `/`、`/workbench/rubrics`、`/api/system/integrations` 为 200；
未登录请求 `/api/rubrics/synthetic-release-check/review-workspace` 为 401。入口
`index-BQnCK4in.js`、`RubricsView-DSk2UsPD.js` 与相关 CSS 均与本地发布产物字节一致。
尚未在生产登录，未执行合成模板写入；登录后仍须完成导入、原文核对、逐条/当前评分项批量
确认、刷新保留与不自动发布验收。未发现已检查项异常，未执行回滚。

原子编辑验收：同一 METHOD 评分项导入两条规则，展开“编辑当前评分项的原子规则”，
修改其中一条正文与分档，保存后核对规则编号集合、数量、来源原文与其它条款均保留。
新草稿须重新确认。若出现 `band_criterion_invalid`，同一评分项不得保留两条计分分档
规则；按授权模板明确另一条的生效方式，不能通过忽略发布校验或自动合并条款绕过。
`test_rubric_review_workspace.py` 覆盖完整输入、旧摘要拒绝、来源保留及 AI 追加；
普通评分项总分编辑也须验证不再出现 `global_policy_unsupported`；不兼容的政策输入应返回 422 并保留编辑内容。
`e2e/rubric-review.spec.js` 覆盖编辑、保存失败重试、审核、发布和冻结闭环。

### 2026-09-14 OpenRouter 规则起草错误处理

错误协议补充：生产出现上游 400 后错误处理 TypeError，本地用 OpenRouter 数字 error.code=400 重现字符串 join 崩溃。ProviderError 统一将标量 type/code 转为字符串，忽略嵌套非标量字段；数字 400/429/503 保留对应分类，避免再次伪装为通用 503。拒绝直接 stringify 嵌套厂商错误字段，避免把私有内容带入诊断。评分结果语义、API 权限与数据库结构不变；验收覆盖数字码及嵌套字段脱敏。

执行时限补充：76d6806 上线后的同一请求被 Vercel 明确记录为 “Task timed out after 60 seconds”（504）。`vercel.json` 的 Python 函数 maxDuration 改为 300 秒；依据 [Vercel 限制](https://vercel.com/docs/functions/limitations) 验证部署支持，不升级付费套餐。OpenRouter 起草 Schema 请求默认显式关闭额外 reasoning（若连接明确启用 thinking 则保留），依据 [OpenRouter reasoning 参数](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)；单次生成不再叠加传输层重试，仍最多一次结构修正，总共最多两次模型调用。默认 HTTP 超时仍 60 秒，现有显式连接超时不变；平台 300 秒是最终硬上限，超过仍可能 504。原因日志附输出预算与 HTTP 超时数值，不记录正文。接受更长函数运行上限的成本，拒绝无限等待、自动切换付费模型与截断结果放行。

生产补充证据：部署 ef500cf 后，同一 Chrome 连接再次返回 HTTP 200，应用安全日志明确为 output_truncated；连接公开配置 provider_options 为空。起草使用通用评分预算造成容量不匹配，现为 OpenRouter 起草提供 8192 token 的独立默认下限；连接显式 max_tokens 始终优先，不修改连接持久化配置和正式评分预算。拒绝无上限增大及对截断输出静默拼接，较高默认值可能增加输出时长/调用成本；继续保留分项请求与人工确认。起草版本 rubric-rule-draft@5，缓存版本 2026-09-14-3。上线后须验证不再出现截断且返回可核对建议；若仍达到连接显式上限则提示管理员调整，不自动越过该上限。

OpenRouter 起草专用请求显式携带严格 JSON Schema（包括必需的 mutex_group）；其他厂商及评分调用不变。依据 [OpenRouter 结构化输出文档](https://openrouter.ai/docs/guides/features/structured-outputs) 和 [免费路由说明](https://openrouter.ai/openrouter/free/apps)，声明所需输出能力，仍保留应用层业务校验，不将 JSON Schema 当作评分规则授权。

症状：工作台生成规则显示通用 503/422。先在 Chrome Network 读取该请求 Response 的 detail.code/message/user_action，再用 `vercel logs --environment production --since 1h --query draft-deduction-rules --limit 30 --json` 关联请求。上游 HTTP 200 不代表规则有效，也不能据此判为额度不足。新日志 `rubric_ai_draft_failed` / `rubric_ai_draft_repair` 仅含原因码、状态或异常类型；禁止打开包含原文的生产 debug 日志。output_truncated 检查连接输出 token 预算，invalid_json/empty_content 检查模型 JSON 输出，MUTEX_GROUP_MISSING 表示缺少必填字段；不应绕过校验发布。

验证：`.venv/bin/python -m pytest -q`；`cd frontend/workbench && npm run test:unit && npm run typecheck && npm run test:e2e`；根目录 `.venv/bin/python scripts/build_web_static.py --with-workbench`。发布遵循 main CI 全部门禁，通过后核对生产健康与 Chrome 生成→查看建议；验证部分失败保留结果、失败原因可见，未经用户确认不确认或发布真实模板。回滚用 revert 修复提交并经同一 CI 重建发布；无数据库迁移或数据回退。本条记录诊断与实施，生产验证结果另记上线清单。

### 2026-09-15 AI 规则组上限与单条上限冲突（诊断、修复与验证）

症状：deduct max/repeat/cap/levels contract is invalid 多次出现。先在评分项原子规则编辑器只读核对规则编号、分值、重复策略、累计上限和档位；不要点击确认/发布作为排错手段。once 带非空 cap 即违反当前发布合同。沿 ai-draft.js 的组字段映射与 pipeline.py 编译核对来源；其它分值/档位错误须分别诊断。

实施后验证：AI 组输出→应用→重新编译→审核→发布校验完整回归；单次/互斥/组分值边界不变，重复命中仍只扣一次；capped 合法规则不受影响；来源不明的冲突不自动清除；存量修复可预览、创建新草稿、不修改已发布版本、不自动确认；重复修复无新增变化，保存失败和并发冲突可恢复。先运行 `.venv/bin/python -m pytest -q backend/app/tests/test_m4_publish_validation.py backend/app/tests/test_three_doc_contract.py`，新增映射回归后运行全套及前端门禁，不能用现有测试通过代替修复闭环验证。

发布须同步 docs/上线清单.md，经 main CI 全部门禁后部署，再用合成模板完成浏览器闭环；真实模板按用户已授权范围修复：展开原子编辑，逐条核对 AI 来源、once、互斥组及扣分不超过组上限，点击“清除不适用的单条上限”，仅此字段设为空，统一保存重新校验；在新草稿重新确认此前已审核的受影响条款，不替用户确认其它待审核原文或发布模板。回滚撤销本次代码并重建静态产物、走同一门禁；本方案无数据库结构迁移，不回退或删除审核历史。本地全套后端 1947 项、前端单元 179 项及类型检查通过；生产操作与最终门禁证据见 docs/上线清单.md。

### 2026-09-15 发布流程按人工步骤呈现

检查第 3 步：草稿阶段条款、映射、校验各自显示实际状态；未完成时能去核对，发布设置与发布按钮不可见；全部完成时提示“下一步：提交模板审核”，且当前卡片按钮可用。提交审核失败应保留草稿阶段和错误反馈；成功进入 review，核对执行版本及分享范围再点击发布；成功后字段不可编辑并提供复制新版本。审核动作不自动发布。角色权限不足或草稿存在未保存修改时不得执行审核/发布。

验证入口：在 frontend/workbench 运行 `npm run test:unit`、`npm run typecheck`；根目录运行 `.venv/bin/python scripts/build_web_static.py --with-workbench`；再在 frontend/workbench 运行 `npm run test:e2e -- rubric-review.spec.js --project=chromium`。发布沿用完整 main CI 门禁；回滚撤销前端与静态产物变更经同一门禁重新部署，无数据库迁移。线上使用现有模板只读检查界面，不能为验收而替用户提交审核或发布真实模板。

## 12. 维护记录

| 日期 | 主题 | 操作基线变化 |
|---|---|---|
| 2026-09-13 | 评分标准条款确认与原型还原 | 本地后端 1911 passed，后续总分补充修复专项 10 passed；前端 176 passed、类型与接口快照通过；浏览器 139 passed、3 skipped。主线完整 CI 和 production 部署通过，线上健康与资源已验证；待登录后的合成流程，不降级数据库。 |
| 2026-09-11 | 评分标准条款确认与原型还原 | 新增复现、状态区分、确认回归及发布/回滚检查；实施与验证进行中，尚未发布。 |
| 2026-09-10 | 未配置模型的拦截修复 | 新增 §11.20（被静默弹回的排查顺序、模块级 ref 的测试污染）与 §11.21（能力表刷新时机表、反向的洞）。未运行生产操作。 |
| 2026-09-10 | 按钮居中与文件选择框 | 新增 §11.18（视觉契约静默跳过）与 §11.19（同一视觉问题修三次的判断依据）。未运行生产操作。 |
| 2026-09-10 | 复核进度条 | 无新命令；空批次不画进度条属预期，不是渲染失败。未运行生产操作。 |
| 2026-09-10 | 工作区中栏三页签 | 新增 §11.17：发现项在哪看、「未计入」是什么意思、历史 run 为什么是空的。未运行生产操作。 |
| 2026-09-10 | AI 起草闭环与旧入口清理 | 新增 §11.15（起草端点不落库，「什么都没发生」的排查顺序）与 §11.16（已下线入口的源码契约）。未运行生产操作。 |
| 2026-09-10 | 复核表行内动作与验收数据竞争 | 新增 §11.13（用例抢同一行种子数据；「少一行」要先等目标行出现再数）与 §11.14（改种子要查数字断言）。未运行生产操作。 |
| 2026-09-01 | 初始化三文档 | 汇总 SQLite/PostgreSQL/CLI/Compose/Vercel 启动、测试、诊断、批任务、备份恢复、发布和回滚步骤；未执行生产变更。 |
| 2026-09-03 | P0 Provider 错误与 Langfuse 可观测性 | 增加 metadata-only Langfuse v4 配置、Trace 验证、敏感内容事故处理和一键关闭 Exporter 回滚步骤。 |
| 2026-09-04 | 评分韧性、规则检查点与人工复核 | 增加 V4 上下文/Provider 故障诊断、规则与人工任务操作、0023 迁移/有损回退保护及进程内 circuit 的恢复边界。 |
| 2026-09-07 | 前端 v2 计划审查 | 增加现有合同复查命令、15 项定向测试结果及后续浏览器/迁移验收要求；未运行生产操作，发布与回滚入口不变。 |
| 2026-09-10 | 弹窗可关闭与「验可见≠验可用」 | 新增 §11.8：覆盖层之下元素照样 visible，要用 click/fill 验可操作性。后续小节顺延编号。未运行生产操作。 |
| 2026-09-09 | V3-4 与含迁移发布 | 新增 §11.9（本地绿是因为用了环境里的东西）与 §11.10（含迁移的发布顺序）。上线记录见 `docs/上线清单.md` §14。未运行生产操作。 |
| 2026-09-09 | V3-3 发布区与验收排错 | 新增 §11.8：验收失败的三种常见误判（产物未重建、spec 被错误 project 捡走、选择器命中多个）。未运行生产操作。 |
| 2026-09-09 | 平台模型前端接入 | 新增 §11.7：三套验收后端与 spec 的对应关系，以及「新 spec 要加进 testIgnore」「改前端要重建产物」两个会被误读成不稳定的坑。未运行生产操作。 |
| 2026-09-09 | 平台默认模型（V3-0a） | 新增 §11.6 排错条目：「尚未配置平台模型」是期望行为，改环境变量无效。操作基线 head 更新到 `0030_platform_llm_config`。未运行生产操作。 |
| 2026-09-09 | v3 决策与三文档同步规则 | 三文档同步写入 CLAUDE.md 并加可机检门禁（`test_three_doc_contract.py`）；操作基线 head 更新到 `0029`。未运行生产操作。 |
| 2026-09-08 | 入口切换上线与门禁修复 | 导出出口全量角色门控；取消应用内组织切换；旧 SPA 下线、`/register` `/reset-password` 由工作台承接；修 `MIGRATION_SEQUENCE` 过期与验收进程继承 `.env.local`。生产部署经 main 门禁执行。 |
| 2026-09-08 | 运维脚本目标库守卫 | `backfill_export_events` 对非本地目标默认拒绝并要求显式确认；RUNBOOK 命令补 `DATABASE_URL`。`seed_dev` / `build_anchors` **未加同款守卫**：前者在 Docker 冒烟里于容器内执行，那里的库主机本就不是 localhost，照搬会打断一条正当流程。未运行生产变更。 |
| 2026-09-08 | 阶段 6B、浏览器验收与合同门禁 | 旧导出日志补录入口、默认入口开关与常驻 `/legacy/`、Playwright 25 项验收接入 CI（前端 job 补装后端依赖）；覆盖 V03–V07/V09/V11/V12，V01/V02/V08/V10/V13 仍待补。补齐 §12.2 的类型与 OpenAPI 合同门禁（`api:dump` / `api:check` + 前端调用路径静态契约）。未运行生产操作。 |
| 2026-09-07 | 前端 v2 八条审查意见落实 | 同步计划 R1–R8/V01–V13、阶段退出条件、文档检查命令与导出扩展/兼容回退顺序；新增脚本和迁移明确为待实施，未运行生产操作。 |
| 2026-09-11 | 平台模型配置默认折叠 | 验收：未配置显示完整表单；已配置刷新后显示摘要、展开入口与测试按钮，展开后可保存/停用；保存成功收起，失败保留表单，测试反馈在折叠状态可见。运行 `cd frontend/workbench && npm run test:unit && npm run typecheck`；根目录运行 `.venv/bin/python scripts/build_web_static.py --with-workbench` 重建提交产物。发布沿用 main CI 门禁；回滚撤销前端变更并重建产物，无数据库回退。未执行生产发布。 |
| 2026-09-14 | OpenRouter 规则起草错误处理 | 记录输出校验、错误分类、有限纠正和分项保留结果；未改变确认及发布边界。CI 验收等待确认成功提示后再查审核状态，不能以建议面板消失代替确认完成。 |
| 2026-09-14 | OpenRouter 起草输出预算 | 依据生产 output_truncated 增加独立默认预算，保留显式限制和原有评分预算；需再次经完整 CI 发布验证。 |
| 2026-09-14 | OpenRouter 起草执行时限 | 根据生产 60 秒硬超时调整 Vercel 上限至 300 秒，关闭未显式启用的额外推理并限制起草传输重试；无数据迁移。 |
| 2026-09-14 | OpenRouter 数字错误码兼容 | 修复错误投影自身的 TypeError，保留数字码分类，忽略非标量诊断字段。 |
| 2026-09-14 | OpenRouter 规则生成生产验收完成 | 最终 f350092 经 main CI 34819189045 全门禁部署；1934 项后端测试通过，生产健康/资源检查 8/8。生成链路 f453834 已实测 6/6 请求 200、40 条待确认建议；最终提交仅修复错误投影，保留页面建议，未确认或发布模板。详情见 docs/上线清单.md。 |
| 2026-09-15 | AI 规则组上限与单条上限冲突 | 落实组/单条上限分离、中文校验及存量修复验证；生产执行结果见上线清单。 |
| 2026-09-15 | AI 规则组上限修复部署验收 | e0dcb18 经完整 CI 34913724818 部署，线上检查 8/8；生产草稿由 draft.4 修复为 draft.5，15 个上限阻断消除，刷新后 21 条字段对照仅上限变化，审核状态仍为 17 已确认/4 待确认，未发布模板。 |
| 2026-09-15 | 发布流程按人工步骤呈现 | 检查、提交审核、确认发布按阶段集中在第 3 步；沿用现有设计组件，后端状态机和生产模板状态不变。 |
| 2026-09-15 | 发布流程引导生产验收 | 995da3a 经完整 CI 34919369965 部署，线上检查 8/8；Chrome 验证检查就绪提示及卡片内提交审核入口，未改变真实模板审核/发布状态。 |


### 2026-09-15 上传后解析衔接

症状：上传队列 done，但预检提示 uploaded。诊断检查直传归档后是否调用 POST /papers/{id}/parse。验证：直传归档→解析→预检；200 failed 必须显示失败；再次执行及刷新恢复只解析原 ID，不重传，已 parsed 不重解析。运行 frontend/workbench 下 npm run typecheck、npm run test:unit、npm run test:e2e -- upload.spec.js --project=chromium；运行 scripts/build_web_static.py --with-workbench 构建。经 main 完整 CI 部署后检查线上资源；回滚使用修复前提交生成回退提交并通过同一 CI，不回滚数据库。不得将学生文件或身份信息写入日志。

维护记录：2026-09-15 · 上传后解析衔接：已核对本节涉及的上传、解析及预检边界。

维护记录：2026-09-15 · 上传后解析衔接生产验收：c213a23 经 main CI 34920902739 全部门禁部署成功，生产页面/资源校验 9/9；Chrome 恢复原草稿的三份已上传材料并完成解析，刷新后仍为 3 份解析正常、0 阻断，开始评分可用，未启动评分。


### 2026-09-15 平台用量外键与评分异常状态

诊断：评分请求 500，ai_usage_ledger_ai_connection_id_fkey 错误且 platform 被用作连接 ID，批次仍 scoring。验证：运行 .venv/bin/python -m pytest -q backend/app/tests/test_ai_connections.py backend/app/tests/test_scoring_failure_state.py；平台快照/token 可持久化、BYOK 账本保留、模型初始化/评分/真实 flush 异常后批次 scored_with_errors。发布经 main 完整 CI，不改迁移或生产连接表。存量残留需先确认原请求已终止，再通过状态机 finish_scoring(outcome="scored_with_errors", expected_version=原版本) 在锁定目标行、核对原更新时间后处理，并同事务记录审计，不可将仍运行的任务直接改为完成。回滚使用 revert 业务提交后重跑 CI；保留评分和用量数据。Python 无法捕获函数硬终止，相关恢复须另行使用租约后台任务方案。

维护记录：2026-09-15 · 平台用量外键与评分异常状态：核对并更新上述调用链、决策与操作边界。

维护记录：2026-09-15 · 平台用量异常存量修复：生产日志确认原请求 HTTP 500 后，锁定唯一目标批次并校验 scoring、state_version=2、原更新时间及 0 条评分结果，经 finish_scoring 转为 scored_with_errors、版本 3；同事务写入 batch.failed_request_state_reconciled 审计。三份材料保留，未重新评分。

维护记录：2026-09-15 · 平台用量与异常状态生产验收：eaf947f 经完整 CI 34922785204 部署成功，生产服务/资源检查 9/9；Chrome 刷新确认目标批次退出 scoring，显示 scored_with_errors，材料 3 份、有效评分结果 0，未重新调用 AI。平台模型真实再次评分未执行；通过合成 Core 评分落库及全量回归验证修复。

### 2026-09-15 持久化后台评分

操作流程：前端创建 score job 后立即跳到 `/workbench/tasks/:batchId/run`；API 为每个 pending item 发布 `batch-scoring-items` 消息，Vercel 私有 Python subscriber 一次处理一份材料并周期更新 heartbeat。评分任务列表的“评分中”状态和进度条均可进入该页，页面顶部另设“正在评分”入口进入 `/workbench/tasks/running`。运行列表展示 queued/running/cancel_requested，以及 24 小时内的 completed_with_errors/failed。详情页轮询 latest job，展示批次名称、评分标准、开始/更新时间、总数、待处理、运行、成功、失败、取消、最后心跳及逐材料状态；不展示论文正文。整批可跨越 300 秒，单份材料仍必须在一次 Vercel 函数期限内完成。

生产只部署 Vercel。`pyproject.toml` 的 `[[tool.vercel.subscribers]]` 指向 `backend.app.services.batch_scoring.vercel_queue:score_batch_item`；Vercel 构建必须显示队列 subscriber 已生成。平台自动注入 OIDC，生产消息按 deployment 隔离，不新增队列密钥。`AUTH_ENABLED=true` 与 `LLM_FALLBACK_TO_MOCK=false` 仍是运行安全边界：缺少平台/BYOK 模型或真实调用失败时必须记录失败，不得生成 Mock 假分。数据库迁移仍只走现有受控工作流。

诊断顺序：先查看详情页的状态、最后心跳和失败项；再在 Vercel Logs 按 `batch_scoring_dispatched`、`batch_scoring_item_started`、`batch_scoring_item_finished` 或 `batch_scoring_dispatch_failed` 查询，只使用 job/item ID 关联，不记录原文或密钥。heartbeat 在 120 秒内表示至少一个消费者最近仍活动；Vercel 硬终止会留下 running item，队列在函数失败后重投，330 秒 item 租约过期后重新领取并从持久化结果恢复。模型/材料异常落为失败项，失败重试只恢复 failed/canceled/running 项，`rescore=false` 时不得覆盖已有有效结果。直接调用 `/batch-scoring-jobs/{id}/run` 返回 409 是预期保护。

若模型日志出现 HTTP 413，先核对评分上下文预算仍为默认 8k，并确认当前部署已包含 `2026-09-15-2` prompt 版本。修复后重试失败材料；验收时材料只有在每个评分项均形成有效分数后才能显示“已完成”，全项为空或 invalid/blocked 必须显示失败且可重试。历史不完整运行保留作审计，不得作为重试恢复依据。

操作权限：运行中允许“取消剩余任务”，只影响未开始材料；完成含异常后允许“重试失败项”；正常运行时不显示人工接管按钮；心跳未过期时禁止重启。任务完成后主按钮切换为“查看评分结果”，聚合页默认移除已完成任务，任务详情仍可从批次历史进入。

验收至少覆盖：Vercel 构建识别 subscriber；开始请求快速返回并跳转运行详情；每个 item 仅发布 ID 消息；列表“评分中”和进度均可跳转；关闭浏览器后任务继续；三份/42 次调用可跨越 300 秒完成；强制终止单份函数后消息重投、330 秒后恢复且不重复有效结果；模型 429/5xx 与材料失败逐项可见；完成、含异常、取消三种终态正确；生产日志和页面不含正文与非必要身份信息。回滚时先将新建任务入口置为不可用或回滚代码，再撤销 subscriber 版本；保留 job、item、评分结果和审计数据，不执行数据库降级。

维护记录：2026-09-15 · 持久化后台评分：补充 worker 启动、心跳诊断、租约恢复、用户动作、验收与回滚步骤；迁移 head 不变。

维护记录：2026-09-15 · Vercel-only 后台评分：生产操作改为 Queues Python subscriber，补充投递、函数超时恢复、日志、验收与回滚；删除 Render 步骤。

### 2026-09-17 评分模板与评分项导入方案（待实施验证）

当前访问 `/workbench/rubrics`，导入要求规则 Excel，Word 用于可选批注，成功后进入第二步；这与目标流程不同。实施时按 [方案及验证入口](docs/评分模板与评分项导入改进方案.md) 执行，使用合成无 PII 的 Word/Excel 测试文件覆盖单文件、双文件、分值冲突、名称歧义、重新上传、权限与审核状态。浏览器验收应确认两个固定上传框、无组合选择、导入留在第一步、冲突可定位及修改后旧校验失效。

本次无代码、迁移或部署，无需数据回滚。后续发布前完成上线清单、全套测试及真实 CI 门禁；若新增迁移，应先验证备份和恢复，禁止将含数据的拒绝降级迁移强行 downgrade。代码回滚需同时保持导入数据合同可读，不得删除人工确认或来源记录；具体回滚步骤在实施确定迁移后补全。

维护记录：2026-09-17 · 评分模板与评分项导入方案：记录当前行为、待实施验收和发布回滚约束；本次仅文档。

### 2026-09-18 评分规则解析模块重构（待实施验证）

当前行为：`POST /rubrics/import-files` 对同一文件先 `parse_rubric_files` 再 `prepare_file_import`，返回的 warnings 来自前者而非落库结果；多工作表只取第一张有结果的表，未映射列、无满分的行（含表尾备注中的全局规则）被静默丢弃。排查“规则明明在 Excel 里却没导入”时，报错通常不会出现，真正原因在 `parser._find_header` 的别名未命中或 `_criterion_from_row` 返回 `None`。实施后以覆盖率报告定位未认领单元。实施按 [方案](docs/评分规则解析模块重构方案.md) §11 分阶段执行，先建真实模板（合成/脱敏）快照测试；LLM 准确率评估使用独立注入错误测试集离线运行，不进入默认 pytest。本次无代码、迁移或部署，无需回滚。

维护记录：2026-09-18 · 评分规则解析模块重构方案：记录当前静默丢弃的排查指向与待实施验证步骤；本次仅文档。
维护记录：2026-09-18 · 规则解析方案澄清：补充两级门禁阻断与单元处理（/resolve）、规则审查错误豁免、输入指纹过期的验证指引与回滚考虑。

### 2026-09-18 评分规则解析模块重构（已实施）

**验证**：`.venv/bin/python -m pytest -q`（新增 `test_rubric_*`、`test_ai_draft_source_refs`、`test_rubric_llm_eval` 等）；前端 `npm run test:unit`、`npm run typecheck`、`npm run api:dump && npm run api:generate`、`npx playwright test`（新增 `e2e/rubric-parse.spec.js`）。解析结果快照在 `backend/app/tests/snapshots/rubric_parse/`；有意改变解析结果时用 `UPDATE_RUBRIC_PARSE_SNAPSHOTS=1` 重新生成，并先在 DECISIONS 记录。

**LLM 能力离线评估**（不进入默认 pytest，需要真实连接，Mock 会被拒绝并退出码 2）：

```bash
LLM_PROVIDER=openai_compatible OPENAI_COMPATIBLE_API_KEY=... .venv/bin/python -m backend.app.scripts.run_rubric_llm_eval --suite all
```

**排错**：

- 发布报 `评分标准尚不可发布：unresolved_source_units`，但第 2、3 步都已确认——原因不在条款，而在第 1 步“原文识别情况”：还有未处理的疑似规则单元或 Word/Excel 冲突。用 `GET /api/rubrics/{id}/parse-coverage` 查看 `coverage.unclaimed[].blocking` 与 `conflicts[].resolved`，逐条指派或确认不是规则（`POST /units/resolve-batch`）。
- 导入后评分项比 Excel 少一行，报错里没有任何提示——看 `parse-coverage` 的 `extraction.dropped_rows`：`未找到满分` 是分值列为空或不是数字；`整行合并的说明行` 是合并单元格同时覆盖名称列与分值列。
- 某评分项导入后是“仅人工复核”且带阻断 `DEDUCTION_RULE_NEEDS_SPLIT`——不是分值解析失败，而是扣分文本含多个判断（顿号/分号/“各扣”），请在第 2 步拆分或用 AI 起草。
- 合入 AI 结构建议返回 `SUGGESTION_STALE`——建议生成后评分项或台账变化了（包括另一个人刚保存），重新识别即可；`SUGGESTION_BLOCKED` 列出的 `removed:*` 表示该结构会删除已有评分项，无法合入，需重新导入文件；`STRUCTURE_REPARSE_AFTER_EDIT` 表示草稿已人工编辑，只能重新导入。
- LLM 类端点返回 503 `AI_CONNECTION_MISSING`——当前平台连接是 Mock 或未选择 AI 连接，这是有意拒绝，不是服务故障。

**回滚**：无迁移。代码回滚后，新写入的 `raw_parse_output`/`raw_model_output` 附加键会被旧代码忽略，不影响已发布版本；但回滚前以 `structure_reparse` 模式生成的新增评分项会保留在草稿中。

维护记录：2026-09-18 · 评分规则解析模块重构实施：记录验证命令、离线评估、门禁/丢行/拆分/合入冲突的排错指向与回滚说明。

### 2026-09-20 评分模板临时导入会话

症状与诊断：

- 上传成功但模板库没有新 Rubric：这是预期；先查 `POST /api/rubrics/import-sessions` 返回的 `status=draft` 和 `state_version`，用户点击“确认评分项，下一步”后才会创建 Rubric。
- 确认返回 409：会话版本陈旧、已取消/过期，或幂等键不同。重新 `GET /api/rubrics/import-sessions/{id}`；不得用旧版本覆盖新编辑。
- 确认返回 422：检查分值合计、重复/空编号及未解决来源冲突。双文件冲突必须显式选 Excel 或 Word；不得直接改数据库绕过。
- 小数分值：接口应返回 `score_adjustments`，例如 10.4→10、10.5→11；页面必须显示提示。没有提示却发生取整属于回归。
- 同一评分项有多条规则却出现重复行：检查临时草稿是否按 code 折叠；同 code 的名称/分值若不一致应报错，不能静默取第一条。
- 重新上传预览后原草稿变化：属于事务边界回归。preview 只读；只有携带相同 fingerprint 的 confirm 才能替换。正式 Rubric 仅 draft 可重传，review/published 返回 409。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_rubric_import_sessions.py \
  backend/app/tests/test_rubric_review_workspace.py \
  backend/app/tests/test_migrations.py \
  backend/app/tests/test_migration_sequence_is_current.py
cd frontend/workbench
npm run test:unit
npm run typecheck
npm run api:dump && npm run api:generate
npx playwright test e2e/rubric-import-session.spec.js e2e/rubric-parse.spec.js e2e/rubric-review.spec.js --project=chromium
cd ../..
.venv/bin/python scripts/build_web_static.py --with-workbench
```

发布：先备份数据库和私有存储，执行 `alembic upgrade head` 到 `0031_rubric_import_sessions`，再运行 PostgreSQL verifier；确认 `pgs_app` 可读写 `rubric_import_sessions` 且 RLS 已启用。之后按既有 CI/上线清单发布静态产物与 API。日志只记录会话/Rubric/compilation ID 和固定错误，不记录上传文档正文或文件二进制。

回滚：业务代码可用 revert 回退，但只要 `rubric_import_sessions` 有数据，0031 downgrade 会拒绝。先保留/归档草稿并确认恢复目标，不能强行删表；已确认 Rubric 和历史 compilation 永不因会话清理回滚。正式重新上传失败时原活动 compilation 必须仍有效，若已生成后继则按生命周期处理新草稿，不把已 supersede 的历史记录改写回去。

维护记录：2026-09-20 · 评分模板临时导入会话：新增解析/确认/冲突/重新上传诊断、0031 发布和拒绝有损降级的回滚步骤。

### 2026-09-20 评分标准原型对齐与分步工作流（已实施）

复现与诊断：在 `/workbench/rubrics` 分别打开首次导入、已解析未确认会话、正式草稿的第一步。若仅第二种状态显示 Word/Excel 卡片与评分项表，而其余出现原生文件输入或规则评分项侧栏，检查 `RubricsView` 是否仍把 `RubricImportWorkspace` 限制在 `activeImportSession` 分支。第一步出现「缺少规则」「校验阻断」时，检查告警是否置于步骤条件之外；疑似规则应在第二步能被指派/确认，但服务端发布门禁仍须拒绝未处理项。

来源诊断：`GET /api/rubrics/{id}/source-workspace` 先执行评分标准可见性检查，再调用 `services/rubric_import/source_workspace.py` 投影来源，响应须带 `private, no-store`。返回第一步时文件卡片空白，核对该响应、当前活动 compilation、原文件哈希和确认会话的对应关系；不能仅凭浏览器中上一次 `File` 对象恢复文件名。历史标准缺失来源返回 `unknown` 并显示待补充，不能显示虚构的解析成功、时间或文件大小。请求 403/404 时按组织权限排查，不通过前端隐藏按钮代替授权。

临时编辑无效时先看浏览器是否发出 `PATCH /api/rubrics/import-sessions/{id}`。若控制台是 `DataCloneError` 且没有 PATCH，核对更新、新增、删除是否全部使用 `structuredClone(toRaw(criteria))`；不要通过增加后端重试排查尚未发出的请求。确认显示网络失败时，先核对服务端可能已提交的会话状态；再次点击必须携带同一 `confirm:<session_id>`，成功返回原 Rubric。若因重新生成随机键收到 409，应修复客户端收据键，不清会话或重传文件重建标准。浏览器回归需模拟确认 POST 在服务端完成后丢失响应，再次点击应进入同一个标准且无重复创建。

验证使用合成无 PII 的文件，依次执行：选择文件→解析→修改整数分值/核对舍入→查看来源→处理来源冲突→确认→第二步→返回第一步→重新上传预览→取消/确认→第三步。额外检查审核中/已发布只读、只读角色和跨组织访问、未保存修改、非法文件和接口失败保留输入。桌面和窄屏需实点文件选择、拖放、问题定位、来源关闭和下一步，不能以元素可见代替可用。

已取得最新后端全量 `2197 passed, 48 warnings`（116.69 秒）、来源专项 `18 passed`、前端单元 `238 passed`、评分标准/响应式综合 E2E `21 passed`、工作流/按钮 `75 passed, 3 skipped`，类型检查、OpenAPI/schema 和构建通过。本轮受影响 E2E 合计 `96 passed, 3 skipped`，并非全项目 E2E 全量；3 项不适用跳过不计为通过。最后 21 项综合回归已覆盖响应式复制/确认重试修复、桌面及 390px 窄屏，静态重建后已核对本地实际页面；截图输出名为 `rubric-step-one-desktop.png`、`rubric-step-one-mobile.png`。详情见 [执行计划](docs/评分标准原型差异与改造执行计划.md)，未执行生产发布。

复验入口：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_rubric_source_workspace.py backend/app/tests/test_rubric_import_sessions.py backend/app/tests/test_rubric_write_gating.py backend/app/tests/test_frontend_api_contract.py backend/app/tests/test_three_doc_contract.py
cd frontend/workbench
npm run test:unit
npm run typecheck
npm run api:dump
npm run api:generate
npm run test:e2e -- e2e/rubric-import-session.spec.js e2e/rubric-parse.spec.js e2e/rubric-review.spec.js e2e/rubric-design.spec.js e2e/responsive.spec.js --project=chromium
cd ../..
.venv/bin/python scripts/build_web_static.py --with-workbench
```

发布与回滚：本轮只新增读取投影和工作台交互，不新增数据库结构，head 保持 `0031_rubric_import_sessions`。重建静态产物后按现有 main 完整 CI 和上线清单发布，不将本地浏览器通过视为生产已发布。回滚需同步回退前端源代码、生成类型和读取投影并重建产物，保留临时会话、Rubric、来源与 compilation 数据，不执行数据库 downgrade。若界面仍显示旧布局，先核对运行服务实际使用的 `public/workbench` 构建版本，再判断交互回归。

维护记录：2026-09-20 · 评分标准原型对齐与分步工作流：同步统一工作区、来源诊断、代理复制异常和确认响应丢失的复现/复验入口；记录后端全量 2197、前端 238、受影响 E2E 96 通过及 3 项不适用跳过、实际页面与截图核对，未执行全项目 E2E 全量或生产发布。

### 2026-09-21 合并父级评分项名称解析修复

症状：导入 Excel 后，多个 code、分值和说明均独立的评分项显示成同一名称。先在 `raw_parse_output.extraction.mapping` 核对“评价项目”是否映射为 `name`，再查 `source_ledger`：若多行 name 指向同一 `xlsx:<sheet>!R<row>C<col>`，这是纵向合并父级的信号；若 unit_id 不同，只是合法同名，不应自动重映射。

修复后，无冲突结构应显示该列映射为 `dimension`，各评分项名称来自独立打分项且去掉末尾分值，父单元的 `claimed_by` 为各 code 的 `.dimension`。缺少稳定 `item_label` 或已有 dimension 列时，应保留原解析并出现 E9；不得手工去重、补序号或直接改数据库。E9 可进入既有结构建议流程，最终仍需用户确认。

验证使用无 PII 的合成表格：

```bash
.venv/bin/python -m pytest -q \
  backend/app/tests/test_rubric_table_extractor.py \
  backend/app/tests/test_rubric_parse_triggers.py \
  backend/app/tests/test_rubric_structure_override.py \
  backend/app/tests/test_rubric_parse_baseline.py \
  backend/app/tests/test_rubric_import_ledger.py
.venv/bin/python -m pytest -q
```

验收同时核对：评分项数量、code、分值合计、说明和来源行不变；共享父单元改为 dimension 认领；独立同名不改；无 item_label 和已有 dimension 均触发 E9。发布不含迁移，Alembic head 保持 `0031_rubric_import_sessions`；按现有 CI/上线清单发布。回滚只回退抽取器、触发器、解析投影及对应测试/文档，不执行数据库 downgrade；已保存 compilation 保留原始台账，可通过重新上传或重新解析生成新草稿，不原地改写历史记录。

维护记录：2026-09-21 · 合并父级评分项名称解析修复：补充共享 `unit_id` 诊断、自动重映射/E9 验收、无迁移发布及历史数据保留回滚步骤。

### 2026-09-21 合并父级评分项层级展示修正

症状：解析数据中已经有 dimension，但“解析出的评分项”仍只显示“成绩项1～6”。先检查浏览器拿到的 `session.criteria[*].dimension` 和 `description`；字段存在而页面未显示属于前端投影问题，不要重新上传、修改数据库或回退解析映射。修复后，有 dimension 的行必须依次显示父维度、子项和具体要求摘要；无 dimension 行仍保持单层名称。

验证：在 `frontend/workbench` 运行 `npm run test:unit -- RubricImportWorkspace.test.js`，再运行完整 `npm run test:unit` 和 `npm run typecheck`。浏览器分别核对桌面与窄屏：长说明省略不撑破表格，点击摘要可展开编辑，保存仍只更新原 name/description，不把组合展示文本写回后端。发布前重建静态产物；回滚只回退组件、样式和测试，不回退解析代码、数据库或历史 compilation。

维护记录：2026-09-21 · 合并父级评分项层级展示修正：补充分层展示诊断、组件测试、响应式验收和仅前端回滚步骤。

### 2026-09-21 解析辅助与规则拆分步骤归位

验收第一步“基本信息与评分项”：应看到原文识别情况、未认领内容、来源冲突和可选 AI 结构辅助；触发结构识别后，差异与合入操作仍留在第一步。存在阻断单元或冲突时点击第二步，应停留在第一步并给出数量提示。处理完成后进入“评分规则”，页面不得再出现 `ParseCoveragePanel`、AI 结构辅助或结构建议，应显示“AI 规则拆分与细则完整度”、逐项规则核对和原子规则编辑。

验证运行 `npm run test:unit`、`npm run typecheck`，并执行 `npx playwright test e2e/rubric-parse.spec.js --project=chromium`；覆盖 Excel 未认领规则、Word 单文件、第一步门禁和第二步无结构面板。发布前重建静态产物。该修正无迁移；回滚只回退前端挂载、门禁和文案，保留解析台账、结构建议及规则草稿数据。

维护记录：2026-09-21 · 解析辅助与规则拆分步骤归位：补充第一步结构核对、第二步规则拆分、门禁、E2E 与无迁移回滚步骤。

### 2026-09-21 AI 扣分细则有界输出与分批生成

症状：第二步生成扣分细则时显示 `AI_DRAFT_OUTPUT_TRUNCATED` 或“模型输出达到长度上限”，该评分项保留 0 项结果，先前评分项的已生成建议仍在。先检查连接是否显式配置了低于 6144 的 `max_tokens`/`max_output_tokens`；再核对模型是否支持严格 JSON Schema。日志只应包含批次、预算、固定原因码、HTTP 状态和异常类型，不得记录评分原文或模型正文。

修复后，一个复杂评分项应按待处理片段拆成最多 6 批；单批请求最多 2 个规则组、每组最多 3 个严重程度，并带有限来源枚举和字符串长度上限。任一批截断不得返回部分规则；结构或业务校验错误最多纠正一次。全部成功后响应中的 `generation_metadata` 应包含 `batch_count`、`default_max_output_tokens=6144` 和实际请求预算（兼容路径），合并组编号稳定且最终仍为待人工确认。

验证使用合成、无 PII 数据：

```bash
.venv/bin/python -m pytest -q \
  backend/app/tests/test_template_ai_rule_refactor.py \
  backend/app/tests/test_ai_draft_source_refs.py \
  backend/app/tests/test_mock_and_validator.py
.venv/bin/python -m pytest -q backend/app/tests/test_three_doc_contract.py
.venv/bin/python -m pytest -q
```

连接验收应分别覆盖：未显式配置时采用 6144；显式较低值不被代码覆盖并返回明确诊断；OpenRouter 携带其专属 reasoning 参数；其他兼容厂商不携带该参数；OpenAI Responses 与兼容接口都发送严格 schema。若真实厂商拒绝 schema，先在账户连接页测试模型能力，不得回退成自由 JSON 并跳过校验。

发布无数据库迁移，Alembic head 保持 `0031_rubric_import_sessions`。按现有 CI 和上线清单发布；实际厂商验证只使用合成评分项。回滚可同步回退起草器、两个模型适配器、prompt/cache 版本和对应测试，不执行数据库 downgrade，不删除既有规则、草稿、审核记录或已确认版本；回滚前已生成但未确认的建议仍按现有人工确认边界处理。

维护记录：2026-09-21 · AI 扣分细则有界输出与分批生成：补充截断诊断、严格 schema/6144 预算/最多 6 批的验收、无迁移发布和保留历史数据的回滚步骤。

### 2026-09-22 最终规则集合统一确认

症状：第二步先确认原文解析规则，再应用 AI 建议后，原文规则重新显示待确认。先核对 AI 应用是否产生了新的 `compilation_id`，以及评分项的 `scoring_mode`、说明或分值是否变化；这是旧确认按安全签名失效，不应直接修改数据库为 approved。若 `ai_interpreted_user_text` 显示为“用户录入”或计入“原文”，则是来源分类回归。

修复后操作顺序：生成 AI 建议 → 排除不采用项 → 点击“应用到最终草稿” → 等待后继草稿刷新 → 在同一评分项点击“统一确认最终规则”。有待处理 AI 建议时，原文规则确认按钮必须禁用并提示先定稿；丢弃建议后可直接确认原文规则。AI 应用完成时所有新增规则仍为 draft，统一确认后当前评分项的最终规则才全部为 approved；审核记录仍逐条存在。

验证：

```bash
.venv/bin/python -m pytest -q \
  backend/app/tests/test_rule_coverage_api.py \
  backend/app/tests/test_rubric_review_workspace.py
cd frontend/workbench
npm run test:unit -- \
  src/components/AiRuleDraftPanel.test.js \
  src/components/RuleReviewPanel.test.js \
  src/lib/ai-draft.test.js
npm run typecheck
PGS_PYTHON=../../.venv/bin/python npm run test:e2e -- \
  e2e/rubric-review.spec.js --project=chromium
```

验收还需核对：应用建议前不能确认暂态原文规则；应用后一次批量请求完成当前评分项最终集合确认；响应丢失后刷新不会重复审计；真实原文规则显示“原文解析/用户录入”，`ai_interpreted_user_text` 显示“AI 解读原文”，`ai_inferred` 显示“AI 推断”。发布无迁移，按现有 CI 和上线清单重建静态产物。回滚需同步回退组件、编排、来源分类、测试和静态构建；不回退数据库、不删除已生成 compilation 或审核记录。

维护记录：2026-09-22 · 最终规则集合统一确认：补充重复确认诊断、先定稿后统一确认的验收、AI 来源分类及无迁移回滚步骤。

### 2026-09-22 本地 PostgreSQL 评分 Worker 随 Web 启动

使用 `./scripts/start-web-pg.sh` 启动时，日志必须依次出现“启动本地评分 Worker”和“启动 Web API”。在工作台创建评分任务后，任务应从 `queued` 进入 `running`，材料尝试次数由 0 增加并出现 heartbeat；无需另开终端或调用 `/batch-scoring-jobs/{id}/run`。退出脚本后，API 与 worker 都应结束，不应遗留继续领取任务的后台进程。

若任务持续显示“等待执行”、全部尝试次数为 0 且无 heartbeat：先检查启动终端是否仍有 worker 日志，再用 `lsof -nP -iTCP:8000 -sTCP:LISTEN` 确认 API；若仅手工启动了 Uvicorn，应停止后改用 `./scripts/start-web-pg.sh`。若 worker 已退出，脚本会同步关闭 API 并打印退出状态码，应从同一终端向上查数据库、模型配置或导入异常；不要取消并重建尚未被领取的任务，worker 恢复后会自动领取原 `queued` 记录。

验证：运行 `bash -n scripts/start-web-pg.sh`；在合成本地批次创建任务，确认 queued→running→终态、heartbeat、逐材料尝试次数和结果持久化；按 Ctrl-C 后确认 8000 端口释放且无 `run_batch_worker` 进程。生产仍以 Vercel 构建发现 Queue subscriber、逐材料投递和平台重试为验收依据，本地通过不能替代该门禁。本变更无迁移；回滚只恢复脚本为单独启动 API，并在本地另行启动 `run_batch_worker`，不得删除现有 job/item 或评分结果。

维护记录：2026-09-22 · 本地评分 Worker 随 Web 启动：增加启动日志、queued/heartbeat 诊断、联动退出验收和无数据回滚步骤。

### 2026-09-22 本地评分预算与规则错误诊断修复

复现特征：批任务已被 Worker 领取，所有材料 `attempt_count=1`，但材料显示“评分结果不完整”；规则任务大量出现 `TOKEN_BUDGET_UNSATISFIABLE`。先查看 `start-web-pg.sh` 启动摘要，应显示 `.env.intranet` 的 `context / margin / top-k`，当前本地基线为 `32768 / 1024 / 12`。若摘要是 8192、字段缺失或脚本直接退出，不要重试材料；检查 `.env.intranet` 是否可被 Bash source、三个字段是否为非负整数，并确认服务是通过该脚本重新启动而非单独运行 Uvicorn。

启动后可用不含正文的规则检查点诊断：材料级 `error_code=TOKEN_BUDGET_UNSATISFIABLE` 应计为 LLM failure；`RULE_EXECUTION_FAILED` 的 Worker warning 和人工复核消息只显示 `criterion_code`、`rule_code`、稳定 code 与 `exception_type`。不得开启 `LLM_DEBUG_LOG_ENABLED` 处理真实论文，也不得把异常消息、raw payload 或证据正文复制到工单。若需保存控制台，使用 `./scripts/start-web-pg.sh 2>&1 | tee -a /tmp/pgs-local.log`，文件由 tee 创建且只应保存在受控本机。

验证顺序：运行 `bash -n scripts/start-web-pg.sh` 与 `bash -n .env.intranet`；运行 `backend/app/tests/test_local_startup_script.py`、`test_p1_evidence_selection.py`、`test_p2_rule_failure_isolation.py`、`test_m8_batch_scoring_jobs.py` 和三文档契约。随后重启脚本，先用一份授权的合成或测试材料确认规则不再出现 token 预算错误、形成 6 个有效评分项和最终总分，再由用户明确触发“重试失败项”；不要自动重跑现有四份真实材料。若 32768 下仍预算不足，先降低 `SCORING_EVIDENCE_TOP_K` 或细化过大的完整 EvidenceUnit，并核对 Provider 请求体限制，禁止截断证据放行。

发布无迁移，生产 Vercel 仍从平台环境读取配置并由 Queue subscriber 消费，不依赖本地脚本。回滚代码时恢复独立导出等效评分环境变量后再启动旧脚本，否则会重新落回默认值；保留失败 run、规则任务、人工复核任务及审计记录，不删除或改写历史结果。

维护记录：2026-09-22 · 本地评分预算与规则错误诊断修复：补充有效配置摘要、数据库/安全日志诊断、单份复验、真实材料重试授权和无迁移回滚步骤。

### 2026-09-22 评分心跳时区与复核项持久化修复

症状一：Worker 与 API 进程仍存活、模型请求继续返回，但页面显示“执行中断，等待后台自动恢复”。核对任务 `heartbeat_at` 与数据库 `now()`；若心跳刚写入却由 API 判为 stale，检查宿主机时区与响应模型是否把 naive 数据库时间按本地时间比较。修复后，naive heartbeat 按 UTC 解释，活动任务在 120 秒租约内必须显示 healthy。

症状二：材料结束时报 `ck_score_items_aggregation_state`，失败行显示 `criterion_outcome.status=review_required`、`auto_score_status=calculated` 且 `ai_score=null`。这是 review-only 无分结果的状态投影错误，不应删除约束或补 0 分。修复后该行应保存为 `auto_score_status=blocked`、`ai_score=null`、`need_manual_review=true`，原始 aggregation 仍显示 review_required；材料按既有完整性规则进入需处理状态。

若旧失败项显示 `error_code=gkpj` 或说明中包含 SQL/parameters，这是修复前把 SQLAlchemy 内部链接码和异常正文错误投影到了材料记录。新失败应显示稳定 `scoring_failure` 与 `exception_type`，不含 SQL、参数或材料内容；旧审计记录保留，不原地清洗，重试后的新 attempt 才使用安全投影。

若 Worker 日志出现 HTTP 200 后的 `ChatJSONOutputError`，检查启动摘要应为 `context 32768 / output 2400 / margin 1024 / top-k 12 / json true`。平台连接 `provider_options={}` 时会采用这组本地默认；若数据库显式设置了 `max_tokens` 或 `response_format_json`，则以显式值为准。不要开启正文调试来观察原始响应；先确认模型端支持 OpenAI-compatible `response_format.type=json_object`。

验证：运行 `bash -n scripts/start-web-pg.sh`、`bash -n .env.intranet`，再运行 `.venv/bin/python -m pytest -q backend/app/tests/test_local_startup_script.py backend/app/tests/test_m4_execution_persistence.py backend/app/tests/test_m8_batch_scoring_jobs.py backend/app/tests/test_three_doc_contract.py`，然后重启本地脚本。用一份合成材料观察 heartbeat 持续 healthy；若包含 review-only 项，确认数据库不再报约束错误且页面显示需要人工处理。已有失败任务不会被代码自动重跑，修复生效后由用户点击重试。发布无迁移；回滚仅回退响应时间归一化、持久化映射、本地模型默认、测试与三文档，不修改或删除历史 job、run、rule task 和审计记录。

维护记录：2026-09-22 · 评分心跳时区与复核项持久化修复：增加心跳误报和数据库约束失败的诊断、专项验证、显式重试及无迁移回滚步骤。
