import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  documentTypeLabel, isWorkbenchType, materialCode, materialIdentity,
  rememberDocumentType, rememberedDocumentType,
} from "./document-types.js";

let storage;
beforeEach(() => {
  storage = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("文档类型的显示规则", () => {
  const paper = { student_id: "20260001", student_name: "张三", title: null, file_name: "数据中心扩容方案.docx" };

  it("论文显示学号与姓名，通用文档显示文件名与标题", () => {
    expect(materialIdentity(paper, "thesis")).toEqual({ code: "20260001", title: "张三" });
    expect(materialIdentity(paper, "general_document")).toEqual({ code: "数据中心扩容方案.docx", title: "数据中心扩容方案" });
    expect(materialIdentity({ ...paper, title: "扩容方案（技术部分）" }, "general_document").title).toBe("扩容方案（技术部分）");
  });

  it("未版本化的旧标准没有类型，按论文显示", () => {
    expect(materialIdentity(paper, null).code).toBe("20260001");
    expect(documentTypeLabel(null)).toBe("毕业论文");
    expect(documentTypeLabel("general_document")).toBe("通用文档");
  });

  it("复核表的材料列随类型变化", () => {
    const entry = { student_id: "20260001", file_name: "a.docx" };
    expect(materialCode(entry, "thesis")).toBe("20260001");
    expect(materialCode(entry, "general_document")).toBe("a.docx");
    expect(materialCode({}, "general_document")).toBe("—");
  });

  it("技术方案不是工作台能评的类型", () => {
    expect(isWorkbenchType("thesis")).toBe(true);
    expect(isWorkbenchType(null)).toBe(true);
    expect(isWorkbenchType("general_document")).toBe(true);
    expect(isWorkbenchType("technical_proposal")).toBe(false);
  });

  it("记住上次的选择；读写失败时退回论文", () => {
    expect(rememberedDocumentType()).toBe("thesis");
    rememberDocumentType("general_document");
    expect(rememberedDocumentType()).toBe("general_document");
    rememberDocumentType("technical_proposal");
    expect(rememberedDocumentType()).toBe("general_document");
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("blocked"); }, setItem: () => {} });
    expect(rememberedDocumentType()).toBe("thesis");
    vi.stubGlobal("localStorage", undefined);
    expect(rememberedDocumentType()).toBe("thesis");
    expect(() => rememberDocumentType("general_document")).not.toThrow();
  });
});
