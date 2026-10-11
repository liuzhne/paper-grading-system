import { describe, expect, it } from "vitest";

import {
  canRetryAiTask, classificationTaskProgress, draftTaskProgress, isActiveAiTask, isWaitingForRateLimit,
  reviewTaskProgress, structureTaskProgress,
} from "./ai-tasks.js";

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

describe("归类任务进度", () => {
  it("按条目的单元数累计已完成与失败", () => {
    const task = { status: "running", items: [
      { status: "succeeded", unit_count: 3 }, { status: "failed", unit_count: 3 }, { status: "pending", unit_count: 2 },
    ] };
    expect(classificationTaskProgress(task)).toEqual({ completed: 3, failed: 3, total: 8, running: true, stopped: null, error: null });
  });

  it("失败的任务给出根因", () => {
    const progress = classificationTaskProgress({ status: "failed", error_message: "额度已用完。", items: [] });
    expect(progress.stopped).toBe("provider");
    expect(progress.error).toBe("额度已用完。");
  });
});

describe("规则审查任务进度", () => {
  it("按评分项计数，关闭页面不中断", () => {
    const task = { status: "running", total_items: 5, succeeded_count: 2, items: [] };
    expect(reviewTaskProgress(task)).toBe("规则审查中：已完成 2/5 项（关闭页面不会中断）");
  });

  it("失败时指出是哪几项，跨项审查单独命名", () => {
    const task = { status: "failed", total_items: 3, succeeded_count: 1, error_message: "额度已用完。", items: [
      { status: "succeeded", label: "C01" }, { status: "failed", label: "C02" }, { status: "failed", label: "__cross__" },
    ] };
    expect(reviewTaskProgress(task)).toBe("审查未完成（已完成 1/3 项）：C02、跨项审查 额度已用完。");
  });

  it("停止后说明不会写入", () => {
    expect(reviewTaskProgress({ status: "canceled" })).toContain("不会写入");
  });
});

describe("表格结构识别任务进度", () => {
  it("运行中、限流与失败各自说明", () => {
    const now = Date.parse("2026-10-10T00:00:00Z");
    expect(structureTaskProgress({ status: "running", items: [] }, now)).toContain("正在识别");
    const waiting = { status: "running", items: [{ status: "pending", not_before: "2026-10-10T00:01:00" }] };
    expect(structureTaskProgress(waiting, now)).toContain("模型限流");
    expect(structureTaskProgress({ status: "failed", error_message: "模型两次输出的结构都未通过校验。" }))
      .toBe("识别失败：模型两次输出的结构都未通过校验。");
  });
});
