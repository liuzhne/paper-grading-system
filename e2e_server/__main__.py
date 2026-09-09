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


#: 一次性验收环境的凭据。库建在 tmp 下、进程退出即弃，里面没有任何真实论文或
#: 学生信息；生产凭据不出现在仓库里，也不从 `.env.local` 继承。
E2E_USERNAME = "e2e-operator"
E2E_AUTH_PASSWORD = "e2e-Acceptance-Local-1"
E2E_AUTH_SECRET = "e2e-acceptance-secret-not-for-production-0123456789"
#: 验收种子要写一条平台模型配置，加密需要它。一次性环境专用，库建在 tmp 下、
#: 进程退出即弃；生产密钥不出现在仓库里。
E2E_BYOK_MASTER_KEY = "e2e-acceptance-byok-master-key-not-for-production"


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    # 第二个参数打开鉴权模式：V01/V02/V10 要的是真实登录与多组织，开发模式的
    # 「放行一切」测不出任何边界。
    flags = set(sys.argv[2:])
    with_auth = "--auth" in flags
    # 「平台没配模型」是 D-028 的初始状态，需要单独一个后端来验证引导行为：
    # 主鉴权后端配了模型（否则 V01/V02/V10 全挂在第一步），验不了这一条。
    without_platform_model = "--no-platform-model" in flags
    workdir = tempfile.mkdtemp(prefix="pgs-e2e-")

    # 必须在导入 backend 之前设置：settings 在导入期即固化。
    os.environ.update(
        {
            # 不继承 `.env` / `.env.local`。开发机上那份文件带的是**生产**数据库
            # 地址与口令；隐式继承意味着一个本该完全隔离的进程在拿生产密钥跑，
            # 而且本地一直是绿的——只有在没有这个文件的机器上才暴露。
            "PGS_DISABLE_ENV_FILE": "1",
            "DATABASE_URL": "sqlite+pysqlite:///%s/e2e.db" % workdir,
            "STORAGE_ROOT": "%s/storage" % workdir,
            "STORAGE_PROVIDER": "local",
            "AUTH_ENABLED": "true" if with_auth else "false",
            # 验收跑在明文 http 上，Secure cookie 在这里发不出去。**只在这个
            # 一次性环境里关**：生产默认仍是 True，配置本身没有被改动。
            "AUTH_COOKIE_SECURE": "false",
            # 一次性凭据，写死在这里而不是从任何外部文件继承。开鉴权时
            # `Settings` 会强制校验强度，弱口令直接起不来——这条守卫要靠真的
            # 合格来满足，不是靠关掉它。
            "AUTH_USERNAME": E2E_USERNAME,
            "AUTH_PASSWORD": E2E_AUTH_PASSWORD,
            "AUTH_SECRET": E2E_AUTH_SECRET,
            "BYOK_MASTER_KEY": E2E_BYOK_MASTER_KEY,
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

    seed_all(with_auth=with_auth, with_platform_model=not without_platform_model)

    from backend.app.main import app

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
