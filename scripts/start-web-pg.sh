#!/usr/bin/env bash
# 本地启动 Web 操作台与评分 Worker，用 Docker 里的 PostgreSQL 16 + 开启鉴权。
#
# 与 start-web.sh 的区别：那个用临时 SQLite、免登录，图快；这个贴近生产形态，
# 能真正跑到迁移链、组织隔离和平台管理员边界——鉴权关着时这些代码根本不执行。
#
#   打开：http://localhost:8000/        工作台（会要求登录）
#         http://localhost:8000/docs    API 文档
#
# 首次登录用下面打印的 AUTH_USERNAME / AUTH_PASSWORD，系统会就地创建
# platform_admin 账号（ensure_bootstrap_admin）。之后改环境变量不会改已建账号的密码。
#
# 开了鉴权后 LLM_PROVIDER 等环境变量**不生效**：模型要登录后在运维页配置（D-028）。
#
# 可用环境变量覆盖：
#   PORT=8000  HOST=127.0.0.1  SEED=1  ./scripts/start-web-pg.sh
#
# 注意：这是 Web 库，与 pgs CLI 的 ~/.paper-grading/cli.db 是两套库。
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
SEED="${SEED:-0}"
ENV_FILE=".env.intranet"
SECRET_FILE=".env.pg-dev.local"   # 本机密钥，被 .gitignore 的 .env* 覆盖
PY=".venv/bin/python"
COMPOSE=(docker compose --env-file "$ENV_FILE")

step() { echo "[start-web-pg] $*"; }
die()  { echo "[start-web-pg] 错误：$*" >&2; exit 1; }

