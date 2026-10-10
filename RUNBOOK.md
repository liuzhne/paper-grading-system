# 开发、排错与发布 Runbook

> 当前操作基线：2026-09-28；Python 3.10+，推荐/CI 为 3.12；Alembic head `0032_single_active_ai_connection`；默认 `SCORING_ENGINE_MODE=legacy`。以下命令默认在仓库根目录执行，不要把真实 Secret、论文原文或学生 PII 写入终端记录、Git、CI artifact 或工单。

## 1. 先判断运行形态

| 目的 | 数据库/存储 | 推荐入口 |
|---|---|---|
| 快速开发或离线冒烟 | 临时 SQLite + `storage/` 或临时目录 | FastAPI 或 `.venv/bin/pgs` |
| CLI 单机评分 | `~/.paper-grading/cli.db` + 本地 storage | `.venv/bin/pgs`，无需启动 Web |
| 完整本地开发 | PostgreSQL 16 + 本地 storage | FastAPI + 静态 Web |
| 内网试点 | Compose：Caddy + FastAPI + 后台评分 worker + PostgreSQL 16 + volumes | `docker compose` |
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
| 生产迁移工作流报 `tenant/user postgres.<ref> not found` | Supabase 项目状态（控制台或 `get_project`） | 不是连接串或密码错误：免费项目闲置会被暂停（INACTIVE）。恢复项目并等到 ACTIVE_HEALTHY 后，从试运行重来；不要改 `MIGRATION_DATABASE_URL` |
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
docker compose --env-file .env.intranet logs -f app worker caddy
```

验证 HTTPS、`/api/system/integrations`、登录和合成数据冒烟（`smoke_deployment` 会创建后台评分任务并等 worker 跑完），确认 `ps` 里 `worker` 为 running，并确认容器内 `pg_dump --version` 为 16.x。

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

该段是 2026-09-21 的历史验收口径；新解析名称来源按 2026-09-24 条目核对。无冲突结构应显示该列映射为 `dimension`，父单元的 `claimed_by` 为各 code 的 `.dimension`。缺少稳定 `item_label` 或已有 dimension 列时，应保留原解析并出现 E9；不得手工去重、补序号或直接改数据库。E9 可进入既有结构建议流程，最终仍需用户确认。

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

### 2026-09-23 发布前评分细则完整性门禁

症状：全部规则已确认，但某评分项只有人工复核提示。查看 `/api/rubrics/{id}/review-workspace` 的 `structural_blockers`：`criterion_numeric_scoring_missing` 指出缺少可计分规则的评分项；`criterion_rules_missing`、`rule_text_missing`、`deduct_rule_invalid`、`band_levels_invalid` 分别指出规则、正文、扣分参数或等级信息缺口。发布页会显示对应编号和 AI 补全提示，直接发布请求返回 400，版本仍未发布，分享范围、版本哈希和审核记录不应部分提交。

处理：在第 3 步选择“去补全细则（可使用 AI）”；审核中先选择“退回草稿并补全细则”。在第 2 步选定模型连接生成缺失细则，或手工修订，应用后统一确认最终规则，再校验、提交审核和发布。扣分制填写触发条件、正扣分值与重复/封顶策略；等级制填写至少两个不同分值档位及各档判定说明。已有正式版本请复制为新版本修订；本次不会自动修改历史标准或重跑真实任务。

验证：项目解释器下运行 `.venv/bin/python -m pytest -q backend/app/tests/test_m4_publish_validation.py backend/app/tests/test_rubric_review_workspace.py backend/app/tests/test_m4_lifecycle_integration.py backend/app/tests/test_rubric_version_lifecycle.py backend/app/tests/test_three_doc_contract.py`。前端在 `frontend/workbench` 运行 `npm run typecheck`；根目录运行 `.venv/bin/python scripts/build_web_static.py --with-workbench`，然后在前端目录运行 `PGS_PYTHON=../../.venv/bin/python npm run test:e2e -- e2e/rubric-review.spec.js --project=chromium`。验收已确认纯复核规则仍被阻断、点击补全返回对应项、有效扣分/等级规则可发布、混合合法辅助复核规则不误伤。

发布遵循现有 CI 和上线清单，无迁移，head 仍为 `0031_rubric_import_sessions`；本地重启后端并刷新工作台以加载重建产物。回滚同步回退校验器、发布错误提示、工作台、测试和三文档并重建静态文件；回滚会重新开放旧纯复核发布缺口，应暂停此类标准发布，保留全部历史规则、版本和审计。

维护记录：2026-09-23 · 发布前评分细则完整性门禁：补充阻断码定位、AI/人工补全路径、事务回滚验收及无迁移发布回滚步骤。
### 2026-09-24 评价内容作为评分项标题

症状：导入“打分项 / 评价内容 / 具体要求”表后，评分项名称显示为“指导教师成绩项1～6”。诊断时检查解析结果的 `criteria[*].name`、`dimension`、`code`：新解析中名称和维度均应为评价内容，code 仍为 T01～T06。旧会话不会自动改名，应在草稿中重新上传同一来源文件并确认替换；发布版本需复制成新草稿后重新导入，不直接改历史记录。

验证：运行 `.venv/bin/python -m pytest -q backend/app/tests/test_rubric_table_extractor.py backend/app/tests/test_rubric_import.py`，以及在 `frontend/workbench` 运行 `npm run test:unit -- RubricImportWorkspace.test.js`。核对六个计分行仍合计 100 分、具体要求与来源不变。发布按现有 CI 和 `docs/上线清单.md`；回滚代码及静态产物后重新解析草稿，不执行数据库 downgrade。

维护记录：2026-09-24 · 评价内容作为评分项标题：补充旧会话诊断、重新解析、验证与回滚步骤。
### 2026-09-24 评分表模板下载入口恢复

症状：新版导入工作区的评分表区域缺少“下载模板”。诊断时检查 `GET /api/rubrics/import-template.xlsx` 是否返回 XLSX，并确认页面链接采用运行配置的 API 前缀。

验证：运行 `.venv/bin/python -m pytest -q backend/app/tests/test_rubric_import.py`；在 `frontend/workbench` 运行 `npm run test:unit -- RubricImportWorkspace.test.js` 和 `npm run typecheck`；再运行 `.venv/bin/python scripts/build_web_static.py --with-workbench` 并在浏览器点击下载，检查文件能在表格软件中打开。发布按现有 CI 与 `docs/上线清单.md`；回滚前端链接和静态产物即可，后端接口和数据库不需回滚。

维护记录：2026-09-24 · 评分表模板下载入口恢复：补充入口缺失的诊断、模板响应验证及前端回滚步骤。
### 2026-09-24 同名评价内容逐项编号

症状：导入后 T02～T05 均显示“分析与解决问题”。新解析应依次显示“分析与解决问题1～4”，唯一标题不加后缀。诊断时核对 `criteria[*].code/name/dimension/max_score` 及 Excel 行序；旧会话需重新解析来源文件，不原地改历史数据。

验证：运行 `.venv/bin/python -m pytest -q backend/app/tests/test_rubric_table_extractor.py backend/app/tests/test_rubric_import.py`；在 `frontend/workbench` 运行 `npm run test:unit -- RubricImportWorkspace.test.js` 和 `npm run typecheck`；重建静态产物后核对六行合计 100 分。发布按现有 CI 和 `docs/上线清单.md`，回滚代码及静态产物后重新解析草稿，不执行数据库 downgrade。

维护记录：2026-09-24 · 同名评价内容逐项编号：补充复现、重解析、验证与回滚步骤。

### 2026-09-28 BYOK 单选启用

症状与诊断：旧版账户页可出现多个 active；新版同一用户在当前组织只能有一个“已启用”。运行 `.venv/bin/alembic current` 确认 head 为 `0032_single_active_ai_connection`。账户页的最近验证仅代表连通测试，与启用状态分开；测试/换 Key 不会启用连接。

验证：
```bash
.venv/bin/python -m pytest -q backend/app/tests/test_ai_connections.py backend/app/tests/test_migrations.py
npm --prefix frontend/workbench run test:unit -- src/views/AccountView.test.js
```

页面验证：首个连接保存后应为已启用；第二个保存后未启用；点击第二个的“启用”，第一项变为未启用。新任务显示并绑定当前启用的连接，即使已有平台模型也优先用个人连接。AI 起草使用同一选择。旧任务不重绑；切换后旧连接上的后续评分会拒绝执行，已在途请求可能完成。要继续原任务，重新启用原连接且保持原配置/Key 版本；否则新建任务。

发布：先按现有上线清单备份并 verify，停止接收新任务、等待在途请求完成，再执行 `.venv/bin/alembic upgrade head`、构建前端并重启 API/Worker。迁移保留历史 active 中最近验证成功的一项，其余停用；迁移后请在页面核对选择。CI 逐版本迁移已纳入 0032；本地 SQLite 测试不能替代 Postgres 16 CI artifact。

回滚：维护窗口内先停 API/Worker，再执行 `.venv/bin/alembic downgrade 0031_rubric_import_sessions` 并回滚本次应用/前端版本。此步骤只删除唯一索引，保留启停状态，不会自动重新启用其他密钥；严禁为恢复多 active 而批量改状态。若要还原升级前数据，遵守既有备份 verify 和显式确认恢复流程。

维护记录：2026-09-28 · BYOK 单选启用：补充诊断、切换验证、发布/回滚和旧任务处理；不记录 Secret 或学生材料。

### 2026-09-28 结构识别 JSON 错误诊断与恢复

症状：点击确认 AI 识别结构后返回 503，页面称 AI 服务不可用；此前同路由 200 可能只是 dry_run 用量预估。静态文件 304 与 Chrome devtools 配置文件 404 不属于模型错误。

诊断：先看页面错误码。`STRUCTURE_OUTPUT_INVALID`/422 表示两次 JSON 或结构校验未通过；`STRUCTURE_OUTPUT_TRUNCATED`/422 表示模型明确报告长度截断。鉴权/限流/超时仍是 `AI_PROVIDER_ERROR`/503，页面给出分类提示。后端 `rubric_structure_output_failed` 日志只含 reason 与 attempt，`rubric_structure_provider_failed` 只含分类与状态。不要开启原始 LLM debug 日志或复制密钥、文档正文。

本次本机诊断：最小 Responses 与 Chat 请求均返回 200；真实结构请求一次发生 JSON 未结束、随后同配置成功。修复后的同份模板调用返回 completed，使用 1451 输出 Token（推理 1076、正文 375），高于原 1200 上限，成功生成结构建议；事务均回滚，未合入建议。不能凭配置显示 Responses 就认定百炼不支持，也不能将所有 JSON 错误归为网络或 IP 限制。

验证：
```bash
.venv/bin/python -m pytest -q backend/app/tests/test_rubric_llm_structure.py backend/app/tests/test_rubric_structure_api.py backend/app/tests/test_core_llm_adapters.py
```

发布与回滚：本次仅后端与 prompt/cache 版本变化，无新增迁移；按上线清单及 CI 发布，重启 API 后生效。结构识别默认输出预算 8192，若连接显式设置较小 `max_output_tokens`（Responses）或 `max_tokens`（Chat），需核对后调整，代码不会覆盖它。人工确认前 AI 建议不改评分项；回滚时回退本次后端代码及版本标识，保持 head `0032_single_active_ai_connection`，不操作业务数据。

维护记录：2026-09-28 · 结构识别 JSON 错误诊断与恢复：补充错误分类、诊断证据、验证及发布/回滚步骤。

### 2026-09-28 评分标准识别校对双标签（历史布局，已由 09-29 分步改造取代）

症状：第一步原文长列表难以逐条对照；AI 结构识别名称不同导致旧实现报告会移除全部评分项。诊断先读取同一标准的 `parse-coverage` 与 `source-workspace`，核对 extraction 工作表/行号与来源引用，不记录原文或 Key 到共享日志。

使用：表格标签核对列角色和从文字提取的分值，原表对照查看来源；原文标签按章节选择单元，在右侧归入评分项或标记不是规则。疑似规则和来源冲突未处理时不能进入第二步；一般提示不阻断。AI 结构先估算再确认，高置信归类建议也需显式采纳。失败时已成功的处理会重新读取，不能将剩余项显示为成功。刷新后人工处理仍保留，页面分值核对需要重新操作。

验证执行 `docs/评分标准识别校对接口映射.md` 中命令。端到端使用独立临时 SQLite/Mock 服务（8099/8100/8101），不得把测试指向实际业务数据库。额外人工检查桌面与窄屏标签、原文上下文、保存错误是否保留输入、只读版本是否禁止修改。

发布：重新构建 `public/`，重启 API 后刷新工作台；数据库 head 仍是 `0032_single_active_ai_connection`，本次无数据库迁移。生产发布仍执行上线清单及现有 CI。回滚本次 Vue、结构匹配及来源投影代码，重新构建静态文件并重启；不降级数据库、不重置人工台账、不删除已生成草稿。

维护记录：2026-09-28 · 评分标准识别校对双标签：补充症状、操作、隔离测试、发布与回滚。

维护记录：2026-09-29 · 表格视图分段切换样式：验证：运行 `npm --prefix frontend/workbench run test:unit -- src/components/TableRecognitionPanel.test.js`，再执行 `.venv/bin/python scripts/build_web_static.py --with-workbench`；刷新评分标准页，确认浅灰底槽内选中项为白色、两个视图可正常切换。发布使用重建后的静态产物；回滚组件样式并重新构建即可，无数据库操作。

维护记录：2026-09-29 · DOCX AI 归类诊断：若点击后无建议且无报错，先核对 `GET /api/rubrics/{id}/parse-coverage` 的 `unit_classifications.results/failed_unit_ids/unclassified_unit_ids`；HTTP 200 不等于归类成功。真实复现只记录 status、incomplete_details 与用量，不记录原文/Key，不自动采纳。运行 `.venv/bin/python -m pytest -q backend/app/tests/test_rubric_unit_classifier.py backend/app/tests/test_rubric_unit_classification_api.py backend/app/tests/test_rubric_parse_coverage_api.py` 及 `npm --prefix frontend/workbench run test:unit -- src/components/SourceReviewPanel.test.js src/stores/rubrics-parse.test.js`；本次分别 25/14 passed，但尚缺异常路径门禁。详见诊断报告。未发布业务代码，无需重启或数据库回滚；本次文档可单独回退。

### 2026-09-29 评分项与评分规则分步改造

症状与诊断：第一步被 Word 疑似规则阻塞、归入后起草不使用原文、归类 200 但没有建议。查询 `parse-coverage` 的 blocking_count、unit_classifications 和 `source-workspace` 的 locator.review；HTTP 200 仅代表请求处理完成，检查 failed_unit_ids 和 rejected.error。不得记录 Key、论文原文或学生信息到诊断日志。

操作验证：上传合成 Excel/Word，第一步确认分值后应能进入评分规则；左侧待归类中选一条并归入，切换对应评分项核对规则来源；移出后恢复待归类。AI 归类按钮显示本次数量，失败可重试；更改来源会清除未应用建议。确认规则不会自动发布。规则页底部下一步检查疑似规则与细则确认；顶部可查看发布校验诊断。

执行 `.venv/bin/python -m pytest -q`、`npm --prefix frontend/workbench run test:unit` 和 `npm --prefix frontend/workbench run typecheck`。后端全套含会改写 public 的静态构建测试，不得与静态构建或浏览器测试同时运行。然后执行 `.venv/bin/python scripts/build_web_static.py --with-workbench` 和 `npm --prefix frontend/workbench run test:e2e -- rubric-recognition.spec.js rubric-review.spec.js`。E2E 使用临时 SQLite/Mock 与合成文件，不连接业务数据库；真实模型可用性另作验证。

发布：构建静态产物、重启当前 API 服务并刷新页面；数据库 head 仍 0032，不需要数据迁移。生产按上线清单与 CI 流程发布。回滚本次 Vue/分类器/来源起草/restore 动作代码后重建并重启，不降级数据库、不清空人工台账。旧版本仍可读取既有 JSON，但将不支持新 restore 请求。

维护记录：2026-09-29 · 评分项与评分规则分步改造：更新分步操作、失败诊断、顺序回归、发布及回滚入口。

维护记录：2026-09-29 · 评分规则 AI 归类复测：诊断先读取 parse-coverage 的 prompt_version/results/failed_unit_ids/rejected.error；provider_error 无法定位具体上游原因。执行 `.venv/bin/python -m pytest -q backend/app/tests/test_rubric_unit_classifier.py backend/app/tests/test_rubric_unit_classification_api.py backend/app/tests/test_rubric_rule_sources.py`（19 passed），及 `npm --prefix frontend/workbench run test:unit -- src/components/SourceReviewPanel.test.js src/stores/rubrics-parse.test.js`（15 passed）。本次不发布、不改连接、不采纳规则；文档可独立回退。详见 `docs/AI归类复测-2026-09-29.md`。

### 2026-09-29 AI 归类小批次与超时修复

症状：44 条全部 provider_error、长时间等待无进度。诊断读取 parse-coverage 的 rejected.error；新版 request_timeout 表示模型响应超时，authentication_failed 表示鉴权失败，rate_limited 表示限流。不要把 HTTP 200 当作全部成功，也不要把历史 provider_error 自动解释为超时。新版每批最多 3 条，归类默认等待 120 秒（连接显式超时优先），完成后立即显示本轮进度及累计建议；遇到失败停止，可选剩余内容重试。未发送的单元不计为实际调用失败。选中少量单元测试采纳范围，筛选后不得采纳未选择的其它建议。

验证：执行 .venv/bin/python -m pytest -q、npm --prefix frontend/workbench run test:unit、npm --prefix frontend/workbench run typecheck。后端完成后再执行 .venv/bin/python scripts/build_web_static.py --with-workbench，然后 npm --prefix frontend/workbench run test:e2e -- rubric-recognition.spec.js rubric-review.spec.js。真实连接验证只记录状态、用量、错误枚举和数量，不写入原文/密钥；不自动采纳。Mock 通过不能替代真实供应商验证。

发布：重建静态文件后重启当前 API，刷新工作台加载新 JS；生产仍走现有 CI 和上线清单。本次无迁移，数据库 head 保持 0032。回滚分类器、适配器可选参数及前端批次协调代码，重建静态文件并重启即可；不清空建议或人工台账，不降级数据库。

维护记录：2026-09-29 · AI 归类小批次与超时修复：补齐具体错误诊断、逐批进度、范围验证及发布回滚操作。

本次验证记录：后端 2245 passed、前端单元 258 passed、类型检查通过；静态构建成功，隔离页面流程 8 passed。当前百炼连接真实小批次验证 6/6 有效、0 失败，总耗时 132.9 秒；未运行完整 44 条、未写业务库或采纳建议。旧失败记录在重新归类前保留。

### 2026-09-29 AI 归类有限并发

新版页面每批 3 条、最多 3 批同时请求；Network 可见最多 3 条在途 unit-classifications 请求（同一页面操作），失败后不再补发，但要等待已发请求结束。正常完成后刷新页面，累计建议不应丢失；修改原文/评分项导致输入变化时应出现 409 并要求重新归类。跨标签页重复调用尚无服务端幂等，不能用按钮禁用推断不会重复收费。

验证：运行 .venv/bin/python -m pytest -q backend/app/tests/test_rubric_unit_classification_api.py，以及 npm --prefix frontend/workbench run test:unit -- src/lib/classification-batches.test.js src/components/SourceReviewPanel.test.js。数据库并发测试用独立 SQLite 连接和双线程屏障，证明两个模型调用先同时到达再合并，两份建议均保留；不能替代 PostgreSQL CI 证据。真实供应商复测仅输出数量与耗时，不输出 Secret/原文，不自动采纳。

发布：后端测试完成后执行 .venv/bin/python scripts/build_web_static.py --with-workbench；重启非 reload API 并刷新页面。生产仍走现有 CI，数据库 head 0032，无新增迁移。回滚前端调度为串行并重建，后端安全合并可保留；不清空已有建议。

维护记录：2026-09-29 · AI 归类有限并发：新增并发观察、失败收敛、合并与输入变化验证，以及发布回滚步骤。

有限并发验证结果：后端 2247 passed、前端 260 passed、类型检查通过；静态构建成功，页面流程 8 passed。真实百炼 6 条两批并发全部成功，119.4 秒；与先前串行 132.9 秒相比该次约减少 10%，非稳定吞吐基准，未验证完整 44 条。

### 2026-09-29 识别校对原型细节对齐

排错：
- **现象**：第 2 步从某个评分项回到「待归类原文」后，列表显示「当前分类没有内容」，但 parse-coverage 里仍有待处理单元。
  - 报错指向：原文列表，看起来像数据丢失。
  - 真正原因：筛选状态现在由 `RubricsView` 持有，可能还停在上次选的「已处理」等筛选。正常情况下离开原文视图时会重置为「待处理」；如果重置失效，先查 `watch(sourceMode)`，不要查后端。
- **现象**：Playwright 全量有 11 项失败，都在找「导入评分模板」按钮、第 1 步可编辑的「标准名称」、「AI 只补缺失部分」文案，或导航里的「待确认」。
  - 报错指向：按钮或文字找不到，看起来像界面回归。
  - 真正原因：这些用例写于 09-29 分步改造之前，改造删除或移动了这些入口，用例没有更新，不是本轮改动引起的。修用例时对照当前流程（「新建评分标准」「模板库」、先点「编辑基本信息」），不要为了让旧用例通过去恢复旧界面。（2026-10-07 已按当前流程更新这些用例。）

验证：

```bash
npm --prefix frontend/workbench run test:unit
npm --prefix frontend/workbench run typecheck
.venv/bin/python scripts/build_web_static.py --with-workbench
npm --prefix frontend/workbench run test:e2e -- rubric-recognition.spec.js rubric-review.spec.js
```

浏览器用例跑的是 `public/` 里的构建产物，改完前端必须先重建静态产物，否则测到的还是旧页面。

发布：本次只改前端，没有迁移，数据库 head 保持 `0032_single_active_ai_connection`。回滚时还原四个组件和 `rubric-recognition.spec.js`，再重建静态产物。

维护记录：2026-09-29 · 识别校对原型细节对齐：记录了筛选状态和旧用例两个排错点，以及验证与回滚步骤。

本次验证记录：前端单元 264 passed，类型检查通过，静态产物已重建；`rubric-recognition.spec.js` 通过。Playwright 全量 139 passed、2 skipped、11 failed，11 项均为上述旧用例。

### 2026-09-30 规则来源默认折叠

排错：第 2 步某个评分项只看到 1 条归入原文，但标题写着「N 个单元」，这是默认折叠，不是数据丢失。点「展开其余 N−1 个单元」即可看到全部；AI 起草始终读取全部归入的单元。

验证：`rubric-recognition.spec.js` 新增折叠用例，用合成 Word（3 条扣分要求）通过 `resolve-batch` 归入同一评分项，再核对折叠、展开、收起。改完前端后先 `.venv/bin/python scripts/build_web_static.py --with-workbench`，再跑 `npm --prefix frontend/workbench run test:e2e -- rubric-recognition.spec.js rubric-review.spec.js`。

维护记录：2026-09-30 · 规则来源默认折叠：记录折叠与数据丢失的区别，以及验证步骤。

本次验证记录：前端单元 264 passed，类型检查通过，静态产物已重建；`rubric-recognition.spec.js` 2 passed，`rubric-review.spec.js` 7 passed。

### 2026-09-30 工作台基准字号与评分标准页间距

排错：
- **现象**：页面上某段文字明显比周围大、一屏放不下多少内容。
  - 报错指向：看起来是组件样式写错了字号。
  - 真正原因：通常是这个元素根本没写字号。过去这类文字会落到浏览器默认 16px；现在 `body` 有 13.5px 基准，如果又出现 16px，先查是不是有人在 `html` 或局部容器上改了基准字号。
  - 自查方法：在浏览器控制台遍历 `main` 里的文本节点，统计 `getComputedStyle(el).fontSize` 的分布，原型页面里正文 16px 的占比应接近 0。

截图注意：Playwright 整页截图里，贴底操作栏（`.rules-footer`）会画在截图中间、压住下面的卡片。这是整页截图对吸底元素的渲染方式造成的，不是布局错误；要核对间距，看视口截图或实际页面。

验证：`npm --prefix frontend/workbench run test:unit`、`typecheck`，然后 `.venv/bin/python scripts/build_web_static.py --with-workbench`，再跑 Playwright 全量。

维护记录：2026-09-30 · 工作台基准字号与评分标准页间距：记录字号回退的排错方法和整页截图的假象。

本次验证记录：前端单元 264 passed；Playwright 全量 140 passed、2 skipped、11 failed，11 项与 09-29 记录的旧用例相同，没有新增失败；窄屏用例通过。

### 2026-09-30 AI 起草超时与熔断修复

排错：
- **现象**：日志出现 `rubric_ai_draft_failed exception_type=CircuitOpenError` 或提示「系统已暂停调用它约 30 秒」，接口返回 503。
  - 报错指向：熔断器。
  - 真正原因：同一连接之前连续发生的暂时性失败（超时、限流、上游 5xx）。熔断器按连接和进程统计，默认累计 5 次打开，冷却 30 秒；服务重启或 `--reload` 重载后清零。往前找同一连接的 `reason=request_timeout`，不要去查熔断器本身。
- **现象**：`rubric_ai_draft_failed reason=request_timeout status=None`。
  - 排查：看同一请求前面的 `rubric_ai_draft_request batch=i/n ... timeout_seconds=`。修复后未设显式超时的连接应显示 120；如果显示 60，说明运行的还是旧代码，先重启服务。
  - 如果 120 秒仍然超时：在「账户与连接」给该连接设置 `timeout_seconds`（1–300），不要改全局超时。
  - 每批只尝试 1 次，所以一次超时只算一次熔断失败。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_template_ai_rule_refactor.py
.venv/bin/python -m pytest -q
```

