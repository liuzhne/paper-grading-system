# 毕业论文智能评分系统

这是一个核心功能优先的本地系统，实现“评分标准 -> 批次 -> 上传论文 -> 解析 -> 证据召回 -> AI 逐项评分 -> 人工复核 -> Excel/HTML 报告导出 -> 在线写表”的闭环。

当前版本暂不实现登录、JWT、RBAC、Celery、OCR 和 pgvector；真实 LLM 与在线写表通过环境变量启用，未配置时可自动回退到本地 Mock。

## 技术栈

- Backend: FastAPI, SQLAlchemy 2.x, Alembic
- UI: FastAPI 托管的静态 Web 操作台；Streamlit 保留为备用操作台
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
- `POST /api/rubrics/{id}/publish`
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

## 测试

```bash
uv run pytest
```
