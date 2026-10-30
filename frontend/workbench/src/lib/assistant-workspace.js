// @ts-check
/**
 * 评分助手 v2 工作区的纯函数（docs/评分助手v2界面改造方案.md §3）。
 *
 * 工作区不另存状态：**任务标签 = 最近一张计划卡片**（步骤与状态由后端每走一步更新），
 * **结果标签 = 本会话消息里的结果卡片**。都从消息算出来，刷新页面后能原样重建。
 * 这里只放界面要的推导：步骤对应哪个视图、步骤条、标签放得下几个、任务条、建议问题、待办、会话列表的一行说明。
 */

/**
 * 计划步骤（后端 `assistant/plan.STEPS` 的键）→ 步骤条上的短名、视图标题与说明、用哪个视图。
 * 选文件、上传预检、确认开始共用“待评分文件”视图。
 */
/** @type {Record<string, {short: string, title: string, view: string, desc: string}>} */
export const STEPS = {
  rubric: { short: "评分标准", title: "评分标准", view: "rubric", desc: "本次评分使用的评分标准。选文件之前可以换一份。" },
  import: { short: "导入标准", title: "来源文件解析", view: "source", desc: "评分表决定评分项和分值，评分标准文档提供规则原文。" },
  publish: { short: "核对发布", title: "核对评分规则", view: "review", desc: "逐项确认扣分细则，全部确认后发布。确认结果即时保存。" },
  model: { short: "评分模型", title: "评分模型", view: "model", desc: "本次评分使用的模型。" },
  files: { short: "选择文件", title: "待评分文件", view: "files", desc: "选好后自动上传并预检：文字能否读取、能否评分。" },
  upload: { short: "上传预检", title: "待评分文件", view: "files", desc: "上传后自动预检：文字能否读取、能否评分。" },
  confirm: { short: "确认开始", title: "待评分文件", view: "files", desc: "预检结果在下面，确认后开始评分。" },
  scoring: { short: "评分", title: "评分进度", view: "grading", desc: "每评完一份实时更新。" },
  report: { short: "汇报结果", title: "评分结果汇报", view: "report", desc: "本次评分的结果。" },
};

/** 等老师动手的中断（与后端 `flow.USER_WAIT_KINDS` 一致）：步骤显示“待你处理”。 */
export const USER_WAIT_KINDS = new Set(["pick_files", "confirm_start", "confirm_remove", "create_import_session", "wait_publish"]);

/** 流程卡片 → 计划步骤。新卡片带 `step`；旧会话里的卡片没有，按类型推断。 */
/** @type {Record<string, string>} */
const ENTRY_STEPS = {
  pick_files: "files", upload_papers: "upload", confirm_start: "confirm",
  pick_rubric_files: "import", rubric_wait: "publish",
};

/**
 * 只在流程里出现、显示成“在右侧查看”入口的卡片类型（带 `step` 的结果卡片也算）。
 * @param {any} card
 * @returns {string | null}
 */
export function entryStep(card) {
  if (!card) return null;
  if (card.step && STEPS[card.step]) return card.step;
  return ENTRY_STEPS[card.type] || null;
}

/** 结果卡片 → 结果标签的种类。流程里的（带 `step`）是任务标签的入口，不另开标签。 */
/** @type {Record<string, string>} */
const ARTIFACT_KINDS = {
  progress_snapshot: "progress", job_progress: "progress", paper_result: "explain",
  overview: "result", review_list: "result", todo: "todo",
};

/** 标签上的小色块：任务绿、进展灰、解释蓝、建议黄、待办灰；“结果”是设计稿之外加的一种（§3.2）。 */
/** @type {Record<string, {tag: string, tone: string}>} */
export const TAB_KINDS = {
  task: { tag: "任务", tone: "accent" },
  progress: { tag: "进展", tone: "neutral" },
  explain: { tag: "解释", tone: "info" },
  advice: { tag: "建议", tone: "warn" },
  todo: { tag: "待办", tone: "neutral" },
  result: { tag: "结果", tone: "accent" },
};

