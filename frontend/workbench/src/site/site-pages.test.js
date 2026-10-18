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

  it("没开申请开关时（Vercel）保留大按钮，页头「申请试用」照常跳转", () => {
    const wrapper = mount(LandingPage);

    expect(wrapper.get('[data-test="hero-trial"]').attributes("href")).toBe("/about#contact");
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    wrapper.get('[data-test="header-trial"]').element.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(wrapper.find('[data-test="hero-links"]').exists()).toBe(false);
  });

  it("部署没开申请开关时（Vercel）不出现试用表单", () => {
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

  it("部署把宣传页固定为少动效版时，不看访客的系统设置（Vercel，2026-10-17）", () => {
    globalThis.__PGS_CONFIG__ = { landingMotion: "reduced" };
    try {
      const wrapper = mount(LandingPage);

      expect(wrapper.get(".product").classes()).toContain("still");
      expect(wrapper.findAll(".window")).toHaveLength(4);
    } finally {
      delete globalThis.__PGS_CONFIG__;
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

describe("官网申请表单（docs/官网申请落库改造方案.md）", () => {
  /** @param {number} status @param {object} [body] */
  const respond = (status, body = {}) => {
    globalThis.fetch = vi.fn(async (url) => {
      if (String(url).endsWith("/auth/me")) return { ok: false, status: 401, json: async () => ({}) };
      return { ok: status < 300, status, json: async () => body };
    });
  };

  beforeEach(() => {
    globalThis.__PGS_CONFIG__ = { siteLeads: true, siteLeadsRetentionDays: 90 };
  });

  afterEach(() => {
    delete globalThis.__PGS_CONFIG__;
    delete globalThis.fetch;
  });

  it("首屏：提交成功后只说会用这个邮箱联系，不承诺「试用开放时通知」", async () => {
    respond(202, { status: "received" });
    const wrapper = mount(LandingPage, { attachTo: document.body });

    expect(wrapper.text()).toContain("申请记录保存 90 天");
    await wrapper.get("#trial-email").setValue("teacher@example.edu.cn");
    await wrapper.get('[data-test="trial-form"]').trigger("submit");
    await flushPromises();

    const done = wrapper.get('[data-test="trial-done"]');
    expect(done.text()).toContain("已收到 teacher@example.edu.cn，我们会通过这个邮箱联系你。");
    expect(wrapper.text()).not.toContain("试用开放时");
    const [, init] = globalThis.fetch.mock.calls.find(([url]) => String(url).endsWith("/site/leads"));
    expect(JSON.parse(init.body)).toMatchObject({ kind: "trial", email: "teacher@example.edu.cn", website: "" });
    wrapper.unmount();
  });

  it("首屏：空邮箱当场拦下，不发请求", async () => {
    respond(202);
    const wrapper = mount(LandingPage, { attachTo: document.body });

    await wrapper.get('[data-test="trial-form"]').trigger("submit");
    await flushPromises();

    expect(wrapper.get("#trial-email-error").text()).toBe("请填写院系邮箱");
    expect(globalThis.fetch.mock.calls.some(([url]) => String(url).endsWith("/site/leads"))).toBe(false);
    wrapper.unmount();
  });

  it("首屏：限流时提示并给出发邮件的退路，已填的邮箱不清空", async () => {
    respond(429, { detail: { code: "per_ip_hourly" } });
    const wrapper = mount(LandingPage, { attachTo: document.body });

    await wrapper.get("#trial-email").setValue("teacher@example.edu.cn");
    await wrapper.get('[data-test="trial-form"]').trigger("submit");
    await flushPromises();

    const alert = wrapper.findAll('[role="alert"]').at(-1);
    expect(alert.text()).toContain("提交太频繁");
    expect(alert.get("a").attributes("href")).toContain(`mailto:${CONTACT_EMAIL}`);
    expect(wrapper.get("#trial-email").element.value).toBe("teacher@example.edu.cn");
    wrapper.unmount();
  });

  it("首屏只有一个主操作：没有「申请试用」大按钮，两条次要链接并成一行", () => {
    respond(202);
    const wrapper = mount(LandingPage);

    // 同一屏不能再有第三个「申请试用」（2026-10-18）。
    expect(wrapper.find('[data-test="hero-trial"]').exists()).toBe(false);
    const links = wrapper.get('[data-test="hero-links"]');
    expect(links.text()).toContain("院系批量部署？联系我们");
    expect(links.text()).toContain("直接进入工作台");
    expect(links.get('[data-test="enter-app"]').attributes("href")).toBe("/workbench/");
  });

  it("页头「申请试用」把光标送到邮箱框，不跳去子页", async () => {
    respond(202);
    const wrapper = mount(LandingPage, { attachTo: document.body });

    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    wrapper.get('[data-test="header-trial"]').element.dispatchEvent(event);
    await flushPromises();

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement?.id).toBe("trial-email");
    wrapper.unmount();
  });

  it("蜜罐对人隐藏且不可聚焦", () => {
    respond(202);
    const wrapper = mount(LandingPage);
    const honeypot = wrapper.get('input[name="website"]');

    expect(honeypot.attributes("tabindex")).toBe("-1");
    expect(honeypot.attributes("aria-hidden")).toBe("true");
    expect(honeypot.attributes("autocomplete")).toBe("off");
  });

  it("预约区：校验学校与邮箱，提交带上备注", async () => {
    respond(202, { status: "received" });
    window.location.hash = "#contact";
    const wrapper = mount(AboutPage, { attachTo: document.body });
    await flushPromises();

    await wrapper.get('[data-test="deploy-form"]').trigger("submit");
    await flushPromises();
    expect(wrapper.get("#deploy-org-error").text()).toBe("请填写学校与院系");
    expect(wrapper.get("#deploy-email-error").text()).toBe("请填写联系邮箱");

    await wrapper.get("#deploy-org").setValue("某大学 计算机学院");
    await wrapper.get("#deploy-email").setValue("dean@example.edu.cn");
    await wrapper.get("#deploy-note").setValue("三门课");
    await wrapper.get('[data-test="deploy-form"]').trigger("submit");
    await flushPromises();

    expect(wrapper.get('[data-test="deploy-done"]').text()).toBe("已收到，我们会通过邮件联系你。");
    const [, init] = globalThis.fetch.mock.calls.find(([url]) => String(url).endsWith("/site/leads"));
    expect(JSON.parse(init.body)).toEqual({
      kind: "deploy_assessment",
      email: "dean@example.edu.cn",
      organization_name: "某大学 计算机学院",
      note: "三门课",
      website: "",
    });
    wrapper.unmount();
  });

  it("预约区：服务端的逐字段提示显示在对应输入框下", async () => {
    respond(422, { detail: { code: "invalid_fields", fields: { email: "请填写有效的邮箱" } } });
    window.location.hash = "#contact";
    const wrapper = mount(AboutPage, { attachTo: document.body });
    await flushPromises();

    await wrapper.get("#deploy-org").setValue("某大学");
    await wrapper.get("#deploy-email").setValue("bad");
    await wrapper.get('[data-test="deploy-form"]').trigger("submit");
    await flushPromises();

    expect(wrapper.get("#deploy-email-error").text()).toBe("请填写有效的邮箱");
    expect(wrapper.find('[data-test="deploy-done"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("「数据安全」页说明申请信息怎么处理", async () => {
    window.location.hash = "#leads";
    const wrapper = mount(AboutPage, { attachTo: document.body });
    await flushPromises();

    const leads = wrapper.get("#leads");
    expect(leads.text()).toContain("不保存 IP 地址");
    expect(leads.text()).toContain("90 天后自动删除");
    wrapper.unmount();
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

  it("部署没开申请开关时（Vercel），预约区是发邮件，不是假表单", async () => {
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
