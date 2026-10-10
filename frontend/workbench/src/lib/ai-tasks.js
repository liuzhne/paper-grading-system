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

/** 审查条目的显示名：评分项编号，跨项审查另起名字。 @param {any} item */
function reviewItemName(item) {
  return item?.label === "__cross__" ? "跨项审查" : item?.label || `第 ${(item?.ordinal ?? 0) + 1} 项`;
}

/**
 * 规则审查任务的进度说明，例如“规则审查中：已完成 2/5 项”。
 * @param {any} task
 * @param {number} [now]
 * @returns {string}
 */
export function reviewTaskProgress(task, now = Date.now()) {
  if (!task) return "";
  const total = task.total_items || 0;
  const done = task.succeeded_count || 0;
  if (task.status === "queued") return `规则审查排队中：共 ${total} 项（关闭页面不会中断）`;
  if (task.status === "running") {
    const suffix = isWaitingForRateLimit(task, now) ? "（模型限流，稍后自动继续）" : "";
    return `规则审查中：已完成 ${done}/${total} 项${suffix}（关闭页面不会中断）`;
  }
  if (task.status === "failed") {
    const failed = (task.items || []).filter((/** @type {any} */ item) => item.status === "failed").map(reviewItemName);
    const where = failed.length ? `${failed.join("、")} ` : "";
    return `审查未完成（已完成 ${done}/${total} 项）：${where}${task.error_message || task.error_code || "未知原因"}`;
  }
  if (task.status === "canceled") return "已停止审查；本轮结果不会写入。";
  if (task.status === "succeeded") return "审查完成。";
  return "";
}

/**
 * 表格结构识别任务（草稿或导入前）的进度说明。
 * @param {any} task
 * @param {number} [now]
 * @returns {string}
 */
export function structureTaskProgress(task, now = Date.now()) {
  if (!task) return "";
  if (task.status === "queued") return "AI 识别表格结构：排队中（关闭页面不会中断）";
  if (task.status === "running") {
    return isWaitingForRateLimit(task, now)
      ? "AI 识别表格结构：模型限流，稍后自动继续"
      : "AI 正在识别表格结构…（关闭页面不会中断）";
  }
  if (task.status === "failed") return `识别失败：${task.error_message || task.error_code || "未知原因"}`;
  if (task.status === "canceled") return "已取消识别。";
  if (task.status === "succeeded") return "识别完成。";
  return "";
}
