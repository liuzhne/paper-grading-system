/**
 * 评分项的扣分理由（评分助手 Agent 化方案 §31.3）。
 *
 * Core 评分把扣分理由写在 `deduction_items[].reason`，旧评分路径写在 `deductions`（字符串列表）。
 * 评分页与助手的“得分详情”卡片都用这里取，助手按这些理由说“怎么改”，老师在页面上核对得到依据。
 *
 * @param {{ deduction_items?: Array<{ reason?: string | null }> | null, deductions?: string[] | null }} item
 * @returns {string[]}
 */
export function deductionReasons(item) {
  const fromItems = (item?.deduction_items || [])
    .map((entry) => String(entry?.reason || "").trim())
    .filter(Boolean);
  if (fromItems.length) return fromItems;
  return (item?.deductions || []).map((line) => String(line || "").trim()).filter(Boolean);
}