/** @type {Record<string, string>} */
const FIXED_TITLES = {
  progress_snapshot: "任务进展", job_progress: "评分进度", overview: "分数概览", review_list: "复核清单", todo: "下一步",
};

/**
 * 最近一张计划卡片与它所在的消息（不论状态）。没有时返回 null。
 * @param {Array<{id: string, cards?: any[]}>} messages
 */
export function latestPlan(messages) {
  for (let index = (messages || []).length - 1; index >= 0; index -= 1) {
    const card = (messages[index].cards || []).find((item) => item.type === "plan");
    if (card) return { message: messages[index], card, index };
  }
  return null;
}

/**
 * 本次流程的上下文：计划卡片之后的流程卡片里带的编号（任务、作业、导入会话、评分标准）。
 * 焦点会被查询改掉（问别的任务时），任务标签按流程自己的卡片取，不看焦点。
 * @param {Array<{id: string, cards?: any[]}>} messages
 */
export function flowContext(messages) {
  const plan = latestPlan(messages);
  /** @type {Record<string, any>} */
  const context = { plan: plan?.card || null, cards: {} };
  if (!plan) return context;
  for (const message of messages.slice(plan.index)) {
    for (const card of message.cards || []) {
      const step = entryStep(card);
      if (!step && card.type !== "diagnosis") continue;
      context.cards[card.type] = card;
      if (card.batch_id) context.batchId = card.batch_id;
      if (card.job_id) context.jobId = card.job_id;
      if (card.rubric_id) context.rubricId = card.rubric_id;
      if (card.import_session_id) context.importSessionId = card.import_session_id;
      if (card.result?.import_session_id) context.importSessionId = card.result.import_session_id;
      if (card.result?.rubric_id) context.rubricId = card.result.rubric_id;
    }
  }
  return context;
}

/**
 * 当前步骤：第一步没完成（也没跳过）的；都完成了返回 null。
 * @param {Array<{key: string, status: string}>} steps
 */
export function currentStep(steps) {
  return (steps || []).find((step) => !["done", "skipped"].includes(step.status)) || null;
}

/**
 * 步骤能不能点开：完成的、当前的、没完成的（看原因）可以；还没到的不行。
 * @param {{status: string}} step
 */
export function stepReachable(step) {
  return ["done", "current", "blocked", "failed"].includes(step.status);
}

/**
 * 工作区正在显示的步骤：老师点开的那一步（要能点开），否则跟着当前步骤；都完成了停在最后一步。
 * @param {{steps?: Array<{key: string, status: string}>} | null} plan
 * @param {string | null} requested
 */
export function viewStepKey(plan, requested) {
  const steps = plan?.steps || [];
  const chosen = requested && steps.find((step) => step.key === requested);
  if (chosen && stepReachable(chosen)) return chosen.key;
  return (currentStep(steps) || steps[steps.length - 1])?.key || null;
}

/**
 * 正在看的步骤和当前步骤不是同一个视图：顶部提示“正在查看已完成的步骤 · 回到当前”。
 * @param {{status?: string, steps?: Array<{key: string, status: string}>} | null} plan
 * @param {string | null} viewKey
 */
export function viewingPast(plan, viewKey) {
  if (!plan || plan.status !== "running") return false;
  const current = currentStep(plan.steps || []);
  return Boolean(current && viewKey && STEPS[viewKey]?.view !== STEPS[current.key]?.view);
}

/**
 * 步骤条。宽度不够时只给正在看的那一步显示名字（`showLabel`），并让它占更宽。
 * @param {Array<{key: string, label: string, status: string}>} steps
 * @param {string | null} viewKey
 * @param {boolean} showLabels
 */
export function stepper(steps, viewKey, showLabels) {
  return (steps || []).map((step, index) => ({
    key: step.key,
    num: String(index + 1).padStart(2, "0"),
    short: STEPS[step.key]?.short || step.label,
    full: step.label,
    status: step.status,
    isView: step.key === viewKey,
    reachable: stepReachable(step),
    showLabel: showLabels || step.key === viewKey,
    wide: !showLabels && step.key === viewKey,
  }));
}

