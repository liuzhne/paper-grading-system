import { describe, expect, it } from "vitest";

import { MAX_FILES, limitFiles, papersInUploadOrder, publishedRubrics, scoreOverview } from "./assistant-flow.js";

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
});