用例覆盖：Responses 连接超时只发 1 次请求、等待 120 秒、只计 1 次熔断失败；连接显式超时优先；熔断的提示文案；并发峰值为 3 且按批次顺序合并；一批失败后不再发出新批次。真实供应商验证只记录耗时、批数和错误枚举，不记录原文和密钥。

发布：只改后端，没有迁移，head 保持 `0032_single_active_ai_connection`。回滚时还原 `ai_rule_drafter.py` 和对应测试，然后重启 API。

维护记录：2026-09-30 · AI 起草超时与熔断修复：记录熔断与超时的排错顺序、连接超时的调整入口和验证命令。

本次验证记录：后端全量 2252 passed，48 项既有弃用警告；起草专项 45 passed。没有调用真实百炼模型。

### 2026-09-30 AI 连接协议自动识别

新建连接：地址填平台给的前缀即可，也可以直接粘贴完整的 `…/chat/completions` 或 `…/responses` 地址，系统会自动去掉后缀。点「测试配置」后，「接口协议」一栏会显示识别出的协议和依据（地址后缀、已知平台或探测请求）。

排错：
- **现象**：保存时报「无法自动识别协议：测试请求失败…」。
  - 报错指向：协议识别。
  - 真正原因：几乎总是地址、模型名或 API Key 有误。只有未知平台保存时才会探测，而探测遇到 401/403/400/429/超时不会改试另一种协议。先用「测试配置」确认三项输入无误；确实是网关或特殊部署时，再在「高级设置」里手动指定协议。
