# 毕业论文智能评分系统

这是一个核心功能优先的本地系统，实现“评分标准 -> 批次 -> 上传论文 -> 解析 -> 证据召回 -> AI 逐项评分 -> 人工复核 -> Excel/HTML 报告导出 -> 在线写表”的闭环。

当前版本提供单租户简单登录（opt-in），暂不实现完整 JWT/RBAC/Celery/OCR/pgvector；真实 LLM 与在线写表通过环境变量启用，未配置时可自动回退到本地 Mock。

## 技术栈

- Backend: FastAPI, SQLAlchemy 2.x, Alembic
- UI: FastAPI 托管的静态 Web 操作台；`pgs` 命令行端（本地零服务，共享同一评分内核）；Streamlit 保留为备用操作台
- DB: PostgreSQL
- Document parsing: python-docx, PyMuPDF
- Export: openpyxl

## 本地启动

```bash
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run python -m backend.app.scripts.seed_dev
uv run uvicorn backend.app.main:app --reload --port 8000
```

FastAPI 文档地址：

```text
http://localhost:8000/docs
```

正式 Web 操作台：

```text
http://localhost:8000/
```

备用 Streamlit 操作台：

```text
http://localhost:8501
```

如需启动备用 Streamlit：

```bash
uv run streamlit run frontend/streamlit_app.py --server.port 8501
```

如果本机 Docker daemon 未启动，也可以用临时 SQLite 数据库做功能冒烟验证：

```bash
UV_CACHE_DIR=.uv-cache DATABASE_URL=sqlite+pysqlite:////private/tmp/paper_grading_dev.db uv run alembic upgrade head
UV_CACHE_DIR=.uv-cache DATABASE_URL=sqlite+pysqlite:////private/tmp/paper_grading_dev.db uv run python -m backend.app.scripts.seed_dev
UV_CACHE_DIR=.uv-cache DATABASE_URL=sqlite+pysqlite:////private/tmp/paper_grading_dev.db uv run uvicorn backend.app.main:app --reload --port 8000
```

## CLI 端（`pgs`，本地零服务）

设计的双前端之一：与 Web 共享同一套评分内核，**自带本地 sqlite + storage，不起服务即可跑完整流程**。`uv sync` 后即有 `pgs` 命令（或 `uv run pgs` / `python -m backend.app.cli.main`）。数据默认落在 `~/.paper-grading/`，`--db` / `--storage` 可改。

```bash
uv run pgs init --seed                                   # 建本地库 + 默认评分标准/批次
uv run pgs check [--mock]                                # LLM 连通自检（--mock 离线、不联网）
uv run pgs import 规则.xlsx --name 校级标准 --template 模板.docx   # 导入 M4 provenance 草稿图
uv run pgs rubrics                                       # 列出评分标准（--json 机器可读）
uv run pgs rubric-graph <rubric_id> --json               # 查看活动 compilation/version/rule/link 状态
uv run pgs rule-submit <rubric_id> <rule_code> --reason "送审"
uv run pgs rule-approve <rubric_id> <rule_code> --reason "核对通过"  # 逐条规则人工签核
uv run pgs template-link-review <rubric_id> <link_id> --decision confirmed --reason "映射无误"
uv run pgs rubric-submit-review <rubric_id>               # rubric draft → review
uv run pgs publish <rubric_id> --compilation-id <id>     # 严格校验后发布冻结版本
uv run pgs score 论文.docx 目录/ --rubric "校级标准" [--mock] [--workers N] [--report-dir out/]   # 评分（多文件/目录递归）
uv run pgs score 论文.docx --no-db --rubric-file 规则.xlsx [--template 模板.docx] [--mock]   # 无状态评分：不建库、不落任何库
uv run pgs runs [--batch <id>]                           # 列出评分任务（拿 run_id）
uv run pgs show <run_id>                                 # 逐项明细 + 篇章一致性/格式问题
uv run pgs review <run_id> --set C01=18 [--note "理由"] [--submit]   # 人工复核：按 code 改单项分/提交
uv run pgs batches                                       # 列出批次（拿 batch_id）
uv run pgs report <run_id> -o 报告.html                  # 生成 HTML 报告
uv run pgs export <batch_id> -o 成绩.xlsx                # 导出批次 Excel
uv run pgs scores-template <rubric_id> -o 成绩表模板.xlsx   # 生成教师成绩表模板（QWK 填写用）
uv run pgs eval --rubric <id> --papers-dir 论文夹/ --scores 成绩表.xlsx   # QWK + 基线/回归门禁
```

