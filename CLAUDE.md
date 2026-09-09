# CLAUDE.md

> **⚠️ 最重要（MUST，优先级最高）：所有回答必须使用简体中文。** 无论提问用何种语言、涉及代码注释或技术术语，对话回复一律以简体中文输出。

模板驱动的通用评分内核，兼容毕业论文评分并支持非论文 Profile。后端 FastAPI + SQLAlchemy + Alembic；默认 Mock LLM，配 env 切真实模型。当前生产 Profile 为 `thesis / thesis-legacy-profile@1` 与 `technical_proposal / technical-proposal-profile@1`。

生产部署、Supabase、鉴权、发布核验与安全待办见 [PROJECT_MEMORY.md](PROJECT_MEMORY.md)；该文件不得记录任何密钥值。

## 命令
- 测试：`.venv/bin/python -m pytest -q`（或 `uv run pytest`）。**全套自包含**：内存 SQLite + Mock LLM，无需 Postgres/API Key/联网。
- 本地起服务（无 Docker）：`DATABASE_URL=sqlite+pysqlite:////tmp/dev.db uv run alembic upgrade head && ... uv run uvicorn backend.app.main:app --port 8000`（详见 README）。
- 迁移自检：`backend/app/tests/test_migrations.py`（pytest 用 `create_all` 建表，**不走 Alembic**，故迁移单独验证）。
- Runtime：Python 3.10+；仓库锁文件与当前门禁使用 Python 3.12 验证。
- CI：`.github/workflows/ci.yml` 包含锁文件全量测试与 Postgres 16 的逐版本迁移、约束/排序、拒绝 lossy downgrade、备份恢复演练；本机 SQLite 通过不能替代真实 CI artifact。

## 当前发布边界
- Alembic head：`0029_runtime_access_for_v2_tables`（0023 规则检查点与人工复核任务；0024 批次七态约束 + `state_version`；0025 结构化复核原因；0026 命令幂等回执；0027 ExportEvent；0028 旧导出日志幂等补录；0029 为 0026/0027 的新表补 `pgs_app` 授权与 RLS）。**0024–0028 有数据时一律拒绝降级**——降级会删掉复核原因、幂等回执与导出审计，这些重建不回来。
- **新建表必须在迁移里给 `pgs_app` 授权并建 RLS**（照 0023 的做法）。生产运行角色没有 DDL，也不会自动获得新表权限；漏了不会让迁移失败，而是让迁移成功之后应用 permission denied——本地与 CI 不建这个角色，两边都测不出来。
- v1 `score_paper()` 与 v2 `score_generic_submission()` 并存；正式 RubricVersion 走 AtomicRule Core，未版本化标准只能走显式 compatibility。
- `SCORING_ENGINE_MODE` 当前默认 `legacy`；它只控制未版本化兼容路径。真实 `GATE-03` 达到 `gating_eligible=true` 且取得维护者发布批准前，禁止改为默认 Core。

## 架构（backend/app/）
- `api/routes/`：rubrics / batches / papers / submissions_v2 / scoring / exports / release_gates / system / calibration。
- `services/`：
  - `rubric_import/`：Excel 规则解析(`parser`) + Word 批注解析(`docx_comments`) + 结构化扣分规则编译(`compiler`)。
  - `document_parser/`：docx/PDF 解析(`parser`)、分块(`chunking`)、格式解析(`format_resolver`)、格式比对(`format_check`)。
  - `papers/ingestion`：解析+落库共享服务（路由与离线脚本复用）。
  - `scoring/engine`：评分编排（**核心**）；`rules` 总分/等级；`validator` 结构化输出校验+注入检测。
  - `batch_scoring/jobs`：数据库持久化批任务、论文级检查点、租约恢复/取消/定向重试、观察策略与 Core 切换信号；只做观察，不授予 GATE-03 发布权限。
  - `deployment/`：Core inventory、OPS readiness、Postgres verifier、带校验 manifest 的数据库+storage 备份恢复；OPS 报告同样不授予默认 Core。
  - `checkers/`：确定性检查器(`deterministic`)、findings→扣分(`findings_checker`)。
  - `coherence/`：篇章一致性（确定性 `checker` + 语义 `semantic`）。
  - `calibration/`：L2 锚点库(`library`) + 漂移/排名(`analytics`)。
  - `llm/`：base + mock + openai + openai_compatible(默认 zhipu) + 重试/缓存日志；`cache/llm_cache` 是 L0 缓存。
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

