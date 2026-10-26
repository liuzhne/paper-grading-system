import { describe, expect, it } from "vitest";

import { deductionReasons } from "./score-items.js";

describe("deductionReasons", () => {
  it("Core 评分的扣分理由在 deduction_items 里", () => {
    expect(deductionReasons({
      deduction_items: [{ reason: "未说明研究方法", points: 2 }, { reason: " " }, { points: 1 }],
      deductions: ["旧的写法"],
    })).toEqual(["未说明研究方法"]);
  });

  it("没有扣分条目时退回旧评分路径的 deductions", () => {
    expect(deductionReasons({ deduction_items: [], deductions: ["文献综述不充分", ""] })).toEqual(["文献综述不充分"]);
  });

  it("什么都没有时是空列表", () => {
    expect(deductionReasons({})).toEqual([]);
    expect(deductionReasons(null)).toEqual([]);
  });
});
