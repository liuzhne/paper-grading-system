import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * 生成的类型必须真的被引用（前端 v2 计划 §8）。
 *
 * §8 的原话是「OpenAPI 生成 `.d.ts` 并**通过 JSDoc 引用**，保留 JS 源码」。生成
 * 一个没人引用的 `.d.ts` 是死重：它照样进合同门禁、照样产生 diff，但没有任何
 * 一处代码会因为后端改了字段而报错——门禁看起来在工作，实际什么都没保护。
 *
 * `jsconfig.json` 的 `checkJs` 是 false，所以 JSDoc 只在带 `// @ts-check` 的文件
 * 里生效。这两条一起断言：既要有引用，引用它的文件也要真的被检查。
 */
const SRC = path.resolve(import.meta.dirname, "..");

function read(relative) {
  return fs.readFileSync(path.join(SRC, relative), "utf8");
}

describe("生成类型的引用", () => {
  it("schema.d.ts 已生成", () => {
    expect(fs.existsSync(path.join(SRC, "api/schema.d.ts"))).toBe(true);
  });

  it("有 JSDoc 引用它", () => {
    const types = read("api/types.js");

    // 引用写成 `./schema` 而不是 `./schema.d.ts`：JSDoc 里带 .d.ts 后缀会被 TS
    // 当成运行时导入并直接报 TS2846。
    expect(types).toMatch(/import\("\.\/schema"\)/);
    expect(types).toMatch(/@typedef/);
  });

  it("引用它的文件开了 ts-check，否则 JSDoc 不会被校验", () => {
    expect(read("api/types.js")).toMatch(/^\/\/ @ts-check/m);
  });

  it("类型来自真实端点路径，不是手抄的形状", () => {
    const types = read("api/types.js");

    // 手抄一份形状出来，后端改了字段它不会动——那正是这条门禁要防的。
    expect(types).toMatch(/paths\[/);
    expect(types).toContain("/api/batches/{batch_id}/progress");
  });

  it("至少一个 store 用上了这些类型", () => {
    const batches = read("stores/batches.js");

    expect(batches).toMatch(/@ts-check/);
    expect(batches).toContain("@/api/types.js");
  });
});
