/** State guards mirror the strict publication lifecycle; coverage is not validation. */
export function isScoringCompletenessIssue(issue) {
  return ["criterion_rules_missing", "criterion_numeric_scoring_missing", "rule_text_missing",
    "deduct_rule_invalid", "band_levels_invalid", "llm_direct_not_publishable"].includes(issue.code);
}

export function compilationReady(execution, compilationId) {
  const active = execution?.active_compilation;
  return Boolean(active && active.id === compilationId &&
    active.status === "validated" && !(active.blockers || []).length &&
    !execution.ambiguity);
}

/** 参与评分的细则全部已确认、至少 1 条；老师排除（rejected）的细则和它的模板映射不参与。 */
export function rulesReviewed(execution) {
  const active = execution?.active_compilation;
  const all = active?.rules || [];
  const rules = all.filter((rule) => rule.status !== "rejected");
  const excluded = new Set(all.filter((rule) => rule.status === "rejected").map((rule) => rule.rule_code));
  return Boolean(active && rules.length &&
    rules.every((rule) => rule.status === "approved") &&
    (active.template_links || []).filter((link) => !excluded.has(link.rule_code))
      .every((link) => link.review_status === "confirmed"));
}

export function canSubmitReview(status, execution) {
  return status === "draft" && compilationReady(execution, execution?.active_compilation?.id) &&
    rulesReviewed(execution);
}

export function canPublishRubric(status, execution, compilationId) {
  return status === "review" && compilationReady(execution, compilationId) && rulesReviewed(execution);
}
