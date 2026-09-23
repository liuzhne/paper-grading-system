// @ts-check
/**
 * 解析台账的前端纯逻辑（解析重构方案 §8）。
 *
 * - 未处理规则、双文件冲突与结构建议统一属于第 1 步解析核对；
 *   未处理的阻断单元或来源冲突不能进入第 2 步，发布时后端仍统一兜底校验
 *   `unresolved_source_units`。
 * - LLM 结构建议的合入规则与后端 `merge_plan` 一致：新增 / 补全一键合入，
 *   修改须逐条确认（未确认的保留当前值），冲突须排除，移除已有评分项一律阻断。
 */

/**
 * @param {any} state `GET /rubrics/{id}/parse-coverage` 的结果
 * @returns {{blocked: boolean, blocking: number, conflicts: number}}
 */
export function stepOneGate(state) {
  if (!state?.has_ledger) return { blocked: false, blocking: 0, conflicts: 0 };
  const blocking = Number(state.coverage?.blocking_count || 0);
  const conflicts = (state.conflicts || []).filter((/** @type {{resolved?: boolean}} */ item) => !item.resolved).length;
  return { blocked: blocking + conflicts > 0, blocking, conflicts };
}

/**
 * @param {{name: string}|null} rulesFile Excel 评分表
 * @param {{name: string}|null} documentFile Word 评分标准文档
 * @returns {string|null}
 */
export function importFilesError(rulesFile, documentFile) {
  if (!rulesFile && !documentFile) return "请至少上传一份评分标准文件（Word 或 Excel）。";
  if (rulesFile && !/\.(xlsx|xlsm)$/i.test(rulesFile.name)) return "评分表需为 .xlsx 或 .xlsm 文件。";
  if (documentFile && !/\.docx$/i.test(documentFile.name)) return "评分标准文档需为 .docx 文件。";
  return null;
}

/**
 * 提示用户是否使用 LLM 时展示：原因与将发送的单元数量（不自动触发）。
 * @param {Array<{code: string, message: string, unit_ids?: string[]}>} triggers
 */
export function triggerSummary(triggers) {
  const units = new Set((triggers || []).flatMap((item) => item.unit_ids || []));
  return {
    codes: (triggers || []).map((item) => item.code),
    messages: (triggers || []).map((item) => item.message),
    unitCount: units.size,
  };
}

/** @param {any[]} items */
export function diffGroups(items) {
  const list = items || [];
  return {
    oneClick: list.filter((item) => item.kind === "new" || item.kind === "fill"),
    confirmable: list.filter((item) => item.kind === "modify"),
    conflicts: list.filter((item) => item.kind === "conflict"),
    removed: list.filter((item) => item.kind === "removed"),
  };
}

/**
 * @param {any[]} items
 * @param {Set<string>} confirm 已逐条确认的修改
 * @param {Set<string>} exclude 排除的新增 / 补全 / 冲突
 */
export function mergeSelection(items, confirm, exclude) {
  const blocked = [];
  const keptCurrent = [];
  for (const item of items || []) {
    if (item.kind === "removed") blocked.push(item.id);
    else if (item.kind === "conflict" && !exclude.has(item.id)) blocked.push(item.id);
    else if (item.kind === "modify" && !confirm.has(item.id)) keptCurrent.push(item.id);
  }
  const ids = new Set((items || []).map((item) => item.id));
  return {
    blocked,
    keptCurrent,
    payload: {
      confirm: [...confirm].filter((id) => ids.has(id)),
      exclude: [...exclude].filter((id) => ids.has(id)),
    },
  };
}

/**
 * @param {any} state parse-coverage 结果
 * @param {string} unitId
 */
export function suggestionFor(state, unitId) {
  const classifications = state?.unit_classifications;
  if (!classifications || classifications.stale) return null;
  return (classifications.results || []).find((/** @type {{unit_id: string}} */ item) => item.unit_id === unitId) || null;
}
