// @ts-check
/**
 * AI 任务（起草、归类……）在页面上的呈现：进度文字、能否取消与重试。
 *
 * 任务由后端在后台逐条目执行；页面只轮询任务状态，刷新或关页面不影响执行。
 */

export const ACTIVE_AI_TASK_STATUSES = new Set(["queued", "running"]);

/** @param {any} task */
export function isActiveAiTask(task) {
  return ACTIVE_AI_TASK_STATUSES.has(task?.status);
}

/**
 * 有条目因模型限流延后（带最早可执行时间）时为 true。
 * @param {any} task
 * @param {number} [now]
 */
export function isWaitingForRateLimit(task, now = Date.now()) {
  return (task?.items || []).some((/** @type {any} */ item) => item.status === "pending" && item.not_before &&
    Date.parse(`${item.not_before}Z`) > now);
}

/**
 * 起草任务的进度说明，例如“AI 起草中：第 2/4 批”。
 * @param {any} task
 * @param {number} [now]
 * @returns {string}
 */
export function draftTaskProgress(task, now = Date.now()) {
  if (!task) return "";
  const total = task.total_items || 0;
  const done = task.succeeded_count || 0;
  if (task.status === "queued") return total > 1 ? `AI 起草排队中：共 ${total} 批` : "AI 起草排队中";
  if (task.status === "running") {
    const current = Math.min(total, done + 1);
    const suffix = isWaitingForRateLimit(task, now) ? "（模型限流，稍后自动继续）" : "";
    return total > 1 ? `AI 起草中：第 ${current}/${total} 批${suffix}` : `AI 起草中${suffix}`;
  }
  if (task.status === "failed") {
    return `起草失败（已完成 ${done}/${total} 批）：${task.error_message || task.error_code || "未知原因"}`;
  }
  if (task.status === "canceled") return "已取消起草；已完成的批次不会合并。";
  if (task.status === "succeeded" && task.result?.status === "already_structured") {
    return "原文细则已可解析，请直接核对或修改规则，无需 AI 补全。";
  }
  if (task.status === "succeeded") return total > 1 ? `AI 起草完成：共 ${total} 批` : "AI 起草完成";
  return "";
}

/** 失败的任务可以只重试失败的批次（已成功的保留）。 @param {any} task */
export function canRetryAiTask(task) {
  return task?.status === "failed";
}

/**
 * 归类任务 → 来源核对面板的进度：{completed, failed, total, running, stopped, error}。
 * 单元数按条目累计（每批最多 3 个）。
 * @param {any} task
 */
export function classificationTaskProgress(task) {
  if (!task) return null;
  const items = task.items || [];
  const units = (/** @type {string[]} */ statuses) => items
    .filter((/** @type {any} */ item) => statuses.includes(item.status))
    .reduce((/** @type {number} */ sum, /** @type {any} */ item) => sum + (item.unit_count || 0), 0);
  const total = items.reduce((/** @type {number} */ sum, /** @type {any} */ item) => sum + (item.unit_count || 0), 0);
  return {
    completed: units(["succeeded"]),
    failed: units(["failed"]),
    total,
    running: isActiveAiTask(task),
    stopped: task.status === "failed" ? "provider" : task.status === "canceled" ? "canceled" : null,
    error: task.status === "failed" ? task.error_message || task.error_code || null : null,
  };
}
