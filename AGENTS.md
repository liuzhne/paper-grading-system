# AGENTS.md

模板驱动的通用评分内核，兼容毕业论文评分并支持非论文 Profile。后端 FastAPI + SQLAlchemy + Alembic；默认 Mock LLM，配 env 切真实模型。当前生产 Profile 为 `thesis / thesis-legacy-profile@1` 与 `technical_proposal / technical-proposal-profile@1`。

## 命令
- 测试：`.venv/bin/python -m pytest -q`（或 `uv run pytest`）。**全套自包含**：内存 SQLite + Mock LLM，无需 Postgres/API Key/联网。
- 本地起服务（无 Docker）：`DATABASE_URL=sqlite+pysqlite:////tmp/dev.db uv run alembic upgrade head && ... uv run uvicorn backend.app.main:app --port 8000`（详见 README）。
- 迁移自检：`backend/app/tests/test_migrations.py`（pytest 用 `create_all` 建表，**不走 Alembic**，故迁移单独验证）。
- Runtime：Python 3.10+；仓库锁文件与当前门禁使用 Python 3.12 验证。
- CI/生产发布：`.github/workflows/ci.yml` 包含锁文件全量测试、Postgres 16 的逐版本迁移/约束/排序、拒绝 lossy downgrade、备份恢复演练和 Docker 冒烟；推送 `main` 且全部门禁通过后，才由 `deploy-vercel-production` 使用 GitHub `production` Environment 部署 Vercel。Vercel Git 直部署已关闭；当前 CLI 因上游 prebuilt 回归固定为 `58.4.0`。本机 SQLite 通过不能替代真实 CI artifact。

## 当前发布边界
- Alembic head：`0035_assistant_conversations`（0022 将旧单租户资源安全回填至默认组织；0023 持久化规则检查点与人工复核任务；0024 批次七态约束与 `state_version`；0025 结构化复核原因；0026 命令幂等回执；0027 ExportEvent；0028 旧导出日志幂等补录；0031 新增数据库临时评分标准导入会话；0033 规则级决策账本与 `rule_scoring_tasks.decision_reused` / `group_call_id`；0034 连接协议加入 `anthropic_messages`（Claude），有该协议的连接或平台模型时拒绝降级；0035 评分助手的偏好、会话、消息三表（`pgs_app` 授权与 RLS），有数据时拒绝降级。**0024–0028、0031 含数据时一律拒绝降级**——降级会删掉不可重建的审计或导入草稿；0033 的账本是可重建的缓存，但有复用标记的任务时同样拒绝降级）。
- v1 `score_paper()` 与 v2 `score_generic_submission()` 并存；正式 RubricVersion 走 AtomicRule Core，未版本化标准只能走显式 compatibility。
- `SCORING_ENGINE_MODE` 当前默认 `legacy`；它只控制未版本化兼容路径。真实 `GATE-03` 达到 `gating_eligible=true` 且取得维护者发布批准前，禁止改为默认 Core。

## 架构（backend/app/）
- `api/routes/`：rubrics / batches / papers / submissions_v2 / scoring / exports / release_gates / system / calibration / assistant。
- `api/guards.py` 共享可见性守卫、`api/actions.py` 共享写动作（建批次、预检、删论文、开评、重试、取消）：页面接口与评分助手的图调用同一组函数，路由里的 `_visible_*` 是它们的别名。
- `services/`：
  - `rubric_import/`：Excel 规则解析(`parser`) + Word 批注解析(`docx_comments`) + 结构化扣分规则编译(`compiler`)。
  - `document_parser/`：docx/PDF 解析(`parser`)、分块(`chunking`)、格式解析(`format_resolver`)、格式比对(`format_check`)。
  - `papers/ingestion`：解析+落库共享服务（路由与离线脚本复用）。
  - `assistant/`：评分助手（见 `docs/对话评分助手改造方案.md`）。LangGraph 流程图 `flow` + 查询图 `queries`，运行器 `runner`，自写状态存储 `checkpointer`（写入先缓冲、主线程落库），工具登记 `tools`，意图识别 `intents`（规则优先）。评分权为零。
  - `scoring/engine`：评分编排（**核心**）；`rules` 总分/等级；`validator` 结构化输出校验+注入检测。
  - `batch_scoring/jobs`：数据库持久化批任务、论文级检查点、租约恢复/取消/定向重试、观察策略与 Core 切换信号；只做观察，不授予 GATE-03 发布权限。
  - `deployment/`：Core inventory、OPS readiness、Postgres verifier、带校验 manifest 的数据库+storage 备份恢复；OPS 报告同样不授予默认 Core。
  - `checkers/`：确定性检查器(`deterministic`)、findings→扣分(`findings_checker`)。
  - `coherence/`：篇章一致性（确定性 `checker` + 语义 `semantic`）。
  - `calibration/`：L2 锚点库(`library`) + 漂移/排名(`analytics`)。
  - `llm/`：base + mock + openai（Responses）+ openai_compatible（Chat，默认 zhipu）+ anthropic_messages（Claude：Bedrock / api.anthropic.com）；三者共用 `transport`（重试/截止时间/熔断/计量）与 `legacy_prompts`；`cache/llm_cache` 是 L0 缓存。
  - `report/generator`：HTML 报告（含扣分明细/篇章一致性/格式问题）；`spreadsheet/` 导出。
