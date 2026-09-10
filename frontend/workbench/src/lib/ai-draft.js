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
 * 映射关系照旧版模板中心的实现（`frontend/web/assets/app.js`）——那条链路是验证过
 * 能编译通过的，换一套字段名等于重新赌一次。
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
        index,
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
      // 组级的互斥标识与上限必须跟着走：丢了它们，同一个问题的轻微/中等/严重
      // 三档会同时命中，一处毛病被扣三次。
      cap_points: row.capPoints,
      mutex_group: row.mutexGroup,
      source: row.source,
      source_refs: row.sourceRefs,
      generation_fingerprint: row.fingerprint,
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
    const rules = byCode.get(criterion.code);
    // 一条都没确认时保持原样。改成 deductive 却没有任何规则，评分时这一项恒得
    // 满分——比不改更糟，而且看起来像是配置生效了。
    if (!rules || !rules.length) return criterion;
    return {
      ...criterion,
      scoring_mode: "deductive",
      deduction_rules_structured: rules,
    };
  });
}