/**
 * 视图标题上方的一行：“步骤 2 / 8 · 待你处理”。
 * @param {{steps?: Array<{key: string, status: string}>} | null} plan
 * @param {string | null} viewKey
 * @param {string | null | undefined} pendingKind
 */
export function stepEyebrow(plan, viewKey, pendingKind) {
  const steps = plan?.steps || [];
  const index = steps.findIndex((step) => step.key === viewKey);
  if (index < 0) return { text: "", tone: "faint" };
  const step = steps[index];
  const head = `步骤 ${index + 1} / ${steps.length}`;
  if (step.status === "done") return { text: `${head} · 已完成`, tone: "accent" };
  if (step.status === "failed") return { text: `${head} · 没完成`, tone: "danger" };
  if (step.status === "skipped") return { text: `${head} · 已跳过`, tone: "faint" };
  if (step.status === "blocked" || (step.status === "current" && USER_WAIT_KINDS.has(pendingKind || ""))) {
    return { text: `${head} · 待你处理`, tone: "warn" };
  }
  if (step.status === "current") return { text: `${head} · 进行中`, tone: "warn" };
  return { text: head, tone: "faint" };
}

/**
 * 结果标签：本会话里不在流程中的结果卡片，最新的在前。标题取提问那句话（解释类），其余用固定名称。
 * @param {Array<{id: string, role: string, text?: string, created_at?: string, cards?: any[]}>} messages
 */
export function artifactsFrom(messages) {
  const list = [];
  let question = "";
  for (const message of messages || []) {
    if (message.role === "user") {
      question = (message.text || "").trim();
      continue;
    }
    for (const card of message.cards || []) {
      const kind = ARTIFACT_KINDS[card.type];
      if (!kind || card.step) continue;
      const title = card.type === "paper_result" ? truncate(question, 24) || "得分解释" : FIXED_TITLES[card.type];
      list.push({ id: card.id, kind, card, message, title, time: message.created_at });
    }
  }
  return list.reverse();
}

/** @param {string} text @param {number} max */
function truncate(text, max) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/**
 * 标签放得下几个：按工作区宽度，每个约 170px，右侧按钮约 170px，至少 2 个。
 * 放不下的收进“更多”；正在看的那个总在外面。
 * @param {string[]} ids 任务标签在前，结果标签最新的在前
 * @param {string | null} activeId
 * @param {number} width
 */
export function tabLayout(ids, activeId, width) {
  const cap = Math.max(2, Math.floor(((width || WS_DEFAULT) - 170) / 170));
  if (ids.length <= cap) return { visible: ids.slice(), more: [] };
  const visible = ids.slice(0, cap - 1);
  const rest = ids.slice(cap - 1);
  const index = activeId ? rest.indexOf(activeId) : -1;
  visible.push(index >= 0 ? rest.splice(index, 1)[0] : /** @type {string} */ (rest.shift()));
  return { visible, more: rest };
}

export const WS_MIN = 400;
export const WS_DEFAULT = 560;
/** 对话区至少留的宽度。 */
export const CHAT_MIN = 400;

/**
 * 工作区宽度：400 到“总宽 − 400”，没存过就是 560。
 * @param {number | null | undefined} width
 * @param {number} mainWidth
 */
export function clampWorkspace(width, mainWidth) {
  const max = Math.max(WS_MIN, (mainWidth || 0) - CHAT_MIN);
  return Math.max(WS_MIN, Math.min(width ?? WS_DEFAULT, max));
}

/**
 * 输入框上方的任务条：等老师时“待你处理”+ 主动作，后台进行时“进行中”+ 进度。没有进行中的流程时返回 null。
 * 工作区已经显示这一步时，按钮换成一句提示（设计稿：已在右侧打开）。
 * @param {{status?: string, steps?: any[]} | null} plan
 * @param {{kind?: string} | null} pending
 * @param {{ visibleStep?: string | null, confirm?: any, job?: any, upload?: {done: number, total: number} | null }} [extra]
 */
