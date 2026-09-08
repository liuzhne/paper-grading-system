"""浏览器验收用的一次性后端（前端 v2 计划 §12.1）。

把**统一组装产物**（`public/`）挂到真实 FastAPI 上跑，而不是 Vite dev
server：深链接回退、缺失资源必须 404、`/api` 不被 SPA 回退吞掉这几条只在
生产托管路径上才会出问题。

数据库与存储都建在临时目录，进程退出即弃；LLM 固定 Mock。真实论文、Secret
与学生 PII 不会进入这套环境，因此截图与 trace 也不会带上它们。
"""

import os
import shutil
import sys
import tempfile


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    # 第二个参数打开鉴权模式：V01/V02/V10 要的是真实登录与多组织，开发模式的
    # 「放行一切」测不出任何边界。
    with_auth = len(sys.argv) > 2 and sys.argv[2] == "--auth"
    workdir = tempfile.mkdtemp(prefix="pgs-e2e-")

    # 必须在导入 backend 之前设置：settings 在导入期即固化。
    os.environ.update(
        {
            "DATABASE_URL": "sqlite+pysqlite:///%s/e2e.db" % workdir,
            "STORAGE_ROOT": "%s/storage" % workdir,
            "STORAGE_PROVIDER": "local",
            "AUTH_ENABLED": "true" if with_auth else "false",
            # 验收跑在明文 http 上，Secure cookie 在这里发不出去。**只在这个
            # 一次性环境里关**：生产默认仍是 True，配置本身没有被改动。
            "AUTH_COOKIE_SECURE": "false",
            "LLM_PROVIDER": "mock",
            "SHEET_WRITER_PROVIDER": "mock",
            "LLM_DEBUG_LOG_ENABLED": "false",
        }
    )

    from alembic import command
    from alembic.config import Config
    import uvicorn

    config = Config()
    config.set_main_option("script_location", "alembic")
    command.upgrade(config, "head")

    from e2e_server.seed import seed_all

    seed_all(with_auth=with_auth)

    from backend.app.main import app

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
