/**
 * 评分任务的写动作：页面按钮与评分助手的“确定”共用这一份代码。
 *
 * 助手里点“确定”与页面上点按钮必须是同一个函数——不是模拟点击，也不是另写一份
 * 调接口的逻辑。否则两边的参数、前置条件与错误处理迟早分叉（对话评分助手方案 §5）。
 * 后端守卫（角色、归档、state_version、幂等）对两个入口同样生效。
 */

import { api } from "@/api/client.js";

/** 新建评分任务时的默认评分参数。改这里两个入口一起变。 */
export const DEFAULT_SCORE_JOB = Object.freeze({ rescore: false, max_workers: 2 });

/**
 * 建批次。建批次时冻结连接：之后轮换密钥或改配置，旧批次会拒绝继续跑，而不是
 * 悄悄换一个模型接着评。
 * @param {{name: string, rubricId: string, aiConnectionId?: string|null, department?: string|null, major?: string|null}} input
 */
export function createBatch(input) {
  return api.post("/batches", {
    name: input.name.trim(),
    rubric_id: input.rubricId,
    department: input.department || null,
    major: input.major || null,
    ai_connection_id: input.aiConnectionId || null,
  });
}

/** 上传后、评分前的解析预检；只汇总既有解析诊断。 */
export function runUploadPrecheck(batchId, paperIds) {
  return api.post(`/batches/${batchId}/upload-precheck`, { paper_ids: paperIds });
}

/** 评分前的本地用量估算（不调用模型）。 */
export function loadScoreEstimate(batchId) {
  return api.get(`/batches/${batchId}/score-estimate`);
}

export function startScoringJob(batchId) {
  return api.post(`/batches/${batchId}/score-jobs`, { ...DEFAULT_SCORE_JOB });
}

export function cancelScoringJob(jobId) {
  return api.post(`/batch-scoring-jobs/${jobId}/cancel`, {});
}

/** 只重试失败、取消或中断的条目；已成功的结果复用。 */
export function retryScoringJob(jobId) {
  return api.post(`/batch-scoring-jobs/${jobId}/retry`, {});
}

/** 移除解析失败、从未评分的材料（例如扫描件）。 */
export function removeUnparseablePaper(paperId) {
  return api.del(`/papers/${paperId}`);
}

/** 当前用户可用于评分的个人连接（只取已启用的）。 */
export async function loadActiveConnections() {
  return ((await api.get("/ai-connections")) || []).filter((item) => item.status === "active");
}

/** 任务结束后才能重试：有失败、取消或中断的条目时。 */
export function canRetryJob(job) {
  return ["completed_with_errors", "failed", "canceled"].includes(job?.status) &&
    Boolean(job?.items?.some((item) => ["failed", "canceled", "running"].includes(item.status)));
}
