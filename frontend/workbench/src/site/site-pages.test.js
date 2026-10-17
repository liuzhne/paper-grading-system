import fs from "node:fs";
import path from "node:path";

import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AboutPage from "./AboutPage.vue";
import LandingPage from "./LandingPage.vue";
import { resolveHash } from "./about-content.js";
import { CONTACT_EMAIL } from "./links.js";

// jsdom 不做布局：没有 scrollIntoView，scrollTo 只打印「未实现」。
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
  document.body.innerHTML = "";
});

describe("宣传页", () => {
  it("登录与进入工作台都指向工作台的规范地址", () => {
    const wrapper = mount(LandingPage);

    const login = wrapper.findAll("a").filter((a) => a.text() === "登录");
    expect(login).toHaveLength(1);
    expect(login[0].attributes("href")).toBe("/workbench/");
    expect(wrapper.get('[data-test="enter-app"]').attributes("href")).toBe("/workbench/");
  });

  it("不上线只改前端状态的试用表单（用户决定，2026-10-17）", () => {
    const wrapper = mount(LandingPage);

    expect(wrapper.find("form").exists()).toBe(false);
    expect(wrapper.find("input").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("已登记");
  });

  it("开了「减少动态效果」：四步平铺成静态画面，波浪不固定在视口上", () => {
    window.matchMedia = vi.fn(() => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
    try {
      const wrapper = mount(LandingPage);

      expect(wrapper.get(".product").classes()).toContain("still");
      expect(wrapper.findAll(".window")).toHaveLength(4);
      expect(wrapper.findAll(".rail")).toHaveLength(0);
      expect(wrapper.get(".overlay").attributes("style")).toContain("position: absolute");
    } finally {
      delete window.matchMedia;
    }
  });

  it("平时是一张随滚动变化的舞台", () => {
    const wrapper = mount(LandingPage);

    expect(wrapper.get(".product").classes()).not.toContain("still");
    expect(wrapper.findAll(".window")).toHaveLength(1);
    expect(wrapper.get(".overlay").attributes("style")).toContain("translateY");
  });

  it("没有点了没反应的链接", () => {
    const wrapper = mount(LandingPage);

    for (const link of wrapper.findAll("a")) {
      expect(link.attributes("href"), link.text()).toBeTruthy();
      expect(link.attributes("href"), link.text()).not.toBe("#");
    }
  });
});

describe("官网子页", () => {
  it("地址里的分页与分页内锚点都能找回所在分页", () => {
    expect(resolveHash("#security")).toEqual({ page: "security", anchor: null, known: true });
    expect(resolveHash("#contact")).toEqual({ page: "deploy", anchor: "contact", known: true });
    expect(resolveHash("#keys").page).toBe("docs");
    expect(resolveHash("#status").page).toBe("docs");
    expect(resolveHash("")).toEqual({ page: "deploy", anchor: null, known: false });
    expect(resolveHash("#whatever").page).toBe("deploy");
    // 原型链上的名字不是锚点。
    expect(resolveHash("#constructor")).toEqual({ page: "deploy", anchor: null, known: false });
    expect(resolveHash("#toString").known).toBe(false);
  });

  it("按 hash 切换分页并更新标题", async () => {
    window.location.hash = "#security";
    const wrapper = mount(AboutPage, { attachTo: document.body });
    await flushPromises();

    expect(wrapper.find('[data-test="page-security"]').exists()).toBe(true);
    expect(document.title).toBe("数据安全 · 有据智评");

    window.location.hash = "#docs";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await flushPromises();
    expect(wrapper.find('[data-test="page-docs"]').exists()).toBe(true);
    expect(document.title).toBe("帮助文档 · 有据智评");
    wrapper.unmount();
  });

  it("预约区不是假表单，而是发邮件到联系邮箱", async () => {
    window.location.hash = "#contact";
    const wrapper = mount(AboutPage, { attachTo: document.body });
    await flushPromises();

    expect(wrapper.find('[data-test="page-deploy"]').exists()).toBe(true);
    expect(wrapper.find("form").exists()).toBe(false);
    const mail = wrapper.get("#contact a[href^='mailto:']");
    expect(mail.attributes("href")).toContain(CONTACT_EMAIL);
    wrapper.unmount();
  });

  it("三个分页都没有点了没反应的链接", async () => {
    for (const page of ["deploy", "security", "docs"]) {
      window.location.hash = `#${page}`;
      const wrapper = mount(AboutPage, { attachTo: document.body });
      await flushPromises();
      for (const link of wrapper.findAll("a")) {
        expect(link.attributes("href"), `${page}: ${link.text()}`).toBeTruthy();
        expect(link.attributes("href"), `${page}: ${link.text()}`).not.toBe("#");
      }
      wrapper.unmount();
    }
  });
});

it("原型里的占位邮箱与 Google Fonts 不进生产", () => {
  const dir = import.meta.dirname;
  for (const name of fs.readdirSync(dir)) {
    if (name.endsWith(".test.js")) continue;
    const source = fs.readFileSync(path.join(dir, name), "utf8");
    expect(source, name).not.toContain("example.com");
    expect(source, name).not.toContain("fonts.googleapis.com");
  }
});
