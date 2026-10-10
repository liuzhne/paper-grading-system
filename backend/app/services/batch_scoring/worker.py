"""内网与本地的执行器：与 Vercel 共用同一个领取函数（见 services/work_queue）。

保留这个模块名是为了 compose 与 start-web-pg.sh 的启动命令不变。
"""

from backend.app.services.work_queue.runner import run_worker_cycle
from backend.app.services.work_queue.runner import run_worker_loop


__all__ = ["run_worker_cycle", "run_worker_loop"]