- **现象**：报「该接口地址下没有找到 Chat Completions 或 Responses 接口」。
  - 说明两种协议的接口都返回了 404/405。通常是地址少了或多了一段 `/v1`，或者填成了控制台地址，不是 API 地址。
- **现象**：已有连接的协议不对，比如想把百炼从 Responses 改成 Chat。
  - 已保存连接的「测试」不会重新识别。需要新建一个连接（默认自动识别）并启用，再删除旧连接。
- **维护已知平台表**：`backend/app/services/ai_connection_protocol.py` 的 `KNOWN_HOSTS`。只收录 Chat Completions 支持明确的平台，按精确域名或上级域名匹配。改动后运行 `test_ai_connection_protocol.py`。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_ai_connection_protocol.py backend/app/tests/test_ai_connections.py
npm --prefix frontend/workbench run test:unit -- src/views/AccountView.test.js
npm --prefix frontend/workbench run api:dump
npm --prefix frontend/workbench run api:generate
```

改了接口字段之后，要重新生成 `schema.d.ts` 并提交，否则 CI 的 `api:check` 会失败。

发布：没有迁移，head 保持 `0032_single_active_ai_connection`。回滚时还原协议模块、接口、schema 和 AccountView，重新生成 API 类型并重建静态产物。已按自动识别保存的连接不受回滚影响，它们的 `provider_type` 仍是两种合法值之一。

维护记录：2026-09-30 · AI 连接协议自动识别：记录新建方式、报错与真正原因的对应关系、已知平台表维护和验证命令。

本次验证记录：后端全量 2277 passed；前端单元 265 passed，类型检查通过，API 类型重新生成后稳定。Playwright 全量 139 passed、2 skipped、12 failed，12 项均为旧用例：原有 11 项，加上 V3-4「平台已配模型时不强制选连接」，后者因新建任务页的「AI 连接」区块在 09-28 改为始终显示，之前通过只是渲染时序碰巧。

### 2026-09-30 AI 起草来源引用归一化

排错：
- **现象**：「规则组 X 引用了不存在的来源位置（…）」。
  - 报错指向：规则校验。
  - 真正原因：模型写的来源不在本批允许的列表里，而厂商没有强制执行 JSON Schema。括号里是模型实际写的值：
    - 形如 `/batch/...`、`/input_analysis/focus_units/<i>` 的位置指针：新版本会自动改写；如果仍然报这类值，说明运行的是旧代码，重启服务。
    - 形如 `docx:p[999]` 的编号：模型编造了本批不存在的原文，重新生成即可。反复出现时，检查该评分项归入的原文是否过多，或者换一个更守结构化输出的模型。
- **注意**：每批只能引用本批的原文单元，这是有意的限制。一个评分项归入多个原文时会拆成多批，每条规则的来源应当落在它所在批的单元上。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_ai_draft_source_refs.py backend/app/tests/test_template_ai_rule_refactor.py
```