- LLM 由环境变量驱动（与 Web 一致，读 `.env`）；`--mock` 强制本地 Mock、不联网。
- 批量：`score --workers N` 并发评分（适合真实 LLM 批量）+ 进度条 + 等级分布汇总。LLM 计算已脱离 DB 事务（6.1 解耦：collect 读 → compute 纯算 → persist 短写），多 worker 在本地 sqlite 也能并行（compute 无锁，仅末尾短写经 busy_timeout 串行）。
- `score` 任一篇解析/评分失败即**非零退出**（便于脚本/CI）；`--json` 在 `score`/`rubrics`/`runs`/`show`/`batches`/`check` 输出机器可读结果。
- M4 发布不会自动批准规则、确认模板映射或清除 blocker。驳回规则用 `rule-reopen`，审核中标准用 `rubric-return-draft`，旧的未版本化草稿须先显式执行 `rubric-upgrade --reason ...`。
- 正式标准的 batch 会锁定唯一已发布 `rubric_version_id`，评分时必然使用 AtomicRule Core；全局 `SCORING_ENGINE_MODE` 在 M8 前仍默认 `legacy`，只影响未版本化兼容路径。
- 闭环：`score` → 拿 run_id（或 `pgs runs`）→ `pgs show <run_id>` 看逐项明细 → `pgs review` 改分/提交（写 ReviewLog、重算总分）→ `pgs report` 出报告。
- 无状态：`score --no-db --rubric-file 规则.xlsx` 从文件直接评分，**不建 sqlite、不落任何库**（隐私/一次性/脚本友好，由 6.1 纯 Core 支撑）；`--json` 取逐项明细；需 `--rubric-file`，不支持 `--report-dir`。

## P0：真实 LLM、在线写表和正式前端

完整配置见 [docs/integrations.md](docs/integrations.md)。总览页的 `P0 集成状态` 会显示当前是否真正启用了真实 LLM、Google Sheets 写入和正式 Web 前端。

默认配置使用本地 Mock。中国大陆地区建议先接智谱 GLM-4.7-Flash 免费模型：

```bash
export LLM_PROVIDER=openai_compatible
export LLM_FALLBACK_TO_MOCK=true
export LLM_DEBUG_LOG_ENABLED=true
export LLM_DEBUG_LOG_MAX_CHARS=12000
export LLM_RATE_LIMIT_SLEEP_SECONDS=1
export LLM_RETRY_BASE_DELAY_SECONDS=1
export LLM_RETRY_MAX_DELAY_SECONDS=30
export LLM_429_RETRY_DELAY_SECONDS=12
export OPENAI_COMPATIBLE_API_KEY=...
export OPENAI_COMPATIBLE_BASE_URL=https://open.bigmodel.cn/api/paas/v4
export OPENAI_COMPATIBLE_MODEL=glm-4.7-flash
export OPENAI_COMPATIBLE_PROVIDER_NAME=zhipu
export OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON=true
export OPENAI_COMPATIBLE_THINKING_TYPE=disabled
export SCORING_CHUNK_EVAL_TOP_K=3
export SCORING_STANDARD_CAP_RATIO=0.8
export SCORING_EXCEPTIONAL_RATIO=0.92
```

接 Google Sheets/在线表格写入时，推荐先部署一个 Google Apps Script Web App 作为写入端点：

```bash
export SHEET_WRITER_PROVIDER=google_sheets
export SHEET_FALLBACK_TO_MOCK=false
export GOOGLE_SHEETS_WEBAPP_URL=https://script.google.com/macros/s/.../exec
export GOOGLE_SHEETS_WEBAPP_SECRET=...
```

Google Apps Script 写表端模板位于 [docs/google_apps_script_webapp.gs](docs/google_apps_script_webapp.gs)。

## 核心 API

