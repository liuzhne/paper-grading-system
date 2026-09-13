/** State guards mirror the strict publication lifecycle; coverage is not validation. */
export function compilationReady(execution, compilationId) {
  const active = execution?.active_compilation;
  return Boolean(active && active.id === compilationId &&
    active.status === "validated" && !(active.blockers || []).length &&
    !execution.ambiguity);
}

export function rulesReviewed(execution) {
  const active = execution?.active_compilation;
  return Boolean(active && (active.rules || []).length &&
    active.rules.every((rule) => rule.status === "approved") &&
    (active.template_links || []).every((link) => link.review_status === "confirmed"));
}

export function canSubmitReview(status, execution) {
  return status === "draft" && compilationReady(execution, execution?.active_compilation?.id) &&
    rulesReviewed(execution);
}

export function canPublishRubric(status, execution, compilationId) {
  return status === "review" && compilationReady(execution, compilationId) && rulesReviewed(execution);
}
