// @ts-check
/**
 * 评分助手前端的展示用纯函数。
 *
 * 流程判定、论文定位、失败诊断都在后端（`backend/app/services/assistant/`，以后端为准）；
 * 这里只留界面需要的：快捷按钮、文件数上限、换评分标准时的列表、概览卡片的统计。
 */

/** 单个登录用户通过助手一次最多提交的文件数（维护者决定 U2）。 */
export const MAX_FILES = 30;

/** 欢迎页的快捷按钮：发出一句后端规则能识别的话，与自由输入走同一条路径。 */
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
 * 已发布的评分标准，最新发布的在前（换评分标准时的候选）。
 * @param {Array<{status?: string, published_at?: string|null, created_at?: string}>} rubrics
 */
export function publishedRubrics(rubrics) {
  return (rubrics || [])
    .filter((item) => item.status === "published")
    .slice()
    .sort((a, b) => String(b.published_at || b.created_at || "").localeCompare(String(a.published_at || a.created_at || "")));
}

/**
 * 批次汇总里的论文按上传顺序排列（接口按上传时间倒序返回）。“第 3 篇”按这个顺序数。
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
