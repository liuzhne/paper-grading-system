"""批次级服务：阶段状态机、守卫与汇总投影。"""

from backend.app.services.batches.summary import get_batch_summary


__all__ = ["get_batch_summary"]
