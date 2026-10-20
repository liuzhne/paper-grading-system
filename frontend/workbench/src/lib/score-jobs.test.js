import { describe, expect, it } from "vitest";

import { compactTokens, finishedCount, itemStatusLabel, itemUsageText, jobPercent, jobStatusLabel, lastHeartbeat, modelOutputText } from "./score-jobs.js";

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

  it("有思考或截断时一并显示", () => {
    expect(itemUsageText({ telemetry: { prompt_tokens: 58330, completion_tokens: 3304, reasoning_tokens: 36918, truncated_count: 13 } }))
      .toBe("输入 5.8 万 / 输出 3304 · 思考 3.7 万 · 截断 13 次");
  });
});

describe("modelOutputText", () => {
  it("没有评分时直说", () => {
    expect(modelOutputText({ status: "unavailable" })).toBe("近 24 小时无评分");
    expect(modelOutputText(undefined)).toBe("近 24 小时无评分");
  });

  it("给出思考占输出的比例、截断与停下的材料", () => {
    expect(modelOutputText({
      status: "fail", runs: 1, completion_tokens: 3304, reasoning_tokens: 36918,
      truncated_count: 13, thinking_mismatch_items: 1,
    })).toBe("评分 1 次 · 思考 3.7 万（占输出 92%） · 截断 13 次 · 思考未关闭停下 1 份");
    expect(modelOutputText({ status: "pass", runs: 4, completion_tokens: 900, reasoning_tokens: 0, truncated_count: 0 }))
      .toBe("评分 4 次 · 无思考 · 截断 0 次");
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
