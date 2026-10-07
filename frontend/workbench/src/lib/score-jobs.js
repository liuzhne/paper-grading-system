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

// token 数的紧凑写法；缺值显示「—」而不是 0。
export function compactTokens(value) {
  if (value == null) return "—";
  return value >= 10000 ? `${(value / 10000).toFixed(1)} 万` : String(value);
}

// 一份材料本次实际付费的用量，以及复用了多少条已有判定。旧任务没有这些字段时返回「—」，
// 不能显示成 0——0 表示「确实没花」，和「没有记录」不是一回事。
export function itemUsageText(item) {
  const telemetry = item?.telemetry || {};
  if (telemetry.prompt_tokens == null) return "—";
  const parts = [`输入 ${compactTokens(telemetry.prompt_tokens)} / 输出 ${compactTokens(telemetry.completion_tokens || 0)}`];
  if (telemetry.decision_ledger_reused) parts.push(`复用 ${telemetry.decision_ledger_reused} 条`);
  return parts.join(" · ");
}