用例覆盖：位置指针改写、缺前缀编号的唯一补全、不唯一时不猜、编造来源在一次修正后仍然拒绝，以及报错里回显错误的来源。

发布：只改后端，提示词和缓存版本已升级，无迁移。回滚时还原 `ai_rule_drafter.py`、`llm_cache.PROMPT_VERSION` 和对应测试。

维护记录：2026-09-30 · AI 起草来源引用归一化：记录来源报错的判断方法和验证命令。

本次验证记录：后端全量 2280 passed；没有调用真实百炼模型。

### 2026-10-01 AI 起草厂商错误的提示细化

起草失败时，看提示括号里的错误码和等待秒数（日志中对应 `rubric_ai_draft_failed reason=… status=… waited_seconds=…`）：

| 括号里的内容 | 含义 | 处理 |
|---|---|---|
| `rate_limited，HTTP 429`，等待时间很短 | 厂商限流。起草最多 3 批同时调用，每批只尝试一次 | 等一分钟再试；如果频繁出现，需要降低起草并发数，或为 429 增加一次等待重试 |
| `provider_unavailable，HTTP 5xx`，等待约 60 或 120 秒 | 厂商网关在模型生成时间过长时主动断开 | 不能靠调长本地超时解决；需要缩短单批生成量，或改用流式输出 |
| `network_error`，等待时间较长 | 连接在等待中被中间网关断开 | 同上 |
| `capacity_unavailable` | 厂商容量不足 | 稍后重试 |

验证：`.venv/bin/python -m pytest -q backend/app/tests/test_template_ai_rule_refactor.py`

维护记录：2026-10-01 · AI 起草厂商错误的提示细化：新增按错误码排错的对照表。

### 2026-10-04 批次评分全部失败：熔断、参数错误与输出截断

排错：
- **现象**：批次页的材料失败原因是 `PROVIDER_CIRCUIT_OPEN`。
  - 报错指向：连接熔断，看起来像网络问题。
  - 真正原因：熔断是结果，不是原因。先看同一次运行里规则任务的其它错误码：

    ```sql
    select provider_error->>'code' as code, count(*)
    from rule_scoring_tasks where scoring_run_id = '<run_id>' group by 1;
    ```

    这次的根因是 `PROVIDER_INVALID_REQUEST`：百炼 kimi-k3 拒绝 `top_p=1.0`。修复后，批次页会优先显示根因码。错误码不含厂商原文；要看原文，就用该连接发一个最小请求复现，只看状态码和错误体，不要写进日志或数据库。
- **现象**：`RULE_EXECUTION_FAILED (exception_type=ValueError)`，或日志里出现 `did not contain text output`。新版本会显示 `PROVIDER_OUTPUT_TRUNCATED`。
  - 真正原因：推理模型把 `max_output_tokens` 全部用在了推理上。可以关闭思考（Chat 协议，连接选项设 `thinking_type=disabled`），也可以调大 `max_output_tokens` 和 `timeout_seconds`。保留推理时，kimi-k3 单条规则约 30 秒。
- **现象**：改完连接配置后重试，材料立刻失败。旧版本显示 `scoring_failure (exception_type=ValueError)`，新版本显示 `AI_CONNECTION_CONFIG_CHANGED`。
  - 报错指向：评分执行。
  - 真正原因：批次创建时冻结了连接快照，连接配置变了，评分就拒绝执行；这是有意的保护。正规做法是新建评分任务并重新上传材料。只有确认是同一连接、同一密钥，且变更是有意为之时，才在库里更新该批次的 `ai_connection_snapshot`，并写一条审计。
- **现象**：代码修好了，重试后仍然同样失败。
  - 真正原因：`run_batch_worker` 不会热加载代码（`--reload` 只作用于 uvicorn），熔断状态也留在 worker 进程内存里。要重启 `scripts/start-web-pg.sh`。它同时监管 worker 和 API，单独杀掉 worker 会连带关闭 API。

- **现象**：评分进行到一半，材料失败原因变成 `PROVIDER_PERMISSION_DENIED`，后面的规则都是 `PROVIDER_CIRCUIT_OPEN`。
  - 报错指向：权限或密钥问题。
  - 真正原因：用最小请求查看厂商原文。如果是 `insufficient_quota` / “Free quota exhausted”，说明该模型的免费额度用完了，而且控制台开着“仅使用免费额度”。百炼的免费额度按模型计算，同一个密钥换成仍有额度的模型就能继续（`GET {base_url}/models` 可以列出可用模型，不计费）。注意，一篇论文 135 条规则约需 130 万输入 token（每条约 1.1 万），单个模型的免费额度通常跑不完一篇。要稳定跑完，需要在控制台关闭“仅使用免费额度”。
  - 换模型：用 PATCH 修改连接的 `model_name`。已经上传材料的批次需要按上一条处理快照。旧版本还需要重启 worker 才能清掉被 403 永久打开的熔断；新版本的熔断按模型隔离，不需要重启。
  - 注意：Core 评分运行的 `scoring_runs.*_tokens` 目前记为 0，不能据此估算用量。可以用一条规则的信封发 `max_tokens=1` 的请求，读取 `usage.prompt_tokens`。

调整连接选项：前端不展示 `provider_options`，需要调用 `PATCH /api/ai-connections/{id}`，请求体为 `{"provider_options": {...}}`。百炼 kimi-k3 建议使用 Chat 协议，选项设为 `{"thinking_type": "disabled", "top_p": 0.95, "response_format_json": true, "max_tokens": 2400}`。PATCH 不能修改协议：按 09-30 的约定，换协议要新建连接。这次在本地库直接改了 `provider_type` 和 `provider_options`；密钥密文的关联数据不含协议，改后仍能解密，改完已用 `verify_connection_runtime` 复测。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_p4_provider_circuit.py backend/app/tests/test_core_llm_adapters.py backend/app/tests/test_m8_batch_scoring_jobs.py backend/app/tests/test_m1_cache_identity.py
```

用例覆盖：
- 半开探测遇到 400 后关闭熔断，非瞬时错误清零计数；
- Responses 不发送默认的 `top_p`，信封仍记 `"1"`；
- 连接设置的 `top_p` 进入身份和 Chat 请求，范围限制为 (0, 1]；
- Responses 截断投影为 `PROVIDER_OUTPUT_TRUNCATED`；
- 批次失败原因取根因码。

发布：只改后端，没有迁移，head 保持 `0032_single_active_ai_connection`。回滚时还原 `openai_adapter.py`、`openai_compatible_adapter.py`、`core_adapter.py`、`base.py`、`factory.py`、`rate_limit.py`、`failures.py`、`jobs.py`、`ai_connections.py` 和对应测试，然后重启 `start-web-pg.sh`。已经设置 `top_p` 的连接，回滚后该选项会再次被静默忽略。

维护记录：2026-10-04 · 批次评分全部失败：记录熔断码掩盖根因的排查方法、推理截断的处理、worker 需要重启的原因，以及连接选项的调整方式。

### 2026-10-05 评分用量、预估上限与规则决策账本

新增配置（`.env.example`、`.env.intranet.example` 已列出）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `SCORING_DECISION_LEDGER_ENABLED` | `true` | 重试时复用已通过校验、且身份完全相同的规则判定 |
| `SCORING_DECISION_LEDGER_TTL_DAYS` | `30` | 账本记录的保留天数 |
| `SCORING_MAX_INPUT_TOKENS_PER_PAPER` | `0`（不限） | 单篇输入 token 上限：开始前按估算拦截，运行中按实际用量截止 |
| `SCORING_MAX_INPUT_TOKENS_PER_BATCH` | `0`（不限） | 单批输入 token 上限：开始前按估算拦截 |

看用量：

```sql
-- 每篇实际付费的用量（Core 路径从 0033 起才有值；旧运行仍是 0）
select paper_id, prompt_tokens, completion_tokens, created_at from scoring_runs order by created_at desc limit 20;
-- 某次运行复用了多少条
select decision_reused, count(*) from rule_scoring_tasks where scoring_run_id = '<run_id>' group by 1;
```

不调用模型的预估：`GET /api/batches/<batch_id>/score-estimate`（加 `?rescore=true` 表示不复用账本）。新建任务页的“任务摘要”也会显示预估。

排错：
- **现象**：重试后仍然按全量计费（运行页“复用 0 条”）。
  - 报错指向：无。
  - 真正原因：决策身份是整个请求的哈希，以下任一情况都不会命中：
    - 任务是显式重新评分（`rescore=true`）；
    - 换了模型、连接或采样参数，或者重建了连接；
    - 论文重新解析过；
    - 提示词或评分标准版本变了；
    - scorer 是 Mock；
    - `SCORING_DECISION_LEDGER_ENABLED=false`；
    - 记录已超过保留期。

    worker 日志里的 `rule_decision_ledger paper_id=… hits=… misses=… writes=…` 能看到命中情况；`rule_decision_ledger_read_failed` / `write_failed` 表示账本表不可用（例如生产没跑 0033、`pgs_app` 没有权限）。这时评分仍然正常，只是不复用。
- **现象**：开始评分返回 409“预计输入 token 超过上限”。
  - 真正原因：配置了上限，并且按估算超出。估算偏保守（UTF-8 字节数 / 3）。可以调高上限、分批评分，或者先让已成功的规则进入账本（重试只计失败规则）。
- **现象**：材料失败原因为 `TOKEN_BUDGET_EXCEEDED`。
  - 真正原因：单篇实际用量达到上限，其余规则没有发送。调高 `SCORING_MAX_INPUT_TOKENS_PER_PAPER` 后重试即可，已成功的规则会直接复用。
- **现象**：预估显示“N 份材料暂不支持估算”。
  - 真正原因：这些材料走 legacy 评分路径（未版本化评分标准，且 `SCORING_ENGINE_MODE=legacy`），预估只覆盖 Core 路径。
- **注意**：本地 `run_batch_worker` 不会热加载代码，改代码后要重启 `scripts/start-web-pg.sh`。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_scoring_usage_and_decision_ledger.py backend/app/tests/test_migrations.py backend/app/tests/test_m8_ops_readiness.py
npm --prefix frontend/workbench run test:unit -- src/lib/score-jobs.test.js src/views/NewTaskView.test.js
```

