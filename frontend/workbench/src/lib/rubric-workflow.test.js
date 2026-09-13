import { describe, expect, it } from "vitest";
import { canPublishRubric, canSubmitReview } from "./rubric-workflow.js";

const ready = () => ({ active_compilation: {
  id: "c1", status: "validated", blockers: [],
  rules: [{ status: "approved" }], template_links: [{ review_status: "confirmed" }],
} });

describe("评分标准审核与发布门控", () => {
  it("规则齐全也不能发布存在编译阻断的草稿", () => {
    const execution = ready();
    execution.active_compilation.status = "blocked";
    execution.active_compilation.blockers = [{ code: "SEVERITY_CONFIRMATION_REQUIRED" }];
    expect(canPublishRubric("review", execution, "c1")).toBe(false);
    expect(canSubmitReview("draft", execution)).toBe(false);
  });
  it("必须先确认规则及模板映射，再提交审核；确认不自动发布", () => {
    const execution = ready();
    expect(canSubmitReview("draft", execution)).toBe(true);
    expect(canPublishRubric("draft", execution, "c1")).toBe(false);
    expect(canPublishRubric("review", execution, "c1")).toBe(true);
    execution.active_compilation.rules[0].status = "review";
    expect(canSubmitReview("draft", execution)).toBe(false);
    execution.active_compilation.rules[0].status = "approved";
    execution.active_compilation.template_links[0].review_status = "pending";
    expect(canPublishRubric("review", execution, "c1")).toBe(false);
  });
  it("不允许无草稿、歧义或非当前版本通过门控", () => {
    expect(canPublishRubric("review", null, "c1")).toBe(false);
    expect(canPublishRubric("review", ready(), "old")).toBe(false);
    expect(canPublishRubric("review", { ...ready(), ambiguity: "multiple" }, "c1")).toBe(false);
  });
});