- `POST/GET /api/rubrics`
- `GET /api/rubrics/import-template.xlsx`
- `POST /api/rubrics/import-files`
- `PATCH /api/rubrics/{id}`
- `GET /api/rubrics/{id}/execution-draft`
- `POST /api/rubrics/{id}/submit-review`、`POST /api/rubrics/{id}/return-to-draft`
- `POST /api/rubrics/{id}/recompile`
- `PATCH /api/rubrics/{id}/rules/{rule_code}`
- `POST /api/rubrics/{id}/rules/{rule_code}/submit-review|approve|reject|reopen`
- `POST /api/rubrics/{id}/template-links/{link_id}/review`
- `POST /api/rubrics/{id}/publish`（body 必须指定 `compilation_id`）
- `POST /api/rubrics/{id}/clone`
- `POST/GET /api/batches`
- `PATCH /api/batches/{id}`
- `GET /api/batches/{id}/summary`
- `POST /api/batches/{id}/score?rescore=false`
- `POST /api/batches/{id}/start?rescore=false`
- `POST /api/papers/upload`
- `POST /api/papers/bulk-upload`
- `GET /api/papers?batch_id={batch_id}`
- `GET /api/papers/{id}`
- `PATCH /api/papers/{id}`
- `GET /api/papers/{id}/parsed`
- `GET /api/papers/{id}/chunks`
- `POST /api/papers/{id}/parse`
- `POST /api/papers/{id}/score`
- `GET /api/scoring-runs?paper_id={paper_id}&batch_id={batch_id}`
- `GET /api/scoring-runs/{id}`
- `GET /api/scoring-runs/{id}/items`
- `POST /api/scoring-runs/{id}/retry`
- `PATCH /api/score-items/{id}`
- `POST /api/scoring-runs/{id}/review`
- `POST /api/scoring-runs/{id}/write-sheet`
- `GET /api/batches/{id}/export.xlsx`
- `GET /api/export-logs?batch_id={batch_id}&run_id={run_id}`
- `GET /api/scoring-runs/{id}/report`
- `GET /api/system/integrations`
- `GET /api/rubrics/{id}/scores-template.xlsx`（QWK 教师成绩表模板）
- `POST/GET /api/calibration/anchors`（L2 校准锚点）
- `GET /api/batches/{id}/ranking`、`GET /api/batches/{id}/drift`（批量排名 / 评分漂移）

## 测试

```bash
uv run pytest
```

## QWK 评估验收（"打得准不准"）

设计把 QWK（系统分 vs **教师分**的一致性）列为上线门槛。准备一批**真实已评论文**后：

1. 下载该评分标准的成绩表模板（列随评分项自动展开）：`GET /api/rubrics/{rubric_id}/scores-template.xlsx`
2. 按模板填教师分（文件名 / 总分 / 各评分项分），论文 `.docx` 放一个文件夹。
3. 跑评估（需真实 LLM）：

```bash
LLM_PROVIDER=openai_compatible OPENAI_COMPATIBLE_API_KEY=... \
uv run python -m backend.app.scripts.run_qwk_eval \
    --rubric-id <rubric_id> --papers-dir /路径/论文文件夹 --scores /路径/成绩表.xlsx
```

输出 QWK/MAE/等级一致率/逐维度偏宽偏严；普通首跑写 `storage/eval/baseline.json`，之后重跑做实验回归检查。该聚合基线明确 `gating_eligible=false`。

M1/M5/M8 真实发布门禁必须使用 `run_qwk_eval --release-gate` 的仓库外私有归档、完整 identity 和两阶段人工批准流程，见 `docs/baselines/pgs-8-release-gate-runbook.md`。

> 必须用**真实教师评分**作真值（mock 仅验证管线连通）；学生论文与成绩**放仓库外**，仅聚合 `baseline.json` 可提交。细节见 [backend/app/eval/README.md](backend/app/eval/README.md)。

## 部署与运维要点

- **内网试点 Docker 栈**：复制 `.env.intranet.example` 为 `.env.intranet`，改强密码与站点名后运行 `docker compose --env-file .env.intranet up -d --build`。详见 [docs/部署.md](docs/部署.md)。
- **每次部署先迁移**：`uv run alembic upgrade head`（当前到 `0013_core_replay_identity`；测试用 `create_all`，生产必须走迁移；Docker app 容器启动时会自动迁移）。
- **持久化状态**在 `storage/`：`uploads/`(原文)、`parsed/`(解析 JSON)、`reports/`、`exports/`、`llm_cache.sqlite`(L0 缓存/账本)、`eval/`(评估报告/基线)。除占位 `.gitkeep` 外均已 gitignore。
- **真实 LLM**：设 `LLM_PROVIDER=openai_compatible` + `OPENAI_COMPATIBLE_*`（见上）；上线前用 `uv run python -m backend.app.scripts.diagnose_llm` 自检连通。未配置自动回退 Mock（评分项标人工复核）。
- **可复现/降本**：`LLM_CACHE_ENABLED=true` 命中即复用；改 prompt 需 bump `cache/llm_cache.PROMPT_VERSION`。
- **改评分逻辑后**：用 QWK 留出集重跑 `run_qwk_eval` 重新锚定基线，避免静默漂移。
- 维护/交接速览见 [CLAUDE.md](CLAUDE.md)。