发布：
- 迁移到 `0033_rule_decision_ledger`，迁移内已给 `pgs_app` 授权并建 RLS；生产复核时 `verify_postgres_ops` 会检查新表的授权和策略。
- 新增了端点，所以 OpenAPI 和前端类型要重新生成，`public/` 要重新组装。
- 回滚：账本表是缓存，可以降级删除；但只要有任何任务的 `decision_reused=true`，0033 降级就会被拒绝，需要先归档这些任务。

维护记录：2026-10-05 · 评分用量、预估上限与规则决策账本：新增配置、用量查询、复用不命中的排查、上限相关报错与发布步骤。

### 2026-10-05 判断用视图与互斥组合并

新增配置：`SCORING_EVIDENCE_SCOPE=rule|criterion`（默认 `rule`）。`criterion` 让同一评分项的所有规则共用一次选证，形成更长的公共前缀，便于命中厂商前缀缓存，但证据的针对性变差。只有计量数据证明值得时才开启。

本地查看某条规则实际会发什么（不调用模型）：在 Python 里用 `core_view.build_core_request(envelope)`（单条）或 `build_core_group_request(envelopes, group_code=...)`（组），查看 `.user`（视图 JSON）、`.system` 和 `compression_summary(request)`。

排错：
- **现象**：规则全部判为 `not_applicable`，论文拿满分。
  - 真正原因：旧版本发给模型的规则里没有原文。新版本要求快照为 @3（看运行的 `execution_plan_snapshot` 中 `atomic_rule_snapshot.schema_version`）。还是 @2 说明评分标准的编译器版本不在 M4 列表里，或者跑的是旧代码（worker 需要重启）。
- **现象**：同一互斥组的三档仍然各调用一次。
  - 真正原因：组合并需要同时满足：同评分项、全部语义扣分规则、快照 @2 / @3、组内没有依赖、评分器支持组调用（Mock 不支持）、成员选出的证据一致。不满足时会回落为逐条判断，结果仍然正确。
- **现象**：同组三档都失败，错误码相同。
  - 真正原因：组调用只有一次，失败（厂商报错、超出预算、回包不合法）时三档一起失败；重试时只重跑这一组。
- **现象**：引用被判为不合法（`Core provider quote is not authorized`）。
  - 真正原因：发给模型的证据是原文片段加“…”，模型的引用跨越了“…”，或者不是逐字引用。校验始终针对原文，这是有意保留的防编造规则。
- **注意**：概况卡和压缩后的证据都不会包含学生姓名、学号或导师；封面个人信息片段在压缩阶段就会被丢弃（压缩摘要里的 `personal_information`）。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_token_compression_pipeline.py backend/app/tests/test_core_llm_adapters.py backend/app/tests/test_m4_rule_executor.py
```

发布：没有新增迁移；0033 增加了 `rule_scoring_tasks.group_call_id`（0033 尚未发布）。只有生产或本地已经跑过旧版 0033 时，才需要先降级到 0032 再升级。改动会让评分结果变化，发布前需要按 §15 用 QWK 留出集重新锚定（需要真实模型，先取得授权和 token 预算）。

维护记录：2026-10-05 · 判断用视图与互斥组合并：新增证据范围配置、视图查看方法、满分 / 未合并 / 引用不合法的排查。

### 2026-10-07 发布 0031–0033

- **先迁移、后合并**：`deploy-vercel-production` 在 main 门禁通过后自动部署，不检查数据库 head。合并早于迁移时，新代码会访问还不存在的列（如 `rule_scoring_tasks.decision_reused`）而报错。
- **判断新代码是否已上线**：未登录访问 `/api/batches/<任意 id>/score-estimate`。404 是旧代码（路由不存在）；401 是新代码（路由存在，被鉴权守卫拦下）。
- **0032 会停用多余的 AI 连接**：同一 owner + 组织只保留一个启用的连接（最近验证过的优先）。迁移后到设置页确认在用的连接仍是启用状态。
- **现象**：main CI 只有 frontend-workbench 的浏览器验收失败，`deploy-vercel-production` 显示跳过，线上新路由仍是 404。
  - 报错指向：找不到「导入评分模板」按钮、「标准名称」不可编辑等，看起来像界面回归。
  - 真正原因：用例落后于 09-29 分步改造（见 09-29 条目）；发布前本地只跑单元测试和类型检查发现不了。发布前必须本地跑一次 `npm --prefix frontend/workbench run test:e2e` 全量。
- **迁移报 `tenant/user postgres.<ref> not found`**：报错看起来像连接串或密码错了，真正原因是 Supabase 项目闲置被暂停（INACTIVE）。在 Supabase 控制台恢复项目，等到 ACTIVE_HEALTHY（约 3–5 分钟）再从试运行重来；不要去改 `MIGRATION_DATABASE_URL`。

维护记录：2026-10-07 · token 压缩发布与 QWK 豁免：新增先迁移后合并的原因、新代码上线的判断方法、0032 的影响、Supabase 暂停导致迁移报 tenant not found 的排查，以及浏览器验收拦下部署的排查。

### 2026-10-07 AI 归类跑到一半停下

排错：
- **现象**：“用 AI 给出归类建议（60 条）”只得到十几条，提示“本轮已停止”，下方显示“模型未返回有效 JSON”，失败条数是 3 的倍数。
  - 报错指向：模型输出不合格，看起来像整个连接不可用。
  - 真正原因：通常只有一批（3 条）的输出两次都解析失败，旧调度器遇到任何失败就停。新版只在系统级错误（限流、鉴权、超时、熔断、请求报错）或连续两批失败时停止，单批失败会继续跑其它批次。
  - 如果新版仍然频繁“连续两批没有拿到有效结果”：看连接的模型。`openrouter/free` 每次请求随机路由到不同的免费模型，JSON 遵从度不稳定，换成固定的、支持结构化输出的模型。
- **续跑**：停下后直接再点主按钮。未勾选时它会跳过已有有效建议的条目（按钮显示“继续为剩余 N 条…”），不会重复付费；失败的条目也可以用“重试失败或未返回内容”单独重试。要重新判断已有建议的条目，先勾选它们。

验证：

```bash
npm --prefix frontend/workbench run test:unit -- src/lib/classification-batches.test.js src/components/SourceReviewPanel.test.js
```

发布：纯前端改动，重建 `public/` 后走现有 CI；无后端、提示词或迁移变化。回滚：revert 本次提交并重建静态产物。

维护记录：2026-10-07 · AI 归类续跑：新增“跑到一半停下”的排查、续跑方法与验证命令。

### 2026-10-08 自部署后台评分 worker

排错：
- **现象**：Docker 部署后点“开始评分”，任务一直显示“排队中”，进度不动，接口和健康检查都正常。
  - 报错指向：看起来像任务创建失败或前端没刷新。
  - 真正原因：没有 worker 在领取任务。自部署没有队列服务，任务只写在数据库里。检查 `docker compose --env-file .env.intranet ps` 里 `worker` 是否为 running，再看 `logs worker`。旧版 compose 没有这个服务，拉取新代码后执行 `up -d --build`。
- **现象**：改了评分逻辑或模型配置，后台评分还是旧行为。
  - 真正原因：worker 不热加载代码，熔断状态也只在它的进程内存里。执行 `up -d --build`，或单独 `restart worker`。
- **现象**：任务详情显示“本部署尚未配置平台模型……”，同步评分返回 503。
  - 真正原因：受保护部署（`AUTH_ENABLED=true`）不会回落 Mock（D-028）。管理员需要在运维页配置平台默认模型，或者用户在“账户与连接”绑定自己的连接。这时去改 `LLM_PROVIDER` 不起作用。
- **现象**：CI 的 `docker-compose-smoke` 是绿的，冒烟证据 `deployment-smoke.json` 却是空的，或者日志里有 Traceback。
  - 报错指向：步骤显示通过。
  - 真正原因：`python … | tee` 在没有 pipefail 的 shell 里取的是 tee 的退出码。这个任务已设 `defaults.run.shell: bash`（`-eo pipefail`）。在其它任务里写 `| tee` 时，也要显式 `set -euo pipefail` 或设置 shell。
- **本地跑冒烟时注意**：compose 里的容器名是固定的（`paper-grading-db` 等），默认项目名下的 `db` 卷可能是你本地开发的数据。冒烟要用独立项目名（`-p`），并用覆盖文件改掉容器名；`down --volumes` 只能对冒烟项目执行。
- **冒烟模式**：没配模型的栈用 `smoke_deployment --scoring fail-closed`（CI 用法）。已配置平台模型或绑定连接的部署用默认的 `succeed`，它会真实调用模型。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_deployment_smoke_background_job.py backend/app/tests/test_m8_ops_readiness.py
```

