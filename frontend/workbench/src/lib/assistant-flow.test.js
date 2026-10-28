import { describe, expect, it } from "vitest";

import { MAX_FILES, activePlan, limitFiles, papersInUploadOrder, planProgressText, publishedRubrics, scoreOverview } from "./assistant-flow.js";

describe("助手展示用纯函数", () => {
  it("超过 30 份只收前 30 份", () => {
    const files = Array.from({ length: MAX_FILES + 4 }, (_, index) => index);
    const result = limitFiles(files);
    expect(result.accepted).toHaveLength(MAX_FILES);
    expect(result.dropped).toBe(4);
  });

  it("换评分标准的候选只列已发布的，最新发布的在前", () => {
    const list = publishedRubrics([
      { id: "old", status: "published", published_at: "2026-09-01T00:00:00" },
      { id: "draft", status: "draft", created_at: "2026-10-09T00:00:00" },
      { id: "new", status: "published", published_at: "2026-10-01T00:00:00" },
    ]);
    expect(list.map((item) => item.id)).toEqual(["new", "old"]);
  });

  it("候选不列工作台评不了的类型（技术方案只走提交接口）", () => {
    const list = publishedRubrics([
      { id: "doc", status: "published", business_profile_key: "general_document", published_at: "2026-10-02T00:00:00" },
      { id: "tp", status: "published", business_profile_key: "technical_proposal", published_at: "2026-10-03T00:00:00" },
      { id: "legacy", status: "published", business_profile_key: null, published_at: "2026-10-01T00:00:00" },
    ]);
    expect(list.map((item) => item.id)).toEqual(["doc", "legacy"]);
  });

  it("汇总按上传时间倒序返回，序号按上传顺序数", () => {
    expect(papersInUploadOrder([{ id: 3 }, { id: 2 }, { id: 1 }]).map((item) => item.id)).toEqual([1, 2, 3]);
  });

  it("概览只用汇总里的现成分数，与分数同精度", () => {
    const overview = scoreOverview([
      { latest_final_score: 80, latest_need_manual_review: false },
      { latest_final_score: 91.12, latest_need_manual_review: true },
      { latest_final_score: null },
    ]);
    expect(overview).toEqual({ total: 3, scored: 2, average: 85.56, max: 91.12, min: 80, needReview: 1 });
  });

  it("进行中的计划只看最近一张计划卡片：它结束了就没有进行中的计划", () => {
    const old = { type: "plan", status: "dismissed", steps: [] };
    const running = { type: "plan", status: "running", steps: [] };
    expect(activePlan([{ id: "m1", cards: [old] }, { id: "m2", cards: [running] }, { id: "m3", cards: [] }])?.card).toBe(running);
    expect(activePlan([{ id: "m1", cards: [running] }, { id: "m2", cards: [old] }])).toBeNull();
    expect(activePlan([])).toBeNull();
  });

  it("标题下的进度：第几步、共几步，等你决定的那一步写明", () => {
    const steps = [
      { label: "评分标准", status: "done" }, { label: "选择待评分文件", status: "current" }, { label: "评分", status: "todo" },
    ];
    expect(planProgressText({ steps })).toBe("第 2/3 步 · 选择待评分文件");
    steps[1].status = "blocked";
    expect(planProgressText({ steps })).toBe("第 2/3 步 · 选择待评分文件（等你决定）");
    expect(planProgressText({ steps: [{ label: "评分", status: "done" }] })).toBe("");
  });
});
