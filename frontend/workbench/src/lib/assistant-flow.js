// @ts-check
/**
 * 评分助手前端的展示用纯函数。
 *
 * 流程判定、材料定位、失败诊断都在后端（`backend/app/services/assistant/`，以后端为准）；
 * 这里只留界面需要的：快捷按钮、文件数上限、换评分标准时的列表、概览卡片的统计、计划卡片的进度。
 */

import { isWorkbenchType } from "./document-types.js";

/** 单个登录用户通过助手一次最多提交的文件数（维护者决定 U2）。 */
export const MAX_FILES = 30;

/**
 * 欢迎页的快捷按钮。发出的消息带 `quick_action`（这里的 intent），后端按键直接分派、不经规则与模型；
 * 键名与后端 `services/assistant/views.QUICK_ACTIONS`、接口的 `quick_action` 取值一致。
 */
export const QUICK_ACTIONS = [
  { intent: "start_grading", label: "开始评分", text: "开始评分" },
  { intent: "import_rubric", label: "用我的评分规则和模板", text: "上传我的评分规则和模板" },
  { intent: "query_progress", label: "评分进度", text: "评分进度" },
  { intent: "query_paper", label: "查某篇的扣分", text: "查询扣分" },
  { intent: "query_review", label: "需要复核的", text: "哪些需要复核" },
  { intent: "query_overview", label: "整体情况", text: "整体分数情况" },
];

export const TERMINAL_JOB_STATUSES = new Set(["completed", "completed_with_errors", "failed", "canceled"]);

/**
 * 超过上限只接受前 MAX_FILES 份。
 * @template T
 * @param {ArrayLike<T>|Iterable<T>|null|undefined} files
 * @returns {{accepted: T[], dropped: number}}
 */
export function limitFiles(files) {
  const list = Array.from(files || []);
  return { accepted: list.slice(0, MAX_FILES), dropped: Math.max(0, list.length - MAX_FILES) };
}

/**
 * 已发布、且工作台能评的评分标准，最新发布的在前（换评分标准时的候选）。
 * @param {Array<{status?: string, published_at?: string|null, created_at?: string, business_profile_key?: string|null}>} rubrics
 */
export function publishedRubrics(rubrics) {
  return (rubrics || [])
    // 只列工作台能评的类型：技术方案建任务会被拒绝（通用文档评分改造方案 G5）。
    .filter((item) => item.status === "published" && isWorkbenchType(item.business_profile_key))
    .slice()
    .sort((a, b) => String(b.published_at || b.created_at || "").localeCompare(String(a.published_at || a.created_at || "")));
}

/**
 * 批次汇总里的材料按上传顺序排列（接口按上传时间倒序返回）。“第 3 份”按这个顺序数。
 * @template T
 * @param {T[]} papers
 * @returns {T[]}
 */
export function papersInUploadOrder(papers) {
  return (papers || []).slice().reverse();
}

/**
 * 分数概览：只用批次汇总里的现成分数，不经模型（B8）。与后端 `views.score_overview` 同口径。
 * @param {Array<{latest_final_score?: number|null, latest_need_manual_review?: boolean|null}>} papers
 */
export function scoreOverview(papers) {
  const scores = (papers || [])
    .map((paper) => paper.latest_final_score)
    .filter((value) => typeof value === "number");
  const round = (/** @type {number} */ value) => Math.round(value * 100) / 100;
  return {
    total: (papers || []).length,
    scored: scores.length,
    average: scores.length ? round(scores.reduce((sum, value) => sum + value, 0) / scores.length) : null,
    max: scores.length ? Math.max(...scores) : null,
    min: scores.length ? Math.min(...scores) : null,
    needReview: (papers || []).filter((paper) => paper.latest_need_manual_review).length,
  };
}

/** 计划卡片的步骤记号与读屏文字（docs/评分助手计划卡片方案.md §4）。 */
export const PLAN_STEP_MARKS = {
  done: { mark: "✓", text: "完成" },
  current: { mark: "●", text: "进行中" },
  todo: { mark: "○", text: "待做" },
  blocked: { mark: "!", text: "等你决定" },
  failed: { mark: "✕", text: "没完成" },
  skipped: { mark: "–", text: "跳过" },
};

/** 计划卡片自身的状态文字。 */
export const PLAN_STATUS_TEXT = {
  running: "进行中", done: "已完成", failed: "未完成", dismissed: "已放弃", expired: "已过期", ended: "已结束",
};

/**
 * 进行中的计划：最近一张状态为 running 的计划卡片，连同它所在的消息。没有时返回 null。
 * @param {Array<{id: string, cards?: Array<{type?: string, status?: string}>}>} messages
 */
export function activePlan(messages) {
  for (let index = (messages || []).length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    const card = (message.cards || []).find((item) => item.type === "plan");
    if (card) return card.status === "running" ? { message, card } : null;
  }
  return null;
}

/**
 * “第 3/7 步 · 选择待评分文件”：标题下的一行进度。等你决定的那一步写明。
 * @param {{steps?: Array<{label: string, status: string}>}} card
 */
export function planProgressText(card) {
  const steps = card?.steps || [];
  const index = steps.findIndex((step) => step.status === "blocked" || step.status === "current");
  if (index < 0) return "";
  const step = steps[index];
  const suffix = step.status === "blocked" ? "（等你决定）" : "";
  return `第 ${index + 1}/${steps.length} 步 · ${step.label}${suffix}`;
}
