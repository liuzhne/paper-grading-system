# 开发、排错与发布 Runbook

> 当前操作基线：2026-09-09；Python 3.10+，推荐/CI 为 3.12；Alembic head `0030_platform_llm_config`；默认 `SCORING_ENGINE_MODE=legacy`。以下命令默认在仓库根目录执行，不要把真实 Secret、论文原文或学生 PII 写入终端记录、Git、CI artifact 或工单。

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

## 12. 维护记录

| 日期 | 主题 | 操作基线变化 |
|---|---|---|
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
