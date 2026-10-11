"""执行模型的时长与次数（方案第 13.2 节的建议值）。

这些数值决定“一次执行算死了”“多久巡检一次”，改动要同步 RUNBOOK。
"""

# 条目心跳间隔与租约：心跳超过租约未更新，就认为这次执行已经死了（进程被杀、
# 函数超过平台时长上限）。租约过期不等于条目失败：巡检把它重置为待处理后续评。
HEARTBEAT_SECONDS = 15
ITEM_LEASE_SECONDS = 120

# 连续多少次执行都没有写入新的检查点或结果，就把条目标为失败。按“无进展”而不按
# 总次数计算：超过 300 秒的单篇本来就要靠多次执行续评完成。
STALL_LIMIT = 3

# 全系统巡检周期（Vercel 上的自续期消息、worker 的巡检间隔）与每次最多处理的条目数。
SWEEP_INTERVAL_SECONDS = 120
SWEEP_BATCH_LIMIT = 200
# 进度读取触发的单任务巡检，每个任务最多这么久一次。
PAGE_SWEEP_SECONDS = 30

# 一次领取最多跳过多少个被别人抢先的候选（SQLite 没有行锁，靠条件更新防重复领取）。
CLAIM_CANDIDATE_LIMIT = 20

__all__ = [
    "CLAIM_CANDIDATE_LIMIT",
    "HEARTBEAT_SECONDS",
    "ITEM_LEASE_SECONDS",
    "PAGE_SWEEP_SECONDS",
    "STALL_LIMIT",
    "SWEEP_BATCH_LIMIT",
    "SWEEP_INTERVAL_SECONDS",
]
