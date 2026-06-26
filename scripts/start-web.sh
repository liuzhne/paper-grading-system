#!/usr/bin/env bash
# 本地启动 Web 操作台（FastAPI 托管静态前端 + API），用临时 SQLite，免 Docker/Postgres。
#
#   打开：http://localhost:8000/        正式 Web 操作台
#         http://localhost:8000/docs    API 文档（Swagger）
#
# 要真实评分：先用 ./scripts/start-llm.sh 起本地大模型，并在 .env 配好
# OPENAI_COMPATIBLE_BASE_URL=http://localhost:18434/v1（页面「测试 LLM 连接」可自检）。
#
# 可用环境变量覆盖：
#   DB_PATH=/tmp/dev.db   PORT=8000   HOST=127.0.0.1   ./scripts/start-web.sh
#
# 局域网访问（其它设备）：HOST=0.0.0.0 ./scripts/start-web.sh，再用本机局域网 IP 访问。
#   ⚠️ 0.0.0.0 = 同网段任何设备都能进。默认 AUTH_ENABLED=false（无登录），
#      内含学生论文/成绩等 PII，仅在可信网络这么开；公网请设 AUTH_ENABLED=true+AUTH_PASSWORD。
#
# 注意：这是 Web 库（DATABASE_URL），与 pgs CLI 的 ~/.paper-grading/cli.db 是两套库。
set -euo pipefail

cd "$(dirname "$0")/.."

DB_PATH="${DB_PATH:-/tmp/dev.db}"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"   # 默认仅本机；局域网访问传 HOST=0.0.0.0
export DATABASE_URL="sqlite+pysqlite:///${DB_PATH}"

echo "[start-web] DB     = $DATABASE_URL"
echo "[start-web] 监听   = $HOST:$PORT"

# 1. 建表/迁移到最新（幂等）
uv run alembic upgrade head

# 2. 灌默认 dev user + 评分标准 + 批次（幂等：已存在则跳过）
uv run python -m backend.app.scripts.seed_dev

# 3. 起服务（同进程托管 API + Web 操作台）
if [[ "$HOST" == "0.0.0.0" ]]; then
  echo "[start-web] 局域网访问： http://$(ipconfig getifaddr en0 2>/dev/null || echo '本机IP'):${PORT}/"
fi
echo "[start-web] 本机打开 http://localhost:${PORT}/"
exec uv run uvicorn backend.app.main:app --reload --host "$HOST" --port "$PORT"
