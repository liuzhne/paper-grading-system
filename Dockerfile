FROM python:3.12-slim-bookworm AS runtime

ARG POSTGRES_MAJOR=16

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --proto '=https' --tlsv1.2 --location --silent --show-error --fail \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-client-${POSTGRES_MAJOR}" \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project

COPY alembic ./alembic
COPY alembic.ini ./
COPY backend ./backend
COPY frontend ./frontend
# 统一组装产物：FastAPI 从 public/workbench 托管新页（计划 §8.2）。少了这一行，
# 容器里 /workbench/* 全是 404，而 legacy 入口走 frontend/web 反而正常——故障
# 看起来像「新页面没部署上」，而不是「镜像少了一层」。
# 产物在镜像外用 scripts/build_web_static.py --with-workbench 组装好，运行镜像
# 不安装 Node/npm。
COPY public ./public

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