完整验证以 CI 的 `docker-compose-smoke` 为准：`up --wait` 后 `worker` 必须是 running；`smoke_deployment --scoring fail-closed` 输出 `deployment-smoke@2`，`background_job.item_error_code=PLATFORM_MODEL_MISSING`，`synchronous_scoring=refused`。

维护记录：2026-10-08 · Compose 后台评分 worker：新增“任务一直排队”“未配置平台模型”“CI 冒烟假绿”的排查，以及 worker 重启说明、本地冒烟隔离注意事项和冒烟模式说明。

### 2026-10-08 连接并发上限、429 重试与调用日志

设置：账户与连接 → 对应连接点“并发上限” → 填写厂商允许的并发数（Z.ai 免费档的 GLM-4.7-Flash 填 1），留空表示不限制。修改立即生效，不影响已创建的评分任务。新建连接可在“高级设置”里填写。

查看生产调用（不含正文）：

```bash
vercel logs --environment production --since 30m --query "llm_call" --json
```

- `llm_call_failed … code=rate_limited status=429 provider_code=1302 … retry=yes`：被限流，正在按 `Retry-After` 重试。
- `llm_call … routed_model=… upstream=…`：成功，并显示实际路由到的模型和上游服务商。
- `llm_call_error_envelope … provider_code=… upstream=…`：200 但响应体是错误（聚合平台的上游失败）。
- `batch_scoring_item_deferred … delay_seconds=…`：批量评分时连接名额已满，这篇论文延后再领取，属于正常现象。

排错：
- **现象**：设置了并发上限后，起草仍然出现 `rate_limited`，但 `retry=yes` 之后成功了。
  - 真正原因：厂商除了并发，还有频率限制，或者模型本身繁忙；重试已经兜住。如果 `provider_code` 一直是“频率超额”类的代码，说明请求太密，可以降低使用强度。
- **现象**：重试用完仍然失败，`provider_code` 每次都一样。
  - 真正原因：额度耗尽或账户状态问题（看厂商错误码文档），等待重试无效，需要换 Key 或换模型。
- **现象**：批量评分进度很慢，日志里大量 `batch_scoring_item_deferred`。
  - 真正原因：连接的并发上限设得较小（例如 1），论文只能一篇一篇评，这是预期行为。需要更快就换支持更高并发的 Key。
- **注意**：归类的并发上限由页面执行；同一账号开多个标签页同时归类，仍可能超出上限。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_connection_concurrency_and_rate_limits.py
npm --prefix frontend/workbench run test:unit -- src/views/AccountView.test.js src/lib/classification-batches.test.js
```

发布：无迁移、无新环境变量，OpenAPI 不变；`public/` 已重建。回滚时 revert 本次提交并重建静态产物。回滚前先在账户页清空各连接的并发上限：旧代码的复现快照会包含 `max_concurrency`，新代码期间创建、绑定了带上限连接的评分任务会报“连接配置已变更”，只能重新创建任务。

维护记录：2026-10-08 · 连接并发上限与 429 重试：新增设置方法、生产日志查询与字段说明，以及限流、配额和批量变慢的排查。

### 2026-10-09 起草时间预算与额度耗尽

排错：
- **现象**：起草返回“该评分项需要分 N 批生成……来不及在单次请求的时间上限内完成”（`AI_DRAFT_TIME_BUDGET_EXCEEDED`，503）。
  - 报错指向：看起来像服务端超时。
  - 真正原因：连接的并发上限小（例如 1）、模型慢、规则多，几批串行加起来超过了预算（默认 260 秒，为 Vercel 的 300 秒上限留出余量）；或者一次请求里起草了多个评分项，预算按整次请求共用。每批开始前要求至少还剩 min(单次超时, 60 秒)，开始后的调用和重试都会被压到截止时间以内。
  - 处理：调高该连接的并发上限（厂商允许的前提下），换响应更快的模型，或精简该评分项的原文规则。自部署没有 300 秒上限，可以调大 `RUBRIC_AI_DRAFT_TIME_BUDGET_SECONDS`，设为 0 表示不限。
- **现象**：提示“AI 连接的额度已用完（余额不足或配额耗尽）”，日志里是 `llm_call_failed … code=quota_exhausted status=429 provider_code=1113 … retry=no`。
  - 真正原因：厂商账户欠费、余额不足，或额度用完，等待和重试都不会恢复。去厂商平台充值，或者换一个连接。
  - 注意：只有 OpenAI 的 `insufficient_quota` 和智谱/Z.ai 的 1113 会被识别为额度耗尽；其它厂商的同类 429 仍会显示为“限流”并按限流重试。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_connection_concurrency_and_rate_limits.py
```

维护记录：2026-10-09 · 起草时间预算与额度耗尽：新增两类报错的排查方法和预算配置说明。

### 2026-10-09 Claude 连接（Bedrock / Anthropic）

设置（账户与连接 → 新建连接；平台模型在运维页，填法相同）：
- **Bedrock**：接口地址填 `https://bedrock-runtime.<区域>.amazonaws.com/anthropic`（或 mantle：`https://bedrock-mantle.<区域>.api.aws/anthropic`），API Key 填 Bedrock API Key，模型名照 Bedrock 控制台填写（如 `anthropic.claude-opus-5-5`；旧模型可能要推理配置文件前缀 `us.` / `global.`）。粘贴 `…/anthropic/v1/messages` 也可以，系统会统一保存为 `…/anthropic/v1`。
- **Anthropic 官方**：接口地址填 `https://api.anthropic.com`，模型名如 `claude-opus-5-5`。
- 协议自动识别：地址以 `/messages` 结尾、路径含 `/anthropic`，或主机是 `api.anthropic.com`，保存时无需探测即可识别；否则在高级设置里手动选「Anthropic Messages（Claude）」。
- 高级设置里的两项（只对 Claude 显示）：
  - **思考强度**：默认「模型默认」（不发送；Opus 5.5 为 medium）。调高更慢、更贵，思考也占输出 token。
  - **结构化输出**：默认「自动」，只在 `api.anthropic.com` 开启，Bedrock 默认关闭。
- 新环境变量（都只是默认值，可以不设）：`ANTHROPIC_TIMEOUT_SECONDS=120`、`ANTHROPIC_MAX_TOKENS=4096`、`ANTHROPIC_MAX_RETRIES=2`。没有 Claude 的 Key、地址或模型环境变量，Claude 只能通过连接或平台模型使用。

查看调用（不含正文）：`llm_call_failed provider=anthropic … provider_code=… provider_type=…`。Claude 的错误信息主要在 `provider_type` 上（`rate_limit_error`、`overloaded_error` 等）；Bedrock 错误体不带类型时，取 `x-amzn-errortype` 头（如 `ThrottlingException`）。

排错：
- **现象**：mantle 地址测试连接失败，提示「请求被拒绝，请检查……结构化输出……」；或评分全部 `invalid_request`（400）。
  - 报错指向：请求参数不合法，看起来像模型名填错了。
  - 真正原因：结构化输出被设成了「开启」，而 Bedrock mantle（“Claude in Amazon Bedrock”，Opus 4.7 及以后的模型）不支持 `output_config.format`。
  - 处理：高级设置里把结构化输出改回「自动」或「关闭」。
- **现象**：换成新模型（Opus 4.7 及以后、Sonnet 5.x、Haiku 5.5）后测试连接 400。
  - 报错指向：同上。
  - 真正原因：连接里设置了 `temperature` 或 `top_p`（新模型不接受采样参数），或对 Opus 5.5 设置了 `thinking_type=disabled`（该模型不能关思考）。
  - 处理：去掉这些连接参数，改用「思考强度」控制成本。
- **现象**：测试连接通过，但评分或起草大量报「模型输出达到长度上限」（`output_truncated`）。
  - 报错指向：输出 token 上限不够。
  - 真正原因：思考 token 也计入 `max_tokens`；测试连接只发 32 个 token 的请求，只看 HTTP 状态，覆盖不到这种情况。
  - 处理：调低思考强度，或在连接参数里调高 `max_tokens`。
