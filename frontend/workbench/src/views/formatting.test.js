import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * 缺失值不得渲染成 0，也不得让页面崩掉（前端 v2 计划 §5-B、§5-D）。
 *
 * 计划反复强调「置信度缺失显示未提供而不是 0」「空批次显示 0 项而非伪造百分比」。
 * 这条守的是它的另一面：判空写成 `x === null` 而漏掉 `undefined` 时，
 * `undefined.toFixed(1)` 会抛异常——**整块卡片连同页面一起白屏**，而白屏读起来
 * 像「系统坏了」，不像「这个数还没有」。
 *
 * 用源码契约而不是组件测试：这一类缺陷是「少写了半个条件」，逐个页面搭 mount
 * 成本高，且新增页面时很容易漏掉。
 */
const VIEWS = path.resolve(import.meta.dirname);

function views() {
  return fs
    .readdirSync(VIEWS)
    .filter((name) => name.endsWith(".vue"))
    .map((name) => [name, fs.readFileSync(path.join(VIEWS, name), "utf8")]);
}

describe("视图里的判空", () => {
  it("存在待检查的视图文件", () => {
    expect(views().length).toBeGreaterThan(5);
  });

  it("判空一律同时覆盖 null 与 undefined", () => {
    const offenders = [];
    for (const [name, source] of views()) {
      const lines = source.split("\n");
      lines.forEach((line, index) => {
        if (!/===\s*null/.test(line)) return;
        if (/undefined/.test(line)) return;
        // `== null` 的语义正好是「null 或 undefined」，是这条规则要的写法本身。
        if (/[^=!]==\s*null/.test(line)) return;
        offenders.push(`${name}:${index + 1}  ${line.trim()}`);
      });
    }

    expect(offenders).toEqual([]);
  });

  it("数值格式化前必须先判空", () => {
    const offenders = [];
    for (const [name, source] of views()) {
      const lines = source.split("\n");
      lines.forEach((line, index) => {
        if (!/\.toFixed\(/.test(line)) return;
        // 同一行里要能看到判空，或者取值时已经给了默认。
        if (/==\s*null|!=\s*null|\?\?|\?\./.test(line)) return;
        offenders.push(`${name}:${index + 1}  ${line.trim()}`);
      });
    }

    expect(offenders).toEqual([]);
  });
});
