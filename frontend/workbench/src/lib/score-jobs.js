export const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "cancel_requested"]);

const LABELS = {
  queued: "等待执行",
  running: "评分中",
  cancel_requested: "正在取消",
  completed: "已完成",
  completed_with_errors: "完成但有异常",
  canceled: "已取消",
  failed: "执行失败",
};

export function jobStatusLabel(job) {
  if (job?.heartbeat_state === "stale" && ACTIVE_JOB_STATUSES.has(job.status)) {
    return "执行中断，等待恢复";
  }
  return LABELS[job?.status] || job?.status || "未知状态";
}

export function itemStatusLabel(status) {
  return {
    pending: "等待评分",
    running: "评分中",
    succeeded: "已完成",
    skipped: "沿用已有结果",
    failed: "失败",
    canceled: "已取消",
  }[status] || status;
}

export function finishedCount(job) {
  return (job?.succeeded_count || 0) + (job?.skipped_count || 0) +
    (job?.failed_count || 0) + (job?.canceled_count || 0);
}

export function jobPercent(job) {
  return job?.total_items ? Math.round((finishedCount(job) / job.total_items) * 100) : 0;
}