- **现象**：429 一直不恢复，没有 `retry-after`，日志 `code=quota_exhausted provider_code=enforced_spend_limit_reached`。
  - 真正原因：Anthropic 账户达到月度消费上限，要到下个月或升级档位后才恢复，重试无效。用户自设的消费上限是 400，也会被识别为额度耗尽。
- **现象**：Bedrock 上 429 `ThrottlingException`，等待之后仍然反复出现。
  - 真正原因：AWS 侧 RPM/TPM 配额（默认 2M 输入 TPM）或账户的每日配额。系统按限流处理并重试，每日配额用完时重试不会恢复，需要在 AWS 控制台申请提额。
- **现象**：批量评分报 `PROVIDER_OUTPUT_REFUSED`，或起草、归类提示模型拒绝回答（`refused`）。
  - 报错指向：看起来像模型或连接出了故障。
  - 真正原因：Claude 的安全策略拒绝回答（HTTP 200，`stop_reason=refusal`），重试通常得到同样结果。系统不会自动换模型（换了会让实际出分的模型与复现身份不一致）；被拒的规则作为阻断项转人工复核。

发布：
- 迁移 `0034_anthropic_messages_provider` 只改 `ck_ai_connections_provider_type`，不建新表，不涉及 `pgs_app` 授权与 RLS。生产按 D-025 的审批工作流先迁移再部署；`verify_postgres_ops` 会检查该约束是否包含 `anthropic_messages`。
- 回滚：存在 Claude 连接（含软删除）或平台模型为 Claude 时，0034 拒绝降级；先硬删这些连接，并把平台模型换成其它协议。
- OpenAPI 与 `schema.d.ts` 已更新，`public/` 已重建。

验证（全部用 Mock，不调用真实模型）：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_llm_adapter_contract.py backend/app/tests/test_anthropic_messages_adapter.py backend/app/tests/test_ai_connection_protocol.py backend/app/tests/test_migrations.py
```

维护记录：2026-10-09 · Claude 连接：新增 Bedrock/Anthropic 接入步骤、三个默认值环境变量、六类报错的排查，以及 0034 的发布与回滚说明。

### 2026-10-09 旧路径信封与连接参数

排错：
- **现象**：AI 连接里设置了 `max_tokens` / `max_output_tokens` / `temperature` / `response_format_json`，正式版本标准（Core）生效，未版本化标准（旧兼容路径）却不生效；或者在本地开 `LLM_DEBUG_LOG_ENABLED=true`（生产就绪检查会把它报为问题，不要在生产开），看到旧路径请求带着 `thinking: {"type": "disabled"}`，而连接根本没设 `thinking_type`。
  - 报错指向：连接配置没保存，或者厂商忽略了参数。
  - 真正原因：修复前，旧路径的信封参数来自 `base._provider_contract`，它读全局 `OPENAI_COMPATIBLE_*` / `OPENAI_*`，不读连接；`score_envelope` 又只按信封发请求。现在改为读实例，与 Core 一致。
- **现象**：升级后，旧路径评分变慢、token 用量上升，或出现 `output_truncated` / `empty_content`；同一连接在 Core 路径正常。
  - 报错指向：模型或厂商变慢、输出预算不够。
  - 真正原因：连接没设 `thinking_type`。旧路径以前被全局默认值“顺手”关掉了推理，现在按约定不再发 `thinking`，模型用自己的默认值（可能开启推理）。处理：`PATCH /api/ai-connections/{id}`，在 `provider_options` 里显式设 `"thinking_type": "disabled"`（批次快照的注意事项见 10-04 条目）。
- **判断会不会影响缓存**：旧路径的 L0 只对环境变量配置的模型生效；AI 连接（含生产的平台模型）在旧路径不读写 L0。所以这次改动没有让缓存失效，也没有 bump `PROMPT_VERSION`。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_legacy_envelope_provider_controls.py backend/app/tests/test_m1_cache_identity.py backend/app/tests/test_core_llm_adapters.py
```

发布：只改后端，没有迁移。回滚时还原 `backend/app/services/llm/base.py` 并删掉对应测试；回滚后连接参数会在旧路径再次被忽略。

维护记录：2026-10-09 · 旧路径信封冻结实例参数：新增“连接参数在旧路径不生效”和“升级后旧路径开始推理”的排查方法，以及缓存影响的判断方法。

### 2026-10-09 接入 AWS Bedrock 与调高吞吐

**接入 Bedrock（OpenAI 兼容模型，例如 gpt-oss）**：账户与连接 → 新建连接：

| 字段 | 填写 |
|---|---|
| HTTPS 接口地址 | `https://bedrock-runtime.<区域>.amazonaws.com/openai/v1`（例如 `us-east-1`） |
| 模型 | Bedrock 模型 ID，例如 `openai.gpt-oss-120b-1:0`；跨区域推理配置带前缀，例如 `us.…` |
| API Key | Bedrock API Key（在 Bedrock 控制台生成，Bearer 方式） |
| 接口协议 | 自动识别即可（探测会选到 Chat Completions） |
| 同时请求数 | 按该模型在该区域的 RPM / TPM 配额填写（最多 8）；不确定时先填 4 |

注意：
- `bedrock-runtime` 不提供 `GET /models`；“测试配置”发的是一次最小的 `/chat/completions` 请求，不受影响。
- **Claude 系列在 Bedrock 上走 Anthropic Messages 协议**（`…/anthropic/v1/messages`），不是 OpenAI 兼容协议，接口地址与高级设置的填法见「2026-10-09 Claude 连接（Bedrock / Anthropic）」一节。“同时请求数”对 Claude 连接同样适用。
- gpt-oss 是推理模型，思考内容会占用输出预算；如果评分时出现“输出达到长度上限”，调大连接的 `max_tokens`。
- 配额按账户、区域、模型分别计算；频繁出现 `llm_call_failed … code=rate_limited` 时，调低“同时请求数”，或在 AWS Service Quotas 申请提额。

**调高吞吐**：
- 连接的“同时请求数”声明了就以它为准，留空用默认值（起草 3 批、归类 3 路、本地批量评分 2 篇）。
- Vercel 上全系统同时评分的论文数由 `BATCH_SCORING_QUEUE_CONCURRENCY` 决定（默认 8，1–32）。它在**构建期**读取：在 Vercel 项目环境变量里修改后，要重新部署才生效。
- 平台默认模型也可以在配置里声明 `max_concurrency`；没绑私有连接的批次共用这个名额池。

排错：
- **现象**：调高了连接的同时请求数，批量评分仍然只有两三篇在跑。
  - 真正原因：Vercel 队列的全局并发还是旧值（构建期读取），或者同时还有别的用户的批次在占用全局并发。检查 `BATCH_SCORING_QUEUE_CONCURRENCY` 是否已设置并重新部署。

验证：

```bash
.venv/bin/python -m pytest -q backend/app/tests/test_connection_concurrency_and_rate_limits.py
```

维护记录：2026-10-09 · 提高吞吐：新增 Bedrock 接入步骤、同时请求数与队列并发说明，以及“调高后仍不变快”的排查。

### 2026-10-10 统一执行模型与 AI 任务（待实施验证）

方案见 [AI 操作异步任务化与统一执行模型改造方案](docs/AI操作异步任务化改造方案.md)。本次仅文档，没有代码、迁移或部署。

当前行为（实施前排查用）：
- **现象**：批量评分任务一直显示“评分中”，进度不动，没有失败项。
  - 报错指向：看起来像模型慢或 worker 挂了。
  - 真正原因（Vercel）：某篇连续失败 12 次后，Vercel 队列丢弃了它的消息，条目停在 `running` 或 `pending`，数据库不知道消息已经没了（P0 第 4 项）。Vercel Logs 里按该条目 ID 查 `batch_scoring_item_*`，最后一条之后再无记录即属此类。
  - 处理：目前**页面上无法自救**。“重试”只接受失败或已取消的任务（`retry_batch_scoring_job`）；“取消”只会把运行中的任务改为“取消中”，等后续消息来收尾，而消息已经没有了，所以会一直停在“取消中”。A1 上线前只能由维护者在数据库中处理；A1 的巡检会把这类条目重新叫醒，或在连续无进展达到上限后标为失败。
  - 真正原因（内网/本地）：worker 按整个任务领取，前面有大任务时，后面的任务一直是 `queued`；或者 worker 进程没有启动（`docker compose ps` 看 `worker` 服务）。
- **现象**：内网部署同一个连接同时跑两个批次，厂商频繁 429。
  - 真正原因：worker 路径只在建任务时按单个任务限制线程数，任务之间不检查连接名额；Vercel 路径才有跨任务检查。临时处理：调低该连接的“同时请求数”。

实施后的验证步骤（A1）：同一套测试同时覆盖 Vercel 叫醒与 worker 两种叫醒；多 worker 并发领取不超连接名额；一个大任务不挡住其它用户；强制终止后续评且不重复有效结果；连续无进展 3 次后标为失败且可重试；叫醒消息丢失后由巡检恢复；compose 冒烟通过；合并后在 Vercel 预览或生产环境验收叫醒与巡检。

维护记录：2026-10-10 · 统一执行模型与 AI 任务方案：记录“任务一直评分中”与“内网多批次 429”的现有排查方法，以及 A1 的验证步骤；本次仅文档。

### 2026-10-10 统一执行模型 A1（批量评分已实施）

本节取代 2026-09-15「持久化后台评分」与 2026-10-08「自部署后台评分 worker」里关于投递、租约和执行次数的说明，以及上一节“当前行为（实施前排查用）”。