## 前端（v2 评审工作台 · 有据智评）
- 源码 `frontend/workbench/`（Vite + Vue 3 + JS，JSDoc 引类型）；旧 SPA 在 `frontend/web/`。
- 统一组装：`python scripts/build_web_static.py --with-workbench` → `public/`。FastAPI、Vercel、Docker 托管**同一份产物**；`public/` 已提交，CI 有漂移门禁。
- 入口：`/` 直接是 v2 工作台，**旧 SPA 已下线**（`/legacy/` 返回 404）。`/login` `/register` `/reset-password` 由工作台承接——邮件里已发出的邀请与重置链接指向后两条，收件人不会重新拿到新链接，这三条不能只剩 404。
- 静态入口页由 `build_web_static.py` 写出，**改后端路由不够**：Vercel 直接静态托管 `public/`，`/` 命中的是 `public/index.html`，不经过 Python。
- 门禁：`npm run test:unit`（组件/状态）、`typecheck`、`api:dump && api:check`（OpenAPI 合同差异）、`playwright test`（浏览器验收，含一个 `AUTH_ENABLED=true` 的多组织后端）。
- 旧写入口与新端点**共用守卫**：归档批次拒绝改分/复核，改分同样 bump `review_revision`。不能只有新端点防冲突。

## 设计原则（详见 论文打分系统设计方案.md）
确定性优先(代码做判定题、LLM 做判断题)、原子评分项、每个扣分/选档强制带证据(抗幻觉)、结构化优先、无状态可缓存可复现、人在回路。**扣哪项/扣几分来自用户授权的模板/Excel 编译，不写死。**

## 三文档同步（MUST）

**每一次改动方案落地，都要同步这三份文档**——不是发布时补，是改完就补：

| 文档 | 回答什么 | 什么时候必须动 |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 模块边界、核心调用链、数据流 | 新增/删除模块、调用链改向、数据流或持久化边界变化 |
| [DECISIONS.md](DECISIONS.md) | 为什么这样选，**放弃了什么** | 任何有取舍的方案：选了 A 没选 B、放宽或收紧了一条规则、明知有代价仍然这么做 |
| [RUNBOOK.md](RUNBOOK.md) | 怎么启动、测试、排错、发布 | 新增命令/环境变量/门禁步骤，或踩到一个下次还会踩的坑 |

写法要求：

- **DECISIONS 必须写「放弃了什么」**。只写选择等于没写——读的人无法判断当初是没想到，
  还是想到了并且有理由不选。
- **RUNBOOK 的排错条目要写「报错指向哪里、真正的原因在哪里」**。两者一致的坑不值得记；
  值得记的正是不一致的那种。
- 三份文档各自的「维护记录」表追加同一日期/主题的条目。
- 文档与实现冲突时，**先修文档再交付**。

`backend/app/tests/test_m8_documentation_contract.py` 会校验其中可机检的部分
（迁移 head、命令、边界事实）；机器检不出来的部分靠这条约定。

## 约定
- 新增端点/字段要配 Alembic 迁移（当前 head 为 `0029_runtime_access_for_v2_tables`）+ 对应测试（`backend/app/tests/test_*.py`，复用 `conftest` 的 `client` 与 `make_*` 造数据）。
- 改 prompt/输入构造要 bump `cache/llm_cache.PROMPT_VERSION`。
- 改评分逻辑后用 §15 QWK 留出集重新锚定基线。
- 生产变更需完成 `docs/上线清单.md`；备份恢复必须先 verify，restore 只允许显式确认的数据库与空 storage 目标。
- 进度与待办见 `代码改造计划.md §7`；前端 v2 的实施进度与已知偏离见 `docs/前端v2改造计划.md §9.1`。
