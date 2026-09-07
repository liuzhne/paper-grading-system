import { defineConfig, devices } from "@playwright/test";

/**
 * 浏览器验收（前端 v2 计划 §12.1，V01–V13）。
 *
 * 被测对象是**统一组装产物**（`public/`）经真实 FastAPI 托管的结果，不是
 * Vite dev server。dev server 有自己的中间件与模块图，测不出深链接回退、
 * 缺失资源必须 404、`/api` 不被 SPA 吞掉这几条——而它们恰恰只在生产托管
 * 路径上才会出问题。
 *
 * 数据用合成文档与 Mock LLM，库与存储隔离在 tmp 下；真实论文、Secret 与
 * 学生 PII 不得进入截图或 trace。
 */
const PORT = Number(process.env.PGS_E2E_PORT || 8099);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 30_000,
  expect: { timeout: 8_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "zh-CN",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    // 窄屏折叠：设计三栏宽度不是唯一布局（计划 §8）。
    { name: "mobile", use: { ...devices["Pixel 5"] }, testMatch: /responsive\.spec\.js/ },
  ],
  webServer: {
    command: `python -m e2e_server ${PORT}`,
    cwd: "../..",
    url: `http://127.0.0.1:${PORT}/api/system/integrations`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