运行与配置：
- 内网/本地：`python -m backend.app.scripts.run_batch_worker [--threads N] [--sweep-seconds 120] [--poll-seconds 3]`。`--threads` 默认读 `BATCH_SCORING_QUEUE_CONCURRENCY`（未设为 8），即这台 worker 同时执行的条目数；`--scale worker=N` 多开时，每个连接的同时请求数仍在领取时统一检查，不会超。compose 与 `start-web-pg.sh` 的命令不变。
- Vercel：`pyproject.toml` 有两个订阅——`handle_work_message`（主题 `pgs-work`）与旧主题的 `score_batch_item`。构建日志里两个 subscriber 都要出现。消息只带来源键或巡检槽号；巡检链在首次建任务或进度读取时自动补投，不需要手工发消息。
- 在真实 PostgreSQL 上跑并发领取用例：`PGS_TEST_POSTGRES_URL=postgresql+psycopg://…@127.0.0.1:5432/<可清空的空库> .venv/bin/python -m pytest -q backend/app/tests/test_unified_work_queue.py`（只接受本机地址；用例会 `drop_all` / `create_all`）。不设置时该参数化用例跳过，SQLite 版照常运行。

日志关键字（只含 ID，不含原文或密钥）：`work_rung source=… reason=create|relay|sweep`、`work_item_claimed`、`work_source_full`、`work_item_finished` / `work_item_failed code=…`、`work_item_stalled stall_count=…`、`work_item_result_discarded`、`work_item_lease_lost`、`work_sweep_ran recovered=… failed=… converged=… rung=…`、`work_sweep_scheduled` / `work_sweep_revived`、`work_wake_failed`、`batch_scoring_job_finished`。

排错：
- **现象**：进度页显示“正在排队”（`heartbeat_state = waiting`），长时间不动。
  - 报错指向：看起来像模型慢或后台没在跑。
  - 真正原因：①该连接的同时请求数被别的任务占满（按任务内序号轮转，别的任务先评它们的第 1 篇）——日志里是 `work_source_full`；②Vercel 上叫醒全丢且巡检链断了——运维页“最近巡检”标红、日志没有 `work_sweep_ran`；③内网 worker 没启动——`docker compose ps` 看 `worker`。
  - 处理：①调高连接的同时请求数或等待；②打开任一评分进度页即可补投巡检链（`work_sweep_revived`），检查 Vercel 是否识别了 `handle_work_message` 订阅；③启动 worker。
- **现象**：某篇失败，错误码 `WORK_ITEM_STALLED`（“多次执行都没能推进”）。
  - 报错指向：像是这篇论文本身有问题。
  - 真正原因：连续 3 次执行都在写入任何新规则检查点之前死掉——函数超过 300 秒被平台终止、worker 进程被 OOM 杀掉、或数据库连接断开。日志按条目 ID 查 `work_item_stalled`，再看同一时段 Vercel 的函数超时或 worker 的退出记录。
  - 处理：先排除平台原因再点“重试失败项”（会把连续无进展次数清零）；一篇反复卡死时检查单篇规则数与模型超时设置。
- **现象**：内网升级后，同一连接的 429 明显变多。
  - 报错指向：厂商限流。
  - 真正原因：没声明同时请求数的连接以前在 worker 上按“每个任务 2 篇”执行，统一后只受全局并发（默认 8）约束。
  - 处理：在账户页给该连接声明同时请求数（免费档通常为 1）。
- **现象**：重试后某篇的尝试记录里多了 `abandoned`。
  - 真正原因：那次执行的心跳超过 120 秒没更新（进程被杀或超时），巡检把它重置后续评；已经判完的规则会复用，不重复计费。属于正常恢复记录。

验证（A1，本地已完成）：后端全量；`test_unified_work_queue.py`（Vercel 与 worker 两种叫醒同一套断言、多线程领取不超名额并在 PostgreSQL 16 上复跑、大任务不挡其它任务、强制终止后续评且不重复结果、连续无进展 3 次后失败且可重试、叫醒丢失后由巡检恢复、迟到结果被围栏丢弃）；0035 在 PostgreSQL 16 上的回填、部分索引、降级拒绝与 `verify_postgres_ops --exercise-ci-fixture`（含 `pgs_app` 授权与 RLS）；本机无 Docker，compose 冒烟以同等进程（uvicorn + `run_batch_worker` + PostgreSQL，`smoke_deployment --scoring fail-closed`）代替，CI 的 docker-compose-smoke 仍是门禁。**待做**：合并后在 Vercel 预览或生产环境验收叫醒、接力与巡检链（构建识别两个 subscriber；建任务后日志出现 `work_rung reason=create`；强制终止一篇后 2 分钟内出现 `work_item_stalled` 并续评）。

回滚：先确认没有排队、评分中或取消中的批量评分任务（0035 降级会拒绝），再 `alembic downgrade 0034_anthropic_messages_provider` 并部署上一版本。

维护记录：2026-10-10 · 统一执行模型 A1：新增 worker 参数、Vercel 订阅说明、PostgreSQL 并发用例的运行方法、日志关键字，以及“排队不动”“WORK_ITEM_STALLED”“内网 429 变多”的排查；迁移 head → 0035。

### 2026-10-10 AI 任务 A2（起草扣分细则已改为后台任务）

运行与配置：
- 起草需要执行器：Vercel 上由 `pgs-work` 叫醒；内网 compose 用 `worker` 服务；本地 `start-web.sh` 与 `start-web-pg.sh` 都会同时启动 `run_batch_worker`。只起 uvicorn（例如 README 的 SQLite 手工命令）时，另开终端运行 `python -m backend.app.scripts.run_batch_worker`。
- 接口：`POST /api/rubrics/{id}/ai-tasks`（202 新建 / 200 复用）、`GET /api/ai-tasks/{id}`、`GET /api/rubrics/{id}/ai-tasks?kind=rule_draft&active=1`、`POST /api/ai-tasks/{id}/cancel|retry`。`POST /api/rubrics/{id}/draft-deduction-rules` 返回 410 `ENDPOINT_RETIRED`。
- 每个响应带 `X-PGS-Contract`。停用或改变前端在用的接口时，同时修改 `backend/app/core/contract.py` 与 `frontend/workbench/src/api/contract.js`（`test_ai_tasks.py` 校验一致），并重新构建 `public/`。
- 日志关键字：`ai_task_created`、`work_item_claimed kind=ai_task`、`work_item_deferred … code=… delay=…`、`work_item_requeued`、`work_item_failed kind=ai_task code=…`、`ai_task_finished … status=… code=…`、`ai_tasks_cleared`。

排错：
- **现象**：点“AI 根据规则来源起草”后一直显示“AI 起草排队中”。
  - 报错指向：像是模型慢。
  - 真正原因：没有执行器在领取——本地只起了 uvicorn、内网 `worker` 没启动，或 Vercel 上叫醒与巡检链都断了（运维页“最近巡检”标红）。也可能是该连接的同时请求数被批量评分占满（AI 条目优先，但不抢已在跑的名额）。
  - 处理：启动 worker；Vercel 上打开任一评分进度页或起草页会补投巡检链。
- **现象**：进度停在“模型限流，稍后自动继续”。
  - 真正原因：厂商返回 429，条目按 Retry-After（默认 30 秒、5–300 秒之间）延后，最多延后 5 次后才判失败。不是卡住。长期如此请在账户页调低该连接的同时请求数。
- **现象**：起草失败，提示“额度已用完”或“拒绝了请求”。
  - 真正原因：额度耗尽、鉴权失败、请求被拒（400/401/403/404）立即判失败且不重试，同任务剩余批次被取消；“重试失败的批次”在问题解决前会再次失败。
- **现象**：起草失败，错误码 `AI_CONNECTION_KEY_CHANGED` / `AI_CONNECTION_CONFIG_CHANGED`。
  - 真正原因：任务创建后改了连接的密钥或配置；任务按创建时锁定的连接执行，不会换成新配置。处理：重新点起草（新任务锁定新配置）。
- **现象**：用户反馈“点按钮页面就刷新了一下，操作没生效”。
  - 报错指向：前端 bug。
  - 真正原因：版本守卫——页面是旧版本，后端契约已更新，页面在写操作前刷新到新版本，需要再点一次。若刷新后仍然如此，控制台有“接口版本……不一致”的警告：前端产物没有随后端一起部署（`public/` 未重建或 Vercel 静态资源是旧的）。

验证（A2，本地已完成）：后端全量（含 `test_ai_tasks.py`：提交即返回、去重、重新生成、429 延后、输出修正、永久失败只重试失败批次、超时重试一次、取消、连接变更、连续无进展、AI 条目优先且共用来源名额、旧接口 410、契约版本一致、发布清理、组织隔离、采用 AI 规则写入来源与模型名）；0036 迁移回填与降级守卫；前端单元、类型检查、OpenAPI 合同；Playwright `rubric-review.spec.js` 覆盖“提交 → 轮询 → 失败重试 → 应用 → 规则显示 AI · 模型名”。**待做**：Vercel 预览或生产上用并发 1 的连接起草 6 批，确认不超时、刷新后找回进度。

回滚：先确认没有 AI 任务需要保留、没有规则记录生成模型（0036 降级会拒绝），再 `alembic downgrade 0035_unified_work_queue` 并部署上一版本；已打开的新页面会被旧版本的契约头触发一次刷新。

维护记录：2026-10-10 · AI 任务 A2：新增起草任务接口、执行器要求、契约版本守卫的维护方法，以及“一直排队”“限流延后”“失败不重试”“连接变更”“点按钮就刷新”的排查；迁移 head → 0036。