- `eval/`：QWK 评估（`metrics`/`runner`/`labeled_dataset`/`scores_template`）；`scripts/run_qwk_eval.py` 入口。
- `db/models`、`schemas/`、`scripts/`（seed_dev、diagnose_llm、run_qwk_eval）。

## 评分流程要点（engine.score_paper）
按 `criterion.criterion_type` + `scoring_mode` 路由：
- **deterministic + dimension + 结构化扣分规则** → `findings_checker`（把篇章/格式发现按维度领取、按规则转扣分，**opt-in**）。
- 其它 deterministic → `deterministic` 检查器（结构/字数/图表/引文，不调 LLM）。
- hybrid（有 sub_checks）→ 拆子检查求和。
- 否则 LLM 路径：`scoring_mode` = deductive(代码算 awarded=max−Σpoints) / banded(吸附或采用模型选档) / llm_direct(证据门槛，取代旧 0.8 封顶)。
- L2 锚点(脱敏范文)按 code 注入判分 prompt；L0 缓存按 hash(模型+prompt版本+输入+rubric版本+采样+锚点)复用。
- 篇章一致性/格式 findings 存 `ScoringRun.coherence_findings/format_findings`，进报告；被启用项消费的标 `deducted_by`。

## 设计原则（详见 论文打分系统设计方案.md）
确定性优先(代码做判定题、LLM 做判断题)、原子评分项、每个扣分/选档强制带证据(抗幻觉)、结构化优先、无状态可缓存可复现、人在回路。**扣哪项/扣几分来自用户授权的模板/Excel 编译，不写死。**

## 约定
- 新增端点/字段要配 Alembic 迁移（当前 head 为 `0035_assistant_conversations`）+ 对应测试（`backend/app/tests/test_*.py`，复用 `conftest` 的 `client` 与 `make_*` 造数据）。
- 改 prompt/输入构造要 bump `cache/llm_cache.PROMPT_VERSION`。
- 改评分逻辑后用 §15 QWK 留出集重新锚定基线。
- 生产变更需完成 `docs/上线清单.md`；备份恢复必须先 verify，restore 只允许显式确认的数据库与空 storage 目标。
- 进度与待办见 `代码改造计划.md §7`。

## 修复方案与三文档同步（强制）
- 每次提出或落地 bug、故障、回归、安全问题、数据问题的修复方案，都必须在同一变更中同步检查并更新根目录的 `ARCHITECTURE.md`、`DECISIONS.md`、`RUNBOOK.md`；三者缺一时，修复不算完成。
- `ARCHITECTURE.md` 写修复后的事实：受影响模块边界、核心调用链、数据流、不变量或外部依赖。即使边界未改变，也要在维护记录中说明本次核对范围和“不变”的结论。
- `DECISIONS.md` 写决策：问题背景、所选方案、为什么这样选、放弃的方案、代价与后续约束；不得把临时猜测写成已接受决策。
- `RUNBOOK.md` 写可执行操作：复现/症状、诊断、修复后的验证、发布与回滚步骤；命令必须与当前仓库入口一致且不得包含真实 Secret、论文原文或学生 PII。
- 文档描述必须以当前代码、迁移 head、Accepted ADR 和 CI 配置为依据。若三文档与实现冲突，先修正文档再交付，并在三份文档各自的“维护记录”追加同一日期/主题的条目。
