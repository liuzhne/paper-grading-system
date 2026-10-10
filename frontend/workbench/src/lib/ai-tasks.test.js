import { describe, expect, it } from "vitest";

import { canRetryAiTask, draftTaskProgress, isActiveAiTask, isWaitingForRateLimit } from "./ai-tasks.js";

describe("AI 任务呈现", () => {
  it("进行中显示第几批", () => {
    const task = { status: "running", total_items: 4, succeeded_count: 1, items: [] };
    expect(draftTaskProgress(task)).toBe("AI 起草中：第 2/4 批");
    expect(isActiveAiTask(task)).toBe(true);
  });

  it("有批次因限流延后时说明会自动继续", () => {
    const now = Date.parse("2026-10-10T08:00:00Z");
    const task = {
      status: "running", total_items: 2, succeeded_count: 1,
      items: [{ status: "pending", not_before: "2026-10-10T08:00:30" }],
    };
    expect(isWaitingForRateLimit(task, now)).toBe(true);
    expect(draftTaskProgress(task, now)).toContain("模型限流");
  });

  it("失败时给出根因与已完成批次，并允许只重试失败的批次", () => {
    const task = { status: "failed", total_items: 3, succeeded_count: 2, error_message: "额度已用完。", items: [] };
    expect(draftTaskProgress(task)).toBe("起草失败（已完成 2/3 批）：额度已用完。");
    expect(canRetryAiTask(task)).toBe(true);
    expect(isActiveAiTask(task)).toBe(false);
  });

  it("不需要 AI 的评分项直接说明原因", () => {
    expect(draftTaskProgress({ status: "succeeded", total_items: 0, result: { status: "already_structured" } }))
      .toContain("无需 AI 补全");
  });
});
