import { describe, expect, it } from "vitest";

import { finishedCount, itemStatusLabel, jobPercent, jobStatusLabel } from "./score-jobs.js";

describe("score job presentation", () => {
  it("distinguishes a stale runner from ordinary running", () => {
    expect(jobStatusLabel({ status: "running", heartbeat_state: "healthy" })).toBe("评分中");
    expect(jobStatusLabel({ status: "running", heartbeat_state: "stale" })).toBe("执行中断，等待恢复");
  });

  it("uses terminal item counts for progress", () => {
    const job = { total_items: 5, succeeded_count: 2, skipped_count: 1, failed_count: 1, canceled_count: 0 };
    expect(finishedCount(job)).toBe(4);
    expect(jobPercent(job)).toBe(80);
  });

  it("gives each item state an action-oriented label", () => {
    expect(itemStatusLabel("pending")).toBe("等待评分");
    expect(itemStatusLabel("skipped")).toBe("沿用已有结果");
  });
});
