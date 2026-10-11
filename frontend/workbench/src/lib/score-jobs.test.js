import { describe, expect, it } from "vitest";

import { compactTokens, finishedCount, itemStatusLabel, itemUsageText, jobPercent, jobStatusLabel, lastHeartbeat } from "./score-jobs.js";

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

describe("itemUsageText", () => {
  it("显示本次实际用量与复用条数", () => {
    expect(itemUsageText({ telemetry: { prompt_tokens: 112000, completion_tokens: 3500, decision_ledger_reused: 68 } }))
      .toBe("输入 11.2 万 / 输出 3500 · 复用 68 条");
  });

  it("全部复用时如实显示 0 用量", () => {
    expect(itemUsageText({ telemetry: { prompt_tokens: 0, completion_tokens: 0, decision_ledger_reused: 135 } }))
      .toBe("输入 0 / 输出 0 · 复用 135 条");
  });

  it("没有记录时不冒充 0", () => {
    expect(itemUsageText({ telemetry: { cache_hits: 0 } })).toBe("—");
    expect(itemUsageText({})).toBe("—");
  });
});

describe("compactTokens", () => {
  it("缺值不显示成 0", () => {
    expect(compactTokens(undefined)).toBe("—");
    expect(compactTokens(null)).toBe("—");
    expect(compactTokens(0)).toBe("0");
    expect(compactTokens(84000)).toBe("8.4 万");
  });
});

describe("unified work queue heartbeats", () => {
  it("labels a running job that only has queued papers as waiting", () => {
    expect(jobStatusLabel({ status: "running", heartbeat_state: "waiting" })).toBe("排队等待");
    expect(jobStatusLabel({ status: "running", heartbeat_state: "stale" })).toBe("执行中断，等待恢复");
  });

  it("takes the freshest heartbeat of the job and its running papers", () => {
    const job = {
      heartbeat_at: "2026-10-10T08:00:00",
      items: [
        { status: "running", heartbeat_at: "2026-10-10T08:05:00" },
        { status: "succeeded", heartbeat_at: "2026-10-10T09:00:00" },
      ],
    };
    expect(lastHeartbeat(job)).toBe("2026-10-10T08:05:00");
    expect(lastHeartbeat({ heartbeat_at: null, items: [] })).toBeNull();
  });
});
