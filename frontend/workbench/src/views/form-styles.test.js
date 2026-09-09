import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

/**
 * 表单控件必须走设计系统的类（2026-09-09 生产视觉走查）。
 *
 * 裸 `<input>` / `<select>` / `<textarea>` 会继承 tokens.css 的最小样式，但拿不到
 * `.input` / `.select` 的宽度、高度与内边距，也拿不到 `.field` / `.field-label` 的
 * 上下留白。结果是标签与输入挤在一行、宽度随内容伸缩、按钮贴边——**与同一页里
 * 用对了类的区块并排时格外突兀**。
 *
 * 这类问题构建与 typecheck 都不会报，只能靠看。用源码契约把它变成能自动发现的。
 */
const VIEWS = path.resolve(import.meta.dirname);
const LAYOUTS = path.resolve(import.meta.dirname, "..", "layouts");

function vueFiles() {
  const files = [];
  for (const dir of [VIEWS, LAYOUTS]) {
    for (const name of fs.readdirSync(dir)) {
      if (name.endsWith(".vue")) {
        files.push([name, fs.readFileSync(path.join(dir, name), "utf8")]);
      }
    }
  }
  return files;
}

/** 鉴权页有自己的一套 `.auth` 样式，不走工作台的表单类。 */
const AUTH_PAGES = new Set([
  "LoginView.vue",
  "RegisterView.vue",
  "ResetPasswordView.vue",
  "AuthLayout.vue",
]);

function templateOf(source) {
  const start = source.indexOf("<template>");
  return start === -1 ? "" : source.slice(start);
}

describe("表单控件的设计系统类", () => {
  it("有待检查的视图", () => {
    expect(vueFiles().length).toBeGreaterThan(5);
  });

  it("文本类输入都带 .input", () => {
    const offenders = [];
    for (const [name, source] of vueFiles()) {
      if (AUTH_PAGES.has(name)) continue;
      const template = templateOf(source);
      for (const tag of template.matchAll(/<input\b[^>]*>/g)) {
        const html = tag[0];
        // 单选/复选/文件有各自的排版，不套 .input。
        if (/type="(radio|checkbox|file)"/.test(html)) continue;
        if (!/class="[^"]*\binput\b/.test(html)) {
          offenders.push(`${name}: ${html.slice(0, 70)}`);
        }
      }
    }

    expect(offenders).toEqual([]);
  });

  it("下拉都带 .select", () => {
    const offenders = [];
    for (const [name, source] of vueFiles()) {
      if (AUTH_PAGES.has(name)) continue;
      for (const tag of templateOf(source).matchAll(/<select\b[^>]*>/g)) {
        if (!/class="[^"]*\bselect\b/.test(tag[0])) {
          offenders.push(`${name}: ${tag[0].slice(0, 70)}`);
        }
      }
    }

    expect(offenders).toEqual([]);
  });

  it("多行输入都带 .input", () => {
    const offenders = [];
    for (const [name, source] of vueFiles()) {
      if (AUTH_PAGES.has(name)) continue;
      for (const tag of templateOf(source).matchAll(/<textarea\b[^>]*>/g)) {
        if (!/class="[^"]*\binput\b/.test(tag[0])) {
          offenders.push(`${name}: ${tag[0].slice(0, 70)}`);
        }
      }
    }

    expect(offenders).toEqual([]);
  });

  it("不使用裸 for/id 配对的标签，改用 .field 包裹", () => {
    /*
     * 设计稿里标签与控件是上下结构且有固定间距，那来自 `.field` + `.field-label`。
     * 用 `<label for>` 平铺会让它们并排，正是这次走查里「非常突兀」的来源。
     */
    const offenders = [];
    for (const [name, source] of vueFiles()) {
      if (AUTH_PAGES.has(name)) continue;
      for (const tag of templateOf(source).matchAll(/<label\s+(?::for|for)=/g)) {
        offenders.push(`${name}: ${tag[0]}`);
      }
    }

    expect(offenders).toEqual([]);
  });
});

describe("模板里引用的类必须真实存在", () => {
  /*
   * 写一个不存在的类不会报错——它只是不生效。这次的表现是页头的主按钮脱离了
   * 页头、跑到内容区上方，而标题被挤没了。构建、typecheck、单测全绿。
   */
  function stylesheet() {
    const dir = path.resolve(import.meta.dirname, "..", "styles");
    return fs
      .readdirSync(dir)
      .filter((name) => name.endsWith(".css"))
      .map((name) => fs.readFileSync(path.join(dir, name), "utf8"))
      .join("\n");
  }

  it("布局类都能在样式表或组件自身的 style 块里找到", () => {
    const global = stylesheet();
    const offenders = [];
    // 只查这一族：它们决定排版，写错了会静默错位。
    const layoutClasses = /\b(page-head[\w-]*|form-grid|form-actions|field|field-label|field-hint)\b/g;

    for (const [name, source] of vueFiles()) {
      const template = templateOf(source);
      const scoped = source.slice(source.indexOf("<style"));
      for (const match of template.matchAll(layoutClasses)) {
        const cls = match[1];
        if (global.includes(`.${cls}`) || scoped.includes(`.${cls}`)) continue;
        offenders.push(`${name}: .${cls}`);
      }
    }

    expect([...new Set(offenders)]).toEqual([]);
  });
});