export function taskBar(plan, pending, extra = {}) {
  if (!plan || plan.status !== "running" || !pending) return null;
  const visible = (/** @type {string} */ step) => Boolean(extra.visibleStep) && STEPS[extra.visibleStep || ""]?.view === STEPS[step]?.view;
  /** @param {string} text @param {any} action @param {string} step */
  const wait = (text, action, step) => ({
    tone: "wait", label: "待你处理", text, step,
    action: visible(step) ? null : action, note: visible(step) ? "已在右侧打开" : "",
  });
  /** @param {string} text @param {number | null} pct @param {string} step */
  const run = (text, pct, step) => ({ tone: "run", label: "进行中", text, pct, step, action: null, note: "" });
  switch (pending.kind) {
    case "create_import_session":
      return wait("上传评分规则和模板", { kind: "open", step: "import", label: "去上传" }, "import");
    case "wait_publish":
      return wait("核对并发布评分标准", { kind: "open", step: "publish", label: "继续核对" }, "publish");
    case "pick_files":
      return wait("选择待评分的文件", { kind: "pick", label: "选择文件" }, "files");
    case "upload_papers": {
      const upload = extra.upload;
      const text = upload?.total ? `上传与预检 ${upload.done}/${upload.total}` : "上传与预检";
      return run(text, upload?.total ? Math.round((upload.done / upload.total) * 100) : null, "upload");
    }
    case "confirm_start": {
      const card = extra.confirm;
      const ready = card?.ready_count ?? card?.file_count;
      const parts = [`确认开始评分 · ${ready ?? "—"} 份`];
      if (card?.removable?.length) parts.push(`${card.removable.length} 份无法评分`);
      if (card?.warnings?.length) parts.push(`${card.warnings.length} 份需注意`);
      return wait(parts.join("，"), { kind: "confirm", label: "开始评分" }, "confirm");
    }
    case "confirm_remove": // 旧流程留下的“是否移除”，按钮在对话里那张卡片上
      return { tone: "wait", label: "待你处理", text: "有文件无法评分，在对话里的卡片上决定是否移除", step: "upload", action: null, note: "" };
    case "wait_job": {
      const job = extra.job;
      if (!job?.total_items) return run("正在评分", null, "scoring");
      const done = finished(job);
      return run(`正在评分 ${done}/${job.total_items}`, Math.round((done / job.total_items) * 100), "scoring");
    }
    default:
      return null;
  }
}

/** @param {any} job */
function finished(job) {
  return (job?.succeeded_count || 0) + (job?.skipped_count || 0) + (job?.failed_count || 0) + (job?.canceled_count || 0);
}

/**
 * 建议问题：随步骤变化，发出时带 `quick_action`（后端按键直接分派，不经规则与模型）。
 * @param {{status?: string} | null} plan
 */
export function chipsFor(plan) {
  if (!plan) return [];
  if (plan.status === "done") {
    return [
      { label: "哪些需要复核？", text: "哪些需要复核", intent: "query_review" },
      { label: "现在进展到哪了？", text: "现在进展到哪了？", intent: "query_progress" },
    ];
  }
  if (plan.status !== "running") return [];
  return [
    { label: "现在进展到哪了？", text: "现在进展到哪了？", intent: "query_progress" },
    { label: "接下来我要做什么？", text: "接下来我要做什么？", intent: "next_steps" },
  ];
}

/** 待办里每一步怎么叫（只列老师要做的，加上最后查看结果）。 */
/** @type {Record<string, string>} */
const TODO_LABELS = {
  import: "上传评分规则和模板",
  publish: "核对并发布评分标准",
  model: "配置评分模型",
  files: "上传待评分的文件",
  confirm: "确认开始评分",
  report: "查看评分结果",
};

/**
 * 待办清单：按计划实时算，完成一项自动勾选；只有排在最前、而且现在就能做的那一项有按钮。
 * @param {{steps?: Array<{key: string, status: string, detail?: string|null}>} | null} plan
 * @param {{kind?: string} | null} pending
 */
