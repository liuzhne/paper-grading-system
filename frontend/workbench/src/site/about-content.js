/**
 * 官网子页（`/about`）的文案与数据，取自 Claude Design 原型「有据智评 官网子页.dc.html」。
 *
 * 子页用地址里的 `#` 切换三个分页（与原型一致）：`#deploy` 院系部署、
 * `#security` 数据安全、`#docs` 帮助文档。分页内的锚点（如 `#contact`、`#keys`）
 * 也会出现在地址里，刷新后必须回到它所在的分页，而不是落回默认分页找不到锚点。
 */

import { ABOUT_PAGES } from "./links.js";

export const PAGE_TITLES = {
  deploy: "院系部署",
  security: "数据安全",
  docs: "帮助文档",
};

/** 分页内锚点 → 所在分页。 */
const ANCHOR_PAGES = {
  contact: "deploy",
  leads: "security",
  "docs-before": "docs",
  "docs-steps": "docs",
  keys: "docs",
  status: "docs",
};

/**
 * 由地址的 hash 决定显示哪个分页、要不要滚到某个锚点。
 *
 * @param {string} hash 形如 `#security`；空串或未知值回到默认分页
 * @returns {{ page: "deploy"|"security"|"docs", anchor: string|null, known: boolean }}
 */
export function resolveHash(hash) {
  const key = (hash || "").replace(/^#/, "");
  if (ABOUT_PAGES.includes(key)) {
    return { page: /** @type {"deploy"|"security"|"docs"} */ (key), anchor: null, known: true };
  }
  // 用 hasOwn 而不是 in：`#constructor`、`#toString` 会顺着原型链命中。
  if (Object.hasOwn(ANCHOR_PAGES, key)) {
    return { page: ANCHOR_PAGES[key], anchor: key, known: true };
  }
  return { page: "deploy", anchor: null, known: false };
}

const rows = (/** @type {string[]} */ values) =>
  ["数据存放", "AI 模型", "账户", "租户"].map((k, i) => ({ k, v: values[i] }));

export const PLANS = [
  {
    name: "云端试用",
    fit: "单门课程先试一个批次",
    cta: "申请试用",
    recommended: false,
    rows: rows(["有据智评托管服务器", "平台默认模型", "平台账户，邀请制", "多租户，按院系隔离"]),
  },
  {
    name: "校内私有部署",
    fit: "院系或全校正式使用",
    cta: "预约部署评估",
    recommended: true,
    rows: rows(["学校机房，材料不出校", "校方自选模型服务，如百炼平台", "对接学校统一身份认证", "单租户"]),
  },
  {
    name: "离线部署",
    fit: "无外网或有保密要求的环境",
    cta: "咨询离线方案",
    recommended: false,
    rows: rows(["学校机房，完全断网", "校内部署的模型", "本地账户或统一身份认证", "单租户"]),
  },
];

export const DEPLOY_STEPS = [
  { n: "01", t: "需求沟通", d: "确认使用的课程与材料类型、数据存放要求，以及需要对接的现有系统。", who: "院系教务 · 信息中心" },
  { n: "02", t: "环境准备", d: "校方准备服务器与模型服务，我们提供安装包和环境清单。", who: "学校信息中心" },
  { n: "03", t: "安装与对接", d: "安装系统，对接统一身份认证，导入第一套评分标准并完成发布。", who: "双方共同完成" },
  { n: "04", t: "试运行", d: "选一门课程完整走一遍评分、复核与导出，确认结果后正式启用。", who: "任课教师 · 教务" },
];

export const INTEGRATIONS = [
  {
    t: "AI 模型服务",
    d: "评分与规则起草需要一个模型服务。可以使用百炼等平台的模型，也可以接入校内部署的模型；离线部署只能使用校内模型。",
    tag: "必需",
    required: true,
  },
  {
    t: "统一身份认证",
    d: "教师用学校账户登录，权限按院系与角色分配。不对接时可以使用系统内置账户，由管理员邀请开通。",
    tag: "推荐",
    required: true,
  },
  {
    t: "教务系统",
    d: "成绩单以 Excel 导出，可按教务系统的导入模板调整列顺序。不需要系统间直连。",
    tag: "可选",
    required: false,
  },
  {
    t: "存储与备份",
    d: "材料与评分记录存放在校方提供的存储上，备份策略沿用学校现有方案。",
    tag: "必需",
    required: true,
  },
];

export const PRINCIPLES = [
  { n: "01", t: "存放位置由学校决定", d: "私有部署和离线部署时，材料、评分与复核记录全部存放在学校自己的服务器上。" },
  { n: "02", t: "AI 只给建议", d: "AI 不改写学生原文。AI 归类的段落和起草的扣分细则，都要老师确认后才生效。" },
  { n: "03", t: "每一次改分都留痕", d: "人工改分、采纳 AI 建议都会记录操作人、时间、所用模型与版本，可以追溯。" },
  { n: "04", t: "评分标准发布后不可改", d: "一个批次绑定一个已发布的标准版本，同一批材料始终用同一把尺子。" },
];

export const LIFECYCLE = [
  { n: "01", t: "上传", data: "原始材料文件（PDF / DOCX）", where: "学校存储", who: "创建批次的教师、该批次的评审教师" },
  { n: "02", t: "解析", data: "正文段落、篇章结构、格式发现", where: "学校存储", who: "同上" },
  {
    n: "03",
    t: "评分",
    data: "发送给模型的段落文本；返回的给分、证据位置与置信度",
    where: "模型服务（私有 / 离线部署时在校内）；结果存学校存储",
    who: "评审教师",
  },
  { n: "04", t: "复核", data: "改分记录、评语", where: "学校存储", who: "评审教师、教务管理员" },
  { n: "05", t: "导出", data: "HTML 报告、Excel 成绩单、结构化 JSON", where: "下载到操作人本地", who: "导出操作人" },
];

export const AI_LIMITS = [
  { allowed: true, t: "每一项给分都附原文页码和章节，可以点回原文核对" },
  { allowed: true, t: "置信度低于阈值的给分自动交给老师复核" },
  { allowed: false, t: "不改写、不生成学生的论文内容" },
  { allowed: false, t: "不自行新增评分项，也不绕过老师确认直接修改扣分细则" },
];

/**
 * 帮助文档的目录。目前只有「快速开始」这一篇；原型里其余条目是空链接，这里
 * 把能落到本页小节的条目接上锚点，其余标为「即将上线」而不是放一个点了没反应
 * 的链接。
 */
export const DOC_NAV = [
  { t: "入门", items: [{ t: "快速开始", href: "#docs", current: true }, { t: "账户与登录" }] },
  {
    t: "评分标准",
    items: [{ t: "上传评分表与标准文档" }, { t: "核对评分项" }, { t: "归类原文与扣分细则" }, { t: "发布与版本" }],
  },
  { t: "评分任务", items: [{ t: "新建任务与上传材料" }, { t: "评分工作区" }, { t: "结果复核" }] },
  { t: "输出", items: [{ t: "导出报告与成绩单" }] },
  {
    t: "参考",
    items: [{ t: "状态说明", href: "#status" }, { t: "快捷键", href: "#keys" }, { t: "常见问题" }],
  },
];

/** 五个步骤。原型的「上传要求 →」等指向尚未写成的文档，这里改为直达工作台对应页面。 */
export const DOC_STEPS = [
  {
    n: "1",
    t: "上传评分表与标准文档",
    d: "在「评分标准」新建一套标准，上传评分表和评分标准文档。评分表决定有哪些评分项、各多少分；文档提供扣分规则的原文。",
    path: "评分标准 › 新建",
    link: { label: "打开评分标准", href: "/workbench/rubrics" },
  },
  {
    n: "2",
    t: "核对评分项",
    d: "检查系统从评分表识别出的评分项与满分。满分取自文字时会标黄，逐项核对或用 AI 交叉验证后进入下一步。",
    path: "评分标准 › 第 1 步",
    link: { label: "打开评分标准", href: "/workbench/rubrics" },
  },
  {
    n: "3",
    t: "归类原文，确认扣分细则",
    d: "把文档中的要求归入对应评分项，再确认每一项的扣分细则。疑似规则必须归类，未确认的 AI 细则不会生效。确认后发布标准。",
    path: "评分标准 › 第 2、3 步",
    link: { label: "打开评分标准", href: "/workbench/rubrics" },
  },
  {
    n: "4",
    t: "新建评分任务",
    d: "选择已发布的标准版本，上传学生材料。系统先做解析预检，再逐份评分；中断后可从断点续跑。",
    path: "评分任务 › 新建",
    link: { label: "新建评分任务", href: "/workbench/tasks/new" },
  },
  {
    n: "5",
    t: "复核并导出",
    d: "处理低置信度给分，确认后导出 HTML 报告、Excel 成绩单或结构化 JSON。",
    path: "结果复核 · 输出中心",
    link: { label: "打开结果复核", href: "/workbench/review" },
  },
];

export const KEYS = [
  { k: "1 – 6", d: "把当前段落归入对应编号的评分项" },
  { k: "X", d: "标为不是规则" },
  { k: "J", d: "下一段" },
  { k: "K", d: "上一段" },
];

/** tone：ok 进行中/成功（绿）、warn 需处理（黄）、muted 静止（灰）。 */
export const STATUSES = [
  { label: "草稿", d: "已建批次，尚未开始解析", tone: "muted" },
  { label: "解析中", d: "正在提取正文与篇章结构", tone: "ok" },
  { label: "评分中", d: "按已发布标准逐份评分", tone: "ok" },
  { label: "已评分", d: "全部材料评分成功", tone: "ok" },
  { label: "已评分 · 含异常项", d: "部分材料解析失败或给分置信度过低，需要处理", tone: "warn" },
  { label: "已复核", d: "人工确认完成，可以导出", tone: "muted" },
  { label: "已归档", d: "成绩已发布，只读", tone: "muted" },
];

export const TOC = [
  { t: "开始前", anchor: "docs-before" },
  { t: "五个步骤", anchor: "docs-steps" },
  { t: "快捷键", anchor: "keys" },
  { t: "批次状态", anchor: "status" },
];