# --- 0. 前置检查 -----------------------------------------------------------
[[ -x "$PY" ]] || die "找不到 .venv，请先 uv sync"
[[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE，请先 cp .env.intranet.example $ENV_FILE 并填写密码"
docker info >/dev/null 2>&1 || die "Docker daemon 没启动，请先打开 Docker Desktop"

# docker compose 会读取该文件，但宿主机上的 API/Worker 不会自动继承。
# 显式 export 全部运行配置，避免 PGS_DISABLE_ENV_FILE=1 后静默退回代码默认值。
set -a; . "./$ENV_FILE"; set +a

PGPW="${POSTGRES_PASSWORD:-}"
PGDB="${POSTGRES_DB:-paper_grading}"
PGUSER="${POSTGRES_USER:-paper}"
[[ -n "$PGPW" ]] || die "$ENV_FILE 里没有 POSTGRES_PASSWORD"

for setting_name in \
  SCORING_CONTEXT_WINDOW_TOKENS \
  SCORING_CONTEXT_SAFETY_MARGIN_TOKENS \
  SCORING_EVIDENCE_TOP_K \
  OPENAI_COMPATIBLE_MAX_TOKENS; do
  setting_value="${!setting_name:-}"
  [[ "$setting_value" =~ ^[0-9]+$ ]] \
    || die "$ENV_FILE 中的 $setting_name 必须是非负整数，且不能省略"
done
(( SCORING_CONTEXT_WINDOW_TOKENS > SCORING_CONTEXT_SAFETY_MARGIN_TOKENS )) \
  || die "SCORING_CONTEXT_WINDOW_TOKENS 必须大于安全余量"
(( SCORING_CONTEXT_WINDOW_TOKENS > OPENAI_COMPATIBLE_MAX_TOKENS + SCORING_CONTEXT_SAFETY_MARGIN_TOKENS )) \
  || die "评分上下文必须大于兼容模型输出预算与安全余量之和"
(( SCORING_EVIDENCE_TOP_K >= 1 )) \
  || die "SCORING_EVIDENCE_TOP_K 必须至少为 1"
[[ "${OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON:-}" == "true" ]] \
  || die "本地真实评分要求 OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON=true"

# --- 1. 端口映射 -----------------------------------------------------------
# 主 compose 文件不发布 5432：那是内网试点形态，app 也在容器里走 @db:5432，
# 对外开数据库端口纯属扩大暴露面。app 跑在宿主机时才需要，所以放 override。
if [[ ! -f docker-compose.override.yml ]]; then
  step "生成 docker-compose.override.yml（发布 5432 到宿主机）"
  cat > docker-compose.override.yml <<'EOF'
services:
  db:
    ports:
      - "5432:5432"
EOF
fi

# --- 2. 起库并等 healthy ---------------------------------------------------
step "启动 PostgreSQL 容器"
"${COMPOSE[@]}" up -d db >/dev/null

step "等待数据库 healthy"
for i in $(seq 1 60); do
  status="$("${COMPOSE[@]}" ps db --format '{{.Health}}' 2>/dev/null || true)"
  [[ "$status" == "healthy" ]] && break
  [[ $i -eq 60 ]] && die "数据库 60 秒内未就绪，看 docker compose --env-file $ENV_FILE logs db"
  sleep 1
done

# POSTGRES_PASSWORD 只在数据目录首次初始化时生效；volume 已存在时改 .env 不会重设密码。
# 这里对齐一次，避免「文件里改了但库里还是旧密码」。
step "对齐角色密码"
docker exec "$( "${COMPOSE[@]}" ps -q db )" \
  psql -U "$PGUSER" -d "$PGDB" -qtc "ALTER USER $PGUSER WITH PASSWORD '$PGPW';" >/dev/null

# --- 3. 本机密钥（生成一次，之后复用）--------------------------------------
# BYOK_MASTER_KEY 换掉就解不开已存的 BYOK / 平台模型密钥，所以必须落盘复用。
if [[ ! -f "$SECRET_FILE" ]]; then
  step "首次运行：生成 $SECRET_FILE"
  {
    echo "# 本机开发密钥，勿提交。删掉本文件会重新生成，届时库里已存的加密密钥将无法解开。"
    echo "AUTH_USERNAME=admin"
    echo "AUTH_PASSWORD=Local-Dev-2026!pg"
    echo "AUTH_SECRET=$($PY -c 'import secrets;print(secrets.token_urlsafe(48))')"
    echo "BYOK_MASTER_KEY=$($PY -c 'import secrets;print(secrets.token_urlsafe(48))')"
  } > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
fi
set -a; . "./$SECRET_FILE"; set +a

# --- 4. 运行配置 -----------------------------------------------------------
export DATABASE_URL="postgresql+psycopg://${PGUSER}:${PGPW}@localhost:5432/${PGDB}"
export STORAGE_ROOT="$PWD/storage"
export AUTH_ENABLED=true
export AUTH_TOKEN_TTL_SECONDS="${AUTH_TOKEN_TTL_SECONDS:-86400}"
export LLM_DEBUG_LOG_ENABLED=false
export PGS_DISABLE_ENV_FILE=1   # 排除 .env/.env.local 的隐式覆盖，以本脚本为准

# --- 5. 迁移（幂等）--------------------------------------------------------
step "alembic upgrade head"
.venv/bin/alembic upgrade head

# --- 6. 可选演示数据 -------------------------------------------------------
if [[ "$SEED" == "1" ]]; then
  step "灌演示评分标准（SEED=1）"
  $PY -m backend.app.scripts.seed_dev
fi

# --- 7. 起服务与本地后台评分执行器 -----------------------------------------
echo
step "DB       = postgresql+psycopg://${PGUSER}:***@localhost:5432/${PGDB}"
step "鉴权     = 开启（首次登录即创建 platform_admin）"
step "登录账号 = ${AUTH_USERNAME} / ${AUTH_PASSWORD}"
step "评分预算 = context ${SCORING_CONTEXT_WINDOW_TOKENS} / output ${OPENAI_COMPATIBLE_MAX_TOKENS} / margin ${SCORING_CONTEXT_SAFETY_MARGIN_TOKENS} / top-k ${SCORING_EVIDENCE_TOP_K} / json ${OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON}"
step "打开       http://localhost:${PORT}/"
echo

# 生产由 Vercel Queues subscriber 按材料领取任务；本地没有 Vercel 的消息
# 投递器，因此必须同时运行数据库轮询 worker。二者复用同一套持久化 job/item、
# 评分、心跳、取消和重试逻辑，不能只启动 Uvicorn 后让任务永久停在 queued。
WORKER_PID=""
API_PID=""

cleanup() {
  exit_code=$?
  trap - EXIT INT TERM HUP

  for child_pid in "$WORKER_PID" "$API_PID"; do
    if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
      kill "$child_pid" 2>/dev/null || true
    fi
  done
  for child_pid in "$WORKER_PID" "$API_PID"; do
    if [[ -n "$child_pid" ]]; then
      wait "$child_pid" 2>/dev/null || true
    fi
  done
  exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

step "启动本地评分 Worker（自动领取 queued 任务）"
"$PY" -m backend.app.scripts.run_batch_worker &
WORKER_PID=$!

step "启动 Web API"
"$PY" -m uvicorn backend.app.main:app --reload --host "$HOST" --port "$PORT" &
API_PID=$!

# macOS 自带 Bash 3.2 没有 wait -n；轮询两个子进程，任一异常退出都关闭另一方，
# 防止留下“页面可访问但无人评分”或“worker 在跑但页面不可用”的半启动状态。
while true; do
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    set +e
    wait "$WORKER_PID"
    child_status=$?
    set -e
    step "本地评分 Worker 已退出（状态码 $child_status），正在关闭 Web API"
    exit "$child_status"
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    set +e
    wait "$API_PID"
    child_status=$?
    set -e
    step "Web API 已退出（状态码 $child_status），正在关闭本地评分 Worker"
    exit "$child_status"
  fi
  sleep 1
done