export function todoItems(plan, pending) {
  // 评分模型通常一开始就就绪；只有缺模型时才列成一项。
  const items = (plan?.steps || [])
    .filter((step) => TODO_LABELS[step.key] && !(step.key === "model" && step.status === "done"))
    .map((step) => ({ key: step.key, label: TODO_LABELS[step.key], done: step.status === "done",
      skipped: step.status === "skipped", failed: step.status === "failed", detail: step.detail || "" }));
  const nextIndex = items.findIndex((item) => !item.done && !item.skipped);
  return items.map((item, index) => {
    const isNext = index === nextIndex;
    return { ...item, isNext, action: isNext ? todoAction(item.key, pending) : null };
  });
}

/** @param {string} key @param {{kind?: string} | null} pending */
function todoAction(key, pending) {
  const kind = pending?.kind;
  if (key === "import" && kind === "create_import_session") return { kind: "open", step: "import", label: "去上传" };
  if (key === "publish" && kind === "wait_publish") return { kind: "open", step: "publish", label: "去核对" };
  if (key === "files" && kind === "pick_files") return { kind: "pick", label: "选择文件" };
  if (key === "confirm" && kind === "confirm_start") return { kind: "confirm", label: "开始评分" };
  if (key === "model") return { kind: "link", path: "/account", label: "去配置" };
  return null;
}

/** @type {Record<string, string>} */
const PLAN_STATUS_TEXT = { done: "已完成", failed: "未完成", dismissed: "已放弃", expired: "已过期", ended: "已结束" };

/**
 * 会话列表里标题下面那一行：“步骤 2/8 · 今天 10:21”“已完成 · 10月24日”。进度来自 `focus.progress`。
 * @param {{updated_at?: string, focus?: {progress?: {status: string, step: number, total: number}}}} item
 * @param {Date} [now]
 */
export function conversationMeta(item, now = new Date()) {
  const when = dayText(item.updated_at, now);
  const progress = item.focus?.progress;
  if (!progress) return when;
  const head = progress.status === "running" ? `步骤 ${progress.step}/${progress.total}` : PLAN_STATUS_TEXT[progress.status] || "";
  return [head, when].filter(Boolean).join(" · ");
}

/**
 * 后端返回不带时区的 UTC 时间；按 UTC 解析。
 * @param {string | null | undefined} value
 */
export function parseServerTime(value) {
  const raw = String(value || "");
  if (!raw) return null;
  const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(raw) ? raw : `${raw}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * “10:21”。
 * @param {string | null | undefined} value
 */
export function clockText(value) {
  const date = parseServerTime(value);
  if (!date) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/**
 * 今天的显示“今天 10:21”，昨天“昨天 10:21”，更早“10月24日”。
 * @param {string | null | undefined} value
 * @param {Date} [now]
 */
export function dayText(value, now = new Date()) {
  const date = parseServerTime(value);
  if (!date) return "";
  const startOf = (/** @type {Date} */ day) => new Date(day.getFullYear(), day.getMonth(), day.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(date)) / 86400000);
  if (days === 0) return `今天 ${clockText(value)}`;
  if (days === 1) return `昨天 ${clockText(value)}`;
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

/**
 * 分数分布（汇报页）：五档，条长按最多的那档算。
 * @param {Array<number|null|undefined>} scores 百分制总分
 */
export function scoreBands(scores) {
  const values = (scores || []).filter((value) => typeof value === "number");
  const bands = [
    ["90–100", 90, Infinity], ["80–89", 80, 90], ["70–79", 70, 80], ["60–69", 60, 70], ["< 60", -Infinity, 60],
  ].map(([label, low, high]) => ({ label, n: values.filter((value) => value >= Number(low) && value < Number(high)).length }));
  const max = Math.max(0, ...bands.map((band) => band.n));
  return bands.map((band) => ({ ...band, pct: max ? Math.round((band.n / max) * 100) : 0 }));
}

/**
 * 文件扩展名色块：Word 蓝、Excel 绿、PDF 红，其它灰。
 * @param {string | null | undefined} name
 */
export function fileBadge(name) {
  const ext = String(name || "").split(".").pop()?.toUpperCase() || "";
  /** @type {Record<string, string>} */
  const tones = { DOCX: "word", DOC: "word", XLSX: "excel", XLS: "excel", PDF: "pdf" };
  const tone = tones[ext] || "neutral";
  return { ext: ext.slice(0, 4) || "文件", tone };
}
