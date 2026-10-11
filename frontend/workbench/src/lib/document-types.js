/**
 * 文档类型（评分标准版本的 `business_profile_key`）在工作台里的叫法与显示规则。
 *
 * 类型只在导入评分标准时选一次，之后批次、评分页、复核页、导出都按评分标准推出
 * （通用文档评分改造方案 §3）。这里只管“怎么显示”，评分逻辑全在后端。
 */

export const DEFAULT_DOCUMENT_TYPE = "thesis";

/** 工作台能评的类型，顺序即导入页的选项顺序。 */
export const DOCUMENT_TYPES = Object.freeze([
  { key: "thesis", label: "毕业论文", hint: "学号、姓名、学院" },
  { key: "general_document", label: "通用文档", hint: "文件名、标题、类别" },
]);

const BY_KEY = Object.fromEntries(DOCUMENT_TYPES.map((item) => [item.key, item]));
/** 工作台评不了、但列表里可能出现的类型，只用于显示名称。 */
const OTHER_LABELS = { technical_proposal: "技术方案" };

/** 未版本化的旧标准没有类型记录（null），按论文处理。 */
export function documentTypeKey(value) {
  return value || DEFAULT_DOCUMENT_TYPE;
}

export function isWorkbenchType(value) {
  return Boolean(BY_KEY[documentTypeKey(value)]);
}

export function documentTypeLabel(value) {
  const key = documentTypeKey(value);
  return BY_KEY[key]?.label || OTHER_LABELS[key] || key;
}

export function isGeneralDocument(value) {
  return documentTypeKey(value) === "general_document";
}

function fileStem(fileName) {
  const name = String(fileName || "");
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

/**
 * 一份材料在列表与顶栏里的两行：`code` 是等宽小字，`title` 是主标题。
 * 论文：学号 / 题目或姓名；通用文档：文件名 / 标题（没有就用文件名去掉扩展名）。
 * @param {{student_id?: string|null, student_name?: string|null, title?: string|null, file_name?: string|null}|null|undefined} paper
 * @param {string|null|undefined} type
 * @param {string} [emptyTitle] 既没有标题也没有姓名时的占位
 */
export function materialIdentity(paper, type, emptyTitle = "未命名材料") {
  if (!paper) return { code: "—", title: emptyTitle };
  if (isGeneralDocument(type)) {
    return { code: paper.file_name || "—", title: paper.title || fileStem(paper.file_name) || emptyTitle };
  }
  return { code: paper.student_id || "—", title: paper.title || paper.student_name || emptyTitle };
}

/** 复核表“材料”列：论文显示学号，通用文档显示文件名。 */
export function materialCode(entry, type) {
  return (isGeneralDocument(type) ? entry?.file_name : entry?.student_id) || "—";
}

const REMEMBER_KEY = "pgs.documentType";

/** 导入页默认选中上次的类型；读不到（隐私模式、被禁用）就用论文。 */
export function rememberedDocumentType() {
  try {
    const value = globalThis.localStorage.getItem(REMEMBER_KEY);
    return BY_KEY[value] ? value : DEFAULT_DOCUMENT_TYPE;
  } catch {
    return DEFAULT_DOCUMENT_TYPE;
  }
}

export function rememberDocumentType(value) {
  try {
    if (BY_KEY[value]) globalThis.localStorage.setItem(REMEMBER_KEY, value);
  } catch {
    // 存不下只影响下次的默认值。
  }
}
