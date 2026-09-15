// @ts-check
/**
 * AI 起草结果 → 可执行扣分规则（V3-2）。
 *
 * `POST /rubrics/{id}/draft-deduction-rules` 的后端 docstring 写得很明确：
 * **non-persistent**。它只返回待人工确认的建议，一条也不落库。要让这些规则真正
 * 参与评分，必须由前端合进评分项，再走 `POST /rubrics/{id}/recompile` 生成新的
 * 执行草稿。
 *
 * 上一版前端把起草结果存进 store 就再没用过：用户点「生成全部缺失细则」、等一次
 * 真实模型调用、然后页面纹丝不动，阻断项一个没少。这个模块补的正是那一段。
 *
 * 规则组上限使用 group_cap_points 保留；cap_points 专用于单条累计扣分。
 */

/** @type {Record<string, string>} */
export const SEVERITY_LABEL = {
  minor: "轻微",
  moderate: "中等",
  severe: "严重",
};

/** @type {Record<string, string>} */
const SOURCE_LABEL = {
  user_text: "原文",
  excel: "原文",
  ai_inferred: "AI 起草",
};

/**
 * 把嵌套的规则组摊平成表格行。
 *
 * @param {any} draft 单个评分项的起草结果
 * @returns {Array<any>}
 */
export function draftRows(draft) {
  const rows = [];
  for (const group of draft?.rule_groups || []) {
    const rules = group?.rules || [];
    for (let index = 0; index < rules.length; index += 1) {
      const rule = rules[index];
      rows.push({
        criterionCode: draft?.criterion_code || "",
        groupCode: group?.group_code || "",
        issue: group?.issue || group?.group_code || "",
        mutexGroup: group?.mutex_group ?? null,
        capPoints: group?.cap_points ?? null,
        index: rule?.draft_index ?? index,
        severity: rule?.severity || "",
        // 未知的严重程度原样透出：显示成空白等于把一条真实存在的规则藏起来。
        severityLabel: SEVERITY_LABEL[rule?.severity] || rule?.severity || "—",
        trigger: rule?.trigger || "",
        points: rule?.points ?? null,
        reason: rule?.reason || "",
        repeatPolicy: rule?.repeat_policy || "once",
        source: rule?.source || "ai_inferred",
        sourceLabel: SOURCE_LABEL[rule?.source] || "AI 起草",
        sourceRefs: rule?.source_refs || [],
        fingerprint: draft?.generation_metadata?.fingerprint ?? null,
      });
    }
  }
  return rows;
}

/**
 * 行的稳定标识。组内序号单独用不行——两个规则组的第 0 条会撞。
 *
 * @param {any} row
 * @returns {string}
 */
export function rowKey(row) {
  return `${row.criterionCode}::${row.groupCode}::${row.index}`;
}

/**
 * 未被排除的行，转成编译器认识的结构化规则。
 *
 * @param {any} draft
 * @param {Set<string>} excluded 被用户排除的行键
 * @returns {Array<any>}
 */
export function confirmedStructuredRules(draft, excluded) {
  return draftRows(draft)
    .filter((row) => !excluded.has(rowKey(row)))
    .map((row, order) => ({
      // 编译器按 `match` 匹配，界面按 `trigger` 显示。两个都给，避免其中一侧
      // 读到 undefined 时静默退化成「永不命中」。
      match: row.trigger,
      trigger: row.trigger,
      points: row.points,
      reason: row.reason,
      severity: row.severity,
      repeat_policy: row.repeatPolicy,
      // 组上限与单条重复扣分上限是不同字段；后端验证单次、互斥及分值范围。
      group_cap_points: row.capPoints,
      cap_points: null,
      mutex_group: row.mutexGroup,
      source: row.source,
      source_refs: row.sourceRefs,
      generation_fingerprint: row.fingerprint,
      generation_metadata: draft.generation_metadata || null,
      draft_row_key: rowKey(row),
      confirmed: true,
      display_order: order,
    }));
}

/**
 * 把确认后的规则合进评分项列表，产出可直接提交 `recompile` 的 criteria。
 *
 * @param {Array<any>} criteria 来自 `GET /rubrics/{id}` 的完整评分项
 * @param {Array<{criterion_code: string, draft: any}>} items 起草结果
 * @param {Set<string>} excluded 被排除的行键
 * @returns {Array<any>}
 */
export function mergeConfirmedDrafts(criteria, items, excluded) {
  const byCode = new Map();
  for (const item of items || []) {
    if (!item?.draft) continue;
    byCode.set(item.criterion_code, confirmedStructuredRules(item.draft, excluded));
  }

  return (criteria || []).map((criterion) => {
    /** @type {Array<any>|undefined} */
    const rules = byCode.get(criterion.code);
    /** @type {Array<any>} */
    const existing = criterion.deduction_rules_structured || [];
    // 一条都没确认时保持原样。改成 deductive 却没有任何规则，评分时这一项恒得
    // 满分——比不改更糟，而且看起来像是配置生效了。
    if (!rules || !rules.length) return criterion;
    return {
      ...criterion,
      scoring_mode: "deductive",
      deduction_rules_structured: [
        ...existing,
        ...rules.filter((rule) => !existing.some((old) =>
          old.draft_row_key === rule.draft_row_key && old.generation_fingerprint === rule.generation_fingerprint)),
      ],
    };
  });
}
