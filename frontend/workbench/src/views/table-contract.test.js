import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * 空表提示必须横跨整张表（2026-09-10 设计稿对照）。
 *
 * `<td class="table-empty" colspan="N">` 的 N 是手写的，加一列时**没有任何东西
 * 会提醒你改它**：构建、typecheck、单测全过，页面也不报错——只是「还没有评分
 * 任务」那行短了一截，右边空着一格。上一次给评分任务列表补「评分标准」列就漏了。
 *
 * 这条契约把「表头数」与「空行 colspan」绑在一起，让下一次漏改直接失败。
 */
const VIEWS = path.resolve(import.meta.dirname);

function vueFiles() {
  return fs
    .readdirSync(VIEWS)
    .filter((name) => name.endsWith(".vue"))
    .map((name) => [name, fs.readFileSync(path.join(VIEWS, name), "utf8")]);
}

/**
 * 数一段 `<thead>…</thead>` 里的 `<th>`。
 *
 * 只数第一层：目前没有分组表头，出现了要先想清楚 colspan 该对哪一层。
 */
function countHeaders(thead) {
  return [...thead.matchAll(/<th\b/g)].length;
}

describe("表格空行的 colspan", () => {
  it("有带空行提示的表格可检查", () => {
    const withEmpty = vueFiles().filter(([, source]) =>
      source.includes("table-empty"),
    );
    expect(withEmpty.length).toBeGreaterThan(2);
  });

  it("每个 table-empty 的 colspan 等于同一张表的表头数", () => {
    const offenders = [];
    for (const [name, source] of vueFiles()) {
      // 一个视图可能有多张表：按 <table> 切开分别比对。
      for (const table of source.matchAll(/<table\b[\s\S]*?<\/table>/g)) {
        const html = table[0];
        const thead = html.match(/<thead>[\s\S]*?<\/thead>/);
        const empty = html.match(/class="table-empty"[^>]*colspan="(\d+)"/);
        if (!thead || !empty) continue;
        const headers = countHeaders(thead[0]);
        if (Number(empty[1]) !== headers) {
          offenders.push(`${name}：表头 ${headers} 列，空行 colspan=${empty[1]}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
