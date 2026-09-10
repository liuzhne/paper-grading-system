import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * 界面不得把用户指向已经下线的入口（D-024）。
 *
 * 旧 SPA 在 2026-09-08 下线，`/legacy/` 返回 404，`/` 就是本工作台。但页面文案是
 * 在那之前写的，仍然写着「请先在旧版模板中心导入」，其中一处还是 `<a href="/">`
 * ——点下去回到工作台首页，用户以为自己点错了。
 *
 * 这类文案不会让任何测试变红：链接是合法的，页面也渲染得出来。它只是把人送去一个
 * 不存在的地方，而那件事**本页就能做**。
 */
const DIRS = [
  path.resolve(import.meta.dirname),
  path.resolve(import.meta.dirname, "..", "components"),
  path.resolve(import.meta.dirname, "..", "layouts"),
];

/** 已下线入口的说法。写进文案就是把用户支走。 */
const RETIRED = ["旧版模板中心", "旧版界面", "旧 SPA", "/legacy/"];

/**
 * 只看 `<template>`：`<script>` 里的注释解释历史（「原先散落在旧 SPA 三处」）是
 * 正当的，把它一起禁掉会逼人删掉唯一记着来龙去脉的那句话。这条契约管的是**用户
 * 看得到的文案**。
 */
function templates() {
  const files = [];
  for (const dir of DIRS) {
    if (!fs.existsSync(dir)) continue;
    for (const name of fs.readdirSync(dir)) {
      if (!name.endsWith(".vue")) continue;
      const source = fs.readFileSync(path.join(dir, name), "utf8");
      const start = source.indexOf("<template>");
      files.push([name, start === -1 ? "" : source.slice(start)]);
    }
  }
  return files;
}

describe("已下线入口", () => {
  it("有待检查的页面", () => {
    expect(templates().length).toBeGreaterThan(5);
  });

  it("没有页面把用户指向旧版界面", () => {
    const offenders = [];
    for (const [name, source] of templates()) {
      for (const phrase of RETIRED) {
        if (source.includes(phrase)) offenders.push(`${name}：${phrase}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
